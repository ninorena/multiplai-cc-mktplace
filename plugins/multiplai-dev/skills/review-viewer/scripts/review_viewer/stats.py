"""Measured facts about a change, for the badges at the top of the Summary tab.

Everything here is read from git (and, for a PR, from the `gh pr view` call
`serve` already made); nothing is judged by a model. Judgments — does the
commit message explain the change, are the tests enough — are written by the
session into the walkthrough's `assessments` and shown beside these.

Each badge carries a level: "good", "note" (worth a look) or "concern".
The thresholds are conventions, stated where they are defined.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from .gitdata import _DIFF_FLAGS, GIT_MISSING, GitError, git
from .models import Target

log = logging.getLogger(__name__)

# --- classifying files -------------------------------------------------------

LOCK_NAMES = {
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", "Cargo.lock", "go.sum", "Gemfile.lock",
    "composer.lock", "Package.resolved", "flake.lock",
}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_TEST_NAME_RE = re.compile(
    r"(^test_.*\.py$|_test\.(py|go|rb)$|\.(test|spec)\.[cm]?[jt]sx?$|Tests?\.swift$|_spec\.rb$|"
    r"Test\.(java|kt)$)")
_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testing"}


def classify(path: str) -> str:
    """"lock", "generated", "test", "docs" or "code"."""
    p = PurePosixPath(path)
    if p.name in LOCK_NAMES:
        return "lock"
    if ".min." in p.name or p.suffix == ".snap" or p.name.endswith(".pb.go"):
        return "generated"
    if _TEST_NAME_RE.search(p.name) or any(part in _TEST_DIRS for part in p.parts[:-1]):
        return "test"
    if p.suffix.lower() in DOC_SUFFIXES or "docs" in p.parts[:-1]:
        return "docs"
    return "code"


# --- thresholds --------------------------------------------------------------
#
# Size: reviewers find fewer defects per line once a review passes a few
# hundred lines (the SmartBear/Cisco code-review study put the useful limit
# at 200-400 lines per session). Counted lines are added + deleted, excluding
# lock and generated files, which nobody reads line by line.
SIZE_SMALL = 400
SIZE_LARGE = 1000
FILES_MANY = 30
# Tests: test lines added per code line added. Below this, say so.
TEST_RATIO_LOW = 0.3
# Commit subjects: the git convention is 50 characters, with 72 as the limit.
SUBJECT_MAX = 72
_CONVENTIONAL_RE = re.compile(r"^[a-z]+(\([^)]*\))?!?: \S")
_FIXUP_RE = re.compile(r"^(fixup!|squash!|amend!|wip\b|WIP\b)", re.IGNORECASE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


@dataclass
class Badge:
    id: str
    label: str
    level: str  # good | note | concern
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangeStats:
    files: int = 0
    added: int = 0
    deleted: int = 0
    binary: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    commits: list[dict] = field(default_factory=list)
    todos_added: int = 0
    badges: list[Badge] = field(default_factory=list)
    # {path: {"status": A|M|D|R|C|T, "added": int|None, "deleted": int|None}};
    # None counts for a binary file. Drives the file list's marks and counts.
    per_file: dict[str, dict] = field(default_factory=dict)
    # {path: tier} for the changed files the repo's RISK_FILE matches, and why
    # that file could not be used when it exists but is broken.
    tiers: dict[str, int] = field(default_factory=dict)
    tiers_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["badges"] = [b.to_dict() for b in self.badges]
        return d


# --- reading git -------------------------------------------------------------


def parse_numstat(out: str) -> list[tuple[str, int | None, int | None]]:
    """(path, added, deleted) from `git diff --numstat -z`; None for binary.
    A rename is `a\\tb\\t` followed by the old and new paths as two fields."""
    parts = out.split("\0")
    rows: list[tuple[str, int | None, int | None]] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        fields = entry.split("\t", 2)
        if len(fields) < 3:
            continue
        a, d, path = fields
        if not path:  # rename: the next two fields are old and new
            if i + 1 >= len(parts):
                break
            path = parts[i + 1]
            i += 2
        rows.append((path, None if a == "-" else int(a), None if d == "-" else int(d)))
    return rows


def parse_name_status(out: str) -> dict[str, str]:
    """{path: status letter} from `git diff --name-status -z`. A rename or
    copy (`R100`, `C75`) is followed by the old and new paths; the new one is
    the changed file."""
    parts = out.split("\0")
    status: dict[str, str] = {}
    i = 0
    while i < len(parts):
        code = parts[i]
        i += 1
        if not code:
            continue
        letter = code[0]
        if letter in "RC":
            if i + 1 >= len(parts):
                break
            status[parts[i + 1]] = letter
            i += 2
        else:
            if i >= len(parts):
                break
            status[parts[i]] = letter
            i += 1
    return status


def read_commits(repo: str, base: str, head: str) -> list[dict]:
    """Commits in base..head, oldest first: sha, subject, body."""
    out = git(repo, "log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", f"{base}..{head}")
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        commits.append({"sha": sha, "subject": subject, "body": body.strip()})
    return commits


def count_todos(diff: str) -> int:
    return sum(1 for line in diff.split("\n")
               if line.startswith("+") and not line.startswith("+++") and _TODO_RE.search(line))


# --- criticality tiers from the repo --------------------------------------------

# A repo may map paths to tiers (0-3, as in models.RiskInput) in this file at
# its root, read at the base commit, so the change under review cannot set
# its own tiers:
#
#   [tiers]
#   "modules/*" = 3
#   "docs/*" = 0
#
# Patterns are fnmatch globs, so `*` also matches `/`. A file that matches
# several patterns gets the highest tier.
RISK_FILE = ".review-risk.toml"


def parse_tiers(text: str) -> dict[str, int]:
    """The pattern → tier table from RISK_FILE's text. Raises ValueError with a
    reason when the file is not valid."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"not valid TOML: {exc}") from None
    table = data.get("tiers")
    if not isinstance(table, dict) or not table:
        raise ValueError("needs a non-empty [tiers] table")
    for pattern, tier in table.items():
        if not isinstance(tier, int) or isinstance(tier, bool) or not 0 <= tier <= 3:
            raise ValueError(f"tier for {pattern!r} must be a whole number from 0 to 3")
    return dict(table)


