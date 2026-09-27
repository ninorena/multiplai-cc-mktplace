from __future__ import annotations

import json
import subprocess
from sys import executable as PYTHON

import jsonschema
import pytest
from pydantic import ValidationError

from conftest import FIXTURE
from review_viewer.models import SCHEMA_PATH, FindingsFile, finding_id, load_findings, schema_text


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_validates():
    ff = load_findings(FIXTURE)
    assert [f.severity for f in ff.findings] == ["HIGH", "MEDIUM", "LOW"]
    assert [f.status for f in ff.findings] == ["confirmed", "unverifiable", "refuted"]
    assert any(p.kind == "external" for p in ff.findings[0].fix.premises)


def test_fixture_ids_are_stable_hashes():
    for f in load_findings(FIXTURE).findings:
        assert f.id == finding_id(f.file, f.line_start, f.claim)


def test_empty_citations_rejected():
    data = fixture_data()
    data["findings"][0]["citations"] = []
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_unknown_top_level_key_rejected():
    data = fixture_data()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_wrong_schema_version_rejected():
    data = fixture_data()
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_committed_schema_matches_fresh_export():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text(), \
        "run: python -m review_viewer export-schema"


def test_fixture_validates_against_committed_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(fixture_data(), schema)


def test_committed_schema_rejects_empty_citations():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = fixture_data()
    data["findings"][1]["citations"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def _validate_cli(*paths) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, "-m", "review_viewer", "validate", *map(str, paths)],
                          capture_output=True, text=True)


def test_validate_cli_ok_and_broken(tmp_path):
    ok = _validate_cli(FIXTURE)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith(f"OK {FIXTURE} (3 findings)")
    broken = tmp_path / "broken.json"
    data = fixture_data()
    data["findings"][0]["citations"] = []
    broken.write_text(json.dumps(data), encoding="utf-8")
    bad = _validate_cli(broken)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout


# --- walkthrough.json v1 -------------------------------------------------------------

from review_viewer.models import (  # noqa: E402
    WALKTHROUGH_SCHEMA_PATH, InboxRow, Walkthrough, walkthrough_schema_text)

SHA = "a" * 40


def walkthrough_data() -> dict:
    return {
        "schema_version": 1, "generated_at": "2026-09-26T10:00:00Z", "base_sha": SHA,
        "head_sha": "b" * 40, "overview_md": "What this change is for.",
        "steps": [{"id": "core-change", "title": "The core change", "body_md": "Text.",
                   "anchors": [{"path": "app/service.py", "side": "head", "line_start": 1,
                                "line_end": 3}],
                   "diagram": {"kind": "mermaid", "source": "flowchart LR\n A --> B"},
                   "finding_ids": ["b561bd34ce"]}],
        "skipped": [{"path": "uv.lock", "reason": "generated"}], "complete": False,
    }


def test_committed_walkthrough_schema_matches_fresh_export():
    assert WALKTHROUGH_SCHEMA_PATH.read_text(encoding="utf-8") == walkthrough_schema_text(), \
        "run: python -m review_viewer export-schema"


def test_walkthrough_validates_against_model_and_committed_schema():
    Walkthrough.model_validate(walkthrough_data())
    schema = json.loads(WALKTHROUGH_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(walkthrough_data(), schema)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(surprise=True),
    lambda d: d["steps"][0].update(id="Has Spaces"),
    lambda d: d["steps"][0].update(anchors=[]),
    lambda d: d["steps"][0]["anchors"][0].update(side="left"),
    lambda d: d["steps"][0]["anchors"][0].update(line_start=0),
    lambda d: d["steps"][0].update(finding_ids=["XYZ"]),
    lambda d: d["steps"][0]["diagram"].update(kind="plantuml"),
    lambda d: d["steps"][0]["diagram"].update(source="x" * 20001),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(head_sha="short"),
])
def test_walkthrough_rejects(mutate):
    data = walkthrough_data()
    mutate(data)
    with pytest.raises(ValidationError):
        Walkthrough.model_validate(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, json.loads(WALKTHROUGH_SCHEMA_PATH.read_text(encoding="utf-8")))


def test_inbox_row_carries_an_optional_step_id():
    row = InboxRow(id="q-1", ts="t", target="t", kind="question", text="x", step_id="core-change")
    assert row.model_dump()["step_id"] == "core-change"
    assert InboxRow(id="q-1", ts="t", target="t", kind="question", text="x").step_id is None
    with pytest.raises(ValidationError):
        InboxRow(id="q-1", ts="t", target="t", kind="question", text="x", step_id="Bad Id")
