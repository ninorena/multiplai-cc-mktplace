"""Read-only git access: target resolution, the diff rows and the full-file
view for one path.

Every git call here runs with a fixed argv, no shell, and stdin closed. Nothing
in this module checks out, commits or writes a ref in the repository. The only
network calls are in `resolve_target()`: `gh pr view` for a PR target, `git
fetch origin refs/pull/<n>/head <base>` when that PR's commits are missing
(objects and FETCH_HEAD only), and `git fetch origin` when the caller passes
`fetch=True`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .models import FindingsFile, Target

log = logging.getLogger(__name__)

# A file deleted at head shows this many of its old lines, not all of them:
# the point is to recognise the file, not to review what no longer exists.
DELETED_PREVIEW_LINES = 120

# Above this size the view is the hunks plus the cited ranges, not the file.
MAX_FULL_LINES = 4000
CITED_CONTEXT = 12
HUNK_CONTEXT = 3

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".json": "json", ".md": "markdown", ".html": "xml",
    ".htm": "xml", ".xml": "xml", ".svg": "xml", ".css": "css", ".scss": "scss",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".sql": "sql",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".java": "java",
    ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cs": "csharp",
    ".dockerfile": "dockerfile", ".tf": "hcl", ".lua": "lua", ".r": "r",
}
FILENAMES = {"Dockerfile": "dockerfile", "Makefile": "makefile"}

RowKind = Literal["ctx", "add", "del", "gap"]


class GitError(RuntimeError):
    """A git command failed; the message carries git's stderr."""


class PathNotInReview(LookupError):
    """The path is neither changed, cited, nor the file of a finding."""


@dataclass
class Row:
    k: RowKind
    o: int | None
    n: int | None
    t: str


@dataclass
class FileView:
    path: str
    language: str
    rows: list[Row] = field(default_factory=list)
    cited_ranges: list[tuple[int, int]] = field(default_factory=list)
    truncated: bool = False
    deleted: bool = False
    binary: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


GIT_MISSING = "this skill reads the review's commits with git; install git and run it again"


# Settings that would otherwise let the user's or the repo's git config
# rewrite the output this module parses: colour codes in front of `@@`,
# quoted non-ASCII paths, an external diff program, a textconv filter.
_GIT_CONFIG = ("-c", "color.ui=never", "-c", "core.quotepath=off")
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv")


