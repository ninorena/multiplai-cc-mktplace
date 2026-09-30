from __future__ import annotations

import re
import subprocess

import pytest

from review_viewer import stats
from review_viewer.gitdata import summarize_checks
from review_viewer.models import Target, load_findings
from review_viewer.stats import Badge, ChangeStats


@pytest.mark.parametrize("path,kind", [
    ("uv.lock", "lock"), ("web/package-lock.json", "lock"), ("go.sum", "lock"),
    ("static/app.min.js", "generated"), ("tests/__snapshots__/a.snap", "generated"),
    ("tests/test_x.py", "test"), ("app/x_test.go", "test"), ("src/a.test.ts", "test"),
    ("src/a.spec.jsx", "test"), ("Sources/FooTests.swift", "test"), ("pkg/test/helpers.py", "test"),
    ("README.md", "docs"), ("docs/guide.html", "docs"), ("CHANGELOG.md", "docs"),
    ("app/service.py", "code"), ("latest.py", "code"), ("contest/x.py", "code"),
])
def test_classify(path, kind):
    assert stats.classify(path) == kind


def test_parse_numstat_handles_binary_and_renames():
    out = "3\t1\tapp/a.py\0-\t-\tlogo.bin\0" "2\t0\t\0old/name.py\0new/name.py\0" "0\t5\tgone.py\0"
    assert stats.parse_numstat(out) == [
        ("app/a.py", 3, 1), ("logo.bin", None, None), ("new/name.py", 2, 0), ("gone.py", 0, 5)]
    assert stats.parse_numstat("") == []


def test_count_todos_counts_added_lines_only():
    diff = "+++ b/x.py\n+# TODO: later\n-# FIXME old\n+ok = 1\n+HACK = True\n+todo = 'lower case'\n"
    assert stats.count_todos(diff) == 2


def _s(**kinds) -> ChangeStats:
    s = ChangeStats(files=sum(k["files"] for k in kinds.values()))
    s.by_kind = kinds
    s.added = sum(k["added"] for k in kinds.values())
    s.deleted = sum(k["deleted"] for k in kinds.values())
    return s


def k(files, added, deleted=0):
    return {"files": files, "added": added, "deleted": deleted}


@pytest.mark.parametrize("kinds,level,word", [
    ({"code": k(3, 300, 100)}, "good", "Small"),
    ({"code": k(3, 300, 101)}, "note", "Medium"),
    ({"code": k(3, 900, 101)}, "concern", "Large"),
    # Lock files do not count towards size.
    ({"code": k(1, 50), "lock": k(1, 5000, 4000)}, "good", "Small"),
    # Many files make a small change medium.
    ({"code": k(31, 62)}, "note", "Medium"),
])
def test_size_badge(kinds, level, word):
    b = stats.size_badge(_s(**kinds))
    assert (b.level, b.label.split(":")[0]) == (level, word)


@pytest.mark.parametrize("kinds,level,start", [
    ({"docs": k(1, 10)}, "good", "Tests: no code changed"),
    ({"code": k(2, 100)}, "concern", "Tests: none changed"),
    ({"code": k(2, 100), "test": k(1, 20)}, "note", "Tests: 1 file, 0.2×"),
    ({"code": k(2, 100), "test": k(2, 30)}, "good", "Tests: 2 files, 0.3×"),
])
def test_tests_badge(kinds, level, start):
    b = stats.tests_badge(_s(**kinds))
    assert b.level == level and b.label.startswith(start), b
    assert "not coverage" in b.detail or "No code" in b.detail or "no test file" in b.detail


def _commits(*subjects, body="Why."):
    return [{"sha": f"{i:040x}", "subject": s, "body": body} for i, s in enumerate(subjects)]


def test_commits_badge_levels():
    s = ChangeStats(commits=_commits("feat(x): add y", "fix: z"))
    assert stats.commits_badge(s).level == "good"
    s.commits = _commits("feat: " + "x" * 80)
    b = stats.commits_badge(s)
    assert b.level == "note" and "over 72 characters" in b.detail
    s.commits = _commits("feat: a", "fixup! feat: a")
    assert stats.commits_badge(s).level == "concern"
    s.commits = _commits("a", "b", body="")
    b = stats.commits_badge(s)
    assert b.level == "note" and "No commit has a body" in b.detail
    assert stats.commits_badge(ChangeStats()).label == "Commits: none"


def test_summarize_checks():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
        {"__typename": "StatusContext", "state": "SUCCESS"},
        {"__typename": "StatusContext", "state": "PENDING"},
    ]
    assert summarize_checks(rollup) == {"total": 6, "passed": 3, "failed": 1, "pending": 2}
    assert summarize_checks(None)["total"] == 0


def _labels(badges: list[Badge]) -> dict[str, Badge]:
    return {b.id: b for b in badges}


