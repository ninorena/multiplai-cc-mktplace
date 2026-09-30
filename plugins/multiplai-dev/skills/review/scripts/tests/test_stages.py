"""Each stage with `agent_call_structured` replaced by canned answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import KEYWORD_CITATION, KEYWORD_USE_CITATION, cite, high_finding, medium_finding
from review_pipeline import sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.models import DuplicateSet, Finding, FinderOutput, FinderResult, MergeOutput, ReviewState, Verdict
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import conventions_chain, run_find
from review_pipeline.stages.merge import MERGE_LINE_GAP, group_key, overlap_groups, run_merge
from review_pipeline.stages.verify import run_verify


class Canned:
    """Stands in for sdk.agent_call_structured. `answers[label]` is a list
    consumed in order; an Exception in it is raised."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt))
        assert kwargs["allowed_tools"] == ["Read", "Grep", "Glob"]
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer.model_dump()) if hasattr(answer, "model_dump") else answer


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(concurrency=2, dimensions=("diff-bugs", "callers")),
                      snapshot=snapshot, diff=Path(target_info.diff_path).read_text())


def use(monkeypatch, answers) -> Canned:
    canned = Canned(answers)
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return canned


# --- find ------------------------------------------------------------------------


async def test_find_dedupes_gates_and_normalises_paths(target_info, ctx, monkeypatch):
    high = high_finding()
    absolute = high.with_location(file=str(ctx.snapshot / "rateplan_service.py"),
                                  citations=[c.model_copy(update={"path": f"./{c.path}"}).model_dump()
                                             for c in high.citations])
    bad = medium_finding().with_location(citations=[cite("rateplan_service.py", 2, "KEYWORD = 'dolcebot'").model_dump()])
    canned = use(monkeypatch, {
        "find:diff-bugs": [FinderOutput(findings=[absolute, bad])],
        "find:callers": [FinderOutput(findings=[high])],  # duplicate of the first
    })
    state = await run_find(ReviewState(target=target_info), ctx)
    assert [f.id for f in state.findings] == [high.id]
    assert state.findings[0].file == "rateplan_service.py"
    assert state.findings[0].citations[0].path == "rateplan_service.py"
    assert state.findings[0].finder == "diff-bugs"
    assert state.findings[0].finders == ["diff-bugs", "callers"]  # the copy's finder is kept
    assert len(state.rejected) == 1 and "quote not at cited lines" in state.rejected[0].reason
    assert state.stage == "find" and len(canned.calls) == 2


async def test_find_survives_one_failed_finder_but_not_all(target_info, ctx, monkeypatch):
    use(monkeypatch, {"find:diff-bugs": [sdk.AgentCallError("x")], "find:callers": [FinderOutput(findings=[])]})
    state = await run_find(ReviewState(target=target_info), ctx)
    assert state.stage == "find" and state.errors and "finder diff-bugs" in state.errors[0]

    use(monkeypatch, {"find:diff-bugs": [sdk.AgentCallError("x")], "find:callers": [sdk.AgentCallError("y")]})
    with pytest.raises(sdk.AgentCallError):
        await run_find(ReviewState(target=target_info), ctx)


async def test_trust_error_escapes(target_info, ctx, monkeypatch):
    use(monkeypatch, {"find:diff-bugs": [sdk.RepoTrustError("no")], "find:callers": [FinderOutput()]})
    with pytest.raises(sdk.RepoTrustError):
        await run_find(ReviewState(target=target_info), ctx)


