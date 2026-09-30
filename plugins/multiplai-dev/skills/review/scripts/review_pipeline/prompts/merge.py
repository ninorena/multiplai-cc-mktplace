"""Merge prompt: one agent per group of overlapping findings in one file."""

from __future__ import annotations

import json

from ..models import Finding, TargetInfo
from . import JSON_ONLY, workspace_block

SCHEMA = """\
{"duplicate_sets": [{"finding_ids": ["id", "id"],
                     "reason": "why these describe one defect"}]}"""


def build(target: TargetInfo, findings: list[Finding]) -> str:
    listed = json.dumps([
        {"id": f.id, "severity": f.severity, "file": f.file, "line_start": f.line_start,
         "line_end": f.line_end, "claim": f.claim, "failure_scenario": f.failure_scenario,
         "citations": [c.model_dump() for c in f.citations]}
        for f in findings
    ], indent=1)
    return "\n\n".join([
        "Several reviewers checked the same change independently, so the same defect can be reported "
        "more than once in different words. These findings are in the same file and their lines "
        "overlap. Decide which of them describe the same defect.",
        workspace_block(target),
        f"Findings:\n{listed}",
        "Two findings describe the same defect when they point at the same faulty code and the same "
        "wrong behaviour, so that correcting one would correct the other. Findings about different "
        "problems in the same lines are not duplicates, even when they cite the same code. Read the "
        "cited lines when the claims alone do not settle it.\n"
        "Return one set per defect reported more than once, each with at least two ids from the list "
        "above. An id appears in at most one set. Leave out every finding that has no duplicate; an "
        "empty `duplicate_sets` is a valid answer.",
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ])
