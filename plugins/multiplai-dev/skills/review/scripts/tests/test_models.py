from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from conftest import TESTS
from review_pipeline.models import Citation, Finding, ReviewState, finding_id, lower_severity

VIEWER_FIXTURE = TESTS.parents[2] / "review-viewer" / "scripts" / "tests" / "fixtures" / "findings.example.json"


def _finding(**overrides) -> dict:
    data = {"claim": "x", "severity": "HIGH", "file": "a.py", "line_start": 3, "line_end": 3,
            "failure_scenario": "boom", "citations": [{"path": "a.py", "line_start": 3, "line_end": 3, "quote": "q"}]}
    data.update(overrides)
    return data


def test_empty_citations_is_a_validation_error():
    with pytest.raises(ValidationError):
        Finding(**_finding(citations=[]))


def test_missing_citations_is_a_validation_error():
    data = _finding()
    del data["citations"]
    with pytest.raises(ValidationError):
        Finding.model_validate(data)


def test_finding_id_is_the_v1_rule():
    f = Finding(**_finding())
    assert f.id == hashlib.sha1(b"a.py\x003\x00x").hexdigest()[:10]
    assert finding_id("a.py", 3, "x") == f.id


def test_finding_id_from_the_model_is_overwritten():
    f = Finding.model_validate(_finding(id="deadbeef00"))
    assert f.id == finding_id("a.py", 3, "x")


def test_finding_id_matches_the_viewer_for_its_fixture():
    """Pins this reimplementation to review-viewer's: every id in the viewer's
    own fixture (computed by its finding_id) is reproduced here."""
    data = json.loads(VIEWER_FIXTURE.read_text(encoding="utf-8"))
    assert data["findings"]
    for f in data["findings"]:
        assert finding_id(f["file"], f["line_start"], f["claim"]) == f["id"]


def test_with_location_recomputes_the_id():
    f = Finding(**_finding())
    moved = f.with_location(file="b.py")
    assert moved.id == finding_id("b.py", 3, "x") != f.id


def test_severity_is_a_closed_enum():
    with pytest.raises(ValidationError):
        Finding(**_finding(severity="CRITICAL"))


def test_citation_line_order_and_minimum():
    with pytest.raises(ValidationError):
        Citation(path="a", line_start=5, line_end=4, quote="q")
    with pytest.raises(ValidationError):
        Citation(path="a", line_start=0, line_end=1, quote="q")


def test_lower_severity():
    assert [lower_severity(s) for s in ("HIGH", "MEDIUM", "LOW")] == ["MEDIUM", "LOW", "LOW"]


def test_state_round_trips_and_orders_stages(canned_state):
    again = ReviewState.model_validate_json(canned_state.model_dump_json())
    assert again == canned_state
    assert again.past("verify") and again.past("merge") and not again.past("export")


def test_a_checkpoint_from_before_the_fix_stages_were_removed_resumes_at_merge(canned_state):
    data = canned_state.model_dump(mode="json")
    data.update(stage="check_fix", fixes={}, fix_checks={})
    old = ReviewState.model_validate(data)
    assert old.stage == "verify" and not old.past("merge")
