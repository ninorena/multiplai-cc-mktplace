"""target → find → verify → merge → export → render.

`review-state.json` is written after every stage; `resume` reloads it and
continues from the first stage not yet done.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from multiplai_core.log_utils import log_event

from . import budget, target as target_mod
from .config import ReviewConfig
from .export import write_findings_file
from .models import SEVERITIES, ReviewState
from .progress import ProgressWriter
from .render import summary_path, write_review, write_rollups
from .stages import RunContext
from .stages.find import run_find
from .stages.merge import run_merge
from .stages.verify import run_verify
from .state import load_state, save_state

log = logging.getLogger(__name__)

STAGE_FUNCTIONS = (
    ("find", run_find),
    ("verify", run_verify),
    ("merge", run_merge),
)

# The gate reasons that may reach activity.jsonl. A raw reason can quote a
# citation; the log records only which rule fired.
_REASON_KINDS = (
    "quote not at cited lines", "path not at head", "empty quote", "is not a changed file",
    "unknown severity", "no citations", "confirmed without citing", "none of its citations reproduce",
)


def reason_kind(reason: str) -> str:
    for kind in _REASON_KINDS:
        if kind in reason:
            return kind
    return "other"


class ReviewError(Exception):
    """Bad input or an unresolvable target. Exit code 2."""


@dataclass
class TargetSpec:
    repo: str
    branch: str | None = None
    pr: int | None = None
    range: str | None = None
    tickets: list[str] = field(default_factory=list)
    deployed_in: str | None = None
    base_branch: str | None = None
    fetch: bool = False


def _count_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) or "nothing to do"


async def run_state(state: ReviewState, target_dir: Path, config: ReviewConfig, *,
                    session_id: str = "") -> Path:
    """Run every stage not yet done; return the findings.json path."""
    t = state.target
    ledger = budget.start(config.max_cost_usd, state.budget)
    progress = ProgressWriter(target_dir / "progress.log")
    if state.stage == "target":
        progress.started(t.label or t.slug, t.base_sha, t.head_sha, len(t.files))
        log_event("review", "start", f"review started for {t.slug}", session_id=session_id,
                  target=t.slug, base=t.base_sha, head=t.head_sha, files=len(t.files))
    else:
        progress.line(f"RESUMED after {state.stage}")

    snapshot = target_mod.snapshot_head(t, target_dir / "tree")
    diff = Path(t.diff_path).read_text(encoding="utf-8") if t.diff_path else ""
    ctx = RunContext(config=config, snapshot=snapshot, diff=diff, progress=progress, session_id=session_id)
    save_state(state, target_dir)

    for name, fn in STAGE_FUNCTIONS:
        if state.past(name):
            continue
        progress.stage(name, "started")
        ctx.counts, ctx.gate_reasons = {}, []
        try:
            state = await fn(state, ctx)
        except budget.BudgetExceededError as e:
            state.budget = ledger.to_state()
            save_state(state, target_dir)
            progress.failed(str(e))
            log_event("review", "budget_stop", str(e), session_id=session_id, level="WARNING",
                      target=t.slug, cost_usd=round(e.cost_usd, 4), stage=name)
            raise
        state.budget = ledger.to_state()
        save_state(state, target_dir)
        summary = f"{name} done: {_count_line(ctx.counts)}"
        progress.stage(name, summary)
        print(f"{t.slug}: {summary}", flush=True)
        log_event("review", "stage", summary, session_id=session_id, target=t.slug, stage=name,
                  counts=dict(ctx.counts), cost_usd=round(ledger.cost_usd, 4))
        if ctx.gate_reasons:
            noun = "findings" if name == "find" else "verdicts"
            log_event("review", "gate_reject", f"{len(ctx.gate_reasons)} {noun} rejected by gates",
                      session_id=session_id, target=t.slug, stage=name, count=len(ctx.gate_reasons),
                      reasons=sorted({reason_kind(r) for r in ctx.gate_reasons}))

    findings_path = target_dir / "findings.json"
    if not state.past("export"):
        findings_path = write_findings_file(state, target_dir)
        state.stage = "export"
        save_state(state, target_dir)
    if not state.past("render"):
        write_review(state, target_dir, deployed=target_mod.deployed(t))
        state.stage = "render"
        save_state(state, target_dir)
    state.stage = "done"
    save_state(state, target_dir)
    shutil.rmtree(snapshot, ignore_errors=True)

    shown = [f for f in state.findings if getattr(state.verdicts.get(f.id), "status", "") in ("confirmed", "unverifiable")]
    counts = {s: sum(1 for f in shown if f.severity == s) for s in SEVERITIES}
    message = f"review finished: {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW"
    progress.done(f"{message} (${ledger.cost_usd:.2f})")
    print(f"{t.slug}: {message}; output in {target_dir}", flush=True)
    print(f"summary: {summary_path(state, target_dir)}", flush=True)
    log_event("review", "done", message, session_id=session_id, target=t.slug, counts=counts,
              cost_usd=round(ledger.cost_usd, 4), findings_path=str(findings_path))
    return findings_path


def prepare(spec: TargetSpec, out_dir: Path) -> tuple[ReviewState, Path]:
    """Resolve and gate the target, then write its directory. Raises ReviewError."""
    try:
        resolved = target_mod.resolve(spec.repo, branch=spec.branch, pr=spec.pr, range_=spec.range,
                                      base_branch=spec.base_branch, fetch=spec.fetch)
        diff = (target_mod.diff_text(resolved.repo, resolved.base_sha, resolved.head_sha)
                if resolved.base_sha and resolved.head_sha and not resolved.problem else None)
    except target_mod.TargetError as e:
        raise ReviewError(str(e)) from e
    gate = target_mod.target_gate(resolved, diff)
    if not gate.passed:
        raise ReviewError(f"{spec.repo}: {gate.reason}")
    info = target_mod.build_target(resolved, tickets=spec.tickets, deployed_in=spec.deployed_in)
    target_dir = out_dir / info.slug
    info = target_mod.write_target_files(info, diff or "", target_dir)
    progress_log = target_dir / "progress.log"
    if progress_log.exists():
        progress_log.unlink()  # a fresh review starts a fresh progress file
    return ReviewState(target=info), target_dir


async def review(spec: TargetSpec, out_dir: Path, config: ReviewConfig, *, session_id: str = "") -> Path:
    state, target_dir = prepare(spec, out_dir)
    return await run_state(state, target_dir, config, session_id=session_id)


async def resume(target_dir: Path, config: ReviewConfig, *, session_id: str = "") -> Path:
    state = load_state(target_dir / "review-state.json")
    if state is None:
        raise ReviewError(f"no readable review-state.json in {target_dir}")
    return await run_state(state, target_dir, config, session_id=session_id)


def load_batch(path: Path) -> list[TargetSpec]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ReviewError(f"cannot read batch file {path}: {e}") from e
    if isinstance(data, dict) and "targets" in data:
        data = data["targets"]
    if not isinstance(data, list) or not data:
        raise ReviewError(f"{path}: expected a non-empty YAML list of targets")
    specs = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "repo" not in entry:
            raise ReviewError(f"{path}: entry {i} needs a `repo`")
        refs = [k for k in ("branch", "pr", "range") if entry.get(k) not in (None, "")]
        if len(refs) != 1:
            raise ReviewError(f"{path}: entry {i} needs exactly one of branch, pr, range")
        tickets = entry.get("tickets") or []
        specs.append(TargetSpec(
            repo=str(Path(str(entry["repo"])).expanduser()),
            branch=entry.get("branch"), pr=int(entry["pr"]) if entry.get("pr") not in (None, "") else None,
            range=entry.get("range"),
            tickets=[str(x) for x in (tickets if isinstance(tickets, list) else [tickets])],
            deployed_in=entry.get("deployed_in"), base_branch=entry.get("base_branch"),
            fetch=bool(entry.get("fetch", False)),
        ))
    return specs


async def batch(specs: list[TargetSpec], out_dir: Path, config: ReviewConfig, *, parallel: int = 2,
                session_id: str = "") -> tuple[list[Path], list[str]]:
    """Review every target, at most *parallel* at once. Returns (findings paths in order, failures)."""
    sem = asyncio.Semaphore(max(1, parallel))
    results: list[Path | None] = [None] * len(specs)
    failures: list[str] = []

    async def one(i: int, spec: TargetSpec) -> None:
        async with sem:
            label = f"{Path(spec.repo).name} {spec.branch or spec.pr or spec.range}"
            try:
                # Each task has its own context, so budget.start() gives this
                # target its own ledger.
                results[i] = await review(spec, out_dir, config, session_id=session_id)
            except (ReviewError, budget.BudgetExceededError) as e:
                failures.append(f"{label}: {e}")
                print(f"FAILED {label}: {e}", flush=True)
            except Exception as e:  # one broken target must not take the batch down
                log.error("review of %s failed", label, exc_info=True)
                failures.append(f"{label}: {e}")
                print(f"FAILED {label}: {e}", flush=True)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(specs)))
    written = [p for p in results if p is not None]
    if written:
        write_rollups(out_dir, written)
    return written, failures
