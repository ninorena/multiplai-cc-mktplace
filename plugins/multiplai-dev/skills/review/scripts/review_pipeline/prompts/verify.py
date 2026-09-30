"""Verifier prompt: one fresh agent per finding."""

from __future__ import annotations

import json

from ..models import Finding, TargetInfo
from . import CITATION_RULES, JSON_ONLY, workspace_block

SCHEMA = """\
{"status": "confirmed" | "refuted" | "unverifiable",
 "reason": "what you read and why it settles the question",
 "citations": [{"path": "...", "line_start": 1, "line_end": 1, "quote": "exact text"}],
 "expected_behaviour": "one sentence: what correct behaviour would be"}"""


def build(target: TargetInfo, finding: Finding) -> str:
    cited = json.dumps([c.model_dump() for c in finding.citations], indent=1)
    return "\n\n".join([
        "You are checking one claim from a code review. You did not write it; assume nothing it says "
        "until you have read the code yourself.",
        workspace_block(target),
        f"Claim ({finding.severity}) about `{finding.file}` lines {finding.line_start}-{finding.line_end}:\n"
        f"{finding.claim}",
        f"Failure scenario given:\n{finding.failure_scenario}",
        f"Lines cited for it:\n{cited}",
        "Read the cited lines and whatever else decides the question: callers, the values that reach "
        "this code, the settings it reads. Then answer:\n"
        "- `confirmed`: the failure scenario can happen. Cite the lines that make it happen.\n"
        "- `refuted`: the code shows it cannot happen. Cite the lines that prevent it.\n"
        "- `unverifiable`: it depends on something outside this repository (production data, a "
        "vendor's configuration, runtime values) or the code does not settle it. Say what is missing.\n"
        "A `confirmed` answer without a citation that a program can find at the cited lines is "
        "recorded as `unverifiable`.",
        "Unless you answer `refuted`, set `expected_behaviour`: state what correct behaviour would be, in "
        "one sentence, without proposing a code change. For example: \"A null timeout is rejected, not "
        "skipped.\" Say what the code should do, not how to change it.",
        CITATION_RULES,
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ])
