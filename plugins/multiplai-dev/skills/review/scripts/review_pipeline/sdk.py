"""The one way this pipeline calls a model: `agent_call_structured`.

Copies the pattern of buildme's `sdk.py`, not the module: a repo trust gate,
an explicit allow-list with its complement as the deny-list, a validated
pydantic answer, and exactly one re-ask when the answer does not parse.

The re-ask for an answer that came back but does not parse is a reformat: a
no-tools, one-turn call that turns the text already paid for into the schema.
Re-running the whole prompt would repeat the review (a finder re-ask took 68
turns and cost as much as the first attempt). A run that failed returned no
text, so its re-ask re-runs the prompt.

Every stage gets `Read`, `Grep` and `Glob` and nothing else, so the pipeline is
read-only over the reviewed code by construction.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from multiplai_core.agent_runner import MAX_PROMPT_BYTES, AgentRunError, AgentRunTimeout, run_agent
from multiplai_core.text import extract_json

from . import budget

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_CALL_TIMEOUT_S = 1200.0
DEFAULT_MAX_TURNS = 60
REFORMAT_MAX_TURNS = 1
REFORMAT_MAX_ANSWER_CHARS = 60_000

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
FINDER_TOOLS = VERIFIER_TOOLS = MERGER_TOOLS = READ_ONLY_TOOLS

# The deny-list is the complement of each call's allow-list within this
# universe. Under bypassPermissions an allow-list alone removes nothing, so
# every tool not explicitly allowed is also denied. Best-effort enumeration,
# copied from buildme's `_TOOL_UNIVERSE` (2026-08-06); core's `tools=` base set
# is the first layer, this is the second.
_TOOL_UNIVERSE = [
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit",
    "MultiEdit", "REPL", "Task", "Agent", "AskUserQuestion", "SlashCommand",
    "ExitPlanMode", "EnterPlanMode", "TodoWrite",
    "Read", "NotebookRead", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
    "ToolSearch", "Skill",
    "Artifact", "SendMessage", "PushNotification", "RemoteTrigger", "SendFeedback",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
    "TaskOutput", "Workflow", "ScheduleWakeup", "Monitor",
    "CronCreate", "CronDelete", "CronList",
    "Mcp", "ListMcpResources", "ReadMcpResource", "ReadMcpResourceDir", "RefreshMcpTools",
    "EnterWorktree", "ExitWorktree", "ReportFindings", "ProposeSkills",
    "Projects", "ClaudeDesign", "ShowOnboardingRolePicker",
]


class AgentCallError(Exception):
    """An agent call failed, or its answer did not validate after the re-ask."""


class RepoTrustError(AgentCallError):
    """The user has not said the reviewed repository is trusted."""


def repo_is_trusted() -> bool:
    return os.environ.get("REVIEW_TRUST_REPO", "").strip().lower() in ("1", "true", "yes")


def require_trusted_repo() -> None:
    """Checked before every agent call.

    The agents read the repository's own files, and those files become part of
    what the model acts on. A hostile repo can steer them. With read-only
    tools the damage is bounded, but the user still decides.
    """
    if not repo_is_trusted():
        raise RepoTrustError(
            "the review skill sends this repository's contents to a model and lets it read "
            "the tree. Re-run with --trust-repo (or set REVIEW_TRUST_REPO=1) if you trust it."
        )


def deny_list(prompt: str, allowed_tools: list[str]) -> list[str]:
    allowed = set(allowed_tools)
    denied = [t for t in _TOOL_UNIVERSE if t not in allowed]
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        # run_agent's oversized-prompt fallback tells the agent to Read a file.
        denied = [t for t in denied if t != "Read"]
    return denied


async def _run(prompt: str, *, allowed_tools: list[str], model: str | None, effort: str | None,
               max_turns: int, cwd: str | None, call_timeout: float, budget_label: str,
               run_label: str = "") -> str:
    require_trusted_repo()
    budget.check(stage=budget_label)
    try:
        result = await run_agent(
            prompt,
            allowed_tools=list(allowed_tools),
            disallowed_tools=deny_list(prompt, allowed_tools),
            max_turns=max_turns,
            model=model,
            effort=effort,
            cwd=cwd,
            timeout_s=call_timeout,
            label=run_label or budget_label or "review",
            component="review",
        )
    except AgentRunError as e:
        partial = getattr(e, "partial", None)
        if partial is not None:
            budget.record(partial.usage, label=budget_label)
        kind = "timeout" if isinstance(e, AgentRunTimeout) else e.reason
        raise AgentCallError(f"agent call failed ({kind})\n{e.stderr_tail}") from e
    budget.record(result.usage, label=budget_label)
    return result.text


def parse_answer(text: str, schema: type[T]) -> T:
    """*text* as *schema*. Raises ValueError or ValidationError.

    An answer that names none of the schema's fields holds no JSON answer at
    all (a markdown report, say). `extract_json` would still parse the first
    brace in its prose, and the error would describe that fragment, so this
    case gets its own message.
    """
    try:
        return schema.model_validate(extract_json(text))
    except (ValidationError, ValueError) as e:
        if not any(f'"{name}"' in text for name in schema.model_fields):
            start = " ".join(text.split())[:120]
            raise ValueError(f"the answer contains no JSON object with the field(s) "
                             f"{', '.join(schema.model_fields)}; it starts: {start!r}") from e
        raise


def reformat_prompt(text: str, schema: type[BaseModel], error: Exception) -> str:
    if len(text) > REFORMAT_MAX_ANSWER_CHARS:
        text = text[:REFORMAT_MAX_ANSWER_CHARS] + "\n[... answer cut ...]"
    return (
        "Another agent was asked for ONE JSON object matching the schema below. A program rejected "
        f"its answer: {str(error)[:2000]}\n\n"
        "Rewrite that answer as ONE JSON object matching the schema. Use only what the answer says: "
        "copy every quote, path and line number exactly, and add nothing it does not state. Leave out "
        "any item the answer cannot fill in; when it holds no item for a list, leave the list empty.\n\n"
        f"Schema:\n{json.dumps(schema.model_json_schema(), indent=1)}\n\n"
        f"<answer>\n{text}\n</answer>\n\n"
        "Answer with ONE JSON object and nothing after it."
    )


async def agent_call_structured(
    prompt: str,
    schema: type[T],
    *,
    allowed_tools: list[str],
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    cwd: str | None = None,
    budget_label: str = "",
    call_timeout: float = DEFAULT_CALL_TIMEOUT_S,
) -> T:
    """Run an agent and parse its final message into *schema*.

    A failure gets one re-ask; a second failure raises `AgentCallError`. An
    answer that does not parse is re-asked as a reformat of that answer, with
    no tools. A failed run (timeout, CLI error) re-runs the prompt with the
    error appended.
    """
    name = budget_label or "agent"
    common = dict(model=model, effort=effort, cwd=cwd, call_timeout=call_timeout, budget_label=budget_label)
    text: str | None = None
    try:
        text = await _run(prompt, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        return parse_answer(text, schema)
    except RepoTrustError:
        raise
    except (ValidationError, ValueError, AgentCallError) as e:
        first_error = e
        log.warning("%s: answer unusable (attempt 1/2): %s", name, str(e)[:500])

    try:
        if text is None:
            retry = (
                f"{prompt}\n\n---\nYour previous answer was rejected by a program: {str(first_error)[:2000]}\n"
                f"Return ONLY one JSON object matching this schema:\n"
                f"{json.dumps(schema.model_json_schema(), indent=1)}\n"
            )
            text = await _run(retry, allowed_tools=allowed_tools, max_turns=max_turns, **common)
        else:
            text = await _run(reformat_prompt(text, schema, first_error), allowed_tools=[],
                              max_turns=REFORMAT_MAX_TURNS, run_label=f"{name}:reformat", **common)
        return parse_answer(text, schema)
    except RepoTrustError:
        raise
    except (ValidationError, ValueError, AgentCallError) as e:
        log.warning("%s: answer unusable (attempt 2/2): %s", name, str(e)[:500])
        raise AgentCallError(f"{name}: no valid answer after a re-ask: {e}") from e
