from __future__ import annotations

import json
import subprocess

import pytest

from conftest import CLAIM_HIGH, CLAIM_MEDIUM, CLAIM_REFUTED, CLAIM_REJECTED, EXPECTED_HIGH
from review_pipeline import post as post_mod
from review_pipeline.__main__ import main
from review_pipeline.export import write_findings_file
from review_pipeline.models import Merged
from review_pipeline.render import render_review, render_summary, write_review, write_rollups
from review_pipeline.state import save_state


def test_expected_behaviour_is_shown_and_no_fix_is(canned_state):
    text = render_review(canned_state)
    assert f"**Expected behaviour:** {EXPECTED_HIGH}" in text
    assert text.count("**Expected behaviour:**") == 2  # the confirmed and the unverifiable finding
    assert "**Fix:**" not in text and "Premises:" not in text


def test_merged_findings_are_listed_in_the_appendix_and_finders_on_the_survivor(canned_state):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"finders": ["diff-bugs", "callers"]})
    copy = high.with_location(claim="Rate plans match a literal keyword", finder="callers")
    canned_state.merged.append(Merged(finding=copy, into=high.id,
                                      reason=f"the same defect as {high.id} (rateplan_service.py:1): same literal"))
    text = render_review(canned_state)
    assert "**Reported by:** diff-bugs, callers" in text
    appendix = text.split("## Appendix — rejected, refuted and merged", 1)[1]
    assert "**merged** (HIGH) `rateplan_service.py:1-1` — Rate plans match a literal keyword" in appendix
    assert f"Merged into `{high.id}`: the same defect as {high.id}" in appendix
    assert "1 merged into another finding as duplicates." in render_summary(canned_state)


def test_header_and_sections(canned_state):
    text = render_review(canned_state)
    t = canned_state.target
    assert text.startswith(f"# Review — {t.label}")
    assert "- **Tickets:** DB-2038" in text
    assert f"- **Base commit:** [{t.base_sha[:10]}](https://github.com/example/booking-engine/commit/{t.base_sha})" in text
    assert "- **Files changed:** 3" in text
    assert f"### HIGH — rateplan_service.py:1 — {CLAIM_HIGH}" in text
    assert "unverifiable (lowered from MEDIUM)" in text


def test_citations_are_full_sha_github_links(canned_state):
    head = canned_state.target.head_sha
    text = render_review(canned_state)
    assert f"(https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L1-L1)" in text
    assert f"(https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L6-L6)" in text


def test_citations_without_github_remote_are_path_lines(canned_state):
    canned_state.target.remote_url = "https://gitlab.com/example/booking-engine.git"
    text = render_review(canned_state)
    assert "`rateplan_service.py:1`" in text and "github.com" not in text


def test_appendix_lists_rejected_and_refuted_with_reasons(canned_state):
    appendix = render_review(canned_state).split("## Appendix — rejected, refuted and merged", 1)[1]
    assert CLAIM_REFUTED in appendix and "line 5 sets the id" in appendix
    assert CLAIM_REJECTED in appendix and "quote not at cited lines" in appendix
    assert CLAIM_HIGH not in appendix


def test_summary_is_short_and_says_how_the_review_went(canned_state):
    text = render_summary(canned_state)
    lines = text.splitlines()
    assert len(lines) <= 25
    assert "Cost $1.25 over 9 agent calls." in text
    assert "Findings: 1 HIGH, 0 MEDIUM, 1 LOW. Dropped: 1 refuted" in text
    assert "1 refuted by the verifier, 1 rejected by the gates" in text
    (high_line,) = [l for l in lines if l.startswith("- `rateplan_service.py")]
    assert high_line.endswith("(confirmed)")
    assert "1 LOW findings are in the full review." in text
    dropped = text.split("## Dropped", 1)[1]
    assert "refuted:" in dropped and CLAIM_REFUTED.split(". ")[0][:40] in dropped
    assert "line 5 sets the id" not in dropped  # the verifier's reason stays in the full review
    assert "rejected by a gate:" in dropped and "quote not at cited lines" in dropped
    assert CLAIM_MEDIUM not in text  # lowered to LOW: counted, not listed
    assert lines[-1] == f"Full review, with every reason: `review-{canned_state.target.slug}.md`"


def test_summary_truncates_long_claims_and_lists_agent_failures(canned_state):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"claim": "x" * 500})
    canned_state.errors = ["verify: timeout on finding abc"]
    text = render_summary(canned_state)
    assert "x" * 119 + "…" in text and "x" * 121 not in text
    assert "Agent failures: verify: timeout on finding abc" in text


