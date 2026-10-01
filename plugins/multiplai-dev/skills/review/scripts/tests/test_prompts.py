"""The prompt blocks added for PR context, branch rules and the web."""
from __future__ import annotations

from conftest import high_finding
from review_pipeline.prompts import find, merge, verify
from review_pipeline.prompts import description_block, settings_block, workspace_block

RULES_NO_CHECKS = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
RULES_WITH_CHECKS = [{"type": "required_status_checks",
                      "parameters": {"required_status_checks": [{"context": "build"}],
                                     "strict_required_status_checks_policy": True}}]


def pr_target(target_info, **extra):
    return target_info.model_copy(update={"pr": 14, "title": "Add retention", "description": "CI gates the merge.",
                                          "base_ref": "main", "branch_rules": RULES_NO_CHECKS, **extra})


def test_range_targets_add_no_description_or_settings(target_info):
    assert description_block(target_info) == "" and settings_block(target_info) == ""
    prompt = find.build(target_info, "diff-bugs", "diff")
    assert "untrusted-content" not in prompt and "Repository settings" not in prompt


def test_description_is_fenced_and_called_a_claim(target_info):
    block = description_block(pr_target(target_info))
    assert '<untrusted-content source="pull request #14 description">' in block
    assert "Title: Add retention" in block and "CI gates the merge." in block
    assert "claim to check" in block and "</untrusted-content>" in block


def test_description_is_cut_when_long(target_info):
    block = description_block(pr_target(target_info, description="x" * 20_000))
    assert "[... description cut ...]" in block and len(block) < 13_000


def test_settings_say_when_no_check_blocks_a_merge(target_info):
    block = settings_block(pr_target(target_info))
    assert "pull_request: 1 approval required" in block
    assert "no `required_status_checks` rule" in block and "a red run can be merged" in block


def test_settings_name_the_required_checks(target_info):
    block = settings_block(pr_target(target_info, branch_rules=RULES_WITH_CHECKS))
    assert "required_status_checks: build (branch must be up to date)" in block
    assert "a red run can be merged" not in block


def test_settings_when_rules_could_not_be_read(target_info):
    block = settings_block(pr_target(target_info, branch_rules=None))
    assert "could not be read" in block and "Do not assume CI blocks a merge" in block


def test_settings_when_github_reports_no_rules_at_all(target_info):
    block = settings_block(pr_target(target_info, branch_rules=[]))
    assert "GitHub reports no rules on this branch" in block and "a red run can be merged" in block


def test_finders_and_verifier_get_both_blocks_and_the_web(target_info):
    t = pr_target(target_info)
    for dimension in find.DIMENSION_TASKS:
        prompt = find.build(t, dimension, "diff")
        assert "WebFetch and WebSearch" in prompt
        assert "pull request #14 description" in prompt and "Repository settings" in prompt
    prompt = verify.build(t, high_finding())
    assert "WebFetch and WebSearch" in prompt and "Repository settings" in prompt
    assert "`unverifiable`, not `confirmed` with a caveat" in prompt


def test_merger_has_no_web(target_info):
    prompt = merge.build(pr_target(target_info), [high_finding()])
    assert "WebFetch" not in prompt
    assert workspace_block(target_info) == workspace_block(target_info, web=False)


def test_conventions_finder_is_told_to_look_outside_the_diff(target_info):
    prompt = find.build(target_info, "conventions", "diff", conventions="# rules")
    assert "Grep the whole repository for other statements of the old fact" in prompt
    assert "where something must be recorded" in prompt
    assert "documentation the change makes stale" in prompt


def test_citation_rules_put_the_web_second(target_info):
    prompt = find.build(target_info, "diff-bugs", "diff")
    assert "the first\ncitation is always a file at this commit" in prompt
