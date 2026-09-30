"""The whole pipeline through the CLI, every agent call replaced."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import KEYWORD_CITATION, SCHEMA, high_finding
from review_pipeline import budget, sdk
from review_pipeline.__main__ import main
from review_pipeline.models import DuplicateSet, FinderOutput, MergeOutput, ReviewState, Verdict


def reworded_finding():
    """The HIGH finding as a second finder words it: merged into it by the merge stage."""
    return high_finding().with_location(claim="Rate plans are filtered on a literal 'dolcebot'",
                                        severity="MEDIUM", dimension="callers", finder="callers")


class FakeAgents:
    """Canned answers by stage label. `kill_at` raises once at that stage."""

    def __init__(self, kill_at: str | None = None, cost: float = 0.0):
        self.calls: list[str] = []
        self.kill_at = kill_at
        self.cost = cost

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        self.calls.append(budget_label)
        if self.cost:
            budget.record(SimpleNamespace(cost_usd=self.cost), label=budget_label)
        stage = budget_label.split(":")[0]
        if stage == self.kill_at:
            self.kill_at = None
            raise RuntimeError("killed")
        if budget_label == "find:diff-bugs":
            return FinderOutput(findings=[high_finding()])
        if budget_label == "find:callers":
            return FinderOutput(findings=[reworded_finding()])
        if stage == "find":
            return FinderOutput()
        if stage == "verify":
            return Verdict(status="confirmed", reason="the keyword is the only filter", citations=[KEYWORD_CITATION],
                           expected_behaviour="Rate plans match whatever the channel is titled.")
        if stage == "merge":
            return MergeOutput(duplicate_sets=[DuplicateSet(
                finding_ids=[high_finding().id, reworded_finding().id], reason="the same literal keyword")])
        raise AssertionError(budget_label)


@pytest.fixture
def agents(monkeypatch):
    def install(**kwargs) -> FakeAgents:
        fake = FakeAgents(**kwargs)
        monkeypatch.setattr(sdk, "agent_call_structured", fake)
        return fake
    return install


def _findings_line(out: str) -> list[Path]:
    last = out.strip().splitlines()[-1]
    assert last.startswith("findings: ")
    return [Path(p) for p in last[len("findings: "):].split()]


def _review_args(repo, base, head, out, *extra):
    return ["--session-id", "sess-test", "--out", str(out), "review", "--repo", str(repo),
            "--range", f"{base}..{head}", "--ticket", "DB-2038", "--trust-repo", *extra]


def test_review_end_to_end(fixture_repo, tmp_path, agents, capsys):
    import jsonschema

    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    stdout = capsys.readouterr().out
    (path,) = _findings_line(stdout)
    target_dir = out / f"booking-engine--{base}..{head}"
    assert path == target_dir / "findings.json"
    data = json.loads(path.read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert [f["status"] for f in data["findings"]] == ["confirmed"]
    assert f"summary: {target_dir / f'summary-booking-engine--{base}..{head}.md'}" in stdout.splitlines()
    for name in (f"review-booking-engine--{base}..{head}.md", f"summary-booking-engine--{base}..{head}.md",
                 "review-state.json", "progress.log", "diff.patch"):
        assert (target_dir / name).is_file(), name
    assert not (target_dir / "tree").exists()  # the snapshot is removed when done
    for rollup in ("HIGH-only.md", "MEDIUM-only.md", "LOW-only.md"):
        assert (out / rollup).is_file()
    progress = (target_dir / "progress.log").read_text()
    assert "STARTED" in progress and "DONE review finished: 1 HIGH" in progress
    assert ReviewState.model_validate_json((target_dir / "review-state.json").read_text()).stage == "done"
    assert "verify done: 2 confirmed, 0 refuted, 0 unverifiable" in stdout
    assert "merge done: 1 groups, 1 merged, 0 agent failures" in stdout
    assert data["findings"][0]["severity"] == "HIGH" and len(data["findings"]) == 1
    review = (target_dir / f"review-booking-engine--{base}..{head}.md").read_text()
    assert "**Reported by:** diff-bugs, callers" in review
    assert f"Merged into `{high_finding().id}`: the same defect as {high_finding().id}" in review
    assert "merge rateplan_service.py:1-1: 2 findings, 1 duplicates" in progress
    assert data["findings"][0]["expected_behaviour"] == "Rate plans match whatever the channel is titled."
    assert "**Expected behaviour:** Rate plans match whatever the channel is titled." in review


def test_resume_after_a_kill_following_verify(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    fake = agents(kill_at="merge")
    with pytest.raises(RuntimeError, match="killed"):
        main(_review_args(repo, base, head, out))
    target_dir = out / f"booking-engine--{base}..{head}"
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert state.stage == "verify" and not (target_dir / "findings.json").exists()
    first_run = list(fake.calls)
    capsys.readouterr()

    assert main(["--session-id", "sess-test", "resume", str(target_dir), "--trust-repo"]) == 0
    (path,) = _findings_line(capsys.readouterr().out)
    assert path == target_dir / "findings.json" and path.is_file()
    resumed = fake.calls[len(first_run):]
    assert not [c for c in resumed if c.startswith(("find", "verify"))]  # finished stages are not repeated
    assert resumed == ["merge"]
    assert "RESUMED after verify" in (target_dir / "progress.log").read_text()


def test_batch_reviews_every_target_and_writes_rollups(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    batch_file = tmp_path / "batch.yaml"
    batch_file.write_text(
        f"- repo: {repo}\n  range: {base}..{head}\n  tickets: [DB-2038]\n"
        f"- repo: {repo}\n  branch: feature/db-2038\n  deployed_in: main\n"
    )
    agents()
    assert main(["--out", str(out), "batch", str(batch_file), "--parallel", "2", "--trust-repo"]) == 0
    paths = _findings_line(capsys.readouterr().out)
    assert [p.parent.name for p in paths] == [f"booking-engine--{base}..{head}", "booking-engine--feature_db-2038"]
    high = (out / "HIGH-only.md").read_text()
    assert high.startswith("# HIGH findings — 2 across 2 of 2 targets")
    review = (out / "booking-engine--feature_db-2038" / "review-booking-engine--feature_db-2038.md").read_text()
    assert "- **Deployed in main:** no" in review


def test_untrusted_repo_exits_3_without_output(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    args = [a for a in _review_args(repo, base, head, out) if a != "--trust-repo"]
    assert main(args) == 3
    assert "--trust-repo" in capsys.readouterr().err
    assert not out.exists()


def test_failed_target_gate_exits_2_with_no_output_directory(fixture_repo, tmp_path, agents, capsys):
    repo, _, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, head, head, out)) == 2
    assert "empty" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_budget_breaker_stops_the_run_with_exit_4(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents(cost=6.0)
    assert main(_review_args(repo, base, head, out, "--max-cost-usd", "10")) == 4
    err = capsys.readouterr().err
    assert "circuit breaker stopped the run" in err
    target_dir = out / f"booking-engine--{base}..{head}"
    state = ReviewState.model_validate_json((target_dir / "review-state.json").read_text())
    assert state.budget["cost_usd"] >= 10 and not (target_dir / "findings.json").exists()
    assert "FAILED circuit breaker" in (target_dir / "progress.log").read_text()


def test_rollup_subcommand(fixture_repo, tmp_path, agents, capsys):
    repo, base, head = fixture_repo
    out = tmp_path / "out"
    agents()
    assert main(_review_args(repo, base, head, out)) == 0
    for p in out.glob("*-only.md"):
        p.unlink()
    capsys.readouterr()
    assert main(["--out", str(out), "rollup"]) == 0
    assert (out / "HIGH-only.md").is_file()
    assert capsys.readouterr().out.startswith("rollups: ")


def test_out_and_session_id_are_accepted_after_the_subcommand(fixture_repo, tmp_path, agents, capsys):
    """The plan's acceptance command passes --out after `review`."""
    repo, base, head = fixture_repo
    out = tmp_path / "late-out"
    agents()
    assert main(["review", "--repo", str(repo), "--range", f"{base}..{head}", "--trust-repo",
                 "--out", str(out), "--session-id", "sess-late"]) == 0
    (path,) = _findings_line(capsys.readouterr().out)
    assert path.parent.parent == out


def test_global_flags_are_not_overwritten_when_absent_after_the_subcommand():
    from review_pipeline.__main__ import build_parser

    args = build_parser().parse_args(["--out", "/x", "--session-id", "s", "rollup"])
    assert (args.out, args.session_id) == ("/x", "s")


def test_default_out_is_the_workspace_inbox_else_home_never_cwd(tmp_path, monkeypatch):
    from review_pipeline.__main__ import default_out

    cfg, home, ran_from = tmp_path / "cfg", tmp_path / "home", tmp_path / "repo"
    for d in (cfg, home, ran_from):
        d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(ran_from)
    assert default_out() == home / ".multiplai" / "reviews"

    ws = tmp_path / "ws"
    (ws / "INBOX").mkdir(parents=True)
    (cfg / ".workspace").write_text(str(ws))
    assert default_out() == ws / "INBOX" / "reviews"
