from __future__ import annotations

from conftest import DEF_CITATION, KEYWORD_CITATION, cite, high_finding
from review_pipeline import gates
from review_pipeline.models import Citation, Verdict


# --- the DB2038 regression test (plan item 9) -----------------------------------


def test_regression_citation_moved_by_one_line_fails(target_info):
    assert gates.citation_gate(target_info, DEF_CITATION).passed
    moved = DEF_CITATION.model_copy(update={"line_start": DEF_CITATION.line_start + 1,
                                            "line_end": DEF_CITATION.line_end + 1})
    result = gates.citation_gate(target_info, moved)
    assert not result.passed and result.reason == "quote not at cited lines"


# --- citation_gate ---------------------------------------------------------------


def test_citation_whitespace_is_normalised(target_info):
    c = Citation(path="rateplan_service.py", line_start=4, line_end=6,
                 quote="def eligible_rate_plans(rate_plans):\n      \"\"\"Rate plans   the direct-booking modal may offer.\"\"\"")
    assert gates.citation_gate(target_info, c).passed


def test_citation_path_not_at_head(target_info):
    result = gates.citation_gate(target_info, cite("nope.py", 1, "x"))
    assert result.reason == "path not at head"


def test_citation_empty_quote_fails(target_info):
    assert gates.citation_gate(target_info, cite("settings.py", 1, "   ")).reason == "empty quote"


def test_citation_past_end_of_file_fails(target_info):
    assert not gates.citation_gate(target_info, cite("settings.py", 40, "CHANNEX")).passed


# --- finding_gate ----------------------------------------------------------------


def test_finding_gate_passes_a_grounded_finding(target_info):
    assert gates.finding_gate(target_info, high_finding()).passed


def test_finding_gate_rejects_a_bad_citation(target_info):
    f = high_finding().with_location(citations=[cite("rateplan_service.py", 2, "KEYWORD = 'dolcebot'").model_dump()])
    result = gates.finding_gate(target_info, f)
    assert not result.passed and "quote not at cited lines" in result.reason


def test_finding_gate_requires_a_changed_file_unless_pre_existing(target_info):
    f = high_finding().with_location(file="README.md", citations=[cite("README.md", 1, "# Booking engine").model_dump()])
    assert "not a changed file" in gates.finding_gate(target_info, f).reason
    assert gates.finding_gate(target_info, f.with_location(dimension="pre-existing")).passed


# --- verdict_gate ----------------------------------------------------------------


def test_verdict_gate(target_info):
    ok = Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION])
    assert gates.verdict_gate(target_info, ok).passed
    bare = Verdict(status="confirmed", reason="r")
    assert gates.verdict_gate(target_info, bare).action == "downgrade"
    wrong = Verdict(status="confirmed", reason="r", citations=[cite("settings.py", 1, "nothing like this")])
    assert not gates.verdict_gate(target_info, wrong).passed
    assert gates.verdict_gate(target_info, Verdict(status="refuted", reason="r")).passed


def test_no_gate_name_starts_with_test():
    assert not [n for n in dir(gates) if n.startswith("test")]



# --- web citations ---------------------------------------------------------------

WEB = Citation(path="https://example.com/docs#older_than", line_start=1, line_end=1, quote="a duration")


def test_finding_gate_allows_a_web_citation_after_a_repo_one(target_info):
    f = high_finding()
    repo_citation = f.citations[0].model_dump()
    assert gates.finding_gate(target_info, f.with_location(citations=[repo_citation, WEB.model_dump()])).passed


def test_finding_gate_rejects_a_web_citation_first_or_alone(target_info):
    f = high_finding()
    repo_citation = f.citations[0].model_dump()
    alone = gates.finding_gate(target_info, f.with_location(citations=[WEB.model_dump()]))
    assert not alone.passed and "web source" in alone.reason
    first = gates.finding_gate(target_info, f.with_location(citations=[WEB.model_dump(), repo_citation]))
    assert not first.passed and "web source" in first.reason


def test_verdict_gate_does_not_confirm_on_a_web_citation_alone(target_info):
    web_only = Verdict(status="confirmed", reason="r", citations=[WEB])
    result = gates.verdict_gate(target_info, web_only)
    assert result.action == "downgrade" and "web source" in result.reason
    assert gates.verdict_gate(target_info, Verdict(status="confirmed", reason="r", citations=[WEB, KEYWORD_CITATION])).passed
