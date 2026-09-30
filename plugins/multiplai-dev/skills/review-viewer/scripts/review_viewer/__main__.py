"""review-viewer command line.

    python -m review_viewer [--session-id ID] [--agent NAME] <command> ...

    serve          start (or reuse) the viewer for findings files or a diff
    reply          send the session's answer to a question from the page
    pending        print questions and decisions that have no final reply yet
    list           show the live viewers this user can reach
    stop           stop one viewer (--box) or all of them (--all)
    walkthrough    put: check and publish the session's walkthrough; status: what is uncovered
    validate       check findings files against the v1 contract
    export-schema  write the findings and walkthrough v1 JSON Schemas

stdout is a contract: `serve` prints the `open:` line, the reference URLs, the
mailboxes, the Monitor command, the pending command and one `walkthrough:`
line per mailbox, then nothing per request. Diagnostics go to
the logger, never to print().
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
import logging
import time
from pathlib import Path

from multiplai_core.log_utils import log_event, setup_logging
from pydantic import ValidationError

from . import netinfo, registry, server, stats, walkthrough
from .gitdata import (GitError, Resolved, TargetError, TargetSpec, diff_findings, parse_target,
                      resolve_target)
from .mailbox import MAX_ROW_BYTES, Mailbox, MailboxError, utc_now
from .models import (SCHEMA_PATH, WALKTHROUGH_SCHEMA_PATH, FindingsFile, OutboxRow,
                     findings_digest, load_findings, schema_text, walkthrough_schema_text)

COMPONENT = "review-viewer"
log = logging.getLogger(__name__)
EXIT_USAGE = 2
EXIT_OTHER_SESSION = 3
MONITOR_TIMEOUT_MS = 1800000


def invocation_path(value: str) -> Path:
    """Resolve a user-supplied path against the directory the user ran from.

    `uv run --directory` changes the working directory to this package but
    leaves $PWD at the caller's directory, so relative paths are resolved
    against $PWD when it names a real directory.
    """
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    pwd = os.environ.get("PWD")
    base = Path(pwd) if pwd and Path(pwd).is_dir() else Path.cwd()
    return (base / p).resolve()


def output_root() -> Path:
    """Workspace INBOX/ if there is one, else `~/.multiplai`.

    Never the directory the user ran from: that is usually the repository
    being reviewed, and the output would land in its working tree. The
    review skill's `default_out()` makes the same choice; they must agree,
    because `find_review()` looks where the review skill writes."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        marker = Path(cfg) / ".workspace"
        try:
            root = Path(marker.read_text(encoding="utf-8").strip()).expanduser()
        except OSError:
            root = None
        if root and (root / "INBOX").is_dir():
            return root / "INBOX"
    return Path.home() / ".multiplai"


# --- serve --------------------------------------------------------------------


def default_reviews_dir() -> Path:
    """Where the review skill writes by default: the workspace `INBOX/reviews`,
    else `~/.multiplai/reviews`."""
    return output_root() / "reviews"


def find_review(target, dirs: list[Path]) -> tuple[str, Path | None, FindingsFile | None]:
    """Look for a review of these commits under `<dir>/*/findings.json`.

    ("match", path, findings) when one names the same base and head;
    ("stale", path, findings) when one has the same slug but other commits;
    ("none", None, None) otherwise. Newest file wins within each kind.
    """
    candidates: list[Path] = []
    for d in dirs:
        candidates.extend(p for p in Path(d).glob("*/findings.json") if p.is_file())
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    stale: tuple[Path, FindingsFile] | None = None
    for path in candidates:
        try:
            ff = load_findings(path)
        except (OSError, ValidationError) as exc:
            log.warning("skipping %s while looking for a review: %s", path, exc)
            continue
        t = ff.target
        if (t.base_sha, t.head_sha) == (target.base_sha, target.head_sha):
            return "match", path, ff
        if stale is None and t.slug == target.slug:
            stale = (path, ff)
    if stale:
        return "stale", stale[0], stale[1]
    return "none", None, None


