"""The walkthrough: checked in code before the page ever sees it.

The Claude Code session writes `walkthrough.json` (the server never calls a
model); `walkthrough put` checks it against the target the viewer serves and
then replaces `<mailbox>/../walkthrough.json` atomically. The page polls
`/api/targets/<slug>/walkthrough` for it.

The served target is read from `<mailbox>/target.json`, which `serve` writes
when it publishes: the findings file (or the in-memory one for a plain diff),
the PR metadata and the stale-review notice. It holds no secret.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .gitdata import GIT_MISSING, GitError, _is_binary, git, split_lines
from .mailbox import atomic_write
from .models import FindingsFile, Walkthrough

# Findings a walkthrough must link when it says it is complete.
MUST_LINK = ("confirmed", "unverifiable")
# Assessments a walkthrough must carry when it says it is complete.
MUST_ASSESS = ("commits", "tests")


def walkthrough_path(box: Path) -> Path:
    return Path(box).parent / "walkthrough.json"


def served_path(box: Path) -> Path:
    return Path(box) / "target.json"


@dataclass
class Served:
    findings: FindingsFile
    pr: dict | None = None
    notice: str | None = None
    stats: dict | None = None

    def to_json(self) -> str:
        return json.dumps({"findings": self.findings.model_dump(mode="json"), "pr": self.pr,
                           "notice": self.notice, "stats": self.stats},
                          indent=2, ensure_ascii=False) + "\n"


def load_served(box: Path) -> Served:
    """The target `serve` wrote for this mailbox. Raises OSError or ValueError."""
    data = json.loads(served_path(box).read_text(encoding="utf-8"))
    return Served(FindingsFile.model_validate(data["findings"]), data.get("pr"),
                  data.get("notice"), data.get("stats"))


def load_walkthrough(box: Path) -> Walkthrough | None:
    path = walkthrough_path(box)
    if not path.exists():
        return None
    return Walkthrough.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class _Lines:
    """Line counts of files at a commit, read once each."""
    repo: str
    base: str
    head: str
    counts: dict[tuple[str, str], int | None] = field(default_factory=dict)
    binary: dict[str, bool] = field(default_factory=dict)

    def is_binary(self, path: str) -> bool:
        if path not in self.binary:
            self.binary[path] = _is_binary(self.repo, self.base, self.head, path)
        return self.binary[path]

    def count(self, sha: str, path: str) -> int | None:
        """Lines in *path* at *sha*, or None when it does not exist there."""
        key = (sha, path)
        if key not in self.counts:
            try:
                self.counts[key] = len(split_lines(git(self.repo, "show", f"{sha}:{path}")))
            except GitError as exc:
                if str(exc) == GIT_MISSING:
                    raise
                self.counts[key] = None
        return self.counts[key]


def coverage(wt: Walkthrough | None, ff: FindingsFile) -> tuple[list[str], list[str]]:
    """(changed files no anchor covers and no skip lists, ids of findings that
    must be linked and are not)."""
    files = ff.target.files_changed
    if wt is None:
        covered: set[str] = set()
        linked: set[str] = set()
    else:
        covered = {a.path for s in wt.steps for a in s.anchors} | {k.path for k in wt.skipped}
        linked = {fid for s in wt.steps for fid in s.finding_ids}
    uncovered = [f for f in files if f not in covered]
    unlinked = [f.id for f in ff.findings if f.status in MUST_LINK and f.id not in linked]
    return uncovered, unlinked


def check(wt: Walkthrough, ff: FindingsFile) -> list[str]:
    """Every reason this walkthrough cannot be shown for this target; empty
    when it can. Each message names the step (or the skipped entry) at fault."""
    t = ff.target
    errors: list[str] = []
    if wt.base_sha != t.base_sha or wt.head_sha != t.head_sha:
        errors.append(f"walkthrough: base_sha/head_sha {wt.base_sha[:12]}..{wt.head_sha[:12]} "
                      f"are not the served target's {t.base_sha[:12]}..{t.head_sha[:12]}")
    changed = set(t.files_changed)
    known = {f.id for f in ff.findings}
    lines = _Lines(t.repo_path, t.base_sha, t.head_sha)
    seen: set[str] = set()
    for step in wt.steps:
        where = f"step {step.id}"
        if step.id in seen:
            errors.append(f"{where}: the id is used by more than one step")
        seen.add(step.id)
        for a in step.anchors:
            at = f"{a.path}:{a.line_start}-{a.line_end} ({a.side})"
            if a.path not in changed:
                errors.append(f"{where}: anchor {at}: {a.path} is not a changed file in this diff")
                continue
            if a.line_end < a.line_start:
                errors.append(f"{where}: anchor {at}: line_end is before line_start")
                continue
            if lines.is_binary(a.path):
                errors.append(f"{where}: anchor {at}: {a.path} is binary and has no lines; "
                              "list it in skipped instead")
                continue
            sha = t.head_sha if a.side == "head" else t.base_sha
            n = lines.count(sha, a.path)
            if n is None:
                other = "base" if a.side == "head" else "head"
                errors.append(f"{where}: anchor {at}: {a.path} does not exist at {a.side} "
                              f"({sha[:12]}); anchor it on side {other!r}")
            elif a.line_end > n:
                errors.append(f"{where}: anchor {at}: {a.path} has {n} lines at {a.side} "
                              f"({sha[:12]})")
        for fid in step.finding_ids:
            if fid not in known:
                errors.append(f"{where}: finding {fid} is not in the loaded findings")
    for k in wt.skipped:
        if k.path not in changed:
            errors.append(f"skipped {k.path}: not a changed file in this diff")
    topics = [a.topic for a in wt.assessments]
    for topic in sorted({t for t in topics if t != "other" and topics.count(t) > 1}):
        errors.append(f"assessment {topic}: more than one assessment has this topic")
    if wt.complete:
        for topic in MUST_ASSESS:
            if topic not in topics:
                errors.append(f"walkthrough: complete is true but there is no {topic!r} "
                              "assessment")
        uncovered, unlinked = coverage(wt, ff)
        for path in uncovered:
            errors.append(f"walkthrough: complete is true but changed file {path} has no "
                          "anchor in any step and is not in skipped")
        for fid in unlinked:
            errors.append(f"walkthrough: complete is true but finding {fid} is not linked "
                          "from any step")
    return errors


def put(box: Path, text: str, ff: FindingsFile) -> tuple[Walkthrough | None, list[str]]:
    """Parse and check *text*; when it passes, replace the walkthrough file.
    Returns (the walkthrough, []) or (None or the walkthrough, errors)."""
    try:
        wt = Walkthrough.model_validate_json(text)
    except ValueError as exc:
        return None, [f"walkthrough: not a valid walkthrough.json v1:\n{exc}"]
    errors = check(wt, ff)
    if errors:
        return wt, errors
    atomic_write(walkthrough_path(box), wt.model_dump_json(indent=2) + "\n", mode=0o600)
    return wt, []
