"""Stages: find → verify → merge.

Each is `async def run_<stage>(state, ctx) -> state`. A stage returns at once
when `state.stage` is already past it, and skips items it already has a
result for, so a resumed run repeats no finished work.

Only `RepoTrustError` and `BudgetExceededError` escape a stage. Any other
agent failure is logged at ERROR and turned into the conservative outcome for
that item (no findings from that finder, `unverifiable`, an unmerged group).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, TypeVar

from ..config import ReviewConfig
from ..models import Citation, TargetInfo
from ..progress import ProgressWriter

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class RunContext:
    config: ReviewConfig
    snapshot: Path  # the head tree the agents read
    diff: str
    progress: ProgressWriter | None = None
    session_id: str = ""
    # Stage-level counts for the `stage` activity event, filled by each stage.
    counts: dict[str, int] = field(default_factory=dict)
    gate_reasons: list[str] = field(default_factory=list)


async def bounded(items: Iterable[T], fn: Callable[[T], Awaitable[R]], limit: int) -> list[R]:
    """Run fn over items, at most *limit* at a time, results in input order.

    A TaskGroup, so the first exception that escapes `fn` (trust, budget)
    cancels the rest instead of leaving them spending in the background.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def guarded(item: T) -> R:
        async with sem:
            return await fn(item)

    items = list(items)
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(guarded(item)) for item in items]
    except* Exception as group:
        # Re-raise the first underlying error, not the group, so callers can
        # catch BudgetExceededError / RepoTrustError by type.
        raise group.exceptions[0] from None
    return [t.result() for t in tasks]


def relative_path(path: str, target: TargetInfo, snapshot: Path) -> str:
    """A repo-relative path for whatever form the agent wrote."""
    p = path.strip()
    for root in (str(snapshot.resolve()), str(snapshot), target.repo_path):
        root = root.rstrip("/") + "/"
        if p.startswith(root):
            p = p[len(root):]
            break
    while p.startswith("./"):
        p = p[2:]
    return p


def fix_citation(citation: Citation | None, target: TargetInfo, snapshot: Path) -> Citation | None:
    if citation is None:
        return None
    return citation.model_copy(update={"path": relative_path(citation.path, target, snapshot)})
