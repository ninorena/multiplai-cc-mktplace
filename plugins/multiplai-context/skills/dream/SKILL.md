---
name: dream
description: "Generate a processed-learnings proposal from the pending backlog and write it to .multiplai/dreams/ for review. Does NOT apply changes — run /multiplai-context:dream-remember to review and apply."
---

# Multiplai Dream — Generate Learnings Proposal

Runs the Dream analysis pipeline: reads all pending learnings from `.multiplai/learnings/`,
distills them into a structured proposal (a two-pass generate-then-critic flow), and writes
it to `.multiplai/dreams/processed-learnings-YYYY-MM-DD.md`.

The proposal sorts every learning into one of two dispositions:
- **Memory updates** — generalized, reusable lessons, grouped by target memory file.
- **Filtered Out** — one-off events / diary material, and change-requests to the toolchain
  itself (code/config/structure), each with a reason. Toolchain change-requests are dropped
  rather than filed: a change worth making gets made.

Memory updates are numbered `1..N` continuously across every target file, so a reviewer can
say "skip 14" without naming the file it sits under.

Four deterministic sections wrap the model's own output. Each is pure code, and
each fails open — a missing section means that gate did not run, which is itself
worth reporting:

- `## Conflict Resolutions` (**top**) — verified corrections that contradict an
  existing memory line, put where review starts.
- `## Citation Repairs` (near the end, **only when there was something to say**) —
  `**Source:**` citations whose filename was wrong and could be corrected beyond
  doubt, plus any that could not be verified and were left alone.
- `## Routing Warnings` — misrouted sections and cross-file duplicates
  (`(none)` when clean).
- `## Rules Re-learned` (**last**) — rules memory already held that a later
  session derived again, with a verdict on why each one did not stick. It reads
  the rejection log from *previous* runs, so it is empty on a first run and says
  so rather than rendering nothing.

`## Rules Re-learned` also stands alone, and reads the same two logs without
running a dream — useful when the question is "which of my rules are not
working" rather than "what is in the backlog":

```
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/relearn_report.py"
```

Read-only, seconds to run, writes nothing. `--limit 0` for every group,
`--all-kinds` to include re-derived facts, `--json` for the raw grouping.

**No memory files are modified.** The proposal is for review only.
Run `/multiplai-context:dream-remember` to load the proposal and apply approved changes.

---

## Daily review (`--daily-review`): two entries per file, per day

The routine path. Pure code, no model call, seconds to run, deletes nothing:

```
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --daily-review
```

1. Every entry in `.multiplai/learnings/*.md` is stored in a queue for its target file
   (`.multiplai/learnings/queues/`). Entries that say the same thing merge into one, and
   keep every wording. Raw files move to `learnings/archived/` once their entries are safe.
   A file with a line it does not understand stays where it is, and the output names it.
2. Each queue keeps its best 10: corrections first, then entries seen in 2 or more
   sessions, then `verified`, then newest. The rest go to `archived/reserve.md`. A queue
   under 10 refills from the reserve. Reserve entries older than 90 days move to
   `archived/rejected.md`.
3. `.multiplai/dreams/review-YYYY-MM-DD.md` gets the top 2 of each queue, with the exact
   text to add and three boxes. An existing file for the day is kept, not overwritten.

Report the counts and the review file path. Then remind the user to tick the boxes and
run `/multiplai-context:dream-remember --daily`. Do not tick anything.

The steps below are the older whole-backlog proposal, for when the user asks for it.

---

## Steps

1. **Check for pending learnings:**
   ```
   uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --check
   ```
   If the output says no pending learnings, inform the user and exit.

   `--check` also prints an `Estimated wall clock: at least N min`. It is a
   **floor**, not a prediction — a measured 283 KB backlog took 25 min against a
   12 min floor. Quote it to the user as a minimum, never as an ETA.

