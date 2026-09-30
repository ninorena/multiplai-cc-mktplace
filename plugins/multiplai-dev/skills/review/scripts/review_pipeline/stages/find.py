"""find: one agent per dimension, then dedupe, then `finding_gate`."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from .. import sdk
from ..gates import file_at_head, finding_gate
from ..models import Finding, FinderOutput, FinderResult, Rejected, ReviewState, TargetInfo
from ..prompts import find as prompt
from . import RunContext, bounded, fix_citation, relative_path

log = logging.getLogger(__name__)

CONVENTIONS_MAX_CHARS = 40_000


def conventions_chain(target: TargetInfo) -> str:
    """Every CLAUDE.md from the repo root down to each changed file's directory, at head."""
    dirs: list[str] = [""]
    for f in target.files:
        parts = PurePosixPath(f).parent.parts
        for i in range(1, len(parts) + 1):
            dirs.append("/".join(parts[:i]))
    blocks, total = [], 0
    for d in dict.fromkeys(dirs):
        path = f"{d}/CLAUDE.md" if d else "CLAUDE.md"
        text = file_at_head(target, path)
        if text is None:
            continue
        block = f"### {path}\n\n{text.strip()}\n"
        if total + len(block) > CONVENTIONS_MAX_CHARS:
            blocks.append(f"### {path}\n\n[skipped: the rules above already fill the prompt budget]\n")
            continue
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


def dedupe_key(finding: Finding) -> tuple[str, int, str]:
    return (finding.file, finding.line_start, " ".join(finding.claim.lower().split())[:160])


def _normalise(finding: Finding, dimension: str, target: TargetInfo, ctx: RunContext) -> Finding:
    citations = [fix_citation(c, target, ctx.snapshot) for c in finding.citations]
    return finding.with_location(
        file=relative_path(finding.file, target, ctx.snapshot),
        citations=[c.model_dump() for c in citations if c is not None],
        dimension="pre-existing" if finding.dimension == "pre-existing" else dimension,
        finder=dimension,
        finders=[dimension],
    )


async def run_find(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("find"):
        return state
    target, cfg = state.target, ctx.config
    conventions = conventions_chain(target) if "conventions" in cfg.dimensions else ""
    todo = [d for d in cfg.dimensions if d not in state.finder_results]

    async def _one(dimension: str) -> FinderResult:
        try:
            out = await sdk.agent_call_structured(
                prompt.build(target, dimension, ctx.diff, conventions),
                FinderOutput,
                allowed_tools=sdk.FINDER_TOOLS, model=cfg.finder_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snapshot), budget_label=f"find:{dimension}",
            )
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("finder %s failed for %s", dimension, target.slug, exc_info=True)
            return FinderResult(error=f"finder {dimension}: {str(e).splitlines()[0][:200]}")
        if ctx.progress:
            ctx.progress.line(f"  finder {dimension}: {len(out.findings)} findings")
        return FinderResult(findings=[_normalise(f, dimension, target, ctx) for f in out.findings])

    async def one(dimension: str) -> None:
        # Stored as each finder returns: a budget stop mid-stage keeps what
        # was already paid for, and the checkpoint saved then carries it.
        state.finder_results[dimension] = await _one(dimension)

    await bounded(todo, one, cfg.concurrency)

    results = [state.finder_results[d] for d in cfg.dimensions]
    failures = [r.error for r in results if r.error]
    if len(failures) == len(results):
        for d in cfg.dimensions:
            del state.finder_results[d]  # a later run tries every finder again
        raise sdk.AgentCallError("every finder failed: " + "; ".join(failures))

    seen: dict[tuple[str, int, str], Finding] = {}
    kept: list[Finding] = []
    for result in results:
        for finding in result.findings:
            finding = finding.model_copy(deep=True)  # `finders` grows below; the stored result stays as returned
            key = dedupe_key(finding)
            if key in seen:
                first = seen[key]
                if finding.finder not in first.finders:
                    first.finders.append(finding.finder)
                continue
            seen[key] = finding
            gate = finding_gate(target, finding)
            if gate.passed:
                kept.append(finding)
            else:
                state.rejected.append(Rejected(finding=finding, reason=gate.reason, stage="find"))
                ctx.gate_reasons.append(gate.reason)

    state.findings = kept
    ctx.counts = {"found": len(kept) + len([r for r in state.rejected if r.stage == "find"]),
                  "kept": len(kept), "rejected": len([r for r in state.rejected if r.stage == "find"]),
                  "finder_failures": len(failures)}
    state.errors.extend(failures)
    state.stage = "find"
    return state
