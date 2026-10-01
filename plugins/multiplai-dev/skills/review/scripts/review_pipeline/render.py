"""Markdown: the per-target review, and severity rollups across targets.

Both render from `findings.json` v1 dicts, so a rollup never scrapes markdown.
The per-target review adds what only the pipeline state has: the severity a
finding had before it was lowered, the finders that reported it, and the
findings merged into it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .export import to_findings_file
from .models import SEVERITIES, ReviewState
from .target import github_web_base

log = logging.getLogger(__name__)

SHOWN_STATUSES = ("confirmed", "unverifiable")


def code_link(web_base: str | None, head_sha: str, path: str, start: int, end: int) -> str:
    """A full-sha GitHub link for a GitHub remote, else `path:lines`."""
    lines = f"{start}" if start == end else f"{start}-{end}"
    if web_base:
        return f"[{path}:{lines}]({web_base}/blob/{head_sha}/{path}#L{start}-L{end})"
    return f"`{path}:{lines}`"


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def finding_section(fd: dict, *, web_base: str | None, head_sha: str,
                    original_severity: str | None = None,
                    finders: list[str] | None = None) -> str:
    """One finding. *finders* are the finders that reported it, from the pipeline state."""
    lines_ = f"{fd['line_start']}" if fd["line_start"] == fd["line_end"] else f"{fd['line_start']}-{fd['line_end']}"
    out = [f"### {fd['severity']} — {fd['file']}:{lines_} — {_one_line(fd['claim'])}", ""]

    status = fd["status"]
    if original_severity and original_severity != fd["severity"]:
        status += f" (lowered from {original_severity})"
    out += [f"**Verdict:** {status}. {_one_line(fd.get('verdict_reason') or '')}".rstrip(), ""]
    if finders and len(finders) > 1:
        out += [f"**Reported by:** {', '.join(finders)}", ""]

    out.append("**Evidence:**")
    out.append("")
    for c in fd["citations"]:
        if c["path"].lower().startswith(("http://", "https://")):
            out.append(f"- web source: <{c['path']}>")
        else:
            out.append(f"- {code_link(web_base, head_sha, c['path'], c['line_start'], c['line_end'])}")
        out.append("")
        out.append(_fence(c["quote"]))
        out.append("")
    out += [f"**Failure scenario:** {_one_line(fd['failure_scenario'])}", ""]
    if fd.get("expected_behaviour"):
        out += [f"**Expected behaviour:** {_one_line(fd['expected_behaviour'])}", ""]
    return "\n".join(out)


def _counts(findings: list[dict]) -> dict[str, int]:
    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    return {s: sum(1 for f in shown if f["severity"] == s) for s in SEVERITIES}


def render_review(state: ReviewState, *, deployed: str | None = None,
                  findings_file: dict | None = None) -> str:
    data = findings_file or to_findings_file(state)
    t = state.target
    web = github_web_base(t.remote_url)
    head = t.head_sha
    findings = data["findings"]
    counts = _counts(findings)

    commit_link = (lambda sha: f"[{sha[:10]}]({web}/commit/{sha})") if web else (lambda sha: f"`{sha[:10]}`")
    out = [f"# Review — {t.label or t.slug}", ""]
    out.append(f"- **Repo:** {Path(t.repo_path).name}" + (f" ({web})" if web else f" (`{t.repo_path}`)"))
    if t.tickets:
        out.append(f"- **Tickets:** {', '.join(t.tickets)}")
    out.append(f"- **Base commit:** {commit_link(t.base_sha)}")
    out.append(f"- **Head commit:** {commit_link(head)}")
    out.append(f"- **Commits:** {len(t.commits)}")
    out += [f"  - {commit_link(sha)} {_one_line(subject)}" for sha, subject in t.commits]
    out.append(f"- **Files changed:** {len(t.files)}")
    out += [f"  - `{f}`" for f in t.files]
    if t.deployed_in:
        out.append(f"- **Deployed in {t.deployed_in}:** {deployed or 'unknown'}")
    refuted = sum(1 for f in findings if f["status"] == "refuted")
    rejected = sum(1 for f in findings if f["status"] == "rejected")
    out.append(f"- **Findings:** {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW; "
               f"{refuted} refuted, {rejected} rejected by the gates (see the appendix)")
    if state.budget.get("cost_usd") is not None:
        out.append(f"- **Model cost:** ${float(state.budget['cost_usd']):.2f} over {state.budget.get('calls', 0)} agent calls")
    if state.errors:
        out.append("- **Agent failures:** " + "; ".join(_one_line(e) for e in state.errors))
    out += ["", "## Findings", ""]

    finders = {f.id: f.finders for f in state.findings}

    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    if not shown:
        out += ["No confirmed or unverifiable findings.", ""]
    for severity in SEVERITIES:
        group = [f for f in shown if f["severity"] == severity]
        if not group:
            continue
        out += [f"## {severity}", ""]
        for fd in group:
            out.append(finding_section(fd, web_base=web, head_sha=head,
                                       original_severity=state.original_severity.get(fd["id"]),
                                       finders=finders.get(fd["id"])))

    out += ["## Appendix — rejected, refuted and merged", ""]
    dropped = [f for f in findings if f["status"] in ("refuted", "rejected")]
    if not dropped and not state.merged:
        out += ["Nothing was rejected, refuted or merged.", ""]
    for fd in dropped:
        lines_ = f"{fd['line_start']}-{fd['line_end']}"
        who = "the verifier" if fd["status"] == "refuted" else "a gate"
        out.append(f"- **{fd['status']}** ({fd['severity']}) `{fd['file']}:{lines_}` — {_one_line(fd['claim'])}  ")
        out.append(f"  Reason from {who}: {_one_line(fd.get('verdict_reason') or '')}")
    for m in state.merged:
        f = m.finding
        out.append(f"- **merged** ({f.severity}) `{f.file}:{f.line_start}-{f.line_end}` — {_one_line(f.claim)}  ")
        out.append(f"  Merged into `{m.into}`: {_one_line(m.reason)}")
    out.append("")
    return "\n".join(out)


def write_review(state: ReviewState, target_dir: Path, *, deployed: str | None = None) -> Path:
    """Write the full review and its short summary; return the full review's path."""
    path = target_dir / f"review-{state.target.slug}.md"
    path.write_text(render_review(state, deployed=deployed), encoding="utf-8")
    summary_path(state, target_dir).write_text(render_summary(state), encoding="utf-8")
    return path


