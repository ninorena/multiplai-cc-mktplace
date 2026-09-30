# review scripts — orientation

A pipeline of separate agents with gates written in Python between them. The
agents find, verify and merge duplicates; the gates decide what survives, by
re-reading the reviewed commit with git. No gate calls a model. The pipeline
proposes no fixes: the verifier writes each finding's `expected_behaviour`, one
sentence on what correct behaviour looks like, without code.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m review_pipeline --help
uv run --directory plugins/multiplai-dev/skills/review/scripts python -m pytest tests/ -q
# CI form
cd plugins/multiplai-dev/skills/review/scripts && \
  uv run --project ../../../../.. --package review-pipeline --extra dev python -m pytest tests/ -q
```

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `review`, `batch`, `rollup`, `resume`, `post`. Calls `setup_logging` once. Owns the stdout contract and the exit codes. |
| `models.py` | Pipeline models. `Finding.citations` has `min_length=1`; `Finding.id` is computed with the v1 rule, never taken from a model. `Finding.finders` lists every finder that reported it. `STAGES` is the run order; `ReviewState` maps the removed `prescribe`/`check_fix` stages of an old checkpoint to `verify`. |
| `target.py` | Resolves `--branch` / `--pr` / `--range` to shas, writes `diff.patch`, `commits.txt`, `files.txt`; `target_gate`; `snapshot_head()` (`git archive` of head into `<slug>/tree/`). Fixed argv, no shell, no checkout. |
| `gates.py` | `citation_gate`, `finding_gate`, `verdict_gate`. Each takes `TargetInfo` and returns `GateResult`. |
| `sdk.py` | `agent_call_structured`: trust gate, `Read`/`Grep`/`Glob` allow-list with its complement denied, one re-ask on a bad answer: a no-tools, one-turn reformat of the text returned, or a re-run of the prompt when the run itself failed. `parse_answer` says so when an answer holds no JSON at all. The only caller of `run_agent`. |
| `budget.py` | Per-target ledger in a `ContextVar` (a batch runs targets concurrently) and the circuit breaker. |
| `config.py` | `review.yaml` > `multiplai.conf` > session model, for per-stage models, effort, concurrency. |
| `stages/` | `find`, `verify`, `merge`. Each `run_<stage>(state, ctx)` returns at once when the state is past it and skips items it already has. Each stores an agent's answer in the state as it returns (`finder_results`, `verdicts`, `merge_answers`), so the checkpoint saved at a budget stop keeps it. |
| `prompts/` | One module per stage; shared blocks in `__init__.py`. |
| `export.py` | `ReviewState` → `findings.json` v1, key by key (the contract rejects unknown keys). Merged-away findings and `Finding.finders` are not exported; the v1 shape did not change for them. |
| `render.py` | `review-<slug>.md`, the short `summary-<slug>.md` the session pastes into chat, and the `<SEV>-only.md` rollups, all from v1 dicts. |
| `post.py` | One `gh pr comment`; with `--decisions`, only findings whose decision is `accept`. |
| `orchestrator.py` | target → find → verify → merge → export → render, saving `review-state.json` after each; `resume`; `batch`. |
| `state.py`, `progress.py` | Atomic checkpoint; the tailable `progress.log` (`STARTED`, `STAGE`, `DONE`, `FAILED`). |

## Why the agents read a snapshot

The gates check quotes against `git show <head_sha>:<path>`. The agents' tools
see a directory, and the repository's working tree is whatever is checked out.
So `snapshot_head()` extracts the head tree with `git archive` (which writes
nothing to the repo) and every agent runs with `cwd` there. Paths the agents
return are made repo-relative in `stages/__init__.py`. The snapshot is deleted
when the run finishes.

## The gates

| Gate | Passes when | On failure |
|---|---|---|
| `citation_gate` | the whitespace-normalised quote is inside lines `line_start..line_end` at head | building block |
| `finding_gate` | every citation passes; the file changed (unless `dimension == "pre-existing"`); severity is known | finding → `rejected` |
| `verdict_gate` | a `confirmed` verdict has at least one citation that passes | verdict → `unverifiable`, severity lowered one step |

Gate function names must not start with `test` (pytest would collect them).

## The merge stage

`find` drops a finding only when file, first line and the first 160 characters
of the normalised claim match (`dedupe_key`), and records the dropped copy's
finder in `finders`. Reworded duplicates get through, so `stages/merge.py`
runs after verify:

- `overlap_groups` takes the `confirmed` and `unverifiable` findings, per file,
  and chains every pair whose line ranges overlap or are at most
  `MERGE_LINE_GAP` (2) lines apart. Groups of two or more go to one agent each.
- The agent returns `duplicate_sets` of ids. `usable_sets` keeps only ids in
  the group, each id once, sets of two or more.
- `merge_set` keeps one member (confirmed before unverifiable, then the most
  severe, then the first), so its id and verdict stand; it takes the highest severity in the
  set, the union of citations and of finders. For an all-unverifiable set,
  `original_severity` becomes the highest of the members'.
- Each finding merged away goes to `state.merged` with `into` and a reason.
  `render.py` lists them in the appendix.
- Answers are stored in `state.merge_answers` by `group_key` as they arrive,
  so a resume asks no group twice. A failed agent call stores an empty answer
  (the group stays unmerged) and adds a line to `state.errors`.

## Logging

`setup_logging("review-pipeline", propagate_loggers=("review_pipeline", "multiplai_core"))`
writes `review-pipeline.log`. `log_event("review", …)` fires `start`, `stage`,
`gate_reject`, `budget_stop`, `done`, `post`. No field holds finding or prompt
text: `gate_reject` records which rule fired (`orchestrator.reason_kind`), not
the reason string.

## Tests

`tests/fixture_repo.py` builds the DB2038 regression repository from
`tests/fixtures/settings_consumer_repo/{base,head}` with fixed dates, so its
shas are stable. Every agent call is monkeypatched; nothing here calls a model.
`test_export.py` validates against
`../../review-viewer/schema/findings.v1.schema.json`, and `test_models.py`
checks this package's `finding_id` against the ids in the viewer's own
fixture. The package never imports `review_viewer`.