def _resolve_for_serve(args) -> Resolved | int:
    cwd = invocation_path(".")
    repo = invocation_path(args.repo) if args.repo else None
    if args.range is not None:
        if args.target is not None:
            print("give --target or --range, not both", file=sys.stderr)
            return EXIT_USAGE
        if not args.repo:
            print("--range needs --repo <path>", file=sys.stderr)
            return EXIT_USAGE
        if ".." not in args.range:
            print(f"cannot read the diff: expected <base>..<head>, got {args.range!r}",
                  file=sys.stderr)
            return EXIT_USAGE
        left, right = args.range.split("..", 1)
        spec = TargetSpec("range2", args.range, left=left or "HEAD", right=right or "HEAD")
    elif args.target is not None:
        spec = parse_target(args.target, cwd=cwd)
    else:
        spec = parse_target("", cwd=repo or cwd)
    try:
        return resolve_target(spec, repo, base_branch=args.base, fetch=args.fetch, cwd=cwd)
    except TargetError as exc:
        print(f"cannot resolve the target: {exc}", file=sys.stderr)
    except (GitError, ValueError) as exc:
        print(f"cannot read the diff: {exc}", file=sys.stderr)
    return EXIT_USAGE


def _load_resolved(args, resolved: Resolved):
    target = resolved.target
    dirs = [invocation_path(d) for d in args.reviews_dir] if args.reviews_dir \
        else [default_reviews_dir()]
    kind, path, review = find_review(target, dirs)
    pr = resolved.pr.to_dict() if resolved.pr else None
    if kind == "match":
        review.target.repo_path = target.repo_path
        box = path.parent / "viewer"
        meta = {"pr": pr, "notice": None, "review": f"findings: {len(review.findings)}"}
        return [(review, box)], {target.slug: meta}
    notice = None
    status = "none"
    if kind == "stale":
        rt = review.target
        notice = (f"a review exists for {rt.head_sha[:8]}; this diff is at "
                  f"{target.head_sha[:8]}. It was not loaded: {path}")
        status = f"stale {rt.head_sha[:8]}"
    box = output_root() / "review-viewer" / target.slug / "viewer"
    return [(diff_findings(target), box)], {target.slug: {"pr": pr, "notice": notice,
                                                          "review": status}}


def _load_targets(args):
    """([(findings, mailbox)], {slug: {"pr", "notice", "review"}}), or an exit code."""
    loaded: list[tuple[FindingsFile, Path]] = []
    if args.target is not None or args.range is not None or not args.findings:
        if args.findings:
            print("give findings.json files, or a target (--target, or --repo with --range), "
                  "not both", file=sys.stderr)
            return EXIT_USAGE
        resolved = _resolve_for_serve(args)
        if isinstance(resolved, int):
            return resolved
        return _load_resolved(args, resolved)
    if args.repo:
        print("with findings files, point at the clone with --repo-root, not --repo",
              file=sys.stderr)
        return EXIT_USAGE
    for raw in args.findings:
        path = invocation_path(raw)
        try:
            ff = load_findings(path)
        except OSError as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except ValidationError as exc:
            print(f"{path} is not a valid findings.json v1:\n{exc}", file=sys.stderr)
            return EXIT_USAGE
        if args.repo_root:
            ff.target.repo_path = str(invocation_path(args.repo_root))
        loaded.append((ff, path.parent / "viewer"))
    slugs = [ff.target.slug for ff, _ in loaded]
    if len(set(slugs)) != len(slugs):
        print(f"target slugs must be unique across files; got {slugs}", file=sys.stderr)
        return EXIT_USAGE
    boxes = [b.resolve() for _, b in loaded]
    if len(set(boxes)) != len(boxes):
        print("two findings files share one directory, so they would share one mailbox; "
              "move one of them", file=sys.stderr)
        return EXIT_USAGE
    return loaded, {ff.target.slug: {"review": f"findings: {len(ff.findings)}"}
                    for ff, _ in loaded}


