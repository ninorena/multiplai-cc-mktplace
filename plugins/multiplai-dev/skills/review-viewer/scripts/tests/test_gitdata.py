from __future__ import annotations

import pytest

from fixture_repo import SERVICE_HEAD
from review_viewer.gitdata import (
    DELETED_PREVIEW_LINES, PathNotInReview, allowed_paths, diff_target, file_view,
    parse_unified,
)
from review_viewer.models import load_findings


@pytest.fixture
def review(findings_path):
    ff = load_findings(findings_path)
    return ff.target, ff


def test_parse_unified_rows():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
            "@@ -10,2 +10,3 @@\n j\n+k\n l\n\\ No newline at end of file\n")
    rows = parse_unified(diff)
    kinds = [r.k for r in rows]
    assert kinds == ["ctx", "del", "add", "ctx", "gap", "ctx", "add", "ctx"]
    assert (rows[1].o, rows[1].n) == (2, None)
    assert (rows[2].o, rows[2].n) == (None, 2)
    assert rows[4].t.startswith("@@ -10,2")
    assert (rows[6].n, rows[6].t) == (11, "k")


def test_modified_file_is_full_with_marks(review):
    target, ff = review
    view = file_view(target, "app/service.py", ff)
    assert not (view.truncated or view.deleted or view.binary)
    assert view.language == "python"
    head_lines = SERVICE_HEAD.splitlines()
    shown = [r for r in view.rows if r.k in ("ctx", "add")]
    assert [r.t for r in shown] == head_lines
    added = {r.n for r in view.rows if r.k == "add"}
    assert {3, 5, 9, 13, 14, 20} <= added
    deleted = [r for r in view.rows if r.k == "del"]
    assert [d.t for d in deleted] == ['    return sum(i["price"] for i in items)']
    # The deleted line sits right before its replacement.
    idx = view.rows.index(deleted[0])
    assert view.rows[idx + 1].n == 9
    assert (9, 9) in view.cited_ranges and (12, 15) in view.cited_ranges


def test_added_file_is_all_added(review):
    target, ff = review
    view = file_view(target, "app/new_feature.py", ff)
    assert {r.k for r in view.rows} == {"add"}
    assert len(view.rows) == 5


def test_deleted_file_gets_a_preview(review):
    target, ff = review
    view = file_view(target, "app/old_module.py", ff)
    assert view.deleted and view.truncated
    assert len(view.rows) == DELETED_PREVIEW_LINES
    assert all(r.k == "del" for r in view.rows)
    assert view.rows[0].t == "LEGACY_1 = 1"


def test_binary_file_has_no_rows(review):
    target, ff = review
    view = file_view(target, "assets/logo.bin", ff)
    assert view.binary and view.rows == []


def test_large_file_is_truncated_to_hunks(review):
    target, ff = review
    view = file_view(target, "app/big.py", ff)
    assert view.truncated
    assert len(view.rows) < 100
    assert any(r.k == "add" and r.t == "value_2000 = 'changed'" for r in view.rows)
    assert any(r.k == "del" and r.t == "value_2000 = 2000" for r in view.rows)
    assert view.rows[0].k == "gap" or view.rows[0].n > 1


def test_path_outside_review_is_refused(review):
    target, ff = review
    assert "setup.py" not in allowed_paths(target, ff)
    with pytest.raises(PathNotInReview):
        file_view(target, "setup.py", ff)
    with pytest.raises(PathNotInReview):
        file_view(target, "../../etc/passwd", ff)


def test_diff_target_resolves_range(fixture_repo):
    repo, base, head = fixture_repo
    target = diff_target(repo, "main~1..main")
    assert (target.base_sha, target.head_sha) == (base, head)
    assert target.slug == f"fixture-repo--{base[:8]}..{head[:8]}"
    assert sorted(target.files_changed) == [
        "app/big.py", "app/new_feature.py", "app/old_module.py", "app/service.py",
        "assets/logo.bin"]


def test_missing_git_names_git(monkeypatch, fixture_repo):
    from review_viewer import gitdata
    monkeypatch.setenv("PATH", "/nonexistent")
    repo, _, _ = fixture_repo
    with pytest.raises(gitdata.GitError, match="install git"):
        diff_target(repo, "main~1..main")


# --- regressions from the PR 243 review --------------------------------------------

import subprocess  # noqa: E402

from review_viewer.gitdata import split_lines  # noqa: E402
from review_viewer.models import Target  # noqa: E402


