from __future__ import annotations

import copy
import json
import subprocess
from sys import executable as PYTHON

import pytest

from review_viewer import walkthrough
from review_viewer.mailbox import read_rows
from review_viewer.models import Walkthrough, load_findings

HIGH, MEDIUM, REFUTED = "b561bd34ce", "7ca6553fd7", "cebb020bdb"


def _step(sid, path="app/service.py", start=1, end=3, side="head", findings=(), diagram=None):
    return {"id": sid, "title": sid.replace("-", " "), "body_md": f"About {sid}.",
            "anchors": [{"path": path, "side": side, "line_start": start, "line_end": end}],
            "diagram": diagram, "finding_ids": list(findings)}


@pytest.fixture
def ff(findings_path):
    return load_findings(findings_path)


@pytest.fixture
def complete(ff) -> dict:
    """A walkthrough that covers every changed file and links every finding
    that must be linked."""
    t = ff.target
    return {
        "schema_version": 1, "generated_at": "2026-09-26T10:00:00Z",
        "base_sha": t.base_sha, "head_sha": t.head_sha,
        "overview_md": "Adds quantities to totals, caps discounts, logs refunds.",
        "steps": [
            _step("totals", start=7, end=9, findings=[HIGH]),
            _step("refund-logging", start=17, end=20, findings=[MEDIUM],
                  diagram={"kind": "mermaid", "source": "flowchart LR\n A --> B"}),
            _step("loyalty", path="app/new_feature.py", start=1, end=5),
            _step("legacy-removed", path="app/old_module.py", side="base", start=1, end=150),
            _step("big-value", path="app/big.py", start=2000, end=2000),
        ],
        "skipped": [{"path": "assets/logo.bin", "reason": "binary logo swap"}],
        "assessments": [
            {"topic": "commits", "verdict": "good", "title": "Messages explain it",
             "detail_md": "Each commit says why."},
            {"topic": "tests", "verdict": "concern", "title": "Refunds untested",
             "detail_md": "`refund()` has no test."},
        ],
        "risk": {"tier": 3, "tier_why": "refunds move money", "revertable": True,
                 "revert_why": "no migration or external call"},
        "complete": True,
    }


def errors_for(data, ff) -> list[str]:
    return walkthrough.check(Walkthrough.model_validate(data), ff)


def test_complete_walkthrough_passes(complete, ff):
    assert errors_for(complete, ff) == []
    assert walkthrough.coverage(Walkthrough.model_validate(complete), ff) == ([], [])


def test_rule1_shas_must_match_the_served_target(complete, ff):
    complete["head_sha"] = "0" * 40
    errs = errors_for(complete, ff)
    assert len(errs) == 1 and "not the served target's" in errs[0]


def test_rule2_anchor_path_must_be_changed(complete, ff):
    complete["steps"][0]["anchors"][0]["path"] = "setup.py"
    errs = errors_for(complete, ff)
    assert any(e.startswith("step totals:") and "setup.py is not a changed file" in e
               for e in errs), errs


def test_rule2_anchor_range_must_exist_at_its_side(complete, ff):
    complete["steps"][0]["anchors"][0]["line_end"] = 22  # service.py has 21 lines at head
    complete["steps"][3]["anchors"][0]["side"] = "head"  # old_module.py is gone at head
    complete["steps"][2]["anchors"][0]["side"] = "base"  # new_feature.py is new at head
    errs = errors_for(complete, ff)
    assert any(e.startswith("step totals:") and "has 21 lines at head" in e for e in errs), errs
    assert any(e.startswith("step legacy-removed:") and "does not exist at head" in e
               for e in errs), errs
    assert any(e.startswith("step loyalty:") and "does not exist at base" in e
               for e in errs), errs
    assert len(errs) == 3


def test_rule2_binary_file_cannot_be_anchored(complete, ff):
    complete["steps"][2]["anchors"].append(
        {"path": "assets/logo.bin", "side": "head", "line_start": 1, "line_end": 1})
    errs = errors_for(complete, ff)
    assert any("step loyalty:" in e and "binary" in e for e in errs), errs


def test_rule3_finding_ids_must_exist(complete, ff):
    complete["steps"][1]["finding_ids"].append("0123456789")
    errs = errors_for(complete, ff)
    assert errs == ["step refund-logging: finding 0123456789 is not in the loaded findings"]