def test_pr_badges():
    b = _labels(stats.pr_badges({"body": "", "checks": {"total": 3, "passed": 2, "failed": 1},
                                 "mergeable": "CONFLICTING", "draft": True,
                                 "review_decision": "CHANGES_REQUESTED"}))
    assert b["pr-body"].level == "concern" and b["pr-body"].label.endswith("empty")
    assert b["checks"].label == "Checks: 1 failing" and b["checks"].level == "concern"
    assert {"conflicts", "draft", "approval"} <= set(b)
    b = _labels(stats.pr_badges({"body": "x" * 100, "checks": {"total": 2, "passed": 2},
                                 "mergeable": "MERGEABLE", "review_decision": "APPROVED"}))
    assert set(b) == {"checks", "approval"}
    assert b["checks"].label == "Checks: 2 passing" and b["approval"].level == "good"
    assert stats.pr_badges(None) == []


def test_change_stats_on_the_fixture_repo(findings_path):
    ff = load_findings(findings_path)
    s = stats.change_stats(ff.target)
    assert s.files == len(ff.target.files_changed)
    assert s.binary == 1  # assets/logo.bin
    assert s.added > 0 and s.deleted > 0
    assert [c["subject"] for c in s.commits] == ["head"]
    ids = [b.id for b in s.badges]
    assert ids[:4] == ["totals", "size", "tests", "commits"]
    # Code changed and no test file did.
    assert _labels(s.badges)["tests"].level == "concern"
    d = stats.safe_change_stats(ff.target, {"body": ""})
    assert d["badges"][-1]["id"] == "pr-body"
    # One entry per changed file; the binary file has no line counts.
    assert set(s.per_file) == set(ff.target.files_changed)
    assert s.per_file["assets/logo.bin"]["added"] is None
    assert all(v["status"] in "AMDRCT" for v in s.per_file.values())
    assert d["per_file"] == s.per_file
    # The fixture repo has no tier file.
    assert (d["tiers"], d["tiers_error"]) == ({}, "")


def test_parse_tiers_and_match_take_the_highest_tier():
    table = stats.parse_tiers('[tiers]\n"modules/*" = 3\n"*.md" = 0\n"modules/x/*" = 1\n')
    assert stats.match_tiers(table, ["modules/x/main.tf", "README.md", "app.py"]) == {
        "modules/x/main.tf": 3, "README.md": 0}


@pytest.mark.parametrize("text, reason", [
    ("[tiers\n", "not valid TOML"),
    ("x = 1\n", "needs a non-empty [tiers] table"),
    ('[tiers]\n"a" = 4\n', "whole number from 0 to 3"),
    ('[tiers]\n"a" = true\n', "whole number from 0 to 3"),
])
def test_parse_tiers_rejects_bad_files(text, reason):
    with pytest.raises(ValueError, match=re.escape(reason)):
        stats.parse_tiers(text)


def _repo_with(tmp_path, files: dict[str, str]) -> str:
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q")
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return "HEAD"


def test_repo_tiers_reads_the_file_at_the_given_commit(tmp_path):
    ref = _repo_with(tmp_path, {stats.RISK_FILE: '[tiers]\n"infra/*" = 3\n', "a.py": ""})
    assert stats.repo_tiers(tmp_path, ref, ["infra/main.tf", "a.py"]) == ({"infra/main.tf": 3}, "")


def test_change_stats_reads_the_tiers_at_base_not_at_head(tmp_path):
    # The change under review rewrites the tier file to rate everything 0; the
    # score must still use the table the change cannot touch.
    _repo_with(tmp_path, {stats.RISK_FILE: '[tiers]\n"infra/*" = 3\n', "infra/main.tf": "a"})
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (tmp_path / "infra/main.tf").write_text("b")
    (tmp_path / stats.RISK_FILE).write_text('[tiers]\n"*" = 0\n')
    _repo_with(tmp_path, {})
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    target = Target(slug="t", label="t", repo_path=str(tmp_path), base_sha=base, head_sha=head,
                    files_changed=[stats.RISK_FILE, "infra/main.tf"])
    assert stats.change_stats(target).tiers == {"infra/main.tf": 3}


def test_repo_tiers_without_a_file_or_with_a_broken_one(tmp_path):
    head = _repo_with(tmp_path, {"a.py": ""})
    assert stats.repo_tiers(tmp_path, head, ["a.py"]) == ({}, "")
    (tmp_path / stats.RISK_FILE).write_text("[tiers]\n")
    head = _repo_with(tmp_path, {})
    assert stats.repo_tiers(tmp_path, head, ["a.py"]) == (
        {}, f"{stats.RISK_FILE}: needs a non-empty [tiers] table")


def test_parse_name_status_handles_renames():
    out = "M\0a.py\0A\0b.py\0D\0c.py\0R087\0old/x.py\0new/x.py\0"
    assert stats.parse_name_status(out) == {"a.py": "M", "b.py": "A", "c.py": "D", "new/x.py": "R"}
    assert stats.parse_name_status("") == {}


def test_safe_change_stats_returns_none_when_git_cannot_read(findings_path, tmp_path):
    ff = load_findings(findings_path)
    ff.target.repo_path = str(tmp_path)  # not a repository
    assert stats.safe_change_stats(ff.target) is None
