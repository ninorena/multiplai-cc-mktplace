"""`post`: one PR comment with the HIGH and MEDIUM findings.

With `--decisions`, only findings whose recorded decision is `accept` go in.
The viewer keeps one entry per finding id, so the entry is the latest
decision. The skill runs this only on an explicit yes typed in the terminal.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from multiplai_core.log_utils import log_event

from .render import SHOWN_STATUSES
from .state import load_state
from .target import github_web_base

log = logging.getLogger(__name__)

POSTED_SEVERITIES = ("HIGH", "MEDIUM")


class PostError(Exception):
    """Refused or failed; the message says why. Exit code 2."""


def select(findings: list[dict], decisions: dict | None) -> list[dict]:
    chosen = [f for f in findings if f["severity"] in POSTED_SEVERITIES and f["status"] in SHOWN_STATUSES]
    if decisions is not None:
        chosen = [f for f in chosen if (decisions.get(f["id"]) or {}).get("decision") == "accept"]
    order = {s: i for i, s in enumerate(POSTED_SEVERITIES)}
    return sorted(chosen, key=lambda f: order[f["severity"]])


def link(web_base: str, head_sha: str, path: str, start: int, end: int) -> str:
    """Full-sha link with one line of context each side, as the code-review plugin requires."""
    return f"{web_base}/blob/{head_sha}/{path}#L{max(1, start - 1)}-L{end + 1}"


def comment_body(findings_file: dict, chosen: list[dict]) -> str:
    t = findings_file["target"]
    web = github_web_base(t.get("remote_url"))
    if not web:
        raise PostError(f"the review's remote ({t.get('remote_url')}) is not on GitHub; post needs a GitHub PR")
    out = ["### Code review", "", f"Found {len(chosen)} issue{'s' if len(chosen) != 1 else ''}:", ""]
    for i, f in enumerate(chosen, 1):
        out.append(f"{i}. {' '.join(f['claim'].split())} ({f['severity']})")
        out.append("")
        out.append(" ".join(f["failure_scenario"].split()))
        out.append("")
        if f.get("expected_behaviour"):
            out.append(f"Expected behaviour: {' '.join(f['expected_behaviour'].split())}")
            out.append("")
        c = f["citations"][0]
        out.append(link(web, t["head_sha"], c["path"], c["line_start"], c["line_end"]))
        out.append("")
    out.append("🤖 Generated with multiplai-dev:review")
    return "\n".join(out) + "\n"


def post(target_dir: Path, decisions_path: Path | None, *, session_id: str = "") -> str:
    """Post the comment; return a one-line summary. Raises PostError to refuse."""
    target_dir = target_dir.resolve()
    state = load_state(target_dir / "review-state.json")
    if state is None:
        raise PostError(f"no review-state.json in {target_dir}; run a review first")
    t = state.target
    if t.pr is None:
        raise PostError(f"post needs a PR target; this review is of {t.kind} {t.ref}. "
                        f"Re-run the review with --pr <number> to post it.")
    decisions = None
    if decisions_path is not None:
        if not decisions_path.is_file():
            raise PostError(f"decisions file not found: {decisions_path}")
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    findings_file = json.loads((target_dir / "findings.json").read_text(encoding="utf-8"))
    chosen = select(findings_file["findings"], decisions)
    if not chosen:
        return "nothing to post: no accepted HIGH or MEDIUM findings"
    body = comment_body(findings_file, chosen)

    gh = shutil.which("gh")
    if gh is None:
        raise PostError("posting needs the GitHub CLI (`gh`), which is not installed. Install it from "
                        "https://cli.github.com and run `gh auth login`.")
    proc = subprocess.run([gh, "pr", "comment", str(t.pr), "--body-file", "-"], input=body,
                          cwd=t.repo_path, capture_output=True, text=True, shell=False, check=False)
    if proc.returncode != 0:
        raise PostError(f"gh pr comment failed: {proc.stderr.strip()}")
    summary = f"posted {len(chosen)} {'accepted ' if decisions is not None else ''}findings to PR #{t.pr}"
    log_event("review", "post", summary, session_id=session_id, target=t.slug, pr=t.pr, count=len(chosen))
    return summary
