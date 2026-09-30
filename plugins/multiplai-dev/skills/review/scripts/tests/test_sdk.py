from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from multiplai_core.agent_runner import AgentRunError
from review_pipeline import budget, sdk


class Answer(BaseModel):
    value: int


def _result(text: str, cost: float = 0.5):
    return SimpleNamespace(text=text, usage=SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=cost))


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")


@pytest.fixture
def fake_run(monkeypatch):
    calls: list[dict] = []
    replies: list = []

    async def run_agent(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(sdk, "run_agent", run_agent)
    return calls, replies


async def test_untrusted_repo_is_refused_before_any_call(fake_run):
    calls, _ = fake_run
    with pytest.raises(sdk.RepoTrustError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert calls == []


async def test_read_only_tools_and_their_complement(trusted, fake_run):
    calls, replies = fake_run
    replies.append(_result('{"value": 3}'))
    out = await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, cwd="/repo",
                                          budget_label="find:diff-bugs", model="m", effort="high")
    assert out.value == 3
    call = calls[0]
    assert call["allowed_tools"] == ["Read", "Grep", "Glob"]
    for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
        assert tool in call["disallowed_tools"]
    assert not {"Read", "Grep", "Glob"} & set(call["disallowed_tools"])
    assert (call["cwd"], call["component"], call["label"], call["model"], call["effort"]) == \
        ("/repo", "review", "find:diff-bugs", "m", "high")


def test_every_stage_gets_only_read_grep_glob():
    for tools in (sdk.FINDER_TOOLS, sdk.VERIFIER_TOOLS, sdk.MERGER_TOOLS):
        assert tools == ["Read", "Grep", "Glob"]


async def test_one_reask_quotes_the_error(trusted, fake_run):
    calls, replies = fake_run
    replies += [_result('{"value": "not a number"}'), _result('```json\n{"value": 7}\n```')]
    out = await sdk.agent_call_structured("original", Answer, allowed_tools=sdk.VERIFIER_TOOLS)
    assert out.value == 7
    assert len(calls) == 2
    assert calls[1]["prompt"].startswith("original")
    assert "rejected by a program" in calls[1]["prompt"] and "value" in calls[1]["prompt"]


async def test_second_failure_raises(trusted, fake_run):
    calls, replies = fake_run
    replies += [_result("no json here"), _result("still none")]
    with pytest.raises(sdk.AgentCallError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.VERIFIER_TOOLS)
    assert len(calls) == 2


async def test_failed_run_counts_as_a_failure_and_is_reasked(trusted, fake_run):
    calls, replies = fake_run
    err = AgentRunError.__new__(AgentRunError)
    RuntimeError.__init__(err, "boom")
    err.reason, err.stderr_tail, err.partial = "boom", "", None
    replies += [err, _result(json.dumps({"value": 1}))]
    assert (await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.MERGER_TOOLS)).value == 1
    assert len(calls) == 2


async def test_spend_is_recorded_and_the_breaker_stops_the_next_call(trusted, fake_run):
    calls, replies = fake_run
    ledger = budget.start(1.0)
    replies += [_result('{"value": 1}', cost=1.2)]
    await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, budget_label="find:tests")
    assert ledger.cost_usd == pytest.approx(1.2) and ledger.by_label["find:tests"] == pytest.approx(1.2)
    with pytest.raises(budget.BudgetExceededError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert len(calls) == 1


def test_budget_state_round_trip():
    a = budget.ReviewBudget(max_usd=10)
    a.record(SimpleNamespace(input_tokens=1, output_tokens=2, cost_usd=0.25), label="verify")
    b = budget.ReviewBudget(max_usd=10)
    b.load_state(a.to_state())
    assert (b.cost_usd, b.calls, b.by_label) == (0.25, 1, {"verify": 0.25})
