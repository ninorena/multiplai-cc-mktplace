"""Finder prompt: one per dimension."""

from __future__ import annotations

from ..models import TargetInfo
from . import (CITATION_RULES, JSON_ONLY, commits_block, description_block, diff_block, files_block,
               settings_block, workspace_block)

DIMENSION_TASKS = {
    "diff-bugs": (
        "Read the diff and the changed files, and nothing else. Find correctness bugs the change "
        "introduces: wrong conditions, wrong values, missed cases, broken error handling, data loss."
    ),
    "callers": (
        "For the changed functions, methods and settings, open what the changed lines call and what "
        "calls them (use Grep to find callers). Find breakages at those boundaries: changed "
        "signatures or return shapes, callers relying on behaviour the change removed, values used "
        "in a way the change did not expect."
    ),
    "history": (
        "The commit subjects say what the change is meant to do. Read the changed files and the diff. "
        "Find places where the code does not do what its commits say, and behaviour the removed lines "
        "had that nothing replaces."
    ),
    "conventions": (
        "The repository's own rules (its CLAUDE.md files) are below. Find places where the changed "
        "lines break a rule that has a concrete consequence. Cite the changed line, and cite the rule "
        "as a second citation. Three cases the diff alone does not show:\n"
        "- When the change retires or alters a fact (a thing that 'does not exist yet' now exists, "
        "'nothing runs it' now something does, a number or a date changes), Grep the whole repository "
        "for other statements of the old fact: README, docs, comments, plans. Each one left standing "
        "is a finding with `dimension` \"pre-existing\", citing the stale line and the rule that says the "
        "docs must stay correct.\n"
        "- When a rule says where something must be recorded (an open item in a tracker, a decision in "
        "a log, a check in a checklist) and the change records that kind of thing somewhere else only, "
        "that is a finding: cite where it was written and the rule.\n"
        "- When the change makes a claim about the project's settings or process (CI gates a merge, a "
        "check is required, a phase is complete), check it against the repository settings and the "
        "repository, and report a claim they do not bear out."
    ),
    "tests": (
        "Find changed behaviour that no test would catch breaking. Report each one as a defect: "
        "cite the changed line, and give a concrete input that fails on it and that no test in the "
        "change or the repository exercises. Do not describe what the tests cover."
    ),
}

SEVERITY_GUIDE = """\
Severity: HIGH = breaks production behaviour, loses or corrupts data, or opens a security hole on a
realistic path. MEDIUM = wrong in a realistic case that is not the main path. LOW = wrong in an edge
case, or a trap for the next change."""

SCHEMA = """\
{"findings": [{"claim": "one sentence: what is wrong",
               "severity": "HIGH" | "MEDIUM" | "LOW",
               "file": "path of the changed file the bug is in",
               "line_start": 1, "line_end": 1,
               "failure_scenario": "concrete inputs or state -> the wrong output or crash",
               "citations": [{"path": "...", "line_start": 1, "line_end": 1, "quote": "exact text"}],
               "dimension": "" }]}"""


def build(target: TargetInfo, dimension: str, diff: str, conventions: str = "") -> str:
    parts = [
        f"You are one reviewer in a code review, looking at one aspect: {dimension}. Your final "
        f"message is one JSON object listing defects (schema at the end), read by a program; it is "
        f"not a report.",
        workspace_block(target, web=True),
        DIMENSION_TASKS[dimension],
        commits_block(target),
        description_block(target),
        settings_block(target),
        files_block(target),
        diff_block(target, diff),
    ]
    parts = [p for p in parts if p]
    if dimension == "conventions":
        parts.append("The repository's rules:\n\n" + (conventions or "(no CLAUDE.md files found)"))
    parts += [
        SEVERITY_GUIDE,
        CITATION_RULES,
        "Every finding cites the lines that show the problem, with the exact quote. A finding "
        "without a quote is discarded by a program, not a person. `file` must be one of the changed "
        "files. If the problem is in an unchanged file that the change makes reachable, executes, or "
        "makes wrong (code the change now calls or builds, documentation the change makes stale), set "
        "`dimension` to \"pre-existing\" and `file` to that file; otherwise leave `dimension` empty. "
        "Report only what you can show from the code; an empty list is a valid answer.",
        f"Schema:\n{SCHEMA}",
        JSON_ONLY,
    ]
    return "\n\n".join(parts)
