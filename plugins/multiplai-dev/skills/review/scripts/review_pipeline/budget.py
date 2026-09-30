"""Per-target token/cost ledger with a circuit breaker.

Same shape as buildme's `budget.py`: recording never raises, only `check()`
can stop a run, and a resumed run restores what was already spent. One
difference: a batch reviews several targets concurrently in one process, and
the ceiling is per target. The current ledger therefore lives in a
`ContextVar`, so each target's asyncio task records against its own ledger.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Per target. Past reviews spent $1-11 for up to 18 changed files, and $60 for
# 52; the finders cost about $0.40-0.50 per changed file on the larger ones.
DEFAULT_MAX_USD = 50.0
WARN_FRACTION = 0.8


class BudgetExceededError(Exception):
    """The target has spent its ceiling. Carries where the money went."""

    def __init__(self, message: str, *, diagnosis: str = "", cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.diagnosis = diagnosis
        self.cost_usd = cost_usd


@dataclass
class ReviewBudget:
    max_usd: float | None = DEFAULT_MAX_USD
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    by_label: dict[str, float] = field(default_factory=dict)  # {stage label: usd}
    _warned: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def record(self, usage, *, label: str = "") -> None:
        """Add one call's usage (`AgentUsage` or anything shaped like it). Never raises."""
        try:
            self.calls += 1
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            self.cache_read_tokens += int(getattr(usage, "cache_read_tokens", 0) or 0)
            self.cache_creation_tokens += int(getattr(usage, "cache_creation_tokens", 0) or 0)
            cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
            self.cost_usd += cost
            if label:
                self.by_label[label] = self.by_label.get(label, 0.0) + cost
        except Exception as e:  # pragma: no cover — accounting must never break a run
            log.warning("Budget accounting failed for a call (ignored): %s", e)

    def diagnosis(self) -> str:
        lines = [f"Spent ${self.cost_usd:.2f} over {self.calls} agent calls ({self.total_tokens:,} tokens)."]
        if self.max_usd:
            lines.append(f"  ceiling: ${self.max_usd:.2f}")
        if self.by_label:
            top = sorted(self.by_label.items(), key=lambda kv: -kv[1])
            lines.append("  by stage: " + ", ".join(f"{k}=${v:.2f}" for k, v in top))
        return "\n".join(lines)

    def check(self, *, stage: str = "") -> None:
        """Raise when the ceiling is spent; warn once at 80%."""
        if not self.max_usd:
            return
        used = self.cost_usd / self.max_usd
        if used >= 1.0:
            where = f" during {stage}" if stage else ""
            raise BudgetExceededError(
                f"circuit breaker stopped the run at ${self.cost_usd:.2f}{where} "
                f"(ceiling ${self.max_usd:.2f})",
                diagnosis=self.diagnosis(), cost_usd=self.cost_usd,
            )
        if used >= WARN_FRACTION and not self._warned:
            self._warned = True
            log.warning("Review has used %.0f%% of its budget — %s", used * 100, self.diagnosis())

    def to_state(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_label": {k: round(v, 6) for k, v in self.by_label.items()},
            "max_usd": self.max_usd,
        }

    def load_state(self, data: dict) -> None:
        """Restore spend from a checkpoint, so a resume does not get a fresh budget."""
        if not data:
            return
        self.calls = int(data.get("calls", 0) or 0)
        self.input_tokens = int(data.get("input_tokens", 0) or 0)
        self.output_tokens = int(data.get("output_tokens", 0) or 0)
        self.cache_read_tokens = int(data.get("cache_read_tokens", 0) or 0)
        self.cache_creation_tokens = int(data.get("cache_creation_tokens", 0) or 0)
        self.cost_usd = float(data.get("cost_usd", 0.0) or 0.0)
        self.by_label = {k: float(v) for k, v in (data.get("by_label") or {}).items()}


_current: contextvars.ContextVar[ReviewBudget | None] = contextvars.ContextVar("review_budget", default=None)


def get_budget() -> ReviewBudget:
    """The ledger of the target this task is reviewing (created on first use)."""
    budget = _current.get()
    if budget is None:
        budget = ReviewBudget()
        _current.set(budget)
    return budget


def start(max_usd: float | None = DEFAULT_MAX_USD, state: dict | None = None) -> ReviewBudget:
    """Begin a target's ledger in the current context, restoring *state* if given."""
    budget = ReviewBudget(max_usd=max_usd)
    budget.load_state(state or {})
    _current.set(budget)
    return budget


def record(usage, *, label: str = "") -> None:
    get_budget().record(usage, label=label)


def check(*, stage: str = "") -> None:
    get_budget().check(stage=stage)
