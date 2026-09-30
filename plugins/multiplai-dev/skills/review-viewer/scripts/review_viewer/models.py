"""The `findings.json` v1 contract, the `walkthrough.json` v1 contract, and the
mailbox rows.

These pydantic models are the source of truth. `export-schema` writes them to
`schema/findings.v1.schema.json` and `schema/walkthrough.v1.schema.json`, which
are committed; producers (the review skill, the session writing a
walkthrough) validate against those files. Every model forbids unknown keys, so a
producer that drifts fails validation instead of losing data silently.

Line numbers are 1-based and refer to the file at `head_sha`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
SCHEMA_PATH = SCHEMA_DIR / "findings.v1.schema.json"
WALKTHROUGH_SCHEMA_PATH = SCHEMA_DIR / "walkthrough.v1.schema.json"

SHA_PATTERN = r"^[0-9a-f]{40}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_Strict):
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str


# `Premise` and `Fix` describe the fix that review wrote for a confirmed
# finding before multiplai-dev 0.22. Review no longer writes one; the models
# stay so a findings file written then still opens. The page does not show it.
class Premise(_Strict):
    statement: str
    kind: Literal["in_repo", "external"]
    citation: Citation | None = None


class Fix(_Strict):
    description: str
    patch_sketch: str | None = None
    premises: list[Premise] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Finding(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{10}$")
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    status: Literal["confirmed", "unverifiable", "refuted", "rejected"]
    claim: str
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    failure_scenario: str
    citations: list[Citation] = Field(min_length=1)
    verdict_reason: str | None = None
    # One sentence from the verifier on what correct behaviour looks like,
    # without proposing code. None for refuted and rejected findings.
    expected_behaviour: str | None = None
    fix: Fix | None = None  # older files only; see Premise


class Target(_Strict):
    slug: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    label: str
    repo_path: str
    remote_url: str | None = None
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    files_changed: list[str]


class FindingsFile(_Strict):
    schema_version: Literal[1]
    generated_at: datetime
    producer: str
    target: Target
    findings: list[Finding]


def finding_id(file: str, line_start: int, claim: str) -> str:
    """Stable id for a finding: survives re-renders of the same review."""
    raw = f"{file}\0{line_start}\0{claim}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


def findings_digest(ff: "FindingsFile") -> str:
    """Identifies one version of a findings file's content."""
    return hashlib.sha256(ff.model_dump_json().encode("utf-8")).hexdigest()


def load_findings(path: str | Path) -> FindingsFile:
    """Parse and validate a findings file. Raises pydantic.ValidationError."""
    text = Path(path).read_text(encoding="utf-8")
    return FindingsFile.model_validate_json(text)


def _schema_text(model: type[BaseModel], schema_id: str) -> str:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def schema_text() -> str:
    """The JSON Schema for FindingsFile, in its committed form."""
    return _schema_text(FindingsFile, "findings.v1.schema.json")


# --- walkthrough.json v1 -----------------------------------------------------------
#
# Written by the Claude Code session that runs the skill (the server never
# calls a model), checked by `walkthrough put`, rendered by the page.

StepId = Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
FindingId = Annotated[str, Field(pattern=r"^[0-9a-f]{10}$")]
MAX_DIAGRAM_CHARS = 20000


class WalkthroughAnchor(_Strict):
    path: str
    side: Literal["head", "base"]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class Diagram(_Strict):
    kind: Literal["mermaid"]
    source: str = Field(max_length=MAX_DIAGRAM_CHARS)


class Step(_Strict):
    id: StepId
    title: str
    body_md: str
    anchors: list[WalkthroughAnchor] = Field(min_length=1)
    diagram: Diagram | None = None
    finding_ids: list[FindingId] = Field(default_factory=list)


class Skipped(_Strict):
    path: str
    reason: str


AssessmentTopic = Literal["commits", "tests", "size", "design", "risk", "other"]


# A badge shows its title on one line, so `walkthrough put` takes titles up to
# this long. The model still loads the 60 characters files written before 0.22
# could use; the published schema advertises this limit, the one put enforces.
ASSESSMENT_TITLE_MAX = 32


class Assessment(_Strict):
    """The session's judgment on one quality question, shown as a badge beside
    the measured ones. `commits` (does anything say why the change is made?)
    and `tests` (would a test fail if the changed behaviour broke?) are
    required once the walkthrough is complete, and judged by the rubric in
    SKILL.md. `size` and `risk` are accepted in files written before 0.22 but
    rejected by `walkthrough put`: size is measured and risk is the header
    score."""
    topic: AssessmentTopic
    verdict: Literal["good", "note", "concern"]
    title: str = Field(min_length=1, max_length=60,
                       json_schema_extra={"maxLength": ASSESSMENT_TITLE_MAX})
    detail_md: str


class RiskInput(_Strict):
    """The session's two judgments the risk score needs; the page applies the
    rules (see logic.js riskLevel). `tier` is how critical the most critical
    changed code is, from the table in SKILL.md: 0 docs, tests and tooling;
    1 one feature; 2 a shared module or public interface; 3 auth, money, data
    deletion or migration, shared infra, production deploy config. A repo's
    `.review-risk.toml`, read at the base commit, can raise it for the files
    it matches, never lower it."""
    tier: int = Field(ge=0, le=3)
    tier_why: str = Field(min_length=1)
    revertable: bool
    revert_why: str = Field(min_length=1)


class Walkthrough(_Strict):
    schema_version: Literal[1]
    generated_at: datetime
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    overview_md: str
    steps: list[Step]
    skipped: list[Skipped] = Field(default_factory=list)
    assessments: list[Assessment] = Field(default_factory=list)
    risk: RiskInput | None = None
    complete: bool


def walkthrough_schema_text() -> str:
    """The JSON Schema for Walkthrough, in its committed form."""
    return _schema_text(Walkthrough, "walkthrough.v1.schema.json")


# --- mailbox rows ------------------------------------------------------------


class Anchor(_Strict):
    """Lines a question is about. `side: base` numbers lines at base_sha, for
    a question about lines the change deletes; `head` (the default) at head_sha."""
    path: str
    side: Literal["head", "base"] = "head"
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class InboxRow(_Strict):
    v: Literal[1] = 1
    id: str
    ts: str
    target: str
    kind: Literal["question", "decision"]
    finding_id: str | None = None
    anchor: Anchor | None = None
    text: str
    decision: Literal["accept", "reject", "defer"] | None = None
    step_id: StepId | None = None
    # Sent by a block's light-bulb button: explain exactly `anchor`. The page
    # shows the answer in a strip above that block, not in the thread.
    explain: bool = False


class OutboxRow(_Strict):
    v: Literal[1] = 1
    reply_to: str
    ts: str
    text: str
    done: bool


class Decision(_Strict):
    decision: Literal["accept", "reject", "defer"]
    note: str = ""
    ts: str