2. **Generate the proposal** — this runs for many minutes (a 283 KB backlog
   measured 25 min), past the Bash tool's 600s max timeout. You **MUST** invoke
   it via the Bash tool with `run_in_background: true`:
   ```
   uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
   ```
   (No flags — default is report mode.)

   - Use the Bash tool's **`run_in_background: true`** option (no `&`, no `nohup`).
     The harness **re-invokes you automatically when the process exits** — no polling.
   - On re-invocation, confirm success by the sentinel line
     **`Proposal written to <path>`** (the file under `.multiplai/dreams/` will also
     exist). Only then proceed to step 3.
   - **NEVER** detect completion with `nohup … & ; until ! ps -p "$PID" …`. In this
     environment PID 1 is the `claude` process, not an init reaper: a finished script
     becomes an **unreaped `<defunct>` zombie that matches `ps -p PID` forever**, so
     the loop never terminates and the user has to kill it by hand. Detect completion
     by the **sentinel line / output file only** — never by process liveness.

3. **Report results:**
   - Path to the proposal file in `.multiplai/dreams/`
   - Number of source files and learnings count
   - Counts by disposition: memory updates, action items, filtered out
   - **The `## Rules Re-learned` headline, if the section has one** — how many
     rules memory already held were derived again, and the split between "not
     routed", "routed but unused" and "inconclusive". These are not proposal
     items and there is nothing to approve; they say which *existing* memory
     lines are not doing their job, and each verdict names a different repair.
     Do not fold them into the disposition counts.
   - Remind: run `/multiplai-context:dream-remember` to review and apply

   **Surface every `⚠` line from the script's own summary, verbatim.** A run can
   succeed with exit 0 having consolidated only part of the backlog — a chunk
   that ran out of time, or a second-pass review batch that failed. The script
   reports what actually landed rather than what it planned:

   ```
   Sources: 5 files, 122 of 231 new learning block(s) consolidated
     ⚠ 10 of 19 chunk(s) did not complete — 109 block(s) stay pending and are
       picked up by the next run (see dream.log)
     ⚠ second-pass review incomplete: 8 of 8 batch(es) failed — duplicates and
       mis-routed items may remain
   ```

   Nothing is lost when this happens — deferred learnings are never marked
   processed and come back on the next run — but reporting `231 new learning
   block(s)` for a run that consolidated 122 tells the user their backlog is
   done when half of it is queued. Do not summarize the warnings away, and do
   not present a partial run as a complete one.

---

## Autonomous mode (--auto)

If the user explicitly asks to apply changes without review:
```
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --auto
```

This rewrites memory files directly and commits. Use only when the user explicitly
requests fully autonomous operation — the default is always human-in-the-loop.

---

## Catalog Regeneration

After `--auto` mode completes, regenerate catalogs:
```
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
```
(Skipped for report-only mode — catalogs are regenerated after the user applies
changes via `/multiplai-context:dream-remember`.)

---

## Backlog size and machine load

**Any backlog size runs in one pass.** The script splits the backlog into chunks
sized to what it can finish inside a deadline, and calibrates that size from the
throughput it measured on this machine. There is no size limit to work around:
do **not** split learnings files by hand, do **not** move files to a scratch dir
to shrink the input, and do **not** set `MULTIPLAI_SDK_CALL_TIMEOUT_S` — those
were workarounds for a single-call design that no longer exists, and splitting
by hand now costs cross-file deduplication.

A failed chunk is not a failed run. Its learnings are never marked processed, so
they return on the next run — which is why an interrupted or partial run needs no
cleanup and no manual retry.

**`/dream` runs 8 model calls concurrently by default**, each a Claude Code CLI
subprocess. That is a deliberate trade for wall clock: four workers could not
clear a 283 KB backlog in under ~24 min at measured throughput. If the user needs
their machine responsive, `MULTIPLAI_DREAM_CONCURRENCY=4` (or any number) lowers
it at the cost of a proportionally longer run.

---

## Constraints
- Never invoke `--auto` unless the user explicitly requests autonomous operation.
- The default invocation (`uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"`, no flags) is always report mode — safe to run anytime.
- The dream script uses the path resolver for all file locations — never hardcode paths.
- All LLM calls go through the model client abstraction — never import the SDK directly.
- If catalog generation fails or errors occur, the dream cycle still completes successfully. Catalog failures are logged but do not block or prevent the dream from finishing.
- If there is nothing to consolidate (empty learnings), inform the user and exit — do not run consolidation on an empty backlog.
