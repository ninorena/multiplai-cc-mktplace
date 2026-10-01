"""Pure-code gates. None of them calls a model.

Each gate takes the `TargetInfo` and the object it judges and returns a
`GateResult`. Everything they check is re-read from the target's head commit
with `git show`, never taken from what an agent said it read.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import functools
import logging
import subprocess

from .models import SEVERITIES, Citation, Finding, GateResult, TargetInfo, Verdict
from .target import _GIT, _env

log = logging.getLogger(__name__)


def _normalise(text: str) -> str:
    return " ".join(text.split())


@functools.lru_cache(maxsize=512)
def _show(repo: str, sha: str, path: str) -> str | None:
    """File text at *sha*, or None when the path is not in that commit."""
    proc = subprocess.run(
        [*_GIT, "-C", repo, "show", f"{sha}:{path}"],
        capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def file_at_head(target: TargetInfo, path: str) -> str | None:
    return _show(target.repo_path, target.head_sha, path)


def lines_at_head(target: TargetInfo, path: str, start: int, end: int) -> list[str] | None:
    """Lines start..end (1-based, inclusive) at head, split on \\n like git numbers them."""
    text = file_at_head(target, path)
    if text is None:
        return None
    return text.split("\n")[start - 1:end]


# --- citations ----------------------------------------------------------------


def citation_gate(target: TargetInfo, citation: Citation) -> GateResult:
    """The quote, whitespace-normalised, is inside lines line_start..line_end at head."""
    quote = _normalise(citation.quote)
    if not quote:
        return GateResult(passed=False, reason="empty quote", action="reject")
    text = file_at_head(target, citation.path)
    if text is None:
        return GateResult(passed=False, reason="path not at head", action="reject")
    window = _normalise("\n".join(text.split("\n")[citation.line_start - 1:citation.line_end]))
    if quote not in window:
        return GateResult(passed=False, reason="quote not at cited lines", action="reject")
    return GateResult(passed=True)


def finding_gate(target: TargetInfo, finding: Finding) -> GateResult:
    """Every repo citation reproduces, the file is in the diff (unless pre-existing), severity is known.

    A web citation (an http(s) URL as `path`) is not checked: the gates only
    read git. It may follow a repo citation, never stand first or alone, so a
    finding is always anchored to the reviewed commit.
    """
    if finding.severity not in SEVERITIES:
        return GateResult(passed=False, reason=f"unknown severity {finding.severity!r}", action="reject")
    if finding.dimension != "pre-existing" and finding.file not in target.files:
        return GateResult(passed=False, reason=f"{finding.file} is not a changed file", action="reject")
    if not finding.citations:
        return GateResult(passed=False, reason="no citations", action="reject")
    if finding.citations[0].is_web:
        return GateResult(passed=False, reason="first citation is a web source, not a file at head", action="reject")
    for i, citation in enumerate(finding.citations):
        if citation.is_web:
            continue
        result = citation_gate(target, citation)
        if not result.passed:
            return GateResult(
                passed=False, action="reject",
                reason=f"citation {i} ({citation.path}:{citation.line_start}-{citation.line_end}): {result.reason}",
            )
    return GateResult(passed=True)


def verdict_gate(target: TargetInfo, verdict: Verdict) -> GateResult:
    """A `confirmed` verdict must show at least one citation that reproduces.

    On failure the caller downgrades the verdict to `unverifiable`.
    """
    if verdict.status != "confirmed":
        return GateResult(passed=True)
    if not verdict.citations:
        return GateResult(passed=False, action="downgrade",
                          reason="verifier confirmed without citing what it read")
    reasons = []
    for citation in verdict.citations:
        if citation.is_web:
            reasons.append(f"{citation.path}: a web source does not confirm a finding")
            continue
        result = citation_gate(target, citation)
        if result.passed:
            return GateResult(passed=True)
        reasons.append(f"{citation.path}:{citation.line_start}: {result.reason}")
    return GateResult(passed=False, action="downgrade",
                      reason="verifier confirmed, but none of its citations reproduce (" + "; ".join(reasons) + ")")