def _repo(path, base_files: dict, head_files: dict, config: dict | None = None):
    """A two-commit repo; returns a Target for base..head over every file."""
    def run(*args):
        return subprocess.run(["git", "-C", str(path), "-c", "commit.gpgsign=false",
                               "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    path.mkdir(parents=True)
    run("init", "-q", "-b", "main")
    for k, v in (config or {}).items():
        run("config", k, v)
    for rel, text in base_files.items():
        (path / rel).write_text(text, encoding="utf-8", newline="")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    base = run("rev-parse", "HEAD")
    for rel, text in head_files.items():
        (path / rel).write_text(text, encoding="utf-8", newline="")
    run("add", "-A")
    run("commit", "-q", "-m", "head")
    head = run("rev-parse", "HEAD")
    return Target(slug="t", label="t", repo_path=str(path), base_sha=base, head_sha=head,
                  files_changed=sorted(set(base_files) | set(head_files)))


def test_lines_that_look_like_file_headers(tmp_path):
    target = _repo(tmp_path / "r",
                   {"q.sql": "select 1;\n-- old comment\nselect 2;\n", "c.c": "a;\n"},
                   {"q.sql": "select 1;\nselect 2;\n", "c.c": "a;\n++x;\nx++;\n"})
    sql = file_view(target, "q.sql")
    assert [r.t for r in sql.rows if r.k == "del"] == ["-- old comment"]
    c = file_view(target, "c.c")
    assert {r.n for r in c.rows if r.k == "add"} == {2, 3}
    rows = parse_unified(subprocess.run(
        ["git", "-C", target.repo_path, "diff", target.base_sha, target.head_sha, "--", "c.c"],
        capture_output=True, text=True).stdout)
    assert [(r.k, r.n, r.t) for r in rows if r.k == "add"] == [("add", 2, "++x;"), ("add", 3, "x++;")]


def test_git_config_cannot_rewrite_the_diff(tmp_path, monkeypatch):
    target = _repo(tmp_path / "r", {"a.py": "a = 1\n"}, {"a.py": "a = 1\nb = 2\n"},
                   config={"color.ui": "always", "color.diff": "always",
                           "diff.external": "false"})
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "false")
    view = file_view(target, "a.py")
    assert [(r.k, r.n) for r in view.rows] == [("ctx", 1), ("add", 2)]


def test_form_feed_does_not_shift_line_numbers(tmp_path):
    base = "a = 1\n\x0c\nb = 2\nc = 3\n"
    head = "a = 1\n\x0c\nb = 2\nc = 3\nd = 4\n"
    target = _repo(tmp_path / "r", {"f.py": base}, {"f.py": head})
    view = file_view(target, "f.py")
    assert [(r.k, r.n, r.t) for r in view.rows if r.k == "add"] == [("add", 5, "d = 4")]
    assert split_lines("x y\n\x85z\r\n") == ["x y", "\x85z"]


def test_plain_diff_slug_from_awkward_repo_name(tmp_path):
    target = _repo(tmp_path / "my repo+été", {"a": "1\n"}, {"a": "2\n"})
    t = diff_target(target.repo_path, "main~1..main")
    assert t.slug.startswith("my-repo-") and t.files_changed == ["a"]


def test_non_ascii_path_is_listed_unquoted(tmp_path):
    target = _repo(tmp_path / "r", {"é.txt": "1\n"}, {"é.txt": "2\n"})
    assert diff_target(target.repo_path, "main~1..main").files_changed == ["é.txt"]


# --- target resolution --------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402

from fixture_repo import build_remote  # noqa: E402
from review_viewer.gitdata import (  # noqa: E402
    TargetError, parse_target, resolve_target, sanitize_slug)


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    return build_remote(tmp_path_factory.mktemp("remote"))


@pytest.mark.parametrize("text,kind,fields", [
    ("123", "pr", {"number": 123, "owner": None}),
    ("#9", "pr", {"number": 9}),
    ("https://github.com/o/r/pull/123", "pr", {"owner": "o", "repo": "r", "number": 123}),
    ("https://github.com/o/r/pull/123/files", "pr", {"owner": "o", "repo": "r", "number": 123}),
    ("o/r#45", "pr", {"owner": "o", "repo": "r", "number": 45}),
    ("feature-x", "branch", {}),
    ("feat/a.b", "branch", {}),
    ("a..b", "range2", {"left": "a", "right": "b"}),
    ("main..", "range2", {"left": "main", "right": "HEAD"}),
    ("a...b", "range3", {"left": "a", "right": "b"}),
    ("", "path", {}),
])
def test_parse_target_table(text, kind, fields, tmp_path):
    spec = parse_target(text, cwd=tmp_path)
    assert spec.kind == kind
    for k, v in fields.items():
        assert getattr(spec, k) == v, k


def test_parse_target_directory_beats_range(tmp_path):
    (tmp_path / "wt").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    spec = parse_target("../wt", cwd=sub)
    assert spec.kind == "path" and spec.path == (tmp_path / "wt").resolve()
    assert parse_target(str(tmp_path), cwd=sub).kind == "path"


def test_branch_base_is_the_merge_base_not_mains_tip(remote):
    r = resolve_target(parse_target("feature"), remote["work"])
    t = r.target
    # The local branch wins over origin/feature, so the unpushed commit shows too.
    assert t.head_sha == remote["feature_local"]
    assert t.base_sha == remote["head"] != remote["main_tip"]
    assert t.files_changed == ["app/feature.py"]
    assert t.slug == sanitize_slug(f"{remote['work'].name}--feature") == "work--feature"
    assert r.pr is None


def test_origin_branch_when_no_local_branch(remote):
    t = resolve_target(parse_target("feature"), remote["clone"]).target
    assert t.head_sha == remote["feature_pushed"]
    assert t.base_sha == remote["head"]


def test_worktree_shows_only_unpushed_commits(remote):
    t = resolve_target(parse_target(str(remote["work"])), None).target
    assert (t.base_sha, t.head_sha) == (remote["feature_pushed"], remote["feature_local"])
    assert t.files_changed == ["app/feature.py"]
    assert t.slug == "work--feature--unpushed"


def test_worktree_without_upstream_uses_the_default_branch(remote, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(remote["work"]), "worktree", "add", "-q", "--detach",
                    str(wt), remote["feature_pushed"]], check=True)
    t = resolve_target(parse_target("", cwd=wt), None, cwd=wt).target
    assert (t.base_sha, t.head_sha) == (remote["head"], remote["feature_pushed"])


