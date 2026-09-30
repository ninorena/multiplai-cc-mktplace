from __future__ import annotations

import json

import jsonschema

from conftest import CLAIM_HIGH, CLAIM_MEDIUM, CLAIM_REFUTED, CLAIM_REJECTED, EXPECTED_HIGH, EXPECTED_MEDIUM, SCHEMA
from review_pipeline.export import plugin_version, to_findings_file, write_findings_file


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_exported_fixture_validates_against_the_viewer_schema(canned_state, tmp_path):
    path = write_findings_file(canned_state, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, _schema())
    assert data["schema_version"] == 1


def test_status_mapping(canned_state):
    data = to_findings_file(canned_state)
    by_claim = {f["claim"]: f for f in data["findings"]}
    assert by_claim[CLAIM_HIGH]["status"] == "confirmed"
    assert by_claim[CLAIM_MEDIUM]["status"] == "unverifiable"
    assert by_claim[CLAIM_MEDIUM]["severity"] == "LOW"  # the lowered severity is what is kept
    assert by_claim[CLAIM_REFUTED]["status"] == "refuted"
    assert by_claim[CLAIM_REJECTED]["status"] == "rejected"
    assert "quote not at cited lines" in by_claim[CLAIM_REJECTED]["verdict_reason"]
    assert by_claim[CLAIM_REJECTED]["citations"]  # rejected findings keep their citations
    assert "fix" not in by_claim[CLAIM_HIGH]


def test_expected_behaviour_only_for_shown_findings(canned_state):
    by_claim = {f["claim"]: f for f in to_findings_file(canned_state)["findings"]}
    assert by_claim[CLAIM_HIGH]["expected_behaviour"] == EXPECTED_HIGH
    assert by_claim[CLAIM_MEDIUM]["expected_behaviour"] == EXPECTED_MEDIUM
    assert by_claim[CLAIM_REFUTED]["expected_behaviour"] is None
    assert by_claim[CLAIM_REJECTED]["expected_behaviour"] is None


def test_a_merged_away_finding_is_not_exported(canned_state):
    from review_pipeline.models import Merged

    copy = canned_state.findings[0].with_location(claim="the same defect, reworded")
    canned_state.merged.append(Merged(finding=copy, into=canned_state.findings[0].id, reason="r"))
    ids = [f["id"] for f in to_findings_file(canned_state)["findings"]]
    assert copy.id not in ids and canned_state.findings[0].id in ids


def test_target_and_producer(canned_state):
    data = to_findings_file(canned_state)
    t = data["target"]
    assert (t["base_sha"], t["head_sha"]) == (canned_state.target.base_sha, canned_state.target.head_sha)
    assert t["remote_url"] == "https://github.com/example/booking-engine.git"
    assert data["producer"] == f"multiplai-dev:review {plugin_version()}"
    assert plugin_version() != "unknown"
    assert data["generated_at"].endswith("Z")


def test_written_file_has_sorted_keys_and_two_space_indent(canned_state, tmp_path):
    text = write_findings_file(canned_state, tmp_path).read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_ids_are_unique(canned_state):
    ids = [f["id"] for f in to_findings_file(canned_state)["findings"]]
    assert len(ids) == len(set(ids)) == 4
