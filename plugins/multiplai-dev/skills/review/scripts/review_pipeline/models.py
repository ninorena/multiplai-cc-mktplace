"""Pipeline models.

These are the pipeline's own shapes, richer than the `findings.json` v1
contract (`export.py` maps them onto it). Two rules are enforced here, at parse
time, so no stage has to remember them:

- A finding cites at least one line range (`citations` has `min_length=1`). A
  finder that returns an uncited finding fails structured parsing.
- `Finding.id` is computed from `(file, line_start, claim)` with the v1 rule.
  Whatever id a model puts in its output is overwritten.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["HIGH", "MEDIUM", "LOW"]
SEVERITIES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")

# Stage names in run order. `ReviewState.stage` holds the last one completed.
STAGES: tuple[str, ...] = (
    "target", "find", "verify", "merge", "export", "render", "done",
)
# Stages a checkpoint written before 0.22 can name. Both ran after verify, so
# a resumed review of that age continues with merge.
_REMOVED_STAGES = {"prescribe": "verify", "check_fix": "verify"}


def finding_id(file: str, line_start: int, claim: str) -> str:
    """The v1 finding id: first 10 hex of sha1(f"{file}\\0{line_start}\\0{claim}").

    Reimplemented rather than imported from the viewer skill; `test_models.py`
    pins both to the same value for a fixed input.
    """
    return hashlib.sha1(f"{file}\0{line_start}\0{claim}".encode("utf-8")).hexdigest()[:10]


def lower_severity(severity: str) -> str:
    """One step down; LOW stays LOW."""
    i = SEVERITIES.index(severity)
    return SEVERITIES[min(i + 1, len(SEVERITIES) - 1)]


class _Model(BaseModel):
    # Models answer in JSON; an unknown key is ignored rather than failing the
    # whole parse. Export builds the v1 dicts field by field, so nothing
    # unknown ever reaches findings.json.
    model_config = ConfigDict(extra="ignore")


class Citation(_Model):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str  # exact text expected at those lines

    @model_validator(mode="after")
    def _ordered(self) -> "Citation":
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} is before line_start {self.line_start}")
        return self


class Finding(_Model):
    id: str = ""
    claim: str
    severity: Severity
    dimension: str = ""
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    failure_scenario: str
    citations: list[Citation] = Field(min_length=1)
    finder: str = ""
    # Every finder that reported this defect: the first one, then any whose
    # finding was dropped as a word-for-word copy or merged into this one.
    finders: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compute_id(self) -> "Finding":
        self.id = finding_id(self.file, self.line_start, self.claim)
        return self

    def with_location(self, **changes) -> "Finding":
        """A copy with changed fields and the id recomputed from them."""
        return Finding.model_validate({**self.model_dump(), **changes})


class Verdict(_Model):
    finding_id: str = ""
    status: Literal["confirmed", "refuted", "unverifiable"]
    reason: str
    citations: list[Citation] = Field(default_factory=list)  # the verifier's own re-reads
    # What correct behaviour looks like, in one sentence, without proposing a
    # code change. Exported on the finding as `expected_behaviour`.
    expected_behaviour: str = ""


class DuplicateSet(_Model):
    finding_ids: list[str]
    reason: str = ""


class GateResult(BaseModel):
    passed: bool
    reason: str = ""
    action: str = ""  # "keep" | "reject" | "downgrade" — what the caller does on failure


class TargetInfo(BaseModel):
    repo_path: str
    remote_url: str | None = None
    base_sha: str
    head_sha: str
    commits: list[tuple[str, str]] = Field(default_factory=list)  # (sha, subject)
    files: list[str] = Field(default_factory=list)
    diff_path: str = ""
    slug: str
    label: str = ""
    kind: Literal["branch", "pr", "range"] = "range"
    ref: str = ""  # the branch name, PR number or range as given
    pr: int | None = None
    tickets: list[str] = Field(default_factory=list)
    deployed_in: str | None = None


class Rejected(BaseModel):
    finding: Finding
    reason: str
    stage: str = "find"


class Merged(BaseModel):
    """A finding folded into another that describes the same defect."""
    finding: Finding
    into: str  # the id of the finding it was merged into
    reason: str


class ReviewState(BaseModel):
    target: TargetInfo
    stage: str = "target"  # last stage completed
    findings: list[Finding] = Field(default_factory=list)
    verdicts: dict[str, Verdict] = Field(default_factory=dict)  # by finding id
    rejected: list[Rejected] = Field(default_factory=list)
    merged: list[Merged] = Field(default_factory=list)
    # The merge agent's answer per group of overlapping findings, keyed by the
    # group's sorted ids, so a resumed merge stage asks no group twice.
    merge_answers: dict[str, list[DuplicateSet]] = Field(default_factory=dict)
    original_severity: dict[str, str] = Field(default_factory=dict)  # lowered findings only
    errors: list[str] = Field(default_factory=list)  # agent failures, shown in the review header
    budget: dict = Field(default_factory=dict)

    @field_validator("stage", mode="before")
    @classmethod
    def _removed_stage(cls, value: str) -> str:
        return _REMOVED_STAGES.get(value, value)

    def past(self, stage: str) -> bool:
        """Whether *stage* has already completed."""
        return STAGES.index(self.stage) >= STAGES.index(stage)


# --- structured-output envelopes the agents return ---------------------------


class FinderOutput(_Model):
    findings: list[Finding] = Field(default_factory=list)


class MergeOutput(_Model):
    duplicate_sets: list[DuplicateSet] = Field(default_factory=list)