def match_tiers(table: dict[str, int], paths: list[str]) -> dict[str, int]:
    """{path: highest matching tier} for the paths any pattern matches."""
    out: dict[str, int] = {}
    for path in paths:
        hits = [tier for pattern, tier in table.items() if fnmatch.fnmatchcase(path, pattern)]
        if hits:
            out[path] = max(hits)
    return out


def repo_tiers(repo, base: str, paths: list[str]) -> tuple[dict[str, int], str]:
    """(tiers of the matched paths, error) from RISK_FILE at `base`. No file is
    not an error."""
    try:
        text = git(repo, "show", f"{base}:{RISK_FILE}")
    except GitError:
        return {}, ""
    try:
        return match_tiers(parse_tiers(text), paths), ""
    except ValueError as exc:
        log.warning("ignoring %s in %s: %s", RISK_FILE, repo, exc)
        return {}, f"{RISK_FILE}: {exc}"


# --- badges ------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def size_badge(s: ChangeStats) -> Badge:
    skip = sum(s.by_kind.get(k, {}).get(x, 0) for k in ("lock", "generated") for x in ("added", "deleted"))
    counted = s.added + s.deleted - skip
    level = "good" if counted <= SIZE_SMALL else ("note" if counted <= SIZE_LARGE else "concern")
    if s.files > FILES_MANY and level == "good":
        level = "note"
    word = {"good": "Small", "note": "Medium", "concern": "Large"}[level]
    return Badge("size", f"{word}: {counted} lines", level,
                 f"{counted} lines added or deleted outside lock and generated files, in "
                 f"{_plural(s.files, 'file')}. Up to {SIZE_SMALL} lines is small; over "
                 f"{SIZE_LARGE}, or over {FILES_MANY} files, is hard to review in one sitting "
                 "(reviewers find fewer defects per line past a few hundred lines).")


def tests_badge(s: ChangeStats) -> Badge:
    code = s.by_kind.get("code", {})
    test = s.by_kind.get("test", {})
    code_added, test_added = code.get("added", 0), test.get("added", 0)
    test_files = test.get("files", 0)
    if not code.get("files"):
        return Badge("tests", "Tests: no code changed", "good",
                     "No code files changed, so no test change is expected.")
    if not test_files:
        return Badge("tests", "Tests: none changed", "concern",
                     f"{_plural(code['files'], 'code file')} changed and no test file did.")
    ratio = test_added / code_added if code_added else 1.0
    level = "good" if ratio >= TEST_RATIO_LOW else "note"
    return Badge("tests", f"Tests: {_plural(test_files, 'file')}, {ratio:.1f}× code", level,
                 f"{test_added} test lines added for {code_added} code lines added "
                 f"(below {TEST_RATIO_LOW:.1f}× is flagged). This counts lines, not "
                 "coverage: it cannot tell which new code the tests exercise.")


def commits_badge(s: ChangeStats) -> Badge:
    commits = s.commits
    if not commits:
        return Badge("commits", "Commits: none", "note", "No commits between base and head.")
    long = [c for c in commits if len(c["subject"]) > SUBJECT_MAX]
    fixups = [c for c in commits if _FIXUP_RE.match(c["subject"])]
    bare = [c for c in commits if not c["body"]]
    conventional = sum(1 for c in commits if _CONVENTIONAL_RE.match(c["subject"]))
    problems = []
    if fixups:
        problems.append(f"{_plural(len(fixups), 'fixup or WIP commit')} to squash")
    if long:
        problems.append(f"{_plural(len(long), 'subject')} over {SUBJECT_MAX} characters")
    if bare and len(bare) == len(commits):
        problems.append("no commit has a body")
    level = "concern" if fixups else ("note" if problems else "good")
    detail = (f"{_plural(len(commits), 'commit')}; {conventional} use a conventional prefix; "
              f"{len(commits) - len(bare)} have a body. "
              + ("; ".join(problems).capitalize() + "." if problems else "No mechanical problems."))
    return Badge("commits", f"{_plural(len(commits), 'commit')}", level, detail)