def _monitor_command(boxes: list[Path], label: str) -> str:
    """The Monitor call, with each path shell-quoted and each argument a JSON
    string, so spaces in paths and quotes in labels survive."""
    files = " ".join(shlex.quote(str(b / "inbox.jsonl")) for b in boxes)
    command = json.dumps(f"tail -n 0 -q -F {files}", ensure_ascii=False)
    description = json.dumps(f"review-viewer questions and decisions for {label}",
                             ensure_ascii=False)
    return f"Monitor(command={command}, description={description}, timeout_ms={MONITOR_TIMEOUT_MS})"


def _pending_command(boxes: list[Path]) -> str:
    return "python -m review_viewer pending " + " ".join(
        f"--box {shlex.quote(str(b))}" for b in boxes)


def _print_contract(open_html: Path, urls: list[tuple[str, str]], boxes: list[Path],
                    label: str, reviews: list[str]) -> None:
    print(f"open: file://{open_html}")
    for url, why in urls:
        print(f"url: {url}  ({why}; reference only, needs the link above to authorise)")
    for box in boxes:
        print(f"mailbox: {box}")
    print(f"monitor: {_monitor_command(boxes, label)}")
    print(f"pending: {_pending_command(boxes)}")
    for box, review in zip(boxes, reviews):
        print(f"walkthrough: {walkthrough.walkthrough_path(box)} ({review})")
    sys.stdout.flush()