def git(repo: str | Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_EXTERNAL_DIFF", "GIT_PAGER")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", *_GIT_CONFIG, "-C", str(repo), *args], shell=False,
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
            errors="replace", env=env)
    except FileNotFoundError:
        raise GitError(GIT_MISSING) from None
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def split_lines(text: str) -> list[str]:
    """Split the way git numbers lines: on `\n` only.

    `str.splitlines()` also splits on form feeds, U+2028 and other
    separators, which shifts every later line number away from git's.
    A trailing `\r` (CRLF files) is dropped for display.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _walk(text: str):
    """Yield ("hunk", header, old_start, old_len, new_start, new_len) at each
    `@@`, then ("-"|"+"|" ", body) for each line inside the hunk.

    Hunk bodies are counted from the header's lengths, never recognised by
    their first characters: a deleted line `-- comment` reads `--- comment` in
    the diff and an added `++x;` reads `+++x;`, which look like file headers.
    Everything outside a hunk (file headers included) is skipped.
    """
    old_left = new_left = 0
    for raw in split_lines(text):
        if old_left <= 0 and new_left <= 0:
            m = _HUNK_RE.match(raw)
            if m:
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                old_left, new_left = old_len, new_len
                yield ("hunk", raw, int(m.group(1)), old_len, int(m.group(3)), new_len)
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        marker, body = raw[:1], raw[1:]
        if marker == "-":
            old_left -= 1
        elif marker == "+":
            new_left -= 1
        else:
            marker = " "
            old_left -= 1
            new_left -= 1
        yield (marker, body)


def parse_unified(text: str) -> list[Row]:
    """Turn a unified diff into display rows.

    Each row is `{k: ctx|add|del|gap, o: old line no, n: new line no, t: text}`.
    A `gap` row marks the jump between two hunks and carries the `@@` header.
    """
    rows: list[Row] = []
    old_no = new_no = 0
    for item in _walk(text):
        if item[0] == "hunk":
            _, header, old_no, _, new_no, _ = item
            if rows:
                rows.append(Row("gap", None, None, header))
            continue
        marker, body = item
        if marker == "+":
            rows.append(Row("add", None, new_no, body))
            new_no += 1
        elif marker == "-":
            rows.append(Row("del", old_no, None, body))
            old_no += 1
        else:
            rows.append(Row("ctx", old_no, new_no, body))
            old_no += 1
            new_no += 1
    return rows


def _hunks(diff_text: str) -> tuple[set[int], dict[int, list[Row]]]:
    """Added new-side line numbers, and deleted rows keyed by the new-side line
    they appear before. Expects a `--unified=0` diff."""
    added: set[int] = set()
    deleted_before: dict[int, list[Row]] = {}
    anchor = old_no = new_no = 0
    for item in _walk(diff_text):
        if item[0] == "hunk":
            _, _, old_no, _, new_start, new_len = item
            # A pure deletion reports the new-side line *after which* the lines
            # went; any other hunk reports the first line it touches.
            anchor = new_start + 1 if new_len == 0 else new_start
            new_no = new_start
            continue
        marker, body = item
        if marker == "-":
            deleted_before.setdefault(anchor, []).append(Row("del", old_no, None, body))
            old_no += 1
        elif marker == "+":
            added.add(new_no)
            new_no += 1
    return added, deleted_before


def language_for(path: str) -> str:
    p = Path(path)
    if p.name in FILENAMES:
        return FILENAMES[p.name]
    return LANGUAGES.get(p.suffix.lower(), "plaintext")


def allowed_paths(target: Target, findings: FindingsFile | None) -> set[str]:
    paths = set(target.files_changed)
    if findings is not None:
        for f in findings.findings:
            paths.add(f.file)
            paths.update(c.path for c in f.citations)
    return paths


def _cited_ranges(path: str, findings: FindingsFile | None) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    if findings is None:
        return ranges
    for f in findings.findings:
        if f.file == path:
            ranges.append((f.line_start, f.line_end))
        ranges.extend((c.line_start, c.line_end) for c in f.citations if c.path == path)
    return sorted(set(ranges))


def _is_binary(repo: str, base: str, head: str, path: str) -> bool:
    out = git(repo, "diff", *_DIFF_FLAGS, "--numstat", base, head, "--", path)
    return any(line.startswith("-\t-\t") for line in split_lines(out))


def _exists_at(repo: str, sha: str, path: str) -> bool:
    try:
        git(repo, "cat-file", "-e", f"{sha}:{path}")
    except GitError as exc:
        if str(exc) == GIT_MISSING:
            raise
        return False
    return True


def file_view(target: Target, path: str, findings: FindingsFile | None = None,
              allowed: set[str] | None = None) -> FileView:
    """The file at head, with the diff folded in and cited ranges attached."""
    if allowed is None:
        allowed = allowed_paths(target, findings)
    if path not in allowed:
        raise PathNotInReview(path)
    repo, base, head = target.repo_path, target.base_sha, target.head_sha
    view = FileView(path=path, language=language_for(path),
                    cited_ranges=_cited_ranges(path, findings))

    if _is_binary(repo, base, head, path):
        view.binary = True
        return view

    if not _exists_at(repo, head, path):
        if not _exists_at(repo, base, path):
            raise GitError(f"{path} exists at neither {base[:8]} nor {head[:8]}")
        old = split_lines(git(repo, "show", f"{base}:{path}"))
        view.deleted = True
        view.truncated = len(old) > DELETED_PREVIEW_LINES
        view.rows = [Row("del", i, None, t)
                     for i, t in enumerate(old[:DELETED_PREVIEW_LINES], 1)]
        return view

    lines = split_lines(git(repo, "show", f"{head}:{path}"))
    added, deleted_before = _hunks(
        git(repo, "diff", *_DIFF_FLAGS, "--unified=0", base, head, "--", path))

    keep: set[int] | None = None
    if len(lines) > MAX_FULL_LINES:
        view.truncated = True
        keep = set()
        touched = added | set(deleted_before)
        for n in touched:
            keep.update(range(n - HUNK_CONTEXT, n + HUNK_CONTEXT + 1))
        for start, end in view.cited_ranges:
            keep.update(range(start - CITED_CONTEXT, end + CITED_CONTEXT + 1))

    rows: list[Row] = []
    last = 0
    for n in range(1, len(lines) + 2):
        if keep is not None and n not in keep:
            continue
        if keep is not None and last and n != last + 1:
            rows.append(Row("gap", None, None, f"… lines {last + 1}–{n - 1} not shown"))
        rows.extend(deleted_before.get(n, []))
        if n <= len(lines):
            rows.append(Row("add" if n in added else "ctx", None, n, lines[n - 1]))
        last = n
    view.rows = rows
    return view


def slug_part(name: str) -> str:
    """A repo directory name reduced to the characters a target slug allows."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"