def test_rule4_complete_needs_every_file_and_finding(complete, ff):
    complete["skipped"] = []
    complete["steps"][0]["finding_ids"] = []
    errs = errors_for(complete, ff)
    assert any("assets/logo.bin has no anchor" in e for e in errs), errs
    assert any(f"finding {HIGH} is not linked" in e for e in errs), errs
    # A refuted finding never has to be linked.
    assert not any(REFUTED in e for e in errs)
    complete["complete"] = False
    assert errors_for(complete, ff) == []


def test_step_text_must_not_restate_a_finding(complete, ff):
    complete["steps"][0]["body_md"] = f"**Finding {HIGH} (HIGH):** totals drop the quantity."
    assert errors_for(complete, ff) == [
        f"step totals: body_md names finding {HIGH}; the page shows linked findings under "
        "the step, so explain the code instead"]


def test_rule5_step_ids_are_unique(complete, ff):
    complete["steps"][1]["id"] = "totals"
    errs = errors_for(complete, ff)
    assert errs == ["step totals: the id is used by more than one step"]


def test_rule6_complete_needs_commits_and_tests_assessments(complete, ff):
    complete["assessments"] = [a for a in complete["assessments"] if a["topic"] != "tests"]
    assert errors_for(complete, ff) == [
        "walkthrough: complete is true but there is no 'tests' assessment"]
    complete["complete"] = False
    assert errors_for(complete, ff) == []


def test_rule6_one_assessment_per_topic_except_other(complete, ff):
    extra = {"topic": "other", "verdict": "note", "title": "x", "detail_md": "y"}
    complete["assessments"] += [extra, dict(extra)]
    assert errors_for(complete, ff) == []
    complete["assessments"].append(dict(complete["assessments"][0]))
    assert errors_for(complete, ff) == [
        "assessment commits: more than one assessment has this topic"]


def test_complete_needs_a_risk_block(complete, ff):
    del complete["risk"]
    assert errors_for(complete, ff) == ["walkthrough: complete is true but there is no 'risk' block"]
    complete["complete"] = False
    assert errors_for(complete, ff) == []


def test_risk_tier_is_0_to_3(complete):
    complete["risk"]["tier"] = 4
    with pytest.raises(ValueError):
        Walkthrough.model_validate(complete)


def test_size_and_risk_topics_load_but_put_rejects_them(complete, ff):
    complete["assessments"] += [
        {"topic": "size", "verdict": "good", "title": "Small", "detail_md": "x"},
        {"topic": "risk", "verdict": "note", "title": "Some", "detail_md": "x"}]
    assert errors_for(complete, ff) == [
        "assessment size: not an assessment topic any more (size is measured from git)",
        "assessment risk: not an assessment topic any more (risk is scored from the `risk` block)"]


def test_assessment_title_fits_one_line(complete, ff):
    complete["assessments"][0]["title"] = "PR body is thorough; neither commit has a body"
    assert errors_for(complete, ff) == [
        "assessment commits: title is 46 characters, over 32; put the rest in detail_md"]
    # A file written before 0.22 with a longer title still loads.
    Walkthrough.model_validate(complete)


def test_the_schema_advertises_the_title_limit_put_enforces():
    schema = Walkthrough.model_json_schema()
    assert schema["$defs"]["Assessment"]["properties"]["title"]["maxLength"] == walkthrough.TITLE_MAX


def test_assessment_verdicts_and_topics_are_closed_sets(complete):
    bad = copy.deepcopy(complete)
    bad["assessments"][0]["verdict"] = "great"
    with pytest.raises(ValueError):
        Walkthrough.model_validate(bad)
    bad = copy.deepcopy(complete)
    bad["assessments"][0]["topic"] = "vibes"
    with pytest.raises(ValueError):
        Walkthrough.model_validate(bad)


def test_skipped_path_must_be_changed(complete, ff):
    complete["skipped"].append({"path": "nope.txt", "reason": "x"})
    assert errors_for(complete, ff) == ["skipped nope.txt: not a changed file in this diff"]


# --- CLI and route ------------------------------------------------------------------------


def _cli(*args, cwd=None):
    return subprocess.run([PYTHON, "-m", "review_viewer", *map(str, args)],
                          capture_output=True, text=True, cwd=cwd)