def _wait_for_cleanup(boxes: list[str], timeout: float = 10.0) -> bool:
    """Wait until a stopping server has removed its files. Starting the new
    server earlier would let the old one's cleanup delete the new token."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any((Path(b) / "server.json").exists() for b in boxes):
            return True
        time.sleep(0.1)
    return False


def _reuse_or_replace(args, loaded, boxes: list[Path], label: str,
                      reviews: list[str]) -> int | None:
    """Deal with a viewer already serving one of these mailboxes.

    Returns an exit code when this call is finished (reused, or refused), and
    None when a new server should start (none running, or the old one was
    stopped because a findings file changed).
    """
    for box in boxes:
        live = registry.existing(box)
        if not live:
            continue
        if live.get("unresponsive"):
            print(f"a viewer process (pid {live['pid']}) owns {box} but did not answer on port "
                  f"{live['port']}. Wait a moment and retry, or end that process.",
                  file=sys.stderr)
            return EXIT_USAGE
        owner = live.get("session_id") or ""
        if not args.session_id or owner != args.session_id:
            who = (f"owned by another session ({owner[:8]})" if owner and args.session_id
                   else "owned by a session this command cannot identify (no session id)")
            print(f"a viewer for {box} is already running, {who}: {registry.describe(live)}\n"
                  f"to take it over: python -m review_viewer stop --box {shlex.quote(str(box))}",
                  flush=True)
            return EXIT_OTHER_SESSION
        served = set(live.get("mailboxes", []))
        missing = [b for b in boxes if str(b) not in served]
        if missing:
            print(f"a viewer is already running for {box} but not for {missing[0]}; stop it "
                  f"first: python -m review_viewer stop --box {shlex.quote(str(box))}",
                  file=sys.stderr)
            return EXIT_USAGE
        wanted = {str(b.resolve()): findings_digest(ff) for ff, b in loaded}
        served_digests = live.get("digests", {})
        if any(served_digests.get(b) != d for b, d in wanted.items()):
            token = registry.read_token(box)
            if not token or not registry.shutdown(live, token, reason="findings_changed") \
                    or not _wait_for_cleanup(live.get("mailboxes", [])):
                print(f"a findings file changed, but the running viewer for {box} did not "
                      f"stop; run: python -m review_viewer stop --box {shlex.quote(str(box))}",
                      file=sys.stderr)
                return EXIT_USAGE
            print("a findings file changed; restarted the viewer", flush=True)
            return None
        log_event(COMPONENT, "reuse", f"viewer already running for {', '.join(live['targets'])}; "
                  "reused it", session_id=args.session_id, port=live["port"],
                  owner_session=owner[:8])
        print("viewer already running for this session; reusing it", flush=True)
        _print_contract(box / "open.html", netinfo.display_urls(live["port"]), boxes, label,
                        reviews)
        return 0
    return None


def cmd_serve(args) -> int:
    result = _load_targets(args)
    if isinstance(result, int):
        return result
    loaded, meta = result
    for ff, _ in loaded:
        extra = meta.setdefault(ff.target.slug, {})
        extra["stats"] = stats.safe_change_stats(ff.target, extra.get("pr"))
    boxes = [b.resolve() for _, b in loaded]
    label = ", ".join(ff.target.label for ff, _ in loaded)
    reviews = [meta[ff.target.slug]["review"] for ff, _ in loaded]
    done = _reuse_or_replace(args, loaded, boxes, label, reviews)
    if done is not None:
        return done

    viewer = server.build_viewer(loaded, agent=args.agent, session_id=args.session_id,
                                 idle_minutes=args.idle, meta=meta)
    host = netinfo.bind_host()
    last = args.port + registry.PORT_SPAN - 1
    if server.bind(viewer, host, args.port, registry.PORT_SPAN) is None:
        print(f"no free port between {args.port} and {last}. Old viewers may still be "
              "running: python -m review_viewer stop --all", file=sys.stderr)
        return EXIT_USAGE
    urls = netinfo.display_urls(viewer.port)
    server.publish(viewer, urls)
    container = netinfo.detect_container()
    log_event(COMPONENT, "start",
              f"viewer started for {', '.join(viewer.targets)} on port {viewer.port}",
              session_id=args.session_id, port=viewer.port, targets=list(viewer.targets),
              host=host, container=container)
    _print_contract(boxes[0] / "open.html", urls, boxes, label, reviews)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    server.run(viewer)
    return 0


def _raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt


# --- reply --------------------------------------------------------------------


def _chunks(text: str, limit: int) -> list[str]:
    """Split an answer so every outbox row stays under the row size limit."""
    def size(s: str) -> int:
        return len(OutboxRow(reply_to="x" * 40, ts=utc_now(), text=s, done=False)
                   .model_dump_json().encode("utf-8"))

    if size(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while size(line) > limit:  # one enormous line: hard-split it
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:4000])
            line = line[4000:]
        if current and size(current + line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return [p for p in parts if p]


def cmd_reply(args) -> int:
    box = Mailbox(invocation_path(args.box))
    if not box.dir.is_dir():
        print(f"no mailbox at {box.dir} (it is the viewer/ directory the server printed)",
              file=sys.stderr)
        return EXIT_USAGE
    question = next((r for r in box.read_inbox() if r.get("id") == args.to), None)
    if question is None:
        print(f"no question {args.to!r} in {box.inbox}; check the id and the --box path",
              file=sys.stderr)
        return EXIT_USAGE
    if args.file:
        text = invocation_path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("the answer is empty", file=sys.stderr)
        return EXIT_USAGE
    parts = _chunks(text, MAX_ROW_BYTES - 1024)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            box.append_outbox(OutboxRow(reply_to=args.to, ts=utc_now(), text=part,
                                        done=last and not args.more))
    except MailboxError as exc:
        print(f"could not write the answer: {exc}", file=sys.stderr)
        return EXIT_USAGE
    done = not args.more
    log_event(COMPONENT, "reply", f"reply to {args.to} ({'final' if done else 'partial'})",
              session_id=args.session_id, target=question.get("target"), reply_to=args.to,
              chars=len(text), done=done)
    print(f"{'final answer' if done else 'partial answer'} for {args.to} -> {box.outbox}")
    return 0


# --- list / stop ----------------------------------------------------------------


def cmd_list(args) -> int:
    live = registry.scan()
    if not live:
        print("no live review-viewer found")
        return 0
    print(f"{len(live)} live viewer(s):")
    for who in live:
        print("  " + registry.describe(who))
        for box in who.get("mailboxes", []):
            print(f"    mailbox: {box}")
    return 0


def cmd_stop(args) -> int:
    if args.box:
        box = invocation_path(args.box).resolve()
        live = [w for w in registry.scan([box]) if str(box) in w.get("mailboxes", [])]
        if not live:
            print(f"no viewer answering for {box} (or its token file is not readable)")
            return 0
    elif args.all:
        live = registry.scan()
    else:
        print("stop needs --box <mailbox> or --all", file=sys.stderr)
        return EXIT_USAGE
    if not live:
        print("nothing to stop")
        return 0
    failed = 0
    for who in live:
        token = next((t for t in (registry.read_token(Path(b)) for b in who.get("mailboxes", []))
                      if t), None)
        if token and registry.shutdown(who, token):
            print("stopped: " + registry.describe(who))
        else:
            failed += 1
            print("NOT stopped: " + registry.describe(who))
    return 1 if failed else 0


def cmd_pending(args) -> int:
    """Print the inbox rows that have no final reply yet, one JSON row per
    line, in the same form the Monitor delivers. Run it after (re-)arming the
    Monitor: rows written while no watch was running are otherwise missed."""
    for raw in args.box:
        box = Mailbox(invocation_path(raw))
        if not box.dir.is_dir():
            print(f"no mailbox at {box.dir}", file=sys.stderr)
            return EXIT_USAGE
        last_done: dict[str, bool] = {}
        for row in box.read_outbox(0)[0]:
            last_done[row.get("reply_to")] = row.get("done") is True
        for row in box.read_inbox():
            if not last_done.get(row.get("id")):
                print(json.dumps(row, ensure_ascii=False))
    return 0


# --- walkthrough ----------------------------------------------------------------------


def _served(args):
    box = invocation_path(args.box).resolve()
    try:
        return box, walkthrough.load_served(box)
    except FileNotFoundError:
        print(f"no served target in {box} (it is the mailbox: directory `serve` printed; "
              "start the viewer first)", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"cannot read {walkthrough.served_path(box)}: {exc}", file=sys.stderr)
    return box, None


def cmd_walkthrough_put(args) -> int:
    box, served = _served(args)
    if served is None:
        return EXIT_USAGE
    path = invocation_path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        wt, errors = walkthrough.put(box, text, served.findings)
    except GitError as exc:
        print(f"cannot check the walkthrough: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if errors:
        print(f"walkthrough rejected ({len(errors)} problem{'s' if len(errors) > 1 else ''}); "
              "nothing was published:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return EXIT_USAGE
    slug = served.findings.target.slug
    log_event(COMPONENT, "walkthrough",
              f"walkthrough for {slug}: {len(wt.steps)} steps"
              f"{' (complete)' if wt.complete else ' (in progress)'}",
              session_id=args.session_id, target=slug, steps=len(wt.steps),
              complete=wt.complete)
    uncovered, unlinked = walkthrough.coverage(wt, served.findings)
    print(f"published {walkthrough.walkthrough_path(box)}: {len(wt.steps)} steps, "
          f"{'complete' if wt.complete else 'in progress'}; "
          f"{len(uncovered)} changed files and {len(unlinked)} findings not covered yet")
    return 0


def cmd_walkthrough_status(args) -> int:
    box, served = _served(args)
    if served is None:
        return EXIT_USAGE
    try:
        wt = walkthrough.load_walkthrough(box)
    except (OSError, ValueError) as exc:
        print(f"cannot read {walkthrough.walkthrough_path(box)}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    ff = served.findings
    t = ff.target
    uncovered, unlinked = walkthrough.coverage(wt, ff)
    state = "absent" if wt is None else (
        f"{len(wt.steps)} steps, {'complete' if wt.complete else 'in progress'}")
    print(f"walkthrough: {walkthrough.walkthrough_path(box)} ({state})")
    print(f"target: {t.label}, {len(t.files_changed)} changed files, "
          f"{len(ff.findings)} findings")
    print(f"base_sha: {t.base_sha}")
    print(f"head_sha: {t.head_sha}")
    print(f"changed files not covered ({len(uncovered)}):")
    for path in uncovered:
        print(f"  {path}")
    by_id = {f.id: f for f in ff.findings}
    print(f"findings not linked ({len(unlinked)}):")
    for fid in unlinked:
        f = by_id[fid]
        print(f"  {fid} {f.severity} {f.status} {f.file}:{f.line_start} {f.claim}")
    have = {a.topic for a in wt.assessments} if wt else set()
    missing = [t for t in walkthrough.MUST_ASSESS if t not in have]
    print(f"assessments missing ({len(missing)}): {', '.join(missing)}")
    print("risk: " + ("missing" if not wt or wt.risk is None else
                      f"tier {wt.risk.tier}, {'revertable' if wt.risk.revertable else 'not revertable'}"))
    st = served.stats or {}
    tiers = st.get("tiers") or {}
    if st.get("tiers_error"):
        print(f"repo tiers: ignored, {st['tiers_error']}")
    elif tiers:
        print(f"repo tiers ({stats.RISK_FILE} at base) set {len(tiers)} of {len(t.files_changed)} files; "
              "they can raise your tier, never lower it:")
        for path, tier in sorted(tiers.items()):
            print(f"  tier {tier} {path}")
    else:
        print(f"repo tiers: no {stats.RISK_FILE} at base; judge the tier for every file")
    badges = (served.stats or {}).get("badges") or []
    print(f"measured ({len(badges)}):" if served.stats else "measured: not available")
    for b in badges:
        print(f"  [{b['level']}] {b['label']}: {b['detail']}")
    for c in (served.stats or {}).get("commits") or []:
        print(f"  commit {c['sha'][:8]} {c['subject']}" + ("" if c["body"] else " (no body)"))
    return 0


# --- validate / export-schema -------------------------------------------------------


def cmd_validate(args) -> int:
    failed = False
    for raw in args.files:
        path = invocation_path(raw)
        try:
            ff = load_findings(path)
        except (OSError, ValidationError) as exc:
            failed = True
            print(f"FAIL {path}\n{exc}")
            continue
        print(f"OK {path} ({len(ff.findings)} findings)")
    return 1 if failed else 0


def cmd_export_schema(args) -> int:
    for given, default, text in ((args.out, SCHEMA_PATH, schema_text()),
                                 (args.walkthrough_out, WALKTHROUGH_SCHEMA_PATH,
                                  walkthrough_schema_text())):
        out = invocation_path(given) if given else default
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session-id", default=argparse.SUPPRESS,
                        help="Claude Code session id, for ownership and log correlation")
    common.add_argument("--agent", default=argparse.SUPPRESS,
                        help="who answers, shown in the page header (default: Claude)")

    ap = argparse.ArgumentParser(
        prog="review_viewer", parents=[common],
        description="Local web viewer for code-review findings; questions from the "
                    "page reach the Claude Code session that started it.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("serve", parents=[common], help="start or reuse the viewer")
    s.add_argument("findings", nargs="*", help="findings.json files (v1)")
    s.add_argument("--target",
                   help="a PR (number, URL, owner/repo#N), a branch, a worktree path, "
                        "<base>..<head> or <a>...<b>; with no target and no findings, the "
                        "unpushed commits of --repo or the current directory")
    s.add_argument("--repo", help="the local clone to read (default: the current directory)")
    s.add_argument("--range", help="same as --target <base>..<head>; needs --repo")
    s.add_argument("--base", help="branch targets: the branch to diff against "
                                  "(default: origin/HEAD's branch, else main)")
    s.add_argument("--fetch", action="store_true",
                   help="run `git fetch origin` in the clone before resolving")
    s.add_argument("--reviews-dir", action="append", default=[],
                   help="where to look for a review of the same commits (repeatable; "
                        "default: the review skill's output directory)")
    s.add_argument("--repo-root", help="read git from here instead of target.repo_path")
    s.add_argument("--port", type=int, default=registry.PORT_START,
                   help=f"first port to try (default {registry.PORT_START}; 20 are tried)")
    s.add_argument("--idle", type=float, default=0.0,
                   help="minutes with no open page before the server exits "
                        "(default 0 = never; it runs until `stop` or its container ends)")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("reply", parents=[common], help="answer a question from the page")
    r.add_argument("--box", required=True, help="the mailbox directory the server printed")
    r.add_argument("--to", required=True, help="the question id")
    r.add_argument("--more", action="store_true",
                   help="a partial answer: the page keeps waiting for the rest")
    r.add_argument("--file", help="read the markdown answer from this file (default: stdin)")
    r.set_defaults(func=cmd_reply)

    ls = sub.add_parser("list", parents=[common], help="list live viewers")
    ls.set_defaults(func=cmd_list)

    pe = sub.add_parser("pending", parents=[common],
                        help="print questions and decisions that have no final reply yet")
    pe.add_argument("--box", required=True, action="append",
                    help="a mailbox directory (repeat for several)")
    pe.set_defaults(func=cmd_pending)

    st = sub.add_parser("stop", parents=[common], help="stop a viewer")
    st.add_argument("--box", help="the mailbox of the viewer to stop")
    st.add_argument("--all", action="store_true", help="stop every live viewer")
    st.set_defaults(func=cmd_stop)

    w = sub.add_parser("walkthrough", parents=[common],
                       help="publish or inspect the walkthrough the page shows")
    wsub = w.add_subparsers(dest="wcmd", required=True, metavar="action")
    wp = wsub.add_parser("put", parents=[common],
                         help="check a walkthrough.json and publish it to the page")
    wp.add_argument("--box", required=True, help="the mailbox directory the server printed")
    wp.add_argument("--file", required=True, help="the walkthrough.json the session wrote")
    wp.set_defaults(func=cmd_walkthrough_put)
    ws = wsub.add_parser("status", parents=[common],
                         help="list changed files and findings the walkthrough does not cover")
    ws.add_argument("--box", required=True, help="the mailbox directory the server printed")
    ws.set_defaults(func=cmd_walkthrough_status)

    v = sub.add_parser("validate", parents=[common], help="validate findings.json files")
    v.add_argument("files", nargs="+")
    v.set_defaults(func=cmd_validate)

    e = sub.add_parser("export-schema", parents=[common],
                       help="write the findings and walkthrough v1 JSON Schemas")
    e.add_argument("--out", help=f"findings schema path (default: {SCHEMA_PATH.name} in schema/)")
    e.add_argument("--walkthrough-out",
                   help=f"walkthrough schema path (default: {WALKTHROUGH_SCHEMA_PATH.name} in "
                        "schema/)")
    e.set_defaults(func=cmd_export_schema)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.session_id = getattr(args, "session_id", "")
    if args.session_id in ("", "{session_id}"):
        # Claude Code exports the session id to the commands it runs; a
        # SKILL.md placeholder that was not substituted must not become an id
        # every session shares.
        args.session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    args.agent = getattr(args, "agent", "Claude")
    setup_logging(COMPONENT, session_id=args.session_id,
                  propagate_loggers=("review_viewer", "multiplai_core"))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