# --- summary -------------------------------------------------------------------

SUMMARY_CLAIM_CHARS = 120


def _short(text: str, limit: int = SUMMARY_CLAIM_CHARS) -> str:
    """The first sentence of `text`, cut to `limit` characters."""
    text = _one_line(text)
    end = re.search(r"\. (?=[A-Z])", text)
    if end:
        text = text[:end.start() + 1]
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def summary_path(state: ReviewState, target_dir: Path) -> Path:
    return target_dir / f"summary-{state.target.slug}.md"


def render_summary(state: ReviewState, *, findings_file: dict | None = None) -> str:
    """How the review went, short enough to read in chat.

    One line per HIGH and MEDIUM finding, a count for LOW, and one line per
    finding the verifier refuted or a gate rejected.
    """
    data = findings_file or to_findings_file(state)
    t = state.target
    findings = data["findings"]
    counts = _counts(findings)
    shown = [f for f in findings if f["status"] in SHOWN_STATUSES]
    dropped = [f for f in findings if f["status"] in ("refuted", "rejected")]

    out = [f"# Review summary — {t.label or t.slug}", ""]
    out.append(f"{len(t.commits)} commits, {len(t.files)} files, {t.base_sha[:10]}..{t.head_sha[:10]}.")
    if state.budget.get("cost_usd") is not None:
        out.append(f"Cost ${float(state.budget['cost_usd']):.2f} over {state.budget.get('calls', 0)} agent calls.")
    out.append(f"Findings: {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW. Dropped: "
               f"{sum(1 for f in dropped if f['status'] == 'refuted')} refuted by the verifier, "
               f"{sum(1 for f in dropped if f['status'] == 'rejected')} rejected by the gates"
               + (f", {len(state.merged)} merged into another finding as duplicates." if state.merged else "."))
    if state.errors:
        out.append("Agent failures: " + "; ".join(_short(e) for e in state.errors))

    for severity in ("HIGH", "MEDIUM"):
        group = [f for f in shown if f["severity"] == severity]
        if not group:
            continue
        out += ["", f"## {severity}", ""]
        for fd in group:
            lines_ = f"{fd['line_start']}" if fd["line_start"] == fd["line_end"] else f"{fd['line_start']}-{fd['line_end']}"
            out.append(f"- `{fd['file']}:{lines_}` — {_short(fd['claim'])} ({fd['status']})")
    if counts["LOW"]:
        out += ["", f"{counts['LOW']} LOW findings are in the full review."]
    if dropped:
        out += ["", "## Dropped", ""]
        for fd in dropped:
            # A verifier's reason often opens by conceding part of the claim, so
            # its first sentence misleads; the full reason is in the review's appendix.
            # A gate's reason is one mechanical sentence and fits.
            why = "" if fd["status"] == "refuted" else f" ({_one_line(fd.get('verdict_reason') or '')})"
            who = "refuted" if fd["status"] == "refuted" else "rejected by a gate"
            out.append(f"- {who}: `{fd['file']}:{fd['line_start']}` — {_short(fd['claim'], 90)}{why}")
    out += ["", f"Full review, with every reason: `review-{t.slug}.md`", ""]
    return "\n".join(out)


# --- rollups -------------------------------------------------------------------


def load_findings_files(paths: list[Path]) -> list[dict]:
    loaded = []
    for p in paths:
        try:
            loaded.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.warning("Skipping unreadable %s: %s", p, e)
    return loaded


def render_rollup(severity: str, files: list[dict]) -> str:
    sections, total, targets = [], 0, 0
    for data in files:
        t = data["target"]
        group = [f for f in data["findings"] if f["severity"] == severity and f["status"] in SHOWN_STATUSES]
        if not group:
            continue
        targets += 1
        total += len(group)
        web = github_web_base(t.get("remote_url"))
        sections.append(f"## {t['label']} (`{t['slug']}`)\n")
        for fd in group:
            sections.append(finding_section(fd, web_base=web, head_sha=t["head_sha"]))
    head = [f"# {severity} findings — {total} across {targets} of {len(files)} targets", ""]
    if not sections:
        head.append(f"No confirmed or unverifiable {severity} findings.")
    return "\n".join(head + sections) + "\n"


def write_rollups(out_dir: Path, paths: list[Path] | None = None) -> list[Path]:
    """`<SEV>-only.md` for each severity, from *paths* (default: `<out>/*/findings.json`, sorted)."""
    if paths is None:
        paths = sorted(out_dir.glob("*/findings.json"))
    files = load_findings_files(paths)
    written = []
    for severity in SEVERITIES:
        path = out_dir / f"{severity}-only.md"
        path.write_text(render_rollup(severity, files), encoding="utf-8")
        written.append(path)
    return written