async def test_a_budget_stop_during_find_keeps_finished_finders_and_resume_runs_the_rest(
        target_info, ctx, monkeypatch):
    from review_pipeline import budget

    ctx.config.concurrency = 1
    state = ReviewState(target=target_info)
    use(monkeypatch, {"find:diff-bugs": [FinderOutput(findings=[high_finding()])],
                      "find:callers": [budget.BudgetExceededError("stop")]})
    with pytest.raises(budget.BudgetExceededError):
        await run_find(state, ctx)
    assert state.stage == "target" and state.findings == []
    assert list(state.finder_results) == ["diff-bugs"]
    assert [f.id for f in state.finder_results["diff-bugs"].findings] == [high_finding().id]

    canned = use(monkeypatch, {"find:callers": [FinderOutput(findings=[high_finding()])]})
    state = await run_find(state, ctx)
    assert [label for label, _ in canned.calls] == ["find:callers"]
    assert [f.id for f in state.findings] == [high_finding().id]
    assert state.findings[0].finders == ["diff-bugs", "callers"]
    assert state.finder_results["diff-bugs"].findings[0].finders == ["diff-bugs"]


async def test_a_failed_finder_is_not_asked_again_on_resume_unless_every_finder_failed(
        target_info, ctx, monkeypatch):
    state = ReviewState(target=target_info, finder_results={
        "diff-bugs": FinderResult(error="finder diff-bugs: bad answer")})
    canned = use(monkeypatch, {"find:callers": [FinderOutput()]})
    state = await run_find(state, ctx)
    assert [label for label, _ in canned.calls] == ["find:callers"]
    assert state.errors == ["finder diff-bugs: bad answer"]

    state = ReviewState(target=target_info, finder_results={
        "diff-bugs": FinderResult(error="finder diff-bugs: bad answer")})
    use(monkeypatch, {"find:callers": [sdk.AgentCallError("y")]})
    with pytest.raises(sdk.AgentCallError, match="every finder failed"):
        await run_find(state, ctx)
    assert state.finder_results == {}  # the next run asks every finder again


async def test_stage_already_done_makes_no_calls(target_info, ctx, monkeypatch):
    canned = use(monkeypatch, {})
    state = ReviewState(target=target_info, stage="merge")
    for fn in (run_find, run_verify, run_merge):
        assert await fn(state, ctx) is state
    assert canned.calls == []


def test_conventions_chain_reads_claude_md_at_head(target_info):
    assert conventions_chain(target_info) == ""  # the fixture has none


# --- verify ----------------------------------------------------------------------


async def test_verify_downgrades_uncited_confirmation_and_keeps_refuted(target_info, ctx, monkeypatch):
    high, medium = high_finding(), medium_finding()
    use(monkeypatch, {"verify": [
        Verdict(status="confirmed", reason="looks right"),  # no citation -> unverifiable
        Verdict(status="refuted", reason="line 6 handles it", citations=[KEYWORD_USE_CITATION]),
    ]})
    ctx.config.concurrency = 1  # answers are consumed in finding order
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx)
    assert state.verdicts[high.id].status == "unverifiable"
    assert "without citing" in state.verdicts[high.id].reason
    assert state.findings[0].severity == "MEDIUM" and state.original_severity[high.id] == "HIGH"
    assert state.verdicts[medium.id].status == "refuted"
    # The canned unverifiable verdict has no expected behaviour, so it is counted.
    assert ctx.counts == {"confirmed": 0, "refuted": 1, "unverifiable": 1, "no_expected_behaviour": 1}


async def test_verify_keeps_a_grounded_confirmation_and_its_expected_behaviour(target_info, ctx, monkeypatch):
    high = high_finding()
    canned = use(monkeypatch, {"verify": [Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION],
                                                  expected_behaviour="Rate plans match the channel's title.")]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    assert state.verdicts[high.id].status == "confirmed" and state.findings[0].severity == "HIGH"
    assert state.verdicts[high.id].expected_behaviour == "Rate plans match the channel's title."
    assert "without proposing a code change" in canned.calls[0][1]
    assert state.errors == []