def test_write_review_also_writes_the_summary(canned_state, tmp_path):
    write_review(canned_state, tmp_path)
    assert (tmp_path / f"summary-{canned_state.target.slug}.md").read_text() == render_summary(canned_state)


def test_deployed_line(canned_state):
    canned_state.target.deployed_in = "staging"
    assert "- **Deployed in staging:** yes" in render_review(canned_state, deployed="yes")


def test_rollups_come_from_findings_json(canned_state, tmp_path):
    out = tmp_path / "out"
    first = out / canned_state.target.slug
    first.mkdir(parents=True)
    write_findings_file(canned_state, first)
    second_state = canned_state.model_copy(deep=True)
    second_state.target.slug, second_state.target.label = "other--x", "other x"
    (out / "other--x").mkdir()
    write_findings_file(second_state, out / "other--x")
    (first / "review-decoy.md").write_text("### HIGH — decoy.py:1 — scraped from markdown\n")

    written = write_rollups(out, [first / "findings.json", out / "other--x" / "findings.json"])
    assert [p.name for p in written] == ["HIGH-only.md", "MEDIUM-only.md", "LOW-only.md"]
    high = (out / "HIGH-only.md").read_text()
    assert high.startswith("# HIGH findings — 2 across 2 of 2 targets")
    assert high.index(canned_state.target.label) < high.index("other x")
    assert "decoy" not in high
    low = (out / "LOW-only.md").read_text()
    assert CLAIM_MEDIUM in low  # lowered to LOW, still shown
    assert CLAIM_REFUTED not in low  # refuted never reaches a rollup
    assert CLAIM_REJECTED not in (out / "MEDIUM-only.md").read_text()


# --- post ------------------------------------------------------------------------


@pytest.fixture
def pr_review(canned_state, tmp_path):
    canned_state.target.pr, canned_state.target.kind = 812, "pr"
    target_dir = tmp_path / canned_state.target.slug
    target_dir.mkdir(exist_ok=True)
    save_state(canned_state, target_dir)
    write_findings_file(canned_state, target_dir)
    return canned_state, target_dir


@pytest.fixture
def fake_gh(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(post_mod.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(post_mod.subprocess, "run", run)
    return calls


def test_post_with_decisions_selects_exactly_the_accepted(pr_review, fake_gh, capsys):
    state, target_dir = pr_review
    high = state.findings[0]
    medium = state.findings[1]
    decisions = target_dir / "viewer" / "decisions.json"
    decisions.parent.mkdir()
    decisions.write_text(json.dumps({
        high.id: {"decision": "accept", "note": "", "ts": "2026-09-25T10:00:00Z"},
        medium.id: {"decision": "reject", "note": "", "ts": "2026-09-25T10:01:00Z"},
    }))
    assert main(["post", str(target_dir), "--decisions", str(decisions)]) == 0
    (argv, body), = fake_gh
    assert argv[1:4] == ["pr", "comment", "812"]
    assert body.count("\n1. ") == 1 and "\n2. " not in body
    assert CLAIM_HIGH in body and CLAIM_MEDIUM not in body
    assert f"Expected behaviour: {EXPECTED_HIGH}" in body and "Suggested fix" not in body
    head = state.target.head_sha
    assert f"https://github.com/example/booking-engine/blob/{head}/rateplan_service.py#L1-L2" in body
    assert "posted 1 accepted findings to PR #812" in capsys.readouterr().out


def test_post_without_decisions_takes_high_and_medium(pr_review, fake_gh):
    state, target_dir = pr_review
    state.findings[1].severity = "MEDIUM"
    save_state(state, target_dir)
    write_findings_file(state, target_dir)
    assert main(["post", str(target_dir)]) == 0
    body = fake_gh[0][1]
    assert CLAIM_HIGH in body and CLAIM_MEDIUM in body and CLAIM_REFUTED not in body


def test_post_missing_decisions_file_exits_2(pr_review, fake_gh, capsys):
    _, target_dir = pr_review
    missing = target_dir / "viewer" / "decisions.json"
    assert main(["post", str(target_dir), "--decisions", str(missing)]) == 2
    assert str(missing) in capsys.readouterr().err
    assert fake_gh == []


def test_post_refuses_a_non_pr_target(canned_state, tmp_path, fake_gh, capsys):
    target_dir = tmp_path / canned_state.target.slug
    target_dir.mkdir(exist_ok=True)
    save_state(canned_state, target_dir)
    write_findings_file(canned_state, target_dir)
    assert main(["post", str(target_dir)]) == 2
    assert "needs a PR target" in capsys.readouterr().err
    assert fake_gh == []