def test_worktree_with_nothing_unpushed_says_so(remote, tmp_path):
    with pytest.raises(TargetError, match="not pushed"):
        resolve_target(parse_target(str(remote["clone"])), None)


def test_three_dot_range_uses_the_merge_base(remote):
    t = resolve_target(parse_target("main...feature"), remote["work"]).target
    assert (t.base_sha, t.head_sha) == (remote["head"], remote["feature_local"])
    two = resolve_target(parse_target("main..feature"), remote["work"]).target
    assert (two.base_sha, two.head_sha) == (remote["main_tip"], remote["feature_local"])
    assert "README.md" in two.files_changed and "README.md" not in t.files_changed


def _fake_gh(tmp_path, monkeypatch, payload: dict) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "pr.json").write_text(json.dumps(payload))
    log = tmp_path / "gh.argv"
    script = bindir / "gh"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{log}'\ncat '{tmp_path / 'pr.json'}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _refs(repo) -> str:
    return subprocess.run(["git", "-C", str(repo), "for-each-ref"], capture_output=True,
                          text=True, check=True).stdout


def test_pr_resolves_and_fetches_only_objects(remote, tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(remote["origin"]), str(clone)], check=True)
    log = _fake_gh(tmp_path, monkeypatch, {
        "number": 7, "title": "Add PR module", "author": {"login": "alice"},
        "url": "https://github.com/o/r/pull/7", "headRefOid": remote["pr_head"],
        "baseRefOid": remote["main_tip"], "headRefName": "pr-branch", "baseRefName": "main",
        "body": "Body text"})
    before = _refs(clone)
    missing = subprocess.run(["git", "-C", str(clone), "cat-file", "-e",
                              f"{remote['pr_head']}^{{commit}}"], capture_output=True)
    assert missing.returncode != 0, "the clone must start without the PR head"
    r = resolve_target(parse_target("7"), clone)
    assert (r.target.base_sha, r.target.head_sha) == (remote["head"], remote["pr_head"])
    assert r.target.files_changed == ["app/pr.py"]
    assert r.target.slug == "clone--pr-7" and r.target.label == "clone PR #7"
    assert r.pr.title == "Add PR module" and r.pr.author == "alice"
    assert _refs(clone) == before, "the fetch must not create or move any ref"
    argv = log.read_text().split("\n")
    assert argv[:2] == ["pr", "view"] and argv[2] == "7" and "--repo" not in argv


def test_pr_url_passes_repo_and_needs_a_matching_clone(remote, tmp_path, monkeypatch):
    log = _fake_gh(tmp_path, monkeypatch, {
        "headRefOid": remote["pr_head"], "baseRefOid": remote["main_tip"],
        "baseRefName": "main"})
    r = resolve_target(parse_target("o/r#7"), remote["clone"])
    assert r.target.head_sha == remote["pr_head"]
    assert "--repo\no/r" in log.read_text()
    # No --repo, and the working directory's origin is not o/r.
    with pytest.raises(TargetError, match="pass --repo"):
        resolve_target(parse_target("https://github.com/o/r/pull/7"), None,
                       cwd=remote["clone"])


def test_pr_without_gh_names_gh(remote, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(TargetError, match="GitHub CLI"):
        resolve_target(parse_target("7"), remote["clone"])


def test_unknown_branch_is_a_clear_error(remote):
    with pytest.raises(TargetError, match="no branch 'nope'"):
        resolve_target(parse_target("nope"), remote["work"])