def diff_target(repo: str | Path, rev_range: str) -> Target:
    """A Target for plain-diff mode: `<base>..<head>` resolved to full shas."""
    if ".." not in rev_range:
        raise ValueError(f"expected <base>..<head>, got {rev_range!r}")
    base_ref, head_ref = rev_range.split("..", 1)
    repo_path = Path(repo).resolve()
    base = git(repo_path, "rev-parse", "--verify", f"{base_ref or 'HEAD'}^{{commit}}").strip()
    head = git(repo_path, "rev-parse", "--verify", f"{head_ref or 'HEAD'}^{{commit}}").strip()
    files = [f for f in git(repo_path, "diff", *_DIFF_FLAGS, "--name-only", "-z", base,
                            head).split("\0") if f]
    return Target(
        slug=f"{slug_part(repo_path.name)}--{base[:8]}..{head[:8]}",
        label=f"{repo_path.name} {rev_range}",
        repo_path=str(repo_path), remote_url=None,
        base_sha=base, head_sha=head, files_changed=files)


# --- target resolution ------------------------------------------------------------
#
# The same base/head semantics as the review skill's `review_pipeline/target.py`,
# so a viewer opened on a PR or branch lands on the commits a review of it
# would name. That module is not importable here (a separate workspace
# member), so the rules are restated, not imported.

TargetKind = Literal["pr", "range2", "range3", "path", "branch"]

GH_MISSING = ("a PR target needs the GitHub CLI (`gh`), which is not installed. Install it "
              "from https://cli.github.com and run `gh auth login`, or pass a <base>..<head> "
              "range.")

_PR_URL_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#]\S*)?$")
_PR_SHORT_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")
_PR_NUMBER_RE = re.compile(r"^#?(\d+)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TargetError(ValueError):
    """The target could not be resolved. The message says why and what to pass."""


@dataclass(frozen=True)
class TargetSpec:
    """What the user typed, classified. No git has run yet."""
    kind: TargetKind
    text: str
    owner: str | None = None
    repo: str | None = None
    number: int | None = None
    left: str | None = None
    right: str | None = None
    path: Path | None = None


@dataclass
class PrInfo:
    """PR metadata for the page header. Held in memory, never in findings.json."""
    number: int
    title: str
    author: str
    url: str
    body: str
    head_ref: str
    base_ref: str
    checks: dict = field(default_factory=dict)
    mergeable: str = ""
    draft: bool = False
    review_decision: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_CHECK_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_CHECK_FAIL = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE",
               "STALE"}


def summarize_checks(rollup) -> dict:
    """Counts from `gh pr view --json statusCheckRollup`: CheckRuns carry
    status/conclusion, StatusContexts carry state."""
    counts = {"total": 0, "passed": 0, "failed": 0, "pending": 0}
    for item in rollup if isinstance(rollup, list) else []:
        if not isinstance(item, dict):
            continue
        result = str(item.get("conclusion") or item.get("state") or "").upper()
        if item.get("status") and str(item["status"]).upper() != "COMPLETED":
            result = "PENDING"
        counts["total"] += 1
        if result in _CHECK_PASS:
            counts["passed"] += 1
        elif result in _CHECK_FAIL:
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    return counts


@dataclass
class Resolved:
    """A resolved target, plus the PR metadata when the target was a PR."""
    target: Target
    pr: PrInfo | None = None