async def test_verify_lists_findings_that_carry_no_expected_behaviour(target_info, ctx, monkeypatch):
    high = high_finding()
    use(monkeypatch, {"verify": [Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION])]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    # The confirmation stands; the gap is reported, not hidden.
    assert state.verdicts[high.id].status == "confirmed"
    assert ctx.counts["no_expected_behaviour"] == 1
    assert state.errors == [f"verify: no expected behaviour for 1 finding ({high.id})"]


async def test_verifier_failure_is_unverifiable(target_info, ctx, monkeypatch):
    high = high_finding()
    use(monkeypatch, {"verify": [sdk.AgentCallError("timeout")]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    assert state.verdicts[high.id].status == "unverifiable"


# --- merge -----------------------------------------------------------------------


def _at(claim: str, severity: str, start: int, end: int, finder: str, *citations,
        file: str = "rateplan_service.py") -> Finding:
    return Finding(claim=claim, severity=severity, dimension=finder, file=file, line_start=start,
                   line_end=end, failure_scenario="s", citations=list(citations) or [KEYWORD_CITATION],
                   finder=finder, finders=[finder])


def _verdicts(**status_by_id) -> dict[str, Verdict]:
    return {fid: Verdict(finding_id=fid, status=status, reason="r") for fid, status in status_by_id.items()}


def test_overlap_groups_chain_overlaps_and_near_touches_in_one_file(target_info):
    a = _at("a", "LOW", 1, 1, "diff-bugs")
    b = _at("b", "LOW", 1 + MERGE_LINE_GAP, 3, "callers")      # within the gap of a
    c = _at("c", "LOW", 10, 12, "diff-bugs")
    d = _at("d", "LOW", 11, 11, "tests")                        # inside c
    far = _at("far", "LOW", 12 + MERGE_LINE_GAP + 1, 16, "history")  # one line past the gap
    other_file = _at("o", "LOW", 1, 1, "callers", file="settings.py")
    refuted = _at("r", "LOW", 1, 1, "history")
    findings = [a, b, c, d, far, other_file, refuted]
    state = ReviewState(target=target_info, findings=findings, verdicts={
        **_verdicts(**{f.id: "confirmed" for f in findings}), **_verdicts(**{refuted.id: "refuted"})})
    assert [[f.claim for f in g] for g in overlap_groups(state)] == [["a", "b"], ["c", "d"]]


def _merge_state(target_info) -> tuple[ReviewState, Finding, Finding, Finding]:
    """Three overlapping findings: a confirmed LOW, an unverifiable one lowered from HIGH, and a third."""
    keep = _at("the keyword is hardcoded", "LOW", 1, 1, "diff-bugs", KEYWORD_CITATION)
    copy = _at("rate plans are matched on a literal 'dolcebot'", "MEDIUM", 1, 6, "callers",
               KEYWORD_CITATION, KEYWORD_USE_CITATION)
    other = _at("the match is case-sensitive", "LOW", 6, 6, "tests", KEYWORD_USE_CITATION)
    state = ReviewState(target=target_info, stage="verify", findings=[keep, copy, other],
                        verdicts=_verdicts(**{keep.id: "confirmed", copy.id: "unverifiable",
                                              other.id: "confirmed"}),
                        original_severity={copy.id: "HIGH"})
    return state, keep, copy, other


async def test_merge_keeps_highest_severity_and_every_citation_and_records_the_copy(target_info, ctx, monkeypatch):
    state, keep, copy, other = _merge_state(target_info)
    canned = use(monkeypatch, {"merge": [MergeOutput(duplicate_sets=[
        DuplicateSet(finding_ids=[copy.id, keep.id], reason="both say the keyword is a literal")])]})
    state = await run_merge(state, ctx)

    (label, prompt_text), = canned.calls
    assert label == "merge" and all(f.id in prompt_text for f in (keep, copy, other))
    assert [f.id for f in state.findings] == [keep.id, other.id]
    merged = state.findings[0]
    assert merged.claim == keep.claim  # the confirmed finding survives, id and verdict unchanged
    assert merged.severity == "MEDIUM"  # the highest current severity in the set
    assert merged.citations == [KEYWORD_CITATION, KEYWORD_USE_CITATION]
    assert merged.finders == ["diff-bugs", "callers"]
    (record,) = state.merged
    assert record.finding.id == copy.id and record.into == keep.id
    assert keep.id in record.reason and "both say the keyword is a literal" in record.reason
    assert ctx.counts == {"groups": 1, "merged": 1, "agent_failures": 0}
    assert state.stage == "merge" and group_key([keep, copy, other]) in state.merge_answers


async def test_merge_of_unverifiable_findings_keeps_the_highest_original_severity(target_info, ctx, monkeypatch):
    state, keep, copy, other = _merge_state(target_info)
    state.verdicts[keep.id].status = "unverifiable"
    state.original_severity[keep.id] = "MEDIUM"
    use(monkeypatch, {"merge": [MergeOutput(duplicate_sets=[DuplicateSet(finding_ids=[keep.id, copy.id])])]})
    state = await run_merge(state, ctx)
    survivor = state.findings[0]
    assert survivor.id == copy.id and survivor.severity == "MEDIUM"
    assert state.original_severity[copy.id] == "HIGH"
    assert state.merged[0].into == copy.id


async def test_merge_agent_failure_leaves_the_group_unmerged(target_info, ctx, monkeypatch):
    state, keep, copy, other = _merge_state(target_info)
    before = [f.model_copy() for f in state.findings]
    use(monkeypatch, {"merge": [sdk.AgentCallError("timeout\ntrace")]})
    state = await run_merge(state, ctx)
    assert state.findings == before and state.merged == []
    assert state.errors == ["merge rateplan_service.py:1-6: timeout"]
    assert ctx.counts["agent_failures"] == 1 and state.stage == "merge"


async def test_merge_ignores_ids_outside_the_group_and_ids_used_twice(target_info, ctx, monkeypatch):
    state, keep, copy, other = _merge_state(target_info)
    use(monkeypatch, {"merge": [MergeOutput(duplicate_sets=[
        DuplicateSet(finding_ids=[keep.id, "0123456789"]),  # one real id: not a set
        DuplicateSet(finding_ids=[keep.id, copy.id]),
        DuplicateSet(finding_ids=[copy.id, other.id]),  # copy is already used
    ])]})
    state = await run_merge(state, ctx)
    assert [f.id for f in state.findings] == [keep.id, other.id]
    assert [m.finding.id for m in state.merged] == [copy.id]


async def test_merge_resume_reuses_stored_answers_and_a_budget_stop_keeps_the_findings(target_info, ctx, monkeypatch):
    from review_pipeline import budget

    state, keep, copy, other = _merge_state(target_info)
    use(monkeypatch, {"merge": [budget.BudgetExceededError("stop")]})
    with pytest.raises(budget.BudgetExceededError):
        await run_merge(state, ctx)
    assert state.stage == "verify" and len(state.findings) == 3 and state.merge_answers == {}

    state.merge_answers[group_key(state.findings)] = [DuplicateSet(finding_ids=[keep.id, copy.id])]
    canned = use(monkeypatch, {"merge": []})
    state = await run_merge(state, ctx)
    assert canned.calls == [] and [f.id for f in state.findings] == [keep.id, other.id]


async def test_a_budget_stop_keeps_the_verdicts_already_paid_for(target_info, ctx, monkeypatch):
    from review_pipeline import budget

    high, medium = high_finding(), medium_finding()
    state = ReviewState(target=target_info, stage="find", findings=[high, medium])
    ctx.config.concurrency = 1
    use(monkeypatch, {"verify": [Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION]),
                                 budget.BudgetExceededError("stop")]})
    with pytest.raises(budget.BudgetExceededError):
        await run_verify(state, ctx)
    assert list(state.verdicts) == [high.id] and state.stage == "find"