def todos_badge(s: ChangeStats) -> Badge | None:
    if not s.todos_added:
        return None
    return Badge("todos", f"{s.todos_added} TODO/FIXME added", "note",
                 f"{_plural(s.todos_added, 'added line')} contain TODO, FIXME, XXX or HACK.")


def lockfile_badge(s: ChangeStats) -> Badge | None:
    lock = s.by_kind.get("lock", {})
    if not lock.get("files"):
        return None
    return Badge("lock", f"Lock files: {lock['files']}", "note",
                 f"{_plural(lock['files'], 'lock file')} changed "
                 f"(+{lock.get('added', 0)} −{lock.get('deleted', 0)}); dependencies moved.")


# The ids pr_badges() can return, so a refresh can replace exactly those.
PR_BADGE_IDS = frozenset({"pr-body", "checks", "conflicts", "draft", "approval"})


def pr_badges(pr: dict | None) -> list[Badge]:
    """Badges from the PR metadata `serve` already fetched."""
    if not pr:
        return []
    out: list[Badge] = []
    body = (pr.get("body") or "").strip()
    if len(body) < 40:
        out.append(Badge("pr-body", "PR description: " + ("empty" if not body else "very short"),
                         "concern", "A reviewer has little to go on without a description."))
    checks = pr.get("checks") or {}
    if checks.get("total"):
        failed, pending = checks.get("failed", 0), checks.get("pending", 0)
        level = "concern" if failed else ("note" if pending else "good")
        label = (f"Checks: {failed} failing" if failed else
                 f"Checks: {pending} running" if pending else f"Checks: {checks['total']} passing")
        out.append(Badge("checks", label, level,
                         f"{checks.get('passed', 0)} passed, {failed} failed, {pending} pending, "
                         f"of {checks['total']} (GitHub status checks at the PR head)."))
    if pr.get("mergeable") == "CONFLICTING":
        out.append(Badge("conflicts", "Merge conflicts", "concern",
                         "GitHub reports this PR conflicts with its base branch."))
    if pr.get("draft"):
        out.append(Badge("draft", "Draft", "note", "The PR is marked as a draft."))
    decision = pr.get("review_decision")
    if decision == "APPROVED":
        out.append(Badge("approval", "Approved", "good", "A required reviewer approved it."))
    elif decision == "CHANGES_REQUESTED":
        out.append(Badge("approval", "Changes requested", "concern",
                         "A reviewer requested changes."))
    return out


def change_stats(target: Target, pr: dict | None = None) -> ChangeStats:
    """Measure base..head. Raises GitError when git cannot read the range."""
    repo, base, head = target.repo_path, target.base_sha, target.head_sha
    s = ChangeStats(files=len(target.files_changed))
    status = parse_name_status(git(repo, "diff", *_DIFF_FLAGS, "--name-status", "-z", base, head))
    for path, a, d in parse_numstat(git(repo, "diff", *_DIFF_FLAGS, "--numstat", "-z", base, head)):
        s.per_file[path] = {"status": status.get(path, "M"), "added": a, "deleted": d}
        kind = classify(path)
        k = s.by_kind.setdefault(kind, {"files": 0, "added": 0, "deleted": 0})
        k["files"] += 1
        if a is None:
            s.binary += 1
            continue
        k["added"] += a
        k["deleted"] += d or 0
        s.added += a
        s.deleted += d or 0
    s.commits = read_commits(repo, base, head)
    s.tiers, s.tiers_error = repo_tiers(repo, base, list(target.files_changed))
    s.todos_added = count_todos(git(repo, "diff", *_DIFF_FLAGS, "--unified=0", base, head))
    s.badges = [Badge("totals", f"{_plural(s.files, 'file')} · +{s.added} −{s.deleted}", "good",
                      ", ".join(f"{k} {v['files']} (+{v['added']} −{v['deleted']})"
                                for k, v in sorted(s.by_kind.items()))
                      + (f"; {s.binary} binary" if s.binary else "")),
                size_badge(s), tests_badge(s), commits_badge(s)]
    s.badges += [b for b in (todos_badge(s), lockfile_badge(s)) if b]
    s.badges += pr_badges(pr)
    return s


def safe_change_stats(target: Target, pr: dict | None = None) -> dict | None:
    """change_stats as a dict, or None (logged) when git cannot read it."""
    try:
        return change_stats(target, pr).to_dict()
    except GitError as exc:
        if str(exc) == GIT_MISSING:
            raise
        log.warning("cannot measure %s: %s", target.slug, exc)
        return None