def parse_target(text: str | None, *, cwd: Path | None = None) -> TargetSpec:
    """Classify a target string. Pure apart from checking whether a path is a
    directory; runs no git.

    PR (URL, `o/r#n`, `#n`, all digits) → `pr`; `a...b` → `range3`; `a..b` →
    `range2`; an existing directory (or no text at all) → `path`; anything
    else → `branch`.
    """
    raw = (text or "").strip()
    base = cwd or Path.cwd()
    if not raw:
        return TargetSpec("path", raw, path=base)
    m = _PR_URL_RE.match(raw)
    if m:
        return TargetSpec("pr", raw, owner=m.group(1), repo=m.group(2), number=int(m.group(3)))
    m = _PR_SHORT_RE.match(raw)
    if m:
        return TargetSpec("pr", raw, owner=m.group(1), repo=m.group(2), number=int(m.group(3)))
    m = _PR_NUMBER_RE.match(raw)
    if m:
        return TargetSpec("pr", raw, number=int(m.group(1)))
    # A directory wins over a range: `../other-worktree` contains "..".
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_dir():
        return TargetSpec("path", raw, path=candidate.resolve())
    if "..." in raw:
        left, right = raw.split("...", 1)
        return TargetSpec("range3", raw, left=left or "HEAD", right=right or "HEAD")
    if ".." in raw:
        left, right = raw.split("..", 1)
        return TargetSpec("range2", raw, left=left or "HEAD", right=right or "HEAD")
    return TargetSpec("branch", raw)