def test_put_publishes_and_the_route_serves_it(start_live, complete, tmp_path):
    live = start_live()
    route = f"/api/targets/{live.slug}/walkthrough"
    assert live.request("GET", route)[0] == 404
    assert live.request("GET", route, token=None)[0] == 401
    assert live.request("GET", route, token="wrong")[0] == 401

    early = copy.deepcopy(complete)
    early["steps"], early["skipped"], early["complete"] = early["steps"][:1], [], False
    src = tmp_path / "wt.json"
    src.write_text(json.dumps(early))
    proc = _cli("walkthrough", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 0, proc.stderr
    assert "1 steps, in progress" in proc.stdout
    status, body = live.request("GET", route)
    assert status == 200 and [s["id"] for s in body["steps"]] == ["totals"]
    assert body["complete"] is False

    status_out = _cli("walkthrough", "status", "--box", live.box)
    assert status_out.returncode == 0
    assert f"head_sha: {live.viewer.targets[live.slug].findings.target.head_sha}\n" in status_out.stdout
    assert "  assets/logo.bin" in status_out.stdout and f"  {MEDIUM} " in status_out.stdout

    src.write_text(json.dumps(complete))
    assert _cli("walkthrough", "put", "--box", live.box, "--file", src).returncode == 0
    status, body = live.request("GET", route)
    assert status == 200 and body["complete"] is True and len(body["steps"]) == 5
    out = walkthrough.walkthrough_path(live.box)
    assert out == live.findings.parent / "walkthrough.json"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert "(0)" in _cli("walkthrough", "status", "--box", live.box).stdout


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["steps"][0]["anchors"][0].update(path="setup.py"),
     "step totals: anchor setup.py"),
    (lambda d: d["steps"][0]["anchors"][0].update(line_end=999),
     "step totals: anchor app/service.py:7-999 (head): app/service.py has 21 lines"),
    (lambda d: d["steps"][1]["finding_ids"].append("0123456789"),
     "step refund-logging: finding 0123456789"),
    (lambda d: d.update(skipped=[]),
     "changed file assets/logo.bin has no anchor"),
])
def test_put_rejects_with_exit_2_and_publishes_nothing(start_live, complete, tmp_path,
                                                       mutate, needle):
    live = start_live()
    mutate(complete)
    src = tmp_path / "wt.json"
    src.write_text(json.dumps(complete))
    proc = _cli("walkthrough", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 2
    assert needle in proc.stderr, proc.stderr
    assert not walkthrough.walkthrough_path(live.box).exists()
    assert live.request("GET", f"/api/targets/{live.slug}/walkthrough")[0] == 404


def test_put_rejects_invalid_json_shape(start_live, tmp_path):
    live = start_live()
    src = tmp_path / "wt.json"
    src.write_text('{"schema_version": 1}')
    proc = _cli("walkthrough", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 2 and "not a valid walkthrough.json v1" in proc.stderr


def test_put_without_a_served_target(tmp_path):
    proc = _cli("walkthrough", "put", "--box", tmp_path, "--file", tmp_path / "x.json")
    assert proc.returncode == 2 and "no served target" in proc.stderr


def test_question_about_a_step(start_live, complete, tmp_path):
    live = start_live()
    body = {"target": live.slug, "text": "why here?", "step_id": "totals"}
    assert live.request("POST", "/api/ask", body)[0] == 404  # no walkthrough yet
    src = tmp_path / "wt.json"
    src.write_text(json.dumps(complete))
    assert _cli("walkthrough", "put", "--box", live.box, "--file", src).returncode == 0
    assert live.request("POST", "/api/ask", body)[0] == 200
    assert read_rows(live.box / "inbox.jsonl")[-1]["step_id"] == "totals"
    assert live.request("POST", "/api/ask", {**body, "step_id": "nope"})[0] == 404
    assert live.request("POST", "/api/ask", {**body, "step_id": "Bad Id"})[0] == 400


def test_target_detail_carries_pr_and_notice(findings_path):
    from conftest import free_port
    from review_viewer import netinfo, server
    ff = load_findings(findings_path)
    box = findings_path.parent / "viewer"
    meta = {ff.target.slug: {"pr": {"number": 7, "title": "T"}, "notice": "stale"}}
    viewer = server.build_viewer([(ff, box)], agent="A", session_id="s", idle_minutes=0,
                                 meta=meta)
    server.bind(viewer, "127.0.0.1", free_port(), 1)
    server.publish(viewer, netinfo.display_urls(viewer.port))
    try:
        served = walkthrough.load_served(box)
        assert served.pr == {"number": 7, "title": "T"} and served.notice == "stale"
        assert served.findings.target.head_sha == ff.target.head_sha
    finally:
        viewer.httpd.server_close()
        server.unpublish(viewer)
