from __future__ import annotations

import json
import os
import subprocess
from sys import executable as PYTHON

import pytest

from review_viewer.__main__ import _load_targets, build_parser, find_review


@pytest.fixture
def env(tmp_path, monkeypatch):
    """No workspace: output goes under ~/.multiplai, never the directory the
    user ran from."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    ran_from = tmp_path / "ran-from"
    ran_from.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("PWD", str(ran_from))
    monkeypatch.setenv("HOME", str(home))
    return home / ".multiplai"


def _review(reviews, findings_path, name="fixture-review", **target_changes):
    data = json.loads(findings_path.read_text())
    data["target"].update(target_changes)
    d = reviews / name
    d.mkdir(parents=True)
    (d / "findings.json").write_text(json.dumps(data))
    return d / "findings.json"


def _load(*argv):
    args = build_parser().parse_args(["serve", *argv])
    return _load_targets(args)


def test_exact_match_review_loads_its_findings(env, fixture_repo, findings_path, tmp_path):
    repo, base, head = fixture_repo
    reviews = tmp_path / "reviews"
    path = _review(reviews, findings_path, repo_path="/somewhere/else")
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo),
                         "--reviews-dir", str(reviews))
    (ff, box), = loaded
    assert box == path.parent / "viewer"
    assert len(ff.findings) == 3 and ff.target.repo_path == str(repo.resolve())
    assert meta[ff.target.slug] == {"pr": None, "notice": None, "review": "findings: 3"}


def test_stale_review_gives_a_notice_and_loads_nothing(env, fixture_repo, findings_path,
                                                      tmp_path):
    repo, base, head = fixture_repo
    reviews = tmp_path / "reviews"
    _review(reviews, findings_path, head_sha="f" * 40)
    loaded, meta = _load("--repo", str(repo), "--range", "main~1..main",
                         "--reviews-dir", str(reviews))
    (ff, box), = loaded
    assert ff.findings == [] and (ff.target.base_sha, ff.target.head_sha) == (base, head)
    m = meta[ff.target.slug]
    assert m["review"] == "stale ffffffff"
    assert m["notice"].startswith(f"a review exists for ffffffff; this diff is at {head[:8]}")
    assert box == env / "review-viewer" / ff.target.slug / "viewer"


def test_no_review_is_plain_diff_under_the_output_root(env, fixture_repo, tmp_path):
    repo, base, head = fixture_repo
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo),
                         "--reviews-dir", str(tmp_path / "empty"))
    (ff, box), = loaded
    assert ff.findings == [] and meta[ff.target.slug]["review"] == "none"
    assert box == env / "review-viewer" / ff.target.slug / "viewer"
    assert not (tmp_path / "ran-from" / "review-viewer").exists()


def test_workspace_inbox_wins_when_there_is_one(env, fixture_repo, tmp_path, monkeypatch):
    repo, _, _ = fixture_repo
    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (tmp_path / "cfg" / ".workspace").write_text(str(ws))
    loaded, _ = _load("--target", "main~1..main", "--repo", str(repo),
                      "--reviews-dir", str(tmp_path / "empty"))
    (ff, box), = loaded
    assert box == ws / "INBOX" / "review-viewer" / ff.target.slug / "viewer"


def test_default_reviews_dir_is_the_review_skills(env, fixture_repo, findings_path):
    repo, _, _ = fixture_repo
    _review(env / "reviews", findings_path)
    loaded, meta = _load("--target", "main~1..main", "--repo", str(repo))
    assert meta[loaded[0][0].target.slug]["review"] == "findings: 3"


def test_find_review_skips_broken_files(fixture_repo, findings_path, tmp_path):
    reviews = tmp_path / "reviews"
    (reviews / "broken").mkdir(parents=True)
    (reviews / "broken" / "findings.json").write_text("{not json")
    from review_viewer.models import load_findings
    target = load_findings(findings_path).target
    assert find_review(target, [reviews])[0] == "none"


def test_bad_combinations_exit_2(env, fixture_repo, findings_path):
    repo, _, _ = fixture_repo
    assert _load("--range", "main~1..main") == 2
    assert _load("--target", "x", "--range", "a..b", "--repo", str(repo)) == 2
    assert _load(str(findings_path), "--target", "main~1..main") == 2
    assert _load(str(findings_path), "--repo", str(repo)) == 2
    assert _load("--target", "no-such-branch", "--repo", str(repo)) == 2


def test_serve_prints_the_walkthrough_line_last(env, fixture_repo, tmp_path):
    from conftest import free_port
    repo, base, head = fixture_repo
    proc = subprocess.Popen(
        [PYTHON, "-m", "review_viewer", "--session-id", "sess-W", "serve", "--target",
         "main~1..main", "--repo", str(repo), "--reviews-dir", str(tmp_path / "none"),
         "--port", str(free_port()), "--idle", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONUNBUFFERED="1"))
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if line.startswith("walkthrough:"):
                break
        prefixes = [line.split(":", 1)[0] for line in lines]
        assert prefixes[0] == "open" and prefixes[-3:] == ["monitor", "pending", "walkthrough"]
        assert "mailbox" in prefixes and "url" in prefixes
        box = lines[prefixes.index("mailbox")].split(": ", 1)[1]
        assert lines[-1] == f"walkthrough: {os.path.dirname(box)}/walkthrough.json (none)"
        served = json.loads(open(os.path.join(box, "target.json")).read())
        assert served["findings"]["target"]["head_sha"] == head
    finally:
        subprocess.run([PYTHON, "-m", "review_viewer", "stop", "--all"], capture_output=True)
        try:
            proc.wait(10)
        finally:
            if proc.poll() is None:
                proc.kill()