def sanitize_slug(text: str) -> str:
    """Same rule as `review_pipeline.target.sanitize_slug`, so a PR or branch
    gets the slug its review would have."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return slug or "target"


def github_web_base(url: str | None) -> str | None:
    """`https://github.com/<owner>/<repo>` for a GitHub remote, else None.
    Copied from `review_pipeline.target`."""
    if not url:
        return None
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def _try(repo: Path, *args: str) -> str | None:
    """stdout of a git call that may fail for an ordinary reason, else None.
    A missing git is still an error."""
    try:
        return git(repo, *args).strip()
    except GitError as exc:
        if str(exc) == GIT_MISSING:
            raise
        return None


def rev_parse(repo: Path, ref: str) -> str | None:
    """Full sha of the commit *ref* names, or None."""
    sha = _try(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return sha if sha and _SHA_RE.match(sha) else None


def merge_base(repo: Path, a: str, b: str) -> str | None:
    sha = _try(repo, "merge-base", a, b)
    return sha if sha and _SHA_RE.match(sha) else None


def default_branch(repo: Path) -> str:
    """`origin/HEAD`'s branch; `main` when origin/HEAD is not set."""
    ref = _try(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") or ""
    prefix = "refs/remotes/origin/"
    return ref[len(prefix):] if ref.startswith(prefix) else "main"


def remote_url(repo: Path) -> str | None:
    return _try(repo, "remote", "get-url", "origin") or None


def _changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", *_DIFF_FLAGS, "--name-only", "-z", base, head)
    return [f for f in out.split("\0") if f]


def _is_repo(path: Path) -> bool:
    return _try(path, "rev-parse", "--git-dir") is not None


def _gh_json(repo: Path, number: int, owner_repo: str | None, fields: str,
             timeout: float | None = None) -> dict:
    gh = shutil.which("gh")
    if gh is None:
        raise TargetError(GH_MISSING)
    argv = [gh, "pr", "view", str(number)]
    if owner_repo:
        argv += ["--repo", owner_repo]
    argv += ["--json", fields]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(argv, cwd=repo, shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, encoding="utf-8", errors="replace",
                              env=env, check=False, timeout=timeout)
    except FileNotFoundError:
        raise TargetError(GH_MISSING) from None
    except subprocess.TimeoutExpired:
        raise TargetError(f"gh pr view {number} did not answer within {timeout:g} s") from None
    if proc.returncode != 0:
        raise TargetError(f"gh pr view {number} failed: {proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise TargetError(f"gh pr view {number} printed something that is not JSON") from None
    if not isinstance(info, dict):
        raise TargetError(f"gh pr view {number} printed something that is not an object")
    return info


def _gh_pr_view(repo: Path, number: int, owner_repo: str | None) -> dict:
    info = _gh_json(repo, number, owner_repo,
                    "number,title,author,url,headRefOid,baseRefOid,headRefName,"
                    "baseRefName,body,statusCheckRollup,mergeable,isDraft,reviewDecision")
    for key in ("headRefOid", "baseRefOid", "baseRefName"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise TargetError(f"gh pr view {number} did not return {key}")
    return info


def pr_status(repo: str | Path, number: int, url: str = "", timeout: float = 6.0) -> dict:
    """The parts of an open PR that change while you read it: check counts,
    mergeable, draft, review decision. One `gh pr view` call."""
    m = _PR_URL_RE.match(url or "")
    info = _gh_json(Path(repo), number, f"{m[1]}/{m[2]}" if m else None,
                    "statusCheckRollup,mergeable,isDraft,reviewDecision", timeout=timeout)
    return {"checks": summarize_checks(info.get("statusCheckRollup")),
            "mergeable": str(info.get("mergeable") or ""), "draft": bool(info.get("isDraft")),
            "review_decision": str(info.get("reviewDecision") or "")}


def _pick_repo(spec: TargetSpec, repo: Path | None, cwd: Path) -> Path:
    """The local clone to read: --repo, else the working directory. For an
    `owner/repo` PR with no --repo, the working directory must be a clone of
    that repository."""
    if repo is not None:
        chosen = Path(repo).expanduser().resolve()
        if not _is_repo(chosen):
            raise TargetError(f"{chosen} is not a git repository")
        return chosen
    chosen = cwd.resolve()
    if spec.owner and spec.repo:
        want = f"https://github.com/{spec.owner}/{spec.repo}".lower()
        have = _is_repo(chosen) and github_web_base(remote_url(chosen))
        if not have or have.lower() != want:
            raise TargetError(f"no local clone of {spec.owner}/{spec.repo} here; pass --repo "
                              f"<path to a clone of {spec.owner}/{spec.repo}>")
        return chosen
    if not _is_repo(chosen):
        raise TargetError(f"{chosen} is not a git repository; pass --repo <path>")
    return chosen


def _base_ref(repo: Path, base_branch: str | None) -> str:
    """`origin/<default>`, or the local `<default>` when there is no origin copy."""
    name = base_branch or default_branch(repo)
    for ref in (f"origin/{name}", name):
        if rev_parse(repo, ref):
            return ref
    raise TargetError(f"neither origin/{name} nor {name} exists in {repo}; pass --base <branch>")


def _make_target(repo: Path, slug: str, label: str, base: str, head: str) -> Target:
    return Target(slug=slug, label=label, repo_path=str(repo), remote_url=remote_url(repo),
                  base_sha=base, head_sha=head, files_changed=_changed_files(repo, base, head))


def _resolve_pr(spec: TargetSpec, repo: Path) -> Resolved:
    n = int(spec.number)
    owner_repo = f"{spec.owner}/{spec.repo}" if spec.owner and spec.repo else None
    info = _gh_pr_view(repo, n, owner_repo)
    head_oid, base_oid = info["headRefOid"], info["baseRefOid"]
    if not rev_parse(repo, head_oid) or not rev_parse(repo, base_oid):
        # No destination refspec: this writes objects and FETCH_HEAD only,
        # never a branch or a remote-tracking ref.
        try:
            git(repo, "fetch", "--quiet", "origin", f"refs/pull/{n}/head", info["baseRefName"])
        except GitError as exc:
            if str(exc) == GIT_MISSING:
                raise
            raise TargetError(f"PR #{n}'s commits are not in {repo}, and fetching them from "
                              f"origin failed: {exc}") from None
    head = rev_parse(repo, head_oid)
    base_tip = rev_parse(repo, base_oid)
    if not head:
        raise TargetError(f"PR #{n} head {head_oid[:12]} is not in {repo}, even after fetching "
                          f"refs/pull/{n}/head from origin; is origin the PR's repository?")
    if not base_tip:
        raise TargetError(f"PR #{n} base {base_oid[:12]} is not in {repo}, even after fetching "
                          f"{info['baseRefName']} from origin")
    base = merge_base(repo, base_tip, head)
    if not base:
        raise TargetError(f"PR #{n}: no merge-base between {base_oid[:12]} and {head_oid[:12]}")
    author = info.get("author") or {}
    pr = PrInfo(number=n, title=str(info.get("title") or ""),
                author=str(author.get("login") or "") if isinstance(author, dict) else "",
                url=str(info.get("url") or ""), body=str(info.get("body") or ""),
                head_ref=str(info.get("headRefName") or ""), base_ref=str(info["baseRefName"]),
                checks=summarize_checks(info.get("statusCheckRollup")),
                mergeable=str(info.get("mergeable") or ""), draft=bool(info.get("isDraft")),
                review_decision=str(info.get("reviewDecision") or ""))
    slug = sanitize_slug(f"{repo.name}--pr-{n}")
    return Resolved(_make_target(repo, slug, f"{repo.name} PR #{n}", base, head), pr)


def _resolve_branch(spec: TargetSpec, repo: Path, base_branch: str | None) -> Resolved:
    name = spec.text
    head = rev_parse(repo, f"refs/heads/{name}") or rev_parse(repo, f"origin/{name}")
    if not head:
        raise TargetError(f"no branch {name!r} here or on origin (pass --fetch to fetch origin "
                          "first), and it is not a PR number, a range or a directory")
    base_ref = _base_ref(repo, base_branch)
    base = merge_base(repo, base_ref, head)
    if not base:
        raise TargetError(f"no merge-base between {base_ref} and {name}")
    slug = sanitize_slug(f"{repo.name}--{name}")
    return Resolved(_make_target(repo, slug, f"{repo.name} {name}", base, head))


def _resolve_path(spec: TargetSpec, base_branch: str | None) -> Resolved:
    path = spec.path or Path.cwd()
    top = _try(path, "rev-parse", "--show-toplevel")
    if not top:
        raise TargetError(f"{path} is not inside a git repository")
    repo = Path(top).resolve()
    head = rev_parse(repo, "HEAD")
    if not head:
        raise TargetError(f"{repo} has no commits")
    branch = _try(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    upstream = rev_parse(repo, "HEAD@{upstream}") if branch else None
    name = branch or head[:12]
    if upstream:
        base = merge_base(repo, upstream, head)
        if not base:
            raise TargetError(f"no merge-base between {name} and its upstream")
        if base == head:
            raise TargetError(f"{name} has no commits that are not pushed to its upstream; "
                              f"pass the branch name ({name}) to see it against the default "
                              "branch")
        slug = sanitize_slug(f"{repo.name}--{name}--unpushed")
        label = f"{repo.name} {name} (not pushed)"
    else:
        base_ref = _base_ref(repo, base_branch)
        base = merge_base(repo, base_ref, head)
        if not base:
            raise TargetError(f"no merge-base between {base_ref} and {name}")
        slug = sanitize_slug(f"{repo.name}--{name}")
        label = f"{repo.name} {name}"
    return Resolved(_make_target(repo, slug, label, base, head))


def resolve_target(spec: TargetSpec, repo: Path | None, *, base_branch: str | None = None,
                   fetch: bool = False, cwd: Path | None = None) -> Resolved:
    """Turn a parsed target into base and head shas.

    PR: head = the PR's head commit, base = merge-base(PR base commit, head).
    Branch: head = the local branch, else `origin/<name>`; base =
    merge-base(`origin/<default>`, head). Directory: head = its HEAD; base =
    its upstream when the branch has one (the commits not yet pushed), else
    as for a branch. `a..b`: the two commits. `a...b`: merge-base(a, b) and b.
    """
    cwd = cwd or Path.cwd()
    if spec.kind == "path":
        if fetch:
            top = _try(spec.path or cwd, "rev-parse", "--show-toplevel")
            if top:
                git(Path(top), "fetch", "--quiet", "origin")
        return _resolve_path(spec, base_branch)
    chosen = _pick_repo(spec, repo, cwd)
    if fetch:
        git(chosen, "fetch", "--quiet", "origin")
    if spec.kind == "pr":
        return _resolve_pr(spec, chosen)
    if spec.kind == "branch":
        return _resolve_branch(spec, chosen, base_branch)
    if spec.kind == "range2":
        return Resolved(diff_target(chosen, f"{spec.left}..{spec.right}"))
    left = rev_parse(chosen, spec.left or "HEAD")
    right = rev_parse(chosen, spec.right or "HEAD")
    if not left or not right:
        missing = spec.left if not left else spec.right
        raise TargetError(f"{missing} does not resolve to a commit in {chosen}")
    base = merge_base(chosen, left, right)
    if not base:
        raise TargetError(f"no merge-base between {spec.left} and {spec.right}")
    return Resolved(Target(
        slug=f"{slug_part(chosen.name)}--{base[:8]}..{right[:8]}",
        label=f"{chosen.name} {spec.text}", repo_path=str(chosen), remote_url=None,
        base_sha=base, head_sha=right, files_changed=_changed_files(chosen, base, right)))


def diff_findings(target: Target) -> FindingsFile:
    """The in-memory FindingsFile for plain-diff mode: a target and no findings."""
    return FindingsFile(
        schema_version=1, generated_at=datetime.now(timezone.utc),
        producer="review-viewer:diff", target=target, findings=[])
