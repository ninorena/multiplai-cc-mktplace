# multiplai-context

A Claude Code plugin for **context routing, continuous learning, session
awareness, and memory management**. It injects only the memory relevant
to each prompt, captures learnings from your sessions, keeps a per-session
diary, and consolidates what it learns back into your memory files — and
nothing becomes memory without your approval:
**`/multiplai-context:dream-remember` is the only path that edits your
memory files.**

This is the heart of the [`multiplai`](../../README.md) marketplace.

**Platforms:** built and tested on macOS and Linux. WSL on Windows is
expected to work but not actively tested — please open an issue if it
doesn't. Native Windows (without WSL) isn't supported.

## How it works

1. **Setup** — `/multiplai-context:setup` walks you through populating a small set
   of memory files (who you are, how you work, technical preferences).
2. **Per prompt** — a `UserPromptSubmit` hook routes your prompt against
   indexed catalogs of memory (and optionally skills) and injects only the
   relevant pieces. No memory dump. Files injected on
   recent turns are skipped — they're already in the conversation (see
   [Re-recommendation cooldown](#re-recommendation-cooldown)). The same hook
   also points Claude at the engineering standards for the project's stack,
   detected from its manifests rather than routed — see
   [Dev references](#dev-references--engineering-standards-not-memory).
3. **Per session** — diary entries and a learnings backlog are captured
   in the background; nothing blocks your session.
4. **Consolidation** — `/multiplai-context:dream-remember` distills the backlog
   into a proposal with two dispositions: generalized lessons → your **memory
   files**; everything else — one-off events, things the diary already records,
   and change-requests to the toolchain itself — → **filtered out**, with the
   reason. You approve before anything is written.
5. **Unattended upkeep** — a [proactive maintainer](#proactive-maintenance)
   runs detached at most once a day (lint, dream proposal, catalog, `now/`),
   and [prospective memory](#prospective-memory--intentions-that-fire-later)
   surfaces intentions whose date has arrived. Neither ever writes to your
   memory files.

```
  setup → memory files
              ↓
  every prompt: context_manager picks relevant memory/skills
              ↓
  Stop / SessionEnd: diary written, learnings queued
              ↓
  /multiplai-context:dream-remember: backlog distilled →
              memory updated · rest filtered, with reasons
```

## Installation

From the marketplace (recommended):

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-context@multiplai
```

For local development, point Claude Code at the plugin directory:

```
claude --plugin-dir ./plugins/multiplai-context
```

No manual install step for Python dependencies. `scripts/pyproject.toml`
declares them once, as a member of the repo-root `uv` workspace, and scripts
are launched via `uv run --project`. uv provisions the environment on first
run from the committed `uv.lock` and reuses it after that.

Scripts previously declared dependencies inline (PEP 723). That made `uv run`
re-resolve `multiplai-core` against GitHub on **every** invocation, so the
`UserPromptSubmit` hooks — which fire on every prompt — took 12-68s and hit
their timeout. Resolving from a lockfile instead brought that to ~0.05s.

### Standalone install (no kit)

The plugin is fully standalone. The multiplai kit (launcher, container,
workspace scaffold) is **optional** — it pre-wires configuration, nothing
more. On vanilla Claude Code:

1. **Prerequisite: [uv](https://docs.astral.sh/uv).** It is the only hard
   requirement (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Without
   it the hooks stay disabled and print one clear install pointer — nothing
   crashes, but nothing runs either.
2. Install the plugin:
   ```
   /plugin marketplace add spikelab/multiplai-cc-mktplace
   /plugin install multiplai-context@multiplai
   ```
3. **The first session builds the environment in the background.** On the
   first hook fire the plugin detaches a one-time build of its Python
   environment from the committed `uv.lock` (~1 min; it includes cloning
   `multiplai-core`) and injects a one-line notice; hooks go live on the
   next prompt after the build finishes. Nothing blocks your session and
   no hook gets killed mid-build. To build ahead of time instead, either
   run `claude --init-only` once (the plugin's `Setup` hook builds the
   environment synchronously) or, from a shell:
   ```
   sh <plugin-dir>/hooks/run.sh --warm
   ```
   Both build the environment *and* clear the in-flight marker the hooks
   gate themselves behind — a bare `uv sync` builds but leaves the marker,
   which keeps the hooks quiet.
   Every later start is fast — resolution is already done, so no later run
   touches the network.
4. Run `/multiplai-context:setup` in your first session — two questions
   (your name, your workspace), settings written, memory seeded.
   (`/multiplai-context:setup full` runs the deeper interview any time.)
5. **First recall — see it work.** Setup ends by asking you to restart
   once: `/exit`, then `claude` again. In the new session, ask:

   > What do you know about me?

   The answer should contain what you just told setup — your name, your
   workspace — because the relevant memory files now arrive with your
   prompt as a `MEMORY` block, routed per prompt rather than dumped. To
   see the routing decision itself:

   ```bash
   tail -5 <workspace>/.multiplai/data/logs/activity.log
   ```

   The `[context]` line names exactly which memory files were injected,
   with relevance scores (anatomy in [Observability](#observability)).
   Nothing shows up? See [Troubleshooting](#troubleshooting).

That's the whole loop in miniature. From here it compounds: diary and
learnings accumulate in the background, and `/multiplai-context:dream-remember`
turns them into memory — with your approval, never without.

**What lands where:** with no configuration, all state roots at
`~/.multiplai/` — `memory/` (your profile), `diary/` (per-session
narrative), `learnings/` (pending insights), `now/` (project snapshots) —
and catalogs/logs under `~/.claude/`. Set the `workspace_dir` plugin option
to anchor state at `<workspace>/.multiplai/` instead. All options are
listed under Configuration below; every one has a working default.

## Configuration

Options are collected by `/multiplai-context:setup` (Claude Code does
**not** prompt for `userConfig` values at enable time —
[anthropics/claude-code#39455](https://github.com/anthropics/claude-code/issues/39455)),
or set manually in your `settings.json` under the compound
`<plugin>@<marketplace>` key — a bare `multiplai` key fails silently:

```json
{
  "pluginConfigs": {
    "multiplai-context@multiplai": {
      "options": {
        "workspace_dir": "/path/to/your/workspace"
      }
    }
  }
}
```

Values are exposed to hooks as `CLAUDE_PLUGIN_OPTION_<KEY>` env vars, where
`<KEY>` is the option key **uppercased** — `workspace_dir` arrives as
`CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR`.
(Sideloaded installs via `claude --plugin-dir` ignore `pluginConfigs` —
set the env vars directly, in that same uppercase form.)

### Quick start: the only options you probably need

Most users only touch these:

| Option | Why |
|--------|-----|
| `workspace_dir` | Anchor for all state. Set it once; everything else defaults under it. |
| `anthropic_api_key` | Only if you're not running inside Claude Code's Agent SDK (set as a fallback). |
| `memory_router` | `token_overlap` (offline, instant) is the safe default. `llm` picks better files — one Haiku call per prompt, ~2.9s — at the cost of blocking your prompt on it. |

Optional, for power users:

- `enable_skills` + `skills_dir` — index a skills corpus for routing.
- `enable_resources` + `resources_dir` — retrieve a research/notes corpus
  through qmd. Needs a qmd index built on the host first; see
  [Resources retrieval via qmd](#resources-retrieval-via-qmd).

Everything else (catalog model, TTL, diary catalog window, individual
`*_dir` overrides) has sensible defaults — leave alone unless you're
tuning.

### Directory layout

`workspace_dir` is the anchor. When set, memory/diary/now/learnings
default to `<workspace_dir>/.multiplai/{memory,diary,now,learnings}`. If
unset, everything falls back to `~/.multiplai/`. Individual overrides win
over the anchor:

| Option | Default | Purpose |
|--------|---------|---------|
| `workspace_dir` | `""` | Anchor for all state dirs (see above) |
| `memory_dir` | `<workspace>/.multiplai/memory` | Memory files (`me.md`, `technical-pref.md`, `preferences.md`, …) |
| `diary_dir` | `<workspace>/.multiplai/diary` | Per-session diary entries (`YYYY-MM-DD/<session>.md`) |
| `now_dir` | `<workspace>/.multiplai/now` | Per-project status summaries |
| `learnings_dir` | `<workspace>/.multiplai/learnings` | Per-day captured learnings |

### Model & catalog

| Option | Default | Purpose |
|--------|---------|---------|
| `anthropic_api_key` | _(unset, sensitive)_ | API key fallback when the Agent SDK is unavailable. Marked sensitive — stored in the system keychain, never logged. |
| `catalog_model` | `claude-sonnet-5` | Model for LLM catalog generation |
| `catalog_model_diary` | _(inherits)_ | Optional model override for the diary catalog |
| `catalog_ttl_hours` | `168` | Hours a generated catalog stays valid before the read path flags it stale (advisory warning only — never regenerates inline) |
| `diary_catalog_days` | `7` | Days of diary history the diary catalog covers |
| `memory_router` | `token_overlap` | Context selection strategy: `token_overlap` (offline, instant), `llm` (one model call per prompt — 2.4× better at finding the right file, worse at injecting nothing), or `llm_hybrid` (`llm` picks, then `token_overlap`'s breadth gate and `anti_domains` veto filter the picks — opt-in and unmeasured; it can only remove files, so it trades recall for precision). See [Router latency](#router-latency) before choosing either LLM strategy. |
| `router_model` | `claude-haiku-4-5` | Model for the `llm` router. Haiku by default — routing is cheap classification, so the smallest/fastest model keeps per-prompt latency down. Ignored under `token_overlap`. |
| `router_thinking` | `false` | Extended thinking on router calls. Off by default and that is what makes LLM routing fit the hook budget: a cold call is 18.4 s with thinking on, 2.9 s with it off. Set `true`/`1`/`yes`/`on` to restore it. If the installed `multiplai-core` or `claude-agent-sdk` is too old to carry the setting, the plugin warns once, keeps thinking on, and keeps working. |
| `extraction_thinking` | `false` | Extended thinking on the diary/learnings extraction call — structured parsing over a distilled transcript, so it gets the same 18.4 s → 2.9 s win as the router. Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `checkpoint_thinking` | `false` | Extended thinking on the checkpoint writer's call (folds a transcript segment into the fixed six-section document). Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `duplication_thinking` | `false` | Extended thinking on the memory doctor's **duplication**-confirmation call (stage 2), and that call only. The doctor's contradiction pass is not covered — it keeps extended thinking on and has no switch, because deciding whether two statements can both be true is judgement rather than extraction. Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `utilisation_thinking` | `false` | Extended thinking on the offline utilisation judge's per-session verdict calls. Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `now_thinking` | `false` | Extended thinking on the per-project `now/` status-summary calls. Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `catalog_thinking` | `false` | Extended thinking on catalog-generation calls (memory, banks, skills, diary generators). Set `true`/`1` to restore it. Same old-dependency behaviour as `router_thinking`. |
| `recommend_cooldown_turns` | `4` | After a file is injected, suppress re-injecting it for this many turns (it's already in the conversation). `0` disables. See [Re-recommendation cooldown](#re-recommendation-cooldown). |
| `keep_ratio` | `0.30` | `token_overlap` relevance cutoff: keep a memory file only if it scores ≥ this fraction of the top match (0–1). Higher = stricter (fewer, more-relevant files, less 10-file cap saturation); lower admits more of the weaker tail. Ignored under `llm`. See [Routing relevance cutoff](#routing-relevance-cutoff). |
| `memory_conflict_preamble` | `true` | Conflict-surfacing directive + per-file last-updated stamps above every injected MEMORY block, so the model flags memory-vs-session disagreements. ~90 tokens per memory-carrying turn; turn off to save them. |
| `enable_skills` / `skills_dir` | `false` / `~/.claude/skills` | Optionally catalog skills for routing |
| `enable_resources` / `resources_dir` | `false` / `""` | Optionally retrieve a research/reference corpus per prompt, through qmd. There is no resources catalog and no generator — the index is qmd's, built on the host. See [Resources retrieval via qmd](#resources-retrieval-via-qmd). |

#### Router latency

The LLM strategies run one model call **inside the blocking
`UserPromptSubmit` hook**, before Claude sees your prompt. The hook budget
is 30s and the router gives up at 25s. `token_overlap` is instant.

**With `router_thinking` off (the default), a routing call is ~2.9s.** Turn
thinking on and the same call takes ~18.4s, which is close enough to the
ceiling that some prompts lose the race — and a killed hook injects no
memory at all. This is the single knob that decides whether LLM routing is
practical, so leave it off unless you have a reason.

Two things this document previously got wrong, both corrected by measurement
on 2026-08-09:

- **It is not a cold start.** Spawning the `claude` CLI subprocess costs 4–6s,
  not the ~12s once claimed here. Extended thinking was the rest.
- **A warm external routing service does not fix it**, and is not worth
  building: the Agent SDK's `session_id` does not isolate conversations, so one
  long-lived process cannot serve concurrent sessions. That idea was explored
  and rejected, not merely deferred.

`effort` has no effect on this path.

**On quality**, in case the latency framing above reads as "prefer
`token_overlap`": backtested on 300 real prompts, `llm` beats `token_overlap`
2.4× on F1 (48.6 vs 20.0) and injects fewer bytes overall. An older 17-case
eval said the reverse; that set is phrased in the catalog's own vocabulary,
which is what `token_overlap` matches on, so it could not tell them apart.
Where `token_overlap` genuinely wins is knowing when to inject *nothing* —
76.9% vs 51.3% on the first prompt of a session. `llm_hybrid` exists to take
both, and has not been scored yet.

### Resources retrieval via qmd

**qmd is the only way a resources corpus is retrieved.** Set
`enable_resources` and `resources_dir`, build the index (below), and
every prompt is matched against a [qmd](https://github.com/tobi/qmd)
hybrid index: BM25 keyword search + vector search fused by reciprocal
rank, no LLM in the loop (~1–2s). Results are injected as path +
excerpt entries in the `=== RESOURCES ===` section; Claude reads the
full file on demand.

The catalog+router path memory uses does not apply here and was removed
in 0.52.0. It summarised each resource file with one LLM call and routed
on those summaries — which fits a corpus you wrote and re-read, not one
you collected. A research archive is long, heterogeneous, and answers a
prompt with a passage rather than a whole document, which is the shape
qmd's chunk-level index is built for.

Turning `enable_resources` on **without** a qmd index injects nothing.
Nothing breaks — retrieval is fail-open — but nothing arrives either, so
do the host setup below first.

**This is not yet a one-command feature.** qmd has to be installed and a
collection built on the host, and on a container setup the gateway needs
the qmd allowlist entry too — three steps a plugin install cannot do for
you. Treat resources as opt-in for people who want it enough to run the
setup script; making it work out of the box is future work, and until
then leaving `enable_resources` off costs you nothing.

| Option | Default | Purpose |
|--------|---------|---------|
| `qmd_mode` | `local` | `http` = POST to a resident `qmd mcp --http` daemon on the host (preferred: warm models, no cold start); `local` = qmd on PATH (native installs); `ssh` = qmd runs on the host over the container→host SSH bridge |
| `qmd_http_url` | `http://host.docker.internal:8181` | Daemon base URL for `http` mode — see the exposure note below |
| `qmd_candidate_limit` | `10` | Docs the daemon reranks per query in `http` mode (latency dial; capped at 50) |
| `qmd_ssh_host` | `host.docker.internal` | Bridge host for `ssh` mode |
| `qmd_collection` | `resources` | qmd collection holding the index |
| `qmd_strategy` | `fused` | `local`/`ssh` only: `fused` (vsearch+BM25 RRF), `hybrid` (`qmd query`: expansion+rerank, slow), `fts` (BM25 only) |

#### Exposure of the http daemon

For `http` mode the daemon must bind `0.0.0.0` — with OrbStack (and Docker
Desktop-style setups) that is the only bind reachable from inside the
container, so the instruction stands. Be clear about what that means: the
daemon is an **unauthenticated, read-only HTTP endpoint over your entire
indexed corpus**, reachable from every device on your LAN (and any Wi-Fi
network the machine joins) unless you scope it.

Concrete mitigation on macOS — scope `:8181` to the OrbStack subnet with
a pf anchor (adjust the subnet to OrbStack Settings → Network):

```
# /etc/pf.anchors/qmd8181 — only the OrbStack subnet may reach the daemon
pass in quick proto tcp from 192.168.138.0/23 to any port 8181
block return in quick proto tcp from any to any port 8181
```

Load it by adding `anchor "qmd8181"` and
`load anchor "qmd8181" from "/etc/pf.anchors/qmd8181"` to `/etc/pf.conf`,
then `sudo pfctl -f /etc/pf.conf -E`. Alternatively, use the macOS
Application Firewall to deny inbound connections to the daemon's binary,
or bind the daemon to the OrbStack bridge interface address if your qmd
version supports an explicit bind address. Don't run the daemon on `0.0.0.0`
on untrusted networks without one of these in place.

**Host prerequisites** — one-time, run where qmd will execute (the
machine itself for `local`, the Mac host for `ssh` — llama.cpp needs
Metal; container CPU is ~50x slower):

```bash
bash plugins/multiplai-context/skills/qmd-search/scripts/setup_qmd.sh \
  --workspace /path/to/workspace --resources-dir /path/to/workspace/RESOURCES
```

The script installs bun + qmd if missing, creates the project-local
`.qmd/` index at the workspace root, adds the collection, indexes and
embeds it, and runs a smoke query. The index lives at
`<workspace>/.qmd/` — for `ssh` mode host and container must see the
workspace at the **same absolute path** so the container-side hook
resolves the same index. Add `.qmd/` to the workspace `.gitignore`.

Container setups additionally need the qmd allowlist in the host
SSH-bridge gateway (`container-build-gateway.sh` from
multiplai-container) deployed to `~/.local/bin/` on the host.

Retrieval is fail-open (any qmd error, timeout, or missing binary means
"no resources this turn", never a blocked prompt), and injected files
respect the same re-recommendation cooldown as router picks. A
session-start child (`scripts/qmd_refresh.py`, flock-guarded, detached)
keeps the index incrementally in sync; the `qmd-search` skill covers
manual/deep searches.

## Context checkpointing (long sessions)

Long sessions degrade as the context window fills. The checkpoint system
(MiMo-style) lets one *logical* chat span many *physical* context windows:

1. **Measure** — after every assistant turn, the Stop hook reads the real
   context footprint from the transcript tail (`input + cache_read +
   cache_creation` tokens of the last main-chain assistant message).
2. **Checkpoint** — crossing a token band (default **100K / 200K**) spawns a
   detached writer that distills the transcript and produces a structured
   11-field `checkpoint.md` (intent, next action, constraints, task tree,
   current work, involved files, errors+fixes, discoveries, runtime state,
   decisions, notes). Incremental: later writes merge only the new turns
   into the previous checkpoint. Above the handoff threshold the checkpoint
   auto-refreshes every `checkpoint_refresh_tokens`, so marathon /goal
   sessions always have a current one.

   Bands are a poor trigger for a tab you *stopped* using: a session that
   sat at 40K tokens for three days crosses nothing and so has no
   checkpoint at all — precisely the session you have most lost track of.
   So there is also an **age** trigger: once a session is older than
   `checkpoint_min_session_minutes` and its last checkpoint is older than
   `checkpoint_stale_hours`, the next Stop writes one. A session whose age
   cannot be read does *not* trigger it — unknown age is not evidence of
   staleness, and band-only is the safe default.
3. **Handoff** — at/above the handoff threshold (default **200K**) a pending
   marker is written for the session's project.
4. **Rebuild** — the checkpoint is injected into the fresh context window as
   additionalContext (task tree, next action, file list intact). Two paths:
   - **Automatic (recommended):** steer native auto-compaction to fire near
     the handoff threshold (see *Activation* below). Compaction resets the
     window mid-session — same session id, same terminal, `/goal` loops and
     session-scoped hooks survive — and `SessionStart(source="compact")`
     injects the checkpoint right after the compaction summary. Zero user
     action, works unattended in autonomous sessions.
   - **Manual fallback:** without the auto-compact steering, the user sees a
     `systemMessage` advising `/clear` or `/compact` (one command, no
     restart), and Claude gets a per-prompt notice to finish cleanly and
     suggest it at a natural boundary. The `/clear`-created session (within
     `checkpoint_ttl_hours`) consumes the marker. Deliberate continuations
     only: a plain NEW session in the project (source `startup`/`resume`)
     never inherits the parked checkpoint — soft continuity for those comes
     from the `now/` project-state injection instead.
5. **Retire** — a checkpoint is live state; the diary is the permanent
   record. Once extraction has written a diary entry for that session, the
   checkpoint says nothing the diary does not, so `data/checkpoints/<sid>/`
   is deleted. Four things keep one alive: the session is `parked`, the
   diary write produced nothing, a writer is still in flight, or an
   unconsumed rebuild marker still points at it — that last one is the
   walk-away case (cross the handoff threshold, close the tab instead of
   `/clear`ing), where the checkpoint is exactly what tomorrow rebuilds
   from.

### Activation: fully-automatic rebuild

Add to `settings.json` (or export in your launcher) so native auto-compaction
fires at ~200K instead of near the model window limit. Sharp edge: at the
user level (`CLAUDE_CONFIG_DIR`), the `env` block of **`settings.local.json`
is silently ignored** — only `settings.json`'s env lands (verified
empirically on CLI 2.1.207; `settings.local.json` is a project-level overlay
for env). Put these in the tracked `settings.json` or export them from the
launcher:

```json
{
  "env": {
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "250000",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"
  }
}
```

(250000 × 80% = trigger ≈ 200K, matching `checkpoint_handoff_tokens`.) The
hooks detect these vars and suppress all `/clear` nagging — the loop becomes:
checkpoint at 100K → refresh → auto-compact ≈200K → checkpoint auto-injected →
repeat. If compaction is overdue (vars set but it never fired), the hooks
resume warning the user.

Native semantics (extracted from Claude Code v2.1.201 and field-verified;
re-check on CLI major upgrades):

- Window clamped to **[100000, 1000000]**, and — the sharp edge — an
  env-configured window **below 200000 silently DISABLES soft auto-compact**
  instead of lowering the trigger.
- Actual trigger = `min(usable × pct/100, usable − 13000)`, with
  `usable = window − min(CLAUDE_CODE_MAX_OUTPUT_TOKENS, 20000)`.
- The recommended production pair (250000 / 90) → trigger ≈210K.
- Lowest reliable test trigger: window `200000` + pct `45` → ≈83K.

`autocompact_trigger_tokens()` in `lib/checkpoint.py` mirrors this formula
(including the 200K disable gate, reported as "auto mode off") so the
overdue warning and nudge suppression track native behavior exactly.

**Removed in 0.32.0: the summary-stub directive.** Up to 0.31.0 the
PreCompact hook used a real CLI affordance — a hook's stdout is appended to
the compaction prompt as custom instructions — to ask the summarizer for a
one-sentence stub instead of a multi-KB summary, on the grounds that the
injected checkpoint already carries the state. The channel works. The
directive did not.

To outrank the summarizer's own instructions it had to be phrased as a
priority override telling the model to disregard them, and that is
indistinguishable, from inside the summarizer, from a prompt injection
embedded in the conversation being summarized. Sessions declined it and
produced the full summary anyway; one reported it to its user as a live
injection attack against their own tooling — the correct call on the
evidence it had. Both outcomes cost more than the summary saved. Steering a
model by impersonating an authority it is trained to distrust does not get
more reliable with better wording, so the hook now prints nothing at all and
compaction produces its normal summary.

What survives is the part that was doing real work: the pending rebuild
marker, still written on every compaction with a valid checkpoint, so a
**manual `/compact` below the handoff threshold** still gets the checkpoint
re-injected (session_start additionally falls back to the session's own
checkpoint on `source="compact"`). Freshness gating went with the directive
— it existed to stop a stale checkpoint *replacing* the summary, and the
summary is no longer being replaced.

If compaction cost is what you are trying to avoid, the answer is to not
compact: hand off at the threshold instead, and set
`checkpoint_hard_stop_tokens` if advice alone does not get you there.

Why compaction (not `/clear`) is the automatic path: hooks cannot invoke
slash commands, so a hook-triggered `/clear` is impossible — but the
auto-compact *threshold* is steerable via env, and compaction both preserves
the session (id, session-scoped hooks, terminal) and fires SessionStart with
`source="compact"`, which is a supported context-injection point.

### Turning auto-compaction off instead

The steering above is one of two supported shapes. The other is to disable
native auto-compaction outright and hand off at the threshold every time —
no summary, no summarized-context degradation, one `/clear` per window:

```json
{
  "env": { "DISABLE_AUTO_COMPACT": "1" },
  "autoCompactEnabled": false
}
```

Either alone is enough (the CLI checks `DISABLE_COMPACT`, then
`DISABLE_AUTO_COMPACT`, then the setting). Manual `/compact` still works —
these gate the *automatic* trigger only.

`autocompact_trigger_tokens()` detects all three, so the plugin reports
"auto mode off" and resumes its handoff advice rather than waiting for a
compaction that will never fire. Leaving the window/pct pair in place
alongside a disable is fine and expected — the disable wins. One limit
worth knowing: the env vars are always visible to a hook, but
`autoCompactEnabled` is read best-effort from the **user-level**
`settings.json` only ( `$CLAUDE_CONFIG_DIR/settings.json`, the file the
`/config` toggle writes). Set the env var if you want the detection to be
certain.

**What this costs you:** with the automatic trigger gone, nothing acts on
its own. The handoff nudges are advice, and the next real wall is the
model's actual context ceiling — `CLAUDE_CODE_AUTO_COMPACT_WINDOW` does not
move it, since it only ever fed the compaction trigger. That is what the
next section is for.

### Enforcing the handoff: `checkpoint_hard_stop_tokens`

Everything above is advice. Advice is enough when auto-compaction is doing
the rebuilding, because something else eventually acts. It is not enough
when auto-compaction is **disabled outright** (`DISABLE_AUTO_COMPACT=1` or
`autoCompactEnabled: false`), a reasonable choice if you would rather hand
off cleanly than be summarized: with it off, nothing sits between the
handoff threshold and the model's real context ceiling, and a session
sails on into exactly the degraded zone this system exists to avoid.

Set `checkpoint_hard_stop_tokens` and the per-prompt nudge stops asking.
Above the threshold it returns `{"decision": "block"}` and the prompt is
never sent — the CLI shows the reason instead, naming the token count and
what to do about it.

Three carve-outs, because a hook that can refuse every prompt needs a door
that does not require editing config from inside the blocked session:

- **Slash commands always pass.** `/clear` and `/compact` are what the
  block is asking for; blocking them would be a locked room.
- **`!keepgoing` anywhere in a prompt overrides it**, for one
  `checkpoint_refresh_tokens` band of growth — long enough to finish a
  thought, short enough not to be a silent opt-out.
- **Any failure falls through to not blocking.** A hook that cannot read
  its own state must not be the reason a session stops working.

Child sessions are never blocked (a stopped subagent strands its parent),
and the block is off unless you set it: it changes what a session does when
you talk to it, which is not a default anyone should inherit.

Safety properties, by construction:

- The Stop hook **never emits a `decision`** — it cannot block a Stop, so
  `/goal` loops and other Stop hooks are unaffected. The hard stop is the
  deliberate exception and lives elsewhere on purpose: it blocks a
  *UserPromptSubmit*, which stops the human adding work, not the agent
  finishing it.
- Child sessions (subagents, nested hook sessions) are excluded — a research
  subagent's own giant context never triggers checkpoints, and its sidechain
  usage records are ignored when measuring the main session.
- The writer runs detached with the standard isolation bundle
  (`setting_sources=[]`, `strict-mcp-config`, `_HOOK_CHILD_SESSION=1`) — it
  can't recurse into hooks or goals.
- Writer failure leaves the previous checkpoint in place; checkpointing is
  always best-effort and never blocks the session.

| Option | Default | Purpose |
|--------|---------|---------|
| `checkpoint_enabled` | `true` | Master switch for the checkpoint system |
| `checkpoint_tokens` | `100000,200000` | Comma-separated checkpoint bands (absolute tokens) |
| `checkpoint_handoff_tokens` | last band | Threshold where handoff advice + pending marker kick in (clamped to ≥ last band) |
| `checkpoint_refresh_tokens` | `25000` | Above the handoff threshold, re-checkpoint every this many tokens of growth |
| `checkpoint_hard_stop_tokens` | `0` (off) | Above this, refuse new prompts until the user hands off. `0` keeps the handoff advisory (clamped to ≥ handoff threshold) |
| `checkpoint_stale_hours` | `0.5` | Age trigger: re-checkpoint when the last one is this old. `0` disables it, restoring size-only triggering |
| `checkpoint_min_session_minutes` | `30` | Minimum session age before the staleness trigger applies |
| `checkpoint_ttl_hours` | `6` | Pending rebuild marker expiry (unrelated to `stale_hours`) |
| `checkpoint_gc_days` | `7` | Collect a session's checkpoint once nothing has touched it for this long. `0` keeps every checkpoint indefinitely |
| `checkpoint_timeout_s` | `600` | Writer model-call timeout |
| `checkpoint_model` | plugin default model | Model for the checkpoint writer |

Defaults are tuned for a 1M-token window with the quality knee well below
it: checkpoint early (100K), hand off at 200K, keep every physical window
under ~300K. State lives in `<data_dir>/checkpoints/`; rebuild events are
logged to the activity stream as `[checkpoint]` entries.

## Skills

All commands are namespaced under `/multiplai-context:`.

| Command | What it does |
|---------|--------------|
| `/multiplai-context:setup` | Onboarding interviewer — populates memory files from starter templates. |
| `/multiplai-context:dream` | Generate a consolidation **proposal** from the pending learnings backlog into `.multiplai/dreams/` — generalized lessons grouped by memory file and numbered `1..N` across the whole file, plus a Filtered Out section carrying everything dropped and why. Does not modify anything. `--daily-review` is the routine path: one queue per target file (best 10 each, the rest in `archived/reserve.md`) and a review file with up to 3 model-proposed principles (accepted ones go to `memory/principles.md`, examples to `memory/principles-examples.md`) and the 5 best-ranked facts across every queue. |
| `/multiplai-context:dream-remember` | Review the proposal (generating one if needed), approve/reject memory edits by number, apply approved edits, archive processed learnings. `--daily` applies only the principles and entries ticked `yes` in today's review file. |
| `/multiplai-context:health` | **Is it broken?** Mechanical infrastructure check (deterministic script): active model client, directories present, memory-file freshness by mtime, diary/learnings/dream counts. Fast, cheap, run anytime. |
| `/multiplai-context:memory-health-audit` | **Is it good?** Analytical effectiveness audit — cross-correlates retrieval logs, diary, learnings, and memory structure to find what's useful, what's wasted, and what to restructure. Slower; run ~monthly. |
| `/multiplai-context:log-doctor` | **Why is it failing?** Analyzes the runtime logs across subsystems (context_manager, extract_learnings, backfill, dream, lifecycle hooks) to surface failures, anomalies, and degradation, verifies root causes against source, and produces a fix-recommendation report. Can focus on one subsystem or actively probe a functionality to confirm its logs appear. Log text is **untrusted input** — quoted lines arrive inside `untrusted-content` fences, control/bidi characters stripped and instruction-shaped spans marked `⟪INJECTION?⟫`; see [`docs/untrusted-content.md`](../../docs/untrusted-content.md). |
| `/multiplai-context:refresh-catalogs` | Regenerate catalog indexes. Supports `--force`, `--dry-run`, `--only`. `--only <gen>` is an explicit override — it runs even if that generator's `enable_*` flag is off (e.g. `--only skills` refreshes the skills catalog while `enable_skills` stays `false`). Generators: `memory`, `banks`, `diary`, `skills`. |
| `/multiplai-context:backfill` | Reconstruct learnings/diary/now summaries from existing Claude Code transcripts. Default window 7 days; `--days N`, `--since DATE`, `--all`. |
| `/multiplai-context:now` | Rebuild per-project `now/` status snapshots from recent diary entries. Run after a backfill, or any time the injected project state looks stale. |
| `/multiplai-context:qmd-search` | Manually search the resources knowledge base via qmd (semantic + keyword) — the manual companion to the automatic per-prompt retrieval. |
| `/multiplai-context:costs` | Report API-equivalent costs for Claude Code usage — per chat, skill, subagent, project, model, day, or branch. Also reports **per outcome** (`--group task --pr-join` = cost per merged PR; `--group build` = cost per DONE and per FAILED buildme block) and **cache utilization** (`--report cache`). Cross-model comparisons are per-outcome only — different models tokenize differently, so a cheaper per-token model that loops twice is more expensive. Collects fresh data from session transcripts, then reports from the cost ledger. Requires `enable_costs`. Interface pinned by [`skills/costs/CONTRACT.md`](skills/costs/CONTRACT.md). |
| `/multiplai-context:fleet-status` | **What is actually blocked on me?** One ranked snapshot of everything in flight — agent sessions waiting on an answer, open PRs with CI and review state, dirty or unpushed checkouts, background jobs, and the pending backlog. Ranked by what needs a decision only you can make (approved PR → red CI → stacked PRs → a session that asked a question → collisions), capped at 8 items, with the full report one `--full` away. Read-only: it merges nothing, kills nothing, deletes nothing. Needs `git`; `gh` is optional and its absence reports "not read", never "none". |
| `/multiplai-context:memory-bank` | Work with **shared memory banks** — git repositories of memory files a team or household shares. `list` and `sync` subscribed banks, `check` for cross-bank collisions, `contribute` a dream item as a pull request on the bank, and `adopt` — the migration that moves personal memory into a bank and deletes the local copy. `contribute` and `adopt` are dry-run without `--apply`; adoption never deletes a file the bank does not already contain line for line. See [Shared memory banks](#shared-memory-banks). |
| `/multiplai-context:config-audit` | **Subtractive** config/rules review on a **60-day** cadence (pinned to how often models ship, not to how fast config rots — what the audit removes is scaffolding a newer model no longer needs) — enumerates the active config surface (global + workspace `CLAUDE.md`s, `settings.json` env/permissions, hook registrations, memory-file standing rules), classifies each rule as still-serving / obsolete / model-constraining, and writes a removals-first proposal to `.multiplai/dreams/config-audit-YYYY-MM-DD.md`. Never applies changes; stamps `config_audit_state.yaml` to close the SessionStart nudge gate. |

## Where your data lives

Everything stays on your machine under `<workspace>/.multiplai/`
(or `~/.multiplai/` if `workspace_dir` is unset):

| Subdir | What's in it |
|--------|--------------|
| `memory/` | Your memory files. You edit these directly. Includes `prospective.md` — [intentions that fire later](#prospective-memory--intentions-that-fire-later). |
| `diary/YYYY-MM-DD/` | One file per session — a narrative of what happened. |
| `learnings/` | Extracted insights pending consolidation. |
| `now/` | Per-project current-state summaries. |
| `data/` | Runtime state — catalogs, logs, session state, gate stamps (`dream_state.yaml`, `config_audit_state.yaml`, `maintainer_state.yaml`). Disposable; recreated as needed. Two files here are worth knowing by name: **`rejections.jsonl`**, the append-only record of every proposed memory entry the judge refused ([what may be written](#what-may-be-written-without-you-reading-it)), and `judge_cache.json`, which keeps those verdicts stable across re-runs — delete it to have everything re-judged. `doctor_state.json` caches the [memory doctor](#the-memory-doctor)'s per-file contradiction results; delete it to have every file re-checked. |

Delete any of these any time; the plugin recreates what it needs. If
`.multiplai/` lives inside a git repo and you don't want diary/learnings
tracked, add to `.gitignore`:

```gitignore
.multiplai/diary/
.multiplai/learnings/
.multiplai/data/
```

Memory files are the one thing worth tracking — see the next section.

## Dev references — engineering standards, not memory

If `$CLAUDE_CONFIG_DIR/reference/dev/` exists, Claude is told which of those
docs apply to the project you are working in. multiplai-kit ships that directory
(uv/Python, Django/DRF, React/Next.js, Swift, FastAPI, Docker, auth, …); you can
also just create it and drop your own house standards in.

**Why this is not the router's job.** Memory is context *about you*, so
relevance to your wording is the right way to pick it. A standards doc applies
because of what the project **is** — a Django app needs the Django standards
whether you said "fix the serializer" or "the API is returning 500s". Routing
them by prompt similarity would make adherence depend on phrasing, which is
exactly the failure this replaced: the standards were previously loaded only by
a prose instruction in `CLAUDE.md`, i.e. whenever the model happened to notice.

**How the project is found.** From your cwd, the nearest ancestor holding a
manifest (`pyproject.toml`, `requirements.txt`, `package.json`, `Package.swift`,
`Cargo.toml`, `go.mod`), stopping at `$HOME`. If cwd is a workspace root that
holds many repos and has no manifest of its own — `knowhere/PROJECTS/<name>/…` —
path-like tokens in your prompt are resolved instead, so `fix PROJECTS/site/api`
finds `site`. Prompt text is only ever used to resolve a path that must already
exist on disk.

**Stack → docs.** Manifest filenames give the stack; frameworks have to be read
out of the dependency lists, because a Django app and a plain library are both
`pyproject.toml`:

| Detected | Docs named |
|---|---|
| `pyproject.toml` / `requirements.txt` | `uv-python-best-practices.md`, `python-project-structure.md` |
| `manage.py` or a `django` dependency | `django-drf-best-practices.md` |
| a `fastapi` dependency | `fastapi-best-practices.md` |
| `package.json` | `bun-vite-react-best-practices.md` |
| a `react` or `next` dependency | `react-nextjs-best-practices.md` |
| `Package.swift` | `swift-best-practices.md`, `swift-testing-strategies.md` |
| `Cargo.toml`, `go.mod` | (none yet) |

The map is `STACK_DOCS` in `scripts/lib/reference_docs.py`. A name with no file
on disk is skipped and logged — **renaming a doc without updating the map
silently removes it from every session**, which is how the Django and React
entries went dead for a month. buildme keeps a parallel map
(`_DEFAULT_REFERENCE_DOCS` in multiplai-dev) for the specs it generates; both
are listed in the kit's `reference/dev/README.md` as the renaming contract.

**What is injected: pointers, not contents.** A `DEV REFERENCES` block naming
each doc's absolute path and its section index, ~60 tokens. The Django doc alone
is 60k chars — inlining it every turn would crowd out the conversation it is
meant to inform. Claude holds `Read` and only needs to know the doc exists and
what is in it.

```
=== DEV REFERENCES ===

Engineering standards that apply to site because of its stack. …

- /home/you/.claude/reference/dev/django-drf-best-practices.md
  Sections: Project layout · Settings & secrets · ORM & queries · Migrations · …
```

**Cadence.** Once per session per project, re-announced after 30 turns so a
compaction (which silently drops the earlier block) doesn't leave the session
flying blind. At most one project per turn.

**Off switch:** the **Dev Reference Injection** (`enable_dev_references`)
option. It already no-ops when `reference/dev/` doesn't exist — no warning, no
error, nothing in the way of a vanilla install.

**Where it is not the mechanism.** Inside a buildme run the standards are
inlined into the spec-generation prompts instead (that generator has no tools,
so a pointer would be useless to it) — see the multiplai-dev README. These two
are independent: this one covers ordinary sessions, buildme covers builds.

## Where your memory lives

By default `memory_dir` is under `.multiplai/` with no version control.
Over time memory accumulates and a single bad write can erase state that
took months to build.

**Recommended: point `memory_dir` at a git repository.**
`/multiplai-context:setup` detects whether your chosen `memory_dir` is inside a
git repo and offers to `git init` it. Once tracked, `/multiplai-context:dream`
(in `--auto` mode) commits memory changes after each consolidation so you
always have a recoverable history. Auto-commit is scoped to memory
markdown files, so it won't sweep unrelated work when memory lives inside
a larger repo. If `memory_dir` isn't a git repo, auto-commit is skipped
with a log warning and everything else keeps working.

### Section-level retrieval (`file.md#Section`)

A memory file that grows past a hundred kilobytes stops being one thing. Ask
about your release process and routing would hand Claude all thirty sections of
it — twenty-nine of them irrelevant, all of them charged to your context window.

Catalog generation writes a **`section_anchors`** list for every memory file
that is at least **8 KB** and has at least **three `##` sections**:

```json
"section_anchors": [
  {"name": "Container Runtime", "gloss": "OrbStack container, host bridge, no-docker rule"},
  {"name": "Release Flow",      "gloss": "dev vs runtime checkouts, release.sh, the version pin"}
]
```

The router reads those one-line glosses and, for an anchored file, naming
sections is its **default**: it answers `multiplai.md#Release Flow` rather than
`multiplai.md`, listing as many sections as the prompt needs, and falls back to
the bare filename only when most of the file is relevant. On a 180 KB file with
30 sections that is roughly 6 KB in place of 180. Measured over 17 routing
prompts against a 921 KB corpus, total injected memory fell **68%**
(814 KB → 260 KB).

**Cost, if you run `memory_router: llm`:** the catalog the router reads roughly
doubles, because every section carries a description — 30 KB → 61 KB on a
29-file corpus — and the reply is longer. That showed up as median routing
latency of 22 s against 16 s before, and one 35-section file took 84 s. The
router gives up at **25 s** and injects nothing for that turn, so on a corpus
with very large files you may see more of those. The default `token_overlap`
router makes no model call and is unaffected — anchors simply sit unused.

Four things keep it safe:

- **The names in the catalog are not written by a model.** They are read off
  your file's `##` headers in code and handed to the model as a fixed list to
  describe. What the router *answers with* is the model's own text, though, and
  only the filename half of `file.md#Section` is checked against the catalog —
  so a fragment naming a section that has since been renamed does reach the
  loader. That is the case the next bullet covers.
- **A wrong anchor costs one whole-file load, and says so.** If a name matches
  no header — a stale catalog, a section you renamed — the loader returns the
  **whole file**, which is what happened before this existed. No prompt comes
  out with less context than it would have had. Several stale anchors on one
  file still load it once, and the loader logs a warning naming each anchor
  that did not resolve; run
  `/multiplai-context:refresh-catalogs --only memory` after renaming a section.
- **Anchors are regenerated, not remembered.** Unlike `sections`, `bundle` and
  `co_retrieve_for`, they are re-derived whenever the file's content changes.
  Hand-editing them is pointless: your edit is replaced on the next rebuild of
  that file.
- **The fields that *are* remembered now say so when they are lost.** `sections`,
  `bundle`, `co_retrieve_for`, `intent_domains` and `anti_domains` have no source
  outside the catalog — no model emits them and no memory file contains them — so
  a value that disappears is gone until someone retypes it. Each generator
  declares its `preserved_fields`, and a regeneration that empties one logs
  `HAND_FIELD_LOST` and returns it in `GenerationResult.errors` instead of
  finishing quietly.

### Bundles and co-retrieval pull *whole* files

`expand_bundles` and `expand_co_retrieve` add a companion by bare filename, so a
companion is injected whole even when it has anchors of its own. On a corpus
where the largest companion is 140 KB with 38 sections, that undoes anchoring for
every turn it fires. Measure before you author these fields: on one 514-turn
sample, the four documented `bundle: writing` members plus three `co_retrieve_for`
pairs would have added **3.7 MB** of whole-file injections — against 32.8 MB of
whole-file bytes in the same window. Bundle a set of *small* files, or leave the
fields unset and let the router pick sections.

Files under either threshold — fewer than 3 `##` sections, or smaller than
2 KB — get no anchors at all. That is the intended outcome, not a failure: the
floor exists so a file smaller than its own anchor block does not get indexed.

**That floor was 8 KB and it was too high.** On a real 28-file corpus it left
seven files with no anchors, every one of which had 3–10 sections and failed
*only* the size bar — the largest by 162 bytes, while being injected whole 57
times. The two sides of the trade are not the same currency: an anchor line is
~75 bytes in the *router's* prompt (small, stable, cheap-model input), while a
whole-file injection lands in the main session's own context window. Paying
router bytes to save session bytes stays a good trade well below 8 KB.

**One thing you have to get right yourself:** `##` section names must be unique
across *all* your memory files. Two files both headed `## Overview` make
`#Overview` an ambiguous request. `memory_lint` reports collisions (below); it
will not rename anything for you.

### Utilisation — which memory is earning its place (all numbers ESTIMATED)

Section-level retrieval tells you what got *loaded*. It says nothing about what
got *used*, and those are not the same question: a section injected on every
prompt and relevant on none of them looks maximally valuable counted by
retrievals. Nothing can observe use directly — so this plugin estimates it two
independent ways and shows both, side by side, never blended.

| Estimator | How it works | Its bias |
|---|---|---|
| **self-report** | The end-of-session extraction pass already reads the whole transcript; it is also handed the list of sections that were injected and asked which it relied on, **with evidence**. No extra model call. | Over-reports — the model is grading its own session. A claim with no evidence is recorded `supported: false` and never counted. |
| **offline judge** | A separate cheap-tier pass in `memory_maintainer` compares the injected list against a distilled transcript, sampled (5 sessions/run by default). | Independent of the session's own reasoning, but sampled, so it accumulates slowly and is absent for most sessions. |

Records accumulate in `<data_dir>/utilisation.jsonl` — append-only, one record
per session, rewritten atomically. Read the table with:

```bash
uv run --project scripts scripts/utilisation_report.py          # human table
uv run --project scripts scripts/utilisation_report.py --json   # machine-readable
```

`/multiplai-context:memory-health-audit` renders the same table as part of its
report. It is ranked by **bytes injected per estimated use** — cost per value,
so the expensive-and-unused rows sort to the top.

**How to read it, and how not to.**

- **Every number is an estimate, not a measurement**, and the surface says so
  everywhere. Two model-based estimates with opposite biases, kept apart on
  purpose: there is no single "utilisation score" here and adding one would be
  fabricated precision.
- **Disagreement is a finding.** Where the two estimators' use rates differ by
  more than 35 percentage points the row is marked `!`. That is information
  about how much to trust either one — it is not averaged away.
- **`n` is shown, and thin rows are not ranked.** A section with three
  observations is not evidence of anything, so it appears under *insufficient
  data* rather than at some flattering or damning position in the ranking.
- **"Never retrieved" is a separate list** from "retrieved and unused". A
  section that never reached a prompt has no evidence either way about its
  value; that is a routing question, not a pruning one.
- **"Candidate" means a suggestion to a human, never an automatic deletion.**
  Nothing in this plugin prunes memory from this table. It is evidence for you
  (and for a future doctor pass) to act on; `/multiplai-context:dream-remember`
  remains the only path that edits memory.
- **A blank is not a zero.** A missing judgement means *not judged* — during an
  outage the judge writes nothing rather than a verdict, precisely so a bad
  night cannot make the whole corpus read as dead weight.

Configure the sample size with `utilisation_judge_sample` (default 5); set it
to `0` to turn the judge pass off entirely, leaving self-report as the only
estimator.

### Fact-level freshness (`as of` / `review by`)

A memory file carries one `**Last Updated:**` stamp. That says when the *file*
was touched — it says nothing about whether a particular fact inside it is still
true, and the facts that rot (a price, a version, an employer, "the current best
X") each rot on their own schedule while the file around them stays accurate.

The convention is one suffix on the fact itself:

```markdown
- Container image is on v0.4 (as of 2026-07)
- Flat-tax regime caps at €85k (as of 2026-07, review by 2026-10)
```

Both dates take `YYYY-MM` or `YYYY-MM-DD`, and **a month means the END of that
month**: `review by 2026-10` is not overdue on 2026-10-01, because the whole
month is the review window. Treating it as the 1st would make every
month-granular annotation fire up to 31 days early, which is the kind of steady
early noise that gets a linter switched off.

A linter (`scripts/lib/memory_lint.py`) reports four kinds of finding. Three are
about staleness — `expired` (a `review by` date that has passed), `undated` (an
`as of` stamp more than a year old with no `review by` at all, so nothing can
ever expire it) and `unmarked` (a volatile-class fact with no annotation
whatsoever). The fourth, `duplicate-h2`, is about retrieval: it names any `##`
section title that appears in more than one memory file, which is what makes a
`file.md#Section` pick ambiguous (see
[Section-level retrieval](#section-level-retrieval-filemdsection)). It is
**warn-only and never rewrites a file**:
volatile-class detection is heuristic, so a false positive must cost one noisy
line in a report, never a silently edited fact. Findings surface in
`/multiplai-context:health` (as `memory_validity`, with collisions under
`memory_validity.duplicate_h2`) and in the maintainer's report, and
`/multiplai-context:dream` asks for the annotation on newly proposed volatile
lines.

## Shared memory banks

A **bank** is a git repository of memory files. Point the plugin at one and its
files route and inject exactly like your own — the router picks a team file over
a personal one on relevance alone, because relevance is relevance. Contributions
go back as pull requests. There is no server, no sync protocol and no shared
runtime: sharing is `git pull`, contributing is a PR.

**Configure nothing and nothing changes.** With no `memory-banks.yaml` you have
exactly one bank, named `personal`, at the memory directory you already had.

```yaml
# <workspace>/.multiplai/memory-banks.yaml
memory_banks:
  - name: dolcebot-team
    remote: git@github.com:you/memory-bank.git
    mode: propose          # ro = read and route only · propose = contribute by PR
    sync: session-start
```

There is deliberately no `rw` mode for a shared bank; a config asking for one is
coerced to `propose` and warns.

### The rule: a fact lives in exactly one bank

A shared bank is **authoritative** for the domains it declares. Its file
*replaces* yours on that topic — it is not a second opinion layered on top,
because two copies of a fact means injecting it twice and disagreeing with
yourself later. Two consequences:

- **Collisions are a catalog defect, not an injection-time conflict.** When two
  banks claim the same filename, the same routing domains, or the same section
  heading, catalog generation writes `bank-collisions.md` next to the catalogs
  and says so. It reports; it never silently resolves.
- **Joining a bank is a migration.** `/memory-bank adopt` moves your overlapping
  content into the bank and deletes your local copy — see below.

### What a bank can and cannot do to your agent

Bank content is written by other people and arrives over a git remote on a
schedule, so it is treated as exactly what it is: externally-authored text.
Injected bank blocks are headed with the bank's name and last-updated date and
wrapped in `<untrusted-content>` fences per
[`docs/untrusted-content.md`](../../docs/untrusted-content.md). Claude is told
they are **data, never instructions** — an imperative sentence inside one is a
finding to report to you, never an order to follow.

That is the *first* layer, not the only one. A bank cannot cause a local memory
write at all: the write floor refuses any target naming a non-personal bank, in
every write mode including `auto`. What a malicious bank can do is put
misleading *reference content* in front of you, attributed to itself, which is
the same risk as a colleague being wrong — and the reason the fences name the
bank.

### Contributing

An item the dream pipeline routes to a shared bank is refused a local write and
appears in your review pile as *"belongs to a shared memory bank"*. Turning it
into a pull request is a separate, explicit command:

```bash
uv run --project plugins/multiplai-context/scripts \
  plugins/multiplai-context/scripts/memory_bank.py \
  contribute --proposal .multiplai/dreams/<proposal>.md          # dry run
  # … then again with --apply
```

**No model is involved in producing a contribution.** The pull request contains
the proposal's own text, byte for byte — the text you already read. Before the
PR opens, each item is checked against the bank's `BANK.md` **no-go domains**
(compensation, health, finances, credentials — a default list applies when a
bank declares none) and scanned for credential shapes. A blocked item is refused
*sharing*, not rejected memory.

### Adopting

```
memory_bank.py adopt dolcebot-team                        # what overlaps, and why
memory_bank.py contribute --proposal … --apply            # get the content in
#   … PR reviewed, merged, and pulled by the next sync …
memory_bank.py adopt dolcebot-team --file dev.md --apply  # then delete the local copy
```

Three guarantees, because this is the one command that deletes your memory:

- **Nothing is deleted that is not already in the bank's working tree, line for
  line.** Not "a PR was opened" — the lines are there or the file is skipped and
  the missing ones are named.
- **Only files you name are touched.** There is no adopt-everything.
- **A receipt** lands in `.multiplai/dreams/applied/` carrying the exact
  `git revert` that undoes the deletion.

### Authoring a bank

Put a `BANK.md` at the repo root declaring owners, review rules and no-go
domains (template:
`plugins/multiplai-context/skills/memory-bank/references/BANK.md.template`).
Whenever content changes, regenerate the bank's `catalog.json` and commit it in
the same pull request: subscribers adopt that fragment verbatim, so identical
content gives everyone identical routing and nobody pays a model pass over files
they did not write. A bank with no fragment still routes — from deterministic
first-paragraph summaries — and says so on every catalog rebuild.

**Nothing runtime is ever shared.** Diary, learnings, dreams, costs,
checkpoints, session state and logs stay local. A bank carries curated memory
and nothing else.

## Prospective memory — intentions that fire later

Every file under `memory/` answers *what is true*. `memory/prospective.md`
answers *what did I say I'd come back to* — the September re-check, the "when
the runtime updates, re-run the audit", the deadline three weeks out. Before it
existed those lived in the transcript of whichever session mentioned them, i.e.
until that session ended.

One intention per line, in one of two shapes:

```markdown
- [due: 2026-09-01] Re-check the tax residency rule (captured 2026-07-26)
- [on: the runtime moves past v0.5] Re-run the config audit (captured 2026-07-26)
```

The distinction is the point:

| Kind | Machine-checkable? | How it reaches you |
|------|:---:|---|
| `due:` | yes | A `SessionStart` nudge from **a week before** the date, repeated while overdue |
| `on:` | **no** | Normal memory routing when a prompt touches the topic, plus a 30-day re-listing sweep |

`on:` conditions are **never evaluated**. No code decides whether "the runtime
updated" has happened, because a confident wrong guess fires the reminder at the
wrong time — which trains you to ignore the channel.

The 30-day sweep measures **elapsed time since the entry was last actually
shown**, stamped per-intention in `prospective_sweep.json` under the plugin's
data dir (derived state — never in `prospective.md`, which is yours to edit).
So a day with no session *delays* a re-listing instead of skipping it: a sweep
that only fired on exact 30-day multiples lost a whole cycle to one missed day,
on the one memory channel where being silent is itself the failure. Losing the
stamp file costs one duplicate nudge, never a swallowed one.

The file is seeded by `/multiplai-context:setup` from
`templates/prospective.md`. Entries arrive through the normal pipeline
(extraction → dream → `/dream-remember`), not by hand-editing mid-session.
**Nothing expires automatically** — delete an intention once it's been acted on;
a file of dead intentions is a nudge everybody learns to skip. A malformed line
never blocks a session start.

## Proactive maintenance

Everything else in this plugin is *reactive*: routing fires on a prompt, dream
fires when you run it, catalogs rebuild when something asks. So maintenance
happened exactly as often as it was remembered — which, for a background chore,
means "eventually".

`scripts/memory_maintainer.py` is launched **detached** from `SessionStart` and
owns a **24-hour gate**. `SessionStart` checks that same gate *in-process first*
and skips the spawn entirely when it's closed — the child is authoritative, but
reaching it costs a `uv run` startup, and since the maintainer declares its
dependencies in a PEP 723 header a cold `uv` cache turns that into a network
fetch at session start. Eight passes — seven daily, one weekly:

| Pass | What it does | Model call? |
|---|---|---|
| 1. Memory lint | `lib/memory_lint` — expired `review by` dates, unannotated volatile facts, duplicate `##` section names | no |
| 2. Dream proposal | Only when the dream gate is open, a learnings backlog exists, **and** no un-archived proposal is already waiting | yes |
| 3. Catalog refresh | Only when a memory file is newer than the catalog that indexes it | yes |
| 4. `now/` rebuild | `synthesize_now` for the **active project only**, on a **Haiku** tier | yes (cheap) |
| 5. Utilisation retention | Collapses `utilisation.jsonl` records older than 90 days into per-section totals | no |
| 6. Utilisation judge | Rules on a sample of un-judged sessions (`utilisation_judge_sample`, default 5), on a **Haiku** tier | yes (cheap) |
| 7. Triage apply | Judges the waiting proposal and applies what clears — see [What may be written without you reading it](#what-may-be-written-without-you-reading-it). Off under `memory_write_mode=review` | yes |
| 8. **Memory doctor** — *weekly* | Duplication, within-file contradiction and dead weight over the whole corpus, on a **Haiku** tier. Writes a report; see [The memory doctor](#the-memory-doctor) | yes (cheap) |

Passes 1–2 and 8 write to `.multiplai/dreams/` and the health log; 3–4 write
derived files (catalogs, `now/`) that are rebuilt from source and hold no unique
state; 5–6 write only `utilisation.jsonl`, which is telemetry — nothing reads it
to decide what memory *says*.

**Pass 7 is the one that writes your memory files, and until 0.36.0 nothing
unattended did.** That was a real safety property and it was traded
deliberately, so it is worth saying what for. It held only because the review
queue was unbounded — a 194-item proposal costs a whole context window to walk,
so reviews got abandoned partway and the backlog grew instead of shrinking.
"Never writes" is not safety when the alternative is "never consolidates".

Set **`memory_write_mode: review`** to get the old behaviour back exactly; pass 7
then judges nothing and writes nothing. Otherwise what stands in its place is
four things, none of which a model can reach: a rubric in code that `kind: RULE`
never clears, a judge that may only lower an item, a code floor that runs after
the verdict and can only refuse, and a receipt next to a git repository whose
last line is the `git revert` command for the batch.

Pass 6 **fails closed**: a timed-out, rate-limited or unparseable call writes no
verdict at all, leaving that session unjudged rather than judged-unused. A
missing judgement is never counted as "not used" — otherwise one bad night
would mark your whole corpus dead weight. The run logs how many sessions it
sampled out of how many were eligible, and how many kept their default because
a call failed.

It is silent (nothing at session start needs your attention) and best-effort — a
launch failure, a crashed pass, or an unwritable state file costs at most one
duplicate run next session and never delays a session. Unreadable state opens
the gate on purpose: the cost of one extra pass is small and bounded (see
[What it costs](#what-it-costs)), a wedged gate means maintenance that
silently never runs again.

Run it by hand, or check what it would do:

```bash
uv run --project scripts scripts/memory_maintainer.py --dry-run
uv run --project scripts scripts/memory_maintainer.py --force
```

State lives in `<data_dir>/maintainer_state.yaml`; runs appear in the activity
log as `[maintenance]`. It holds **two** independent gate stamps — `last_run`
for the daily passes and `last_doctor_run` for the weekly doctor — so stamping
either leaves the other alone. `--force` opens both.

### The memory doctor

Passes 1–7 each look at one thing at a time. Triage judges one new item against
one file, so it cannot see the failures that only exist *across* the corpus: the
same fact arriving five times over three months in slightly different words
across three files, or a note that quietly contradicts one written in May. Each
arrival was locally reasonable. Nobody was watching the whole.

Once a week, pass 8 does, and writes what it finds to
`.multiplai/dreams/doctor-YYYY-MM-DD.md`. Three sections, every finding numbered
and citing `file:line`:

| Section | How it works |
|---|---|
| **Duplication** | Two stages. Stage 1 splits every file into bullets and paragraphs and shortlists near-identical pairs with `difflib` — stdlib only, no new dependency, and bounded by a rare-token index so a 3,500-block corpus is not 6 million comparisons. Stage 2 asks a model, per shortlisted pair, whether it is the same fact, and if so drafts the merged wording. Only confirmed pairs are reported. |
| **Contradiction** | One call per memory file, asking for pairs of statements that cannot both be true. Content-hash-gated: a file you have not edited since the last run costs nothing, and its earlier findings are carried forward rather than silently dropped. Every quote must be locatable verbatim in the file or the finding is discarded — a paraphrase is not evidence. |
| **Dead weight** | Deterministic, from the [utilisation](#is-injected-memory-actually-used) table. Three findings kept apart because they mean different things: **never retrieved** (no injection record at all — no evidence either way), **retrieved but estimated unused**, and **expensive** per estimated use. |

**It never edits your memory, and there is no flag, mode or config key that
changes that.** Pass 7 writes *additions*, which a receipt records and `git
revert` undoes cleanly. The doctor would be proposing *deletions and merges*,
where a wrong call destroys something no receipt can reconstruct. So it hands
you suggestions with evidence attached. The report is also built so nothing here
can pick it up: it never contains a `## Routing Warnings` heading, which
`dream --triage` requires before it will apply anything, so pointing the triage
path at a doctor report exits non-zero without writing.

Four things keep the dead-weight section from over-claiming, all inherited from
the utilisation contract:

- **Nothing below the sample-size floor is reported**, and the floor is printed
  in the report. A section seen three times is not evidence.
- **A missing estimate is never read as "not used".** A section is only called
  unused when *both* estimators independently observed it enough times and each
  put its use at or below the threshold. The two are never averaged.
- **A section that reads as a behavioural rule is never proposed for removal on
  usage grounds.** Rules are retrieved rarely by construction and are exactly the
  content whose absence you would not notice. They are listed separately as
  withheld, with the reason.
- **When there is not enough telemetry yet, it proposes nothing and says so.**
  That is the expected output for the first weeks, not a clean bill of health.

Two absences the report states outright, so you do not read them as findings:
**cross-file contradiction is not looked for**, and any model call that timed
out or came back garbled contributed **nothing** — with the count shown. With no
model client at all the deterministic dead-weight pass still runs and the other
two report that they were skipped.

### What it costs

Real numbers, not vibes — derived from the author's own cost ledger
(`/multiplai-context:costs` data, July 2026, heavy daily multi-session use,
Sonnet tier, API-equivalent pricing; derived 2026-07-27):

| Unattended work | Typical cost |
|---|---|
| Dream proposal (the expensive one; at most 1/day via the maintainer, or when you run `/multiplai-context:dream`) | **≈ $1.00 per proposal** (median $1.04 over 19 runs; max $1.91) |
| Catalog refresh | **≈ $0.05 per run** (median over 199 runs) |
| Background calls — diary/learnings extraction, `now/` rebuilds, checkpoint writes | **≈ $0.16 per call** (median over 606 calls; ≈ $4/day at the author's heavy usage — expect far less on normal use) |

Your numbers will differ with usage; once `enable_costs` is on, run
`/multiplai-context:costs` to see them.

**Whose quota?** Unattended calls go through the Claude **Agent SDK**, i.e.
the same auth your Claude Code already uses — your subscription quota (or
whatever credential your CLI is configured with). Only if the SDK is
unavailable *and* you set the `anthropic_api_key` option do calls bill that
API key directly. (Verified in `multiplai-core`'s `model_client.py`:
`AgentSDKClient` is preferred; the `anthropic` client is the explicit
fallback.)

**The off-switch.** To stop *all* unattended model calls, disable the
plugin (`/plugin` → manage → disable, or uninstall) — hooks only run while
it's enabled. Granular switches: `checkpoint_enabled: false` stops the
checkpoint writer; `memory_router: token_overlap` (the default) means
routing itself never calls a model; `enable_skills`/`enable_resources`/
`enable_costs` are off by default and add work only when you turn them on.
There is currently no single config flag that keeps the hooks running but
skips extraction/dream/catalog model calls — if no model client is
available at all, those passes no-op with a one-time warning.

## Architecture

### Lifecycle hooks (`hooks/hooks.json`, official Claude Code schema)

| Event | Script | Role |
|-------|--------|------|
| `Setup` | — (`run.sh --warm`) | Build the plugin's Python environment synchronously, once. Fires on `claude --init-only` (and `--init` / `--maintenance` in `-p` mode) — the place to do the full first resolution in CI or a scripted install. Runs no Python. |
| `SessionStart` | `session_start.py` | Init session state; drain deferred extractions; emit the dream-due nudge, the **60-day** config-audit nudge, and any **due prospective intentions**; launch the **memory maintainer** detached (own 24h gate). **Does not** dump memory into context. |
| `UserPromptSubmit` | `context_manager.py` | Route the prompt against catalogs and inject only the relevant memory. |
| `UserPromptSubmit` | `checkpoint_nudge.py` | Tell *Claude* when the session is past the handoff threshold, so it wraps up and suggests `/clear` at a natural boundary (and, with `checkpoint_hard_stop_tokens` set, blocks new prompts past that ceiling). |
| `Notification` | `session_notification.py` | Stamp the session registry with a `notification` event — Claude Code is waiting on the user, which is the session board's `waiting_input` signal. |
| `Stop` | `session_stop.py` | Lightweight checkpoint (extraction is deferred, not run here). |
| `SessionEnd` | `session_end.py` | Write a deferred-extraction marker for a drain to pick up; record the session's disposition. |
| `PreCompact` | `pre_compact.py` | Enqueue a deferred-extraction marker so pre-compaction learnings survive; clear the re-recommendation cooldown map (injected context is summarized away). |

Heavy LLM extraction never runs inside a kill-within-seconds hook: it is
deferred via a marker queue and run by `extract_learnings.py` as a
detached subprocess.

#### Draining the queue

`scripts/drain_extractions.py` is a standalone entry point for that queue,
so it no longer has to wait for a session:

```
uv run --project . drain_extractions.py --data-dir <workspace>/.multiplai/data
```

`--wait` blocks until each extraction finishes with its errors visible;
`--verbose` prints a one-line summary. A container launcher can call it
right after the container exits — which is what turns "closing your last
tab on Friday" into a Friday diary entry rather than a Monday one — and
you can run it by hand when you suspect a session was never written up.
`SessionStart` drains through the same `lib/extraction_drain.py`, so the
two paths cannot drift, and the dequeue is an atomic rename, so a
launcher drain and a fresh session firing together is safe.

The script's own header documents the environment it needs. The one that
bites: `CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR` (or `WORKSPACE`), without
which the diary silently lands in `~/.multiplai/` instead of your
workspace — `--data-dir` fixes the queue's location, not the diary's.

### The life of a session

*One session from launch to the deletion of its last trace, with every
fork marked.*

The parts are documented separately above — checkpointing, extraction,
the registry. This is the single pass through all of them, in the order
things actually happen. No new machinery appears here; it is the map.

```
  launch ─► SessionStart ─►┌─────────────────────┐─► it stops ─► drain ─► extraction ─► GC
                           │  prompt ─► turn ─►  │      │           │           │
              rebuild ◄────┤  Stop / Notification│      │           │           │
                 ▲         └─────────────────────┘      │           │           │
                 └──── window fills ◄───────────────────┘           │           │
                                        diary · learnings · disposition ◄───────┘
```

Every stage below is **best-effort by construction**: a hook that fails
logs and exits 0. There is no state in this system whose loss stops a
session from starting, running, or ending.

#### Stage 0 — before the first hook *(multiplai-kit only)*

`claude.sh` starts a container and blocks on `docker run`. On vanilla
Claude Code this stage does not exist, and the only thing lost is the
host-side drain in Stage 7 — every hook below is identical either way.

#### Stage 1 — `SessionStart`

Fires once per physical context window, **not** once per conversation:
compaction and `/clear` both fire it again. It branches on `source`,
which is the single most consequential fork in the whole lifecycle:

| `source` | What happened | Checkpoint re-injected? |
|---|---|---|
| `startup` | a genuinely new session | **no** |
| `resume` | `claude --resume <id>` | **no** |
| `clear` | you ran `/clear` | yes — consumes the pending marker |
| `compact` | the window was compacted (auto or `/compact`) | yes — marker, or this session's own `checkpoint.md` as fallback |

`startup`/`resume` not inheriting is deliberate, and was decided against
the opposite behaviour after live testing: a fresh session in a project
should not silently wake up inside week-old parked work. Soft continuity
for those comes from the `now/<project>.md` snapshot instead, which is
injected on *every* source.

Then, in this order, always:

1. **GC the registry** (see [When entries are collected](#5-when-entries-are-collected)) — before anything is written, so a
   just-started session is never a GC candidate.
2. **Record the `start` event**, which also clears any `disposition`:
   picking a session back up makes "parked" obsolete by definition, and
   the next extraction re-labels how you left it this time.
3. Detect the model client. **No client → a one-time warning**, and
   every LLM-backed pass downstream silently no-ops.
4. Inject `now/<project>.md`, then the checkpoint rebuild if the source
   allows it.
5. Launch, detached and each behind its own gate: the qmd index refresh
   *(only with `enable_resources`)*, cost collection *(only with
   `enable_costs`)*, the **extraction drain** for markers left by earlier
   sessions, and the **memory maintainer** *(24h gate, checked before
   spawning so 95% of sessions pay nothing)*.
6. Write the fleet view — in-process, since it is a pure read of
   `sessions/` + `checkpoints/` with no model call.
7. Emit whichever nudges are due: dream *(>24h **and** unprocessed
   learnings on disk)*, config audit *(>60 days)*, prospective intentions
   *(their own due date is the gate — no cadence)*.

#### Stage 2 — every prompt you type

Two `UserPromptSubmit` hooks, both fast, neither able to block:

| Hook | Does | Silent when |
|---|---|---|
| `context_manager.py` | routes the prompt and injects only the memory it matches | nothing scores above the relevance cutoff |
| `checkpoint_nudge.py` | tells **Claude** the context budget is nearly spent, so it can finish cleanly and suggest `/clear` at a boundary | below the handoff threshold (the common case), in auto-compact mode, in a child session, or within the cooldown |

#### Stage 3 — every turn's end (`Stop`)

Three things, none of them an LLM call: refresh the liveness timestamp,
record the `stop` event, and run the checkpoint decision. That decision
has exactly three outcomes:

| Condition | Result |
|---|---|
| crossed a token band (default 100K / 200K), or above handoff and grew by `checkpoint_refresh_tokens` | spawn the **detached** checkpoint writer |
| no band crossed, but the session is older than `checkpoint_min_session_minutes` **and** its last checkpoint is older than `checkpoint_stale_hours` | spawn it anyway — this is the tab you left open, which crosses no band |
| context size unreadable (`0` tokens), a writer already in flight, or checkpointing disabled | do nothing |

At or above the handoff threshold it *also* returns a `systemMessage`
advising `/clear`. That advice is suppressed in auto-compact mode unless
compaction is demonstrably overdue. **This hook never emits a
`decision`** — it structurally cannot block a Stop, so `/goal` loops and
other Stop hooks are unaffected.

#### Stage 4 — when it waits for you (`Notification`)

One job: stamp the entry with a `notification` event. That is what makes
the session read as **Needs you** in the fleet view, and it is the hub's
push trigger. No LLM, no state migration.

#### Stage 5 — when the window fills

Four ways out, and which one you get depends entirely on whether
auto-compaction has been steered (see [Activation](#activation-fully-automatic-rebuild)):

| Path | Trigger | Session id | You do |
|---|---|---|---|
| **Auto-compact** *(recommended)* | native compaction near the handoff threshold | unchanged | nothing at all |
| **`/compact`** | you, manually | unchanged | one command |
| **`/clear`** | you, manually | **new** | one command; the new session consumes the marker within `checkpoint_ttl_hours`, and only in the same project |
| **Nothing** | you sail past the threshold | unchanged | keep going — the nudges repeat every `checkpoint_refresh_tokens` and nothing breaks |

On the first two, `PreCompact` fires and does three things in order:
clears the re-recommendation cooldown map (injected context is about to
be summarized away, so every file must become eligible again); writes a
checkpoint **synchronously** — the one place in the system that blocks,
because this is the last moment the transcript exists; and, only if that
checkpoint is both valid and fresh, steers the native summarizer to a
one-sentence stub instead of a multi-KB summary. Any doubt at all — the
writer timed out, the checkpoint is stale, the context size is
unreadable — falls back to the full native summary. It also enqueues an
extraction marker, so learnings survive the compaction that is about to
discard the transcript.

#### Stage 6 — how it stops

Two observers, neither of them the session. It has its own section:
[How the end of a session is detected](#3-how-the-end-of-a-session-is-detected).
In brief: a clean quit fires `SessionEnd` and is recorded at once. Nothing
inside the session records a reboot, a closed terminal, a `docker kill` or
an OOM — but since 0.22.0 the kit launcher writes the running container
names on every launch, and a container missing from a reading taken after
the session's last event is proof it is over. What is left over is the
session you simply walked away from: still running, just quiet, filed as
**idle** after 12h. That one is a conservative guess rather than a claim
that it died, which is why idle is counted but never ranked — nor, since
0.21.0, listed.

#### Stage 7 — the drain

`SessionEnd` and `PreCompact` only ever write a marker — both hooks are
killed within seconds, so neither can afford an LLM call. Something else
picks the marker up:

| Drain | Runs | Catches |
|---|---|---|
| `SessionStart` | next time any session opens | everything, eventually |
| `drain_extractions.py` *(kit)* | on the host, once the container exits | the last tab of the day, written up that evening instead of next morning |

Both call the same `lib/extraction_drain.py`, and the dequeue is an
atomic rename, so the two firing at the same instant hand each marker to
exactly one of them. The retry ladder from there:

```
pending_extractions/ ──rename──► processing_extractions/ ──► detached extract_learnings.py
        ▲                                   │                          │
        └──── requeue after 15 min ─────────┘                   deletes its own
              (max 3 attempts)                                  marker on success
                     │
                     └──► failed_extractions/   (kept for inspection, never retried)
```

A marker sitting in `processing_extractions/` is the signature of a
container torn down mid-extraction — the detached child dies with it.

#### Stage 8 — what the extraction leaves behind

One model pass over the transcript produces four things:

| Output | Where | Note |
|---|---|---|
| diary entry | `diary/YYYY-MM-DD.md` | the permanent record |
| learnings | `learnings/YYYY-MM-DD.md` | input to `/multiplai-context:dream` |
| `disposition` | on the registry entry | `active` · `parked` · `done`, read from how you actually spoke — no command to remember |
| checkpoint retired | `data/checkpoints/<sid>/` deleted | *only* once the diary supersedes it — see [Retire](#context-checkpointing-long-sessions) for the conditions that keep one alive |

The disposition is gated on the **final** chunk specifically: a partial
extraction leaves it at the default rather than writing a guess as a
fact, because a fabricated `active` would silently strip a real `parked`.

#### Stage 9 — how it is seen, and forgotten

The fleet view (`AGENTS.md`) is regenerated from the two stores every
time a session starts and every time the host drain runs. It is pure
aggregation — delete the file and the next pass rebuilds it
byte-for-byte. What is listed and what counts as a front is
[What you actually see](#4-what-you-actually-see).

Entries are then collected on a schedule that depends on how the session
ended — 7 days for an observed or recorded end, 30 for one that might
still be alive, never for a parked one. See
[When entries are collected](#5-when-entries-are-collected).

#### Branch points at a glance

Everything above that can go two ways, in one table:

| Fork | Branches | Decided by |
|---|---|---|
| `uv` present? | hooks run / **every hook is inert** (one warning per day) | the environment |
| model client present? | extraction, dreams, catalogs run / all no-op | SDK or API key |
| `SessionStart.source` | `clear`/`compact` rebuild · `startup`/`resume` start clean | Claude Code |
| child session (subagent)? | excluded from all checkpointing | transcript shape |
| checkpoint trigger | band · staleness · none | tokens and clock |
| auto-compaction steered? | silent automatic rebuild / `/clear` advice | two env vars |
| checkpoint fresh at `PreCompact`? | one-line stub summary / full native summary | sync write + watermark |
| how it stopped | `SessionEnd` fired · silence | whether you quit cleanly |
| which drain got there first | in-session / host *(kit)* | atomic rename |
| extraction outcome | diary + disposition + retire · partial · requeue · quarantine | the model call |
| disposition | `active` · `parked` · `done` | how you left, read from the transcript |
| fleet grouping | Needs you · Working · Parked (listed) · Idle (counted only) · finished (neither) | status × disposition |

#### A worked example — two sessions, one line of output

Two tabs on a Tuesday. **A** is a bugfix in `PROJECTS/DolceBot` that you
finish and quit out of. **B** is a refactor in `knowhere` that asks you a
question at 12:40, right as you leave for lunch — and you never go back
to that tab.

| Time | Session A (`DolceBot`) | Session B (`knowhere`) |
|---|---|---|
| 09:02 | `./claude.sh` → `SessionStart(startup)`; entry created, `last_event: start` | — |
| 09:05 | first reply → `Stop` → `last_event: stop` | — |
| 11:30 | — | launched; `SessionStart(startup)`, `last_event: start` |
| 12:40 | — | Claude asks a question → `Notification` → `last_event: notification` |
| 13:10 | done. **Ctrl-C Ctrl-C** → `SessionEnd` writes `last_event: end` and an extraction marker | still open, still waiting |
| 13:10 | the host drain picks up that marker → detached extraction → diary entry, learnings, `disposition` | — |

Now the readings. A is `ended`, so it drops off the board entirely. B is
the whole question:

| When | B's group | in the `/fleet-status` digest |
|---|---|---|
| 13:15 Tue — 35 min after B's last event | **Needs you** | ranked item: B, waiting on your answer |
| 01:00 Wed — 13h quiet | **Idle** — counted in the `AGENTS.md` header, not listed, not a front | `1 idle (oldest 13h)` on the in-flight line |
| following Tuesday | still **Idle**; still only a number | unchanged |
| +30 days | entry GC'd — B disappears | unchanged |

**B never becomes `ended`, and that is correct here** — B's tab is still
open, so its container is still in every roster the launcher writes, and
`idle` is the honest reading: quiet for 13 hours, not dead. No hook fires
for a session that is merely sitting there.

Close that tab, though, and B is `ended` at the next launch — not
thirteen hours later, and not on a guess. That is the whole of what the
roster buys: it is the difference between "quiet" and "gone", which
before 0.22.0 read the same.

That is why counting and ranking are kept apart. The digest does **not
rank** B, because a tab that went quiet has no claim on your attention
while a session waiting on an answer does. Every entry the system is
unsure about lands on the counted-but-not-ranked side, which is what
keeps the list you actually read honest.

Its **checkpoint is untouched** either way. `data/checkpoints/<sid>/` is
still on disk with B's intent, next action and files in it, and the diary
is where "what did that session decide" is answered. Dropping B from the
listing costs you nothing you cannot get back by name.

And if you reboot the Mac on Wednesday, B looks **exactly the same** —
which is the point of [How the end of a session is detected](#3-how-the-end-of-a-session-is-detected).
Dead and dormant are indistinguishable from outside, so both are filed
as the harmless one.

**Why this matters — real numbers.** On a registry of 118 entries
(82 ended, one session actually running), the pre-0.15.1 line read:

```
36 fronts · 4 need you · oldest 19d · 8 collisions
```

The same registry, same instant, with 0.15.1's counting:

```
7 fronts · 4 need you · oldest 7h
```

The 29 that vanished are B-shaped: tabs that went quiet days or weeks
ago. At the time they were still listed in `AGENTS.md` under **Idle** —
nothing was hidden — they simply stopped being counted as things needing
you. (0.21.0 took the second step and dropped the section too, once the
same registry showed 36 idle entries to 17 fronts: the graveyard *was*
the file. They are still counted in the header.) `oldest` fell
from 19d to 7h for the same reason: it now measures the oldest *front*,
not the oldest corpse. All 8 collisions were between pairs of sessions
last heard from over a week ago, which is shared history, not a live
conflict over a file.

Worth noting what did **not** contribute: nothing outside the session
was consulted. The entire improvement is counting the entries already on
disk correctly, which is why it works identically on vanilla Claude Code.

### Key libraries

- **`multiplai_core.paths`** — single source of truth for path
  resolution (plugin env → workspace fallback → `~/.multiplai`). All
  runtime state resolves through here. Provided by the external
  `multiplai-core` package (declared as a PEP 723 dependency by each
  script that needs it).
- **`multiplai_core.model_client`** — LLM abstraction: Agent SDK
  (zero-config) with an Anthropic API-key fallback. Also from
  `multiplai-core`.
- **`scripts/lib/`** — plugin-local shared modules shipped with the
  plugin (`extraction.py`, `memory_router.py`, `project_identity.py`, …).

### Learning lifecycle

1. **Capture** — when you exit a session (or it pre-compacts), the
   `SessionEnd`/`PreCompact` hook writes a tiny *marker* JSON to
   `data/pending_extractions/`. The hook itself does no LLM work — those
   hooks get killed within seconds by Claude Code, so any multi-second
   call would be unreliable.
2. **Extract (deferred, async)** — a drain reads the pending markers and
   spawns `extract_learnings.py` as a *detached background subprocess*
   (`subprocess.Popen(..., start_new_session=True)`). The subprocess does
   the LLM call to produce the diary entry + per-day learnings, writes
   them, and removes its marker. Either the next `SessionStart` drains
   (returning immediately, so your first prompt isn't blocked) or
   `drain_extractions.py` does, from outside any session — see
   [Draining the queue](#draining-the-queue).
3. **Propose** — `/multiplai-context:dream` reads learnings + diary and writes a
   review proposal to `.multiplai/dreams/`.
4. **Apply** — `/multiplai-context:dream-remember` walks the proposal with you
   and applies approved edits to memory files.

> **Heads-up on timing.** Because extraction runs in the background, it
> may still be in flight when you ask your first question (or run
> `/multiplai-context:health`). A typical transcript takes 10-30 seconds depending
> on length and model latency. If you started a session and the latest
> diary entry isn't there yet, wait ~30 seconds and check again — the
> subprocess is still working. The plugin will *never* block your prompt
> on extraction; it always catches up asynchronously.
>
> If a marker stays in `data/pending_extractions/` across multiple
> sessions, the next drain retries it (up to 3 attempts); a
> permanently-failing transcript is moved to `data/failed_extractions/`
> for inspection. A marker in `data/processing_extractions/` older than
> 15 minutes is treated the same way — that is what a container torn down
> mid-extraction leaves behind, since the detached child dies with it.

#### How a learning is labelled — provenance × kind

Every learning captured from a session gets two labels, and they answer
different questions. You see the pair at the front of each line in
`.multiplai/learnings/`:

```
- **[CORRECTION/RULE]** Stage with an explicit pathspec. → Target: dev.md — Add to the Git section.
```

**Provenance — where the knowledge came from.** This is what tells you how the
claim could ever be checked again.

| | |
|---|---|
| `RESEARCH` | Read in an external source — docs, a web page, a paper. Re-check by re-reading it. |
| `EMPIRICAL` | Observed while doing the work: it broke, it was fixed, the test went green. Re-check by running it again. |
| `CORRECTION` | You told Claude it had something wrong. |
| `DECLARATION` | You stated it unprompted, with no error to overwrite. |
| `INFERENCE` | Claude concluded it and nobody confirmed it. |

*Worked example:* watching a test fail and then pass after a fix is
`EMPIRICAL`. Concluding from that the sibling case is also fixed, without
running it, is `INFERENCE` — same session, same fix, different provenance.

**Kind — what sort of thing it is.** This is what tells you how much damage a
wrong one does.

| | |
|---|---|
| `FACT` | Can be true or false, and decays. |
| `RULE` | Normative, so neither — it gets revoked, not falsified. |
| `DECISION` | A commitment in force until something overturns it. |
| `INTENTION` | Something to come back to later (see [Prospective memory](#prospective-memory--intentions-that-fire-later)). |

*Worked example:* "the API always returns UTF-8" is a `FACT` — it could turn
out to be false. "Always decode API responses as UTF-8" is a `RULE` — it can
only be revoked. The second one changes what Claude does everywhere, which is
why the two are worth telling apart.

When the extractor genuinely cannot tell, it answers `INFERENCE` and `RULE` —
the cautious end of each axis, so an unclear item lands in front of you rather
than sliding past. Learnings captured before this taxonomy existed keep their
old `**[trust: …]** TYPE` line and are **not** relabelled: guessing where a
month-old note came from would manufacture exactly the signal these labels
exist to make trustworthy.

Both labels ride through into the `**Provenance:**` line of each item in a
dream proposal, so you can see what a proposed memory entry was distilled from
while you review it — and, since 0.36.0, they decide what may be applied without
you reading it.

#### What may be written without you reading it

Three layers decide each proposed memory entry, and **only one of them is a
model**. Any of them can refuse; only all three together can approve.

**1. The rubric, in code.** Provenance sets confidence, kind sets blast radius,
and the intersection is what may be applied:

| | `FACT` | `DECISION` | `RULE` |
|---|---|---|---|
| `CORRECTION` / `DECLARATION` | apply | apply | **review** |
| `EMPIRICAL` / `RESEARCH` | apply, if the citation holds | review | **review** |
| `INFERENCE` | review | review | **review** |

**A `RULE` never applies automatically. Not in any mode, not even one you stated
yourself.** That is about blast radius, not trust: a wrong fact is one you notice
later; a wrong rule changes what you notice. An item with no labels at all —
every proposal drafted before the taxonomy existed — reads as the cautious end of
both axes and waits for you.

**2. The judge, a separate model call.** It is never told it is grading another
pass's output, and its stated job is to find reasons to escalate. Per item it
re-derives both labels, checks whether the cited source actually supports the
claim, and checks the target file for redundancy — three questions no pattern
match could answer, which is the whole reason it exists. **It may only ever
lower an item.** It cannot promote anything the table refused, so text that
talks the judge into "apply" on a rule changes nothing at all.

**3. The floor, in code, after the verdict.** It refuses a target that is not a
plain memory filename, any `CLAUDE.md` or `AGENTS.md`, anything that revises
rather than appends, and anything that did not parse. Running *after* the
verdict is the point: a check consulted before it is one a model can be argued
past; a check on the concrete write is not.

Failure always goes the same way. A timed-out batch, a rate limit, an
unparseable reply, or no SDK at all yields zero verdicts — and with zero
verdicts nothing is applied, which is identical to `memory_write_mode: review`.
The count of items that kept a conservative default is printed rather than
passed over.

**`memory_write_mode`** picks how much of this runs:

| Mode | Behaviour |
|---|---|
| `review` | Nothing is judged, nothing is applied — the pre-0.36.0 flow, one word away. |
| `triage` **(default)** | The judge runs; items the table clears are applied; the rest wait for you. |
| `auto` | Also applies plain `FACT` items the table would have held back. Rules still never apply. |

**Rejections are logged.** When the judge drops an item — usually because your
memory already says it — it is written in full to
`.multiplai/data/rejections.jsonl`: its text, its labels, its source citation and
the judge's one-line reason. Dropping means "not promoted to memory", never
"deleted": the source learning is untouched and the record carries its content
hash, so any drop can be read back and overruled by hand. The receipt shows
every rejection while there are 25 or fewer, and grouped counts above that.

Auditing refusals is how the judge earns the delegation. A pass that reports only
what it wrote is indistinguishable from one that quietly discards good items.

### Session accounting

*How each session's state is decided, and by whom.*

Running several sessions at once, the question at the end of the day is
*which of these needs me?* Everything below exists to answer that one
question honestly. Read this section before changing any of it — the
pieces are individually small and it is the *interaction* that gets
confusing.

#### 1. What is on disk

One directory, four kinds of file, and a strict rule about who may
write which:

| Path | Written by | Holds |
|---|---|---|
| `data/sessions/<sid>.json` | this plugin's lifecycle hooks | the registry entry — project, cwd, container hostname, `in_container`, `started_at`, `last_event`, `disposition` |
| `data/live_containers.json` | **multiplai-kit's launcher**, on the host | which containers were running, and when it looked. Read-only here; absent without the kit |
| `data/sessions/<sid>.adopt` | the multiplai hub | nothing. Its existence means "the hub has taken the driver seat" |
| `data/checkpoints/<sid>/checkpoint.md` | this plugin's checkpoint writer | what the session is *doing* — intent, next action, files in hand |

**Only this plugin writes a registry entry.** Nothing outside a session
may edit one — a second writer of session *state* is how two stores start
disagreeing silently. What an outside observer may do is deposit its own
evidence in its own file, which this plugin then reads: the hub's `.adopt`
marker is a one-bit channel that cannot corrupt anything, and the
launcher's `live_containers.json` is a dated observation, never a verdict
about any particular session. Both are inputs to a derivation here; the
entry stays single-writer.

`data/AGENTS.md` and `data/fleet.json` are **outputs, never inputs** —
pure aggregation over the stores above, no LLM call. Delete them and
the next run reconstructs them byte-for-byte.

#### 2. Three questions, three fields

They get conflated constantly. They are independent:

| Question | Field | Values |
|---|---|---|
| Is its process running, and doing what? | `status` (derived) | `working` · `waiting_input` · `idle` · `ended` |
| How did *you* leave it? | `disposition` (on the entry) | `active` · `parked` · `done` |
| So what do I see? | group (derived from both) | **Needs you** · **Working** · **Parked** · **Idle** · not listed |

`status` is frozen — it is the vocabulary the multiplai-gui API contract
speaks, so nothing here may coin a fifth value. `disposition` is a
*separate key* precisely so intent can never overwrite liveness: a
session can perfectly well be ended **and** parked.

You get a disposition by typing "park it for now" as you would anyway —
the extraction pass that already reads your transcript for the diary
reads how you left on the same model response. There is no command to
remember at the moment you are least likely to remember it. It defaults
to `active` whenever the model is unsure.

#### 3. How the end of a session is detected

This is the part that surprises people, so it is worth stating flatly:
**a hook is code running inside a session, and a session cannot report
its own death.** Only two things observe it, and neither is the session:

| How it stopped | Fires | Recorded as | Noticed |
|---|---|---|---|
| `/exit`, Ctrl-D, Ctrl-C Ctrl-C — a clean quit | `SessionEnd` | `last_event.kind = end` | at once |
| Reboot, closed terminal, `docker kill`, OOM-kill, crash | **nothing** | nothing | its container is missing from the next roster |
| Still running; you walked away | nothing | nothing | still listed — quiet ⇒ `idle` after 12h |

**The roster is what separates rows 2 and 3**, which used to be
indistinguishable and both decayed to `idle`. multiplai-kit's launcher
writes the running container names to
`.multiplai/data/live_containers.json` on every launch — before it
starts your session container and again after that container exits. When
a reading is *newer* than a session's last event and that session's
container is not in it, the session is over. Nothing is inferred; the
host looked.

Everything about it is conditional on evidence, and its absence changes
nothing:

- **No roster file** — no kit, or a kit that has not launched since —
  and the quiet-window behaviour below applies unchanged.
- **A roster older than a session's last event** decides nothing about
  that session. A reading can only retire what already existed when it
  was taken.
- **Only container sessions are judged.** Entries record an
  `in_container` flag, because outside a container the hostname is a
  *machine* name and no string comparison could tell it from a container
  name — so a `--local` session, or one run through `claude-wrapped` on
  the Mac, would otherwise be declared dead by a roster that could never
  have listed it. Entries written before 0.22.0 are not judged either.
- **A corrupt or unreadable roster** is ignored rather than trusted.
- **Parked sessions are never retired this way.** Parking is a stated
  intent; the container being gone is exactly what you meant.

The quiet window still governs everything the roster cannot see — a
session whose container is alive but which has not spoken. The threshold
is **12h**, and it was 24h until 0.21.0. A day sounds conservative until
you notice it spans the previous evening: every container you opened
after dinner still claimed a slot under **Working** the next morning,
which read as nine running agents where there was one. Half a day is
roughly one working session. And quiet is still treated as a **guess,
deliberately the conservative one** — the entry is filed as `idle`, not
declared over, because it may just be thinking or you may be at lunch.
What that buys is that `idle` is *never a front*.

*A different design was tried and rejected.* 0.15.1 briefly had the kit
launcher drop an `.exited` marker beside the entry when `docker run`
returned. It was removed before release once measured: a clean quit
already records `end`, and a reboot or a closed terminal kills the
launcher along with the container, so the marker only ever covered
`docker kill` and OOM-kills — worth zero entries on a real 118-entry
registry. The roster is not that design. A marker is a **write on the
way out**, so it needs the launcher to survive the thing it is reporting;
a roster is a **poll**, and it does not care whether any launcher
survived. That single difference is why it reaches the cases the marker
could not — 49 stuck entries on the registry that motivated it.

#### 4. What you actually see

Listing and counting are deliberately kept apart:

| Group | Listed in `AGENTS.md` | A **front** (counted by the digest) |
|---|---|---|
| Needs you | yes | **yes** |
| Working | yes | **yes** |
| Parked | yes | **yes** |
| Idle | no — a count in the header | no |
| ended / `done` | no (counted as "finished") | no |

`AGENTS.md` **lists the fronts**: what has a claim on you. The
`/multiplai-context:fleet-status` digest ranks the same set. Idle is the
difference, and on a real registry it is most of the entries — 36 to 17
on the reading that prompted 0.21.0, each up to forty lines long, which
put the answer at the top of the file and a graveyard under it. It is now
a number in the header (`… · 36 idle, not listed`) and nothing more.
(`fleet.txt`, the old one-line status-bar count of the same fronts, is
retired — a count with no referent said there was a fire without saying
where.)

What that gives up is "where did I leave that thing last Tuesday", which
an idle entry's checkpoint used to answer in passing. Nothing was
deleted: `data/checkpoints/<sid>/checkpoint.md` is still there, and the
diary is the place that question is supposed to be asked.

Parked counts as a front on purpose. Its process is usually long gone,
but "I am coming back to this" is a claim on you in a way that a tab
which merely went quiet is not — that is the whole difference between
parking something and abandoning it. It is also why parking is the way
to keep something on this list.

An entry does **not** list its involved files. It did, shortened and
capped at six, and the line still never earned its space: repeated under
every heading it wrapped across the terminal and pushed the next agent's
heading off screen, which is the same bulk this file was trimmed to
remove. The paths are still collected and still absolute — `fleet.json`
ships them, and collision detection reads them to answer the one question
the line stood in for, on its own line, below.

A **Collisions** section names every file two agents both have in hand.
Both holders must be `Working` or `Parked` *and* have been heard from
within 24h. In practice unparked work is bounded by the shorter idle
window, since it leaves `Working` at 12h; parked work keeps the full 24,
because uncommitted edits nobody is watching hold a file harder, not
less. A file two sessions both touched last week is shared history.

#### 5. When entries are collected

| Entry | Kept for |
|---|---|
| `disposition: parked` | forever — parking it is you saying you will be back |
| extraction still queued or in flight | until the extraction finishes |
| **container confirmed gone by the roster** | **1 hour** |
| ended cleanly (`SessionEnd` fired) | 7 days |
| anything else (might still be alive) | 30 days |

The 30-day window exists for sessions that *might* still be running —
and since nothing outside a session can prove one is not, every entry
that did not quit cleanly gets it. The roster is exactly that proof
where the kit launcher is installed: it records the running containers
on the host, so an entry whose container is absent from a reading taken
*after* that entry last spoke is over as a matter of observation. Those
are collected on the next session start rather than sitting out a month.
The hour is not doubt about the roster — it is room for the deferred
extraction, which writes `disposition` minutes after a session exits and
only protects an entry once its marker is on disk.

With no kit and no roster, nothing above changes: the two windows are
all there is, as before.

Parked being exempt closes a real asymmetry: transcripts survive a year,
so `claude --resume <id>` works months later, but registry entries used
to age out in 7–30 days. A parked idea stayed *resumable* while becoming
*invisible*.

#### 6. When it refreshes

`SessionStart` regenerates both files in-process (no model call, so it
costs a few file reads), and `drain_extractions.py` does the same on the
host after a container exits — so the view is current at session start
**and** after the last tab closes. `scripts/fleet_status.py --full` writes
them on demand and prints the whole picture.

## Observability

The plugin is not a black box — every meaningful action is logged. All
runtime state (logs, catalogs, session state, dream state) lives with the
workspace, beside memory/diary/learnings:

```
<workspace>/.multiplai/data/logs/
```

`<workspace>` is the configured workspace dir (`workspace_dir` option or
`$WORKSPACE`). The plugin deliberately does **not** scatter logs into
Claude Code's per-install `CLAUDE_PLUGIN_DATA` dir — runtime state stays
with the workspace it describes. Fallbacks: an explicit `data_dir`
option overrides everything; with no workspace configured it uses
`CLAUDE_PLUGIN_DATA` (managed) or finally `~/.multiplai/data`.

### The activity log — what to watch

`activity.log` is the human-in-the-loop view: one plain-language line
per meaningful action — context injected (and the exact files), nudges
fired, diary written, learnings captured, catalogs rebuilt, session
start/end. It's the *current* file (no date); the previous day's stream
rotates to `activity-YYYY-MM-DD.log` on the first write of a new day.

```
14:51:03Z [a1b2c3d4] [context]   injected 4 memory · 0 skills · 0 resources · scores 31.5→9.8 (4/12 kept) → memory: finances.md, life.md, preferences.md, taxes-italy.md
14:51:03Z [a1b2c3d4] [nudge]     dream gate open (>24h, pending learnings) — surfaced to user
14:51:18Z [a1b2c3d4] [diary]     wrote diary entry (1 unit(s)) to <session>.md
14:51:18Z [a1b2c3d4] [learnings] captured 2 learning(s) to backlog
14:52:01Z [e5f6a7b8] [catalog]   rebuilt 3 catalog(s) (14 entries, 0 pruned) in 312ms
```

Each line is `HH:MM:SS**Z** [**session**] [component] message`:

- The **`Z`** marks the time as **UTC** — it is *not* your local
  clock. If you're at UTC+2, `14:51Z` happened at 16:51 your time.
- The **8-char session id** in brackets makes a line self-traceable:
  `grep a1b2c3d4 activity.log` replays everything one session did,
  and the same id maps to the transcript at
  `$CLAUDE_CONFIG_DIR/projects/**/a1b2c3d4-*.jsonl` (which has the
  actual prompts).
- The message is verbatim — no `key=value` tail. Structured fields
  (full file list, byte counts, timings) live in the `.jsonl` mirror.

### Reading a `[context]` routing line

This is the most important line to understand — it tells you *whether
routing is actually working*, not just that it ran. Anatomy:

```
injected 4 memory · 0 skills · 0 resources · scores 31.5→9.8 (4/12 kept) → memory: finances.md, life.md, …
         └── how many files from each corpus made it in           └── files grouped by corpus
                                          └── routing-quality hint (token_overlap only)
```

The file list after `→` is grouped and labelled by corpus —
`memory: … · skills: … · resources: …` — so you can always tell which
corpus each injected file came from (only corpora that contributed are
shown; files are alphabetical within each).

**The score hint** (`scores TOP→FLOOR (KEPT/CANDIDATES kept)`) is the
signal:

- `scores 31.5→9.8` — the highest-scoring file scored 31.5, the
  lowest one that was *actually injected* scored 9.8. A wide gap
  (top ≫ floor) means routing found a clear winner; a flat range
  (e.g. `7.2→6.8`) means everything scored about the same — weak,
  low-confidence routing where the cut is near-arbitrary.
- `(4/12 kept)` — **12 files** had some keyword overlap (candidates);
  only **4** cleared the relevance cutoff and were injected. The
  other 8 were dropped as too weak. Big drop = good filtering;
  `(12/12 kept)` = the filter did nothing.
- `CAP-HIT` — appears as `scores 22.4→6.5 CAP-HIT (10/18 kept)`. It
  means the relevance cutoff would have kept *more* than the 10-file
  ceiling, so the #10/#11 boundary is arbitrary. Frequent `CAP-HIT`
  on low top-scores = the prompt is matching too much weakly; routing
  is noisy, not precise.

**Abstention** — routing deciding *nothing* is relevant is correct
behaviour, not a failure. You'll see one of:

- `· continuation — nothing injected` — the prompt was a bare
  go-ahead (`yes`, `go on`, `do it`); the context is already in the
  conversation, so nothing is added.
- `· no match (best 1.4 < floor) — nothing injected` — files were
  considered but even the best one scored below the relevance floor.
- A `[context] skip` line such as
  `router abstained — best memory score 1.4 below relevance floor
  (3 cand), nothing injected` — same thing when *no* corpus produced
  anything, so there was nothing to inject at all.
- `all 4 matched file(s) injected within the last 4 turns — on
  cooldown, nothing injected` — routing *did* find relevant files, but
  they were all injected recently and are still in the conversation.
  See [Re-recommendation cooldown](#re-recommendation-cooldown).

**Fallback** — `[context] router matched nothing — fell back to
recency-ranked memory → …` means routing failed (catalog/disk drift
or a router error, **not** a clean abstention) and the most-recently-
edited memory files were injected as a safety net. Occasional is fine;
frequent fallback means the catalog is stale — run
`/multiplai-context:refresh-catalogs`.

Notes: the score hint only appears under the `token_overlap` router
(the default) — the `llm` router doesn't expose scores. Healthy
`token_overlap` looks like: a clear `TOP→FLOOR` gap, `KEPT` well
below `CANDIDATES`, and `CAP-HIT` rare. Persistent flat ranges,
`CAP-HIT` everywhere, or constant fallback are the symptoms to act on
(start with `/multiplai-context:health`, which summarises these same numbers).

### Routing relevance cutoff

The `token_overlap` router scores every memory file, then keeps only those
within `keep_ratio` of the top score (and above an absolute floor). This is
what makes the injected set track *how many files are actually relevant*
rather than always filling the 10-file cap.

**Why the default is `0.30`.** The score is an unnormalized sum of matched
IDF weights, so a long or content-rich prompt inflates the whole ranking at
once. Too low a ratio then admits a shallow "filler tail" of weakly-related
files, and the cap silently truncates it to 10 — meaning the *cap*, not
relevance, does the filtering. Replaying real routing logs showed a `0.20`
ratio hitting the cap on ~45% of memory routes; `0.30` roughly halves that
while keeping every genuine multi-domain match. Raise it toward `0.35`–`0.40`
for stricter, smaller injections; lower it if you want the weaker tail back.

> Note: per-prompt score *normalization* (e.g. dividing by prompt length)
> does **not** help here — it scales every file equally, leaving the
> `floor/top` ratio (and thus the picked set) unchanged. The ratio is the
> lever.

**Measuring it.** `scripts/eval_router.py --keep-ratio R` runs the golden-case
harness at a given ratio. Note `keep_ratio` only moves the *relative* cutoff; the
*absolute* floor is guarded separately by a match-breadth eligibility gate —
an entry can clear `MIN_SIGNAL` only when it matched ≥ 2 distinct
`intent_domains` tokens (or a multi-word domain phrase verbatim), so a
single incidental token ("search", "data", …) can no longer pull a file
into an off-topic prompt, and per-term IDF is capped (`MAX_TERM_IDF`) so
one locally-rare term can't dominate a score.

### Re-recommendation cooldown

Routing runs fresh on every prompt, so without a guard a multi-turn
exchange on one topic would re-inject the *same* files turn after turn —
content that's already sitting in the conversation. The cooldown
suppresses that waste.

**How it works.** Each prompt advances a turn counter, and every file
that gets injected is stamped with the current turn in a small
`recently_injected` map (kept in `data/session_state.json`). On the next
prompt, any pick that was injected within the last
`recommend_cooldown_turns` turns is dropped before it's loaded — it's
already in context. A file becomes eligible again once the window passes;
if it's re-injected, its stamp refreshes. Aged entries are pruned so the
map stays small.

```
turn 1  "audit the routing quality"   → injects 7 memory · 1 skill · 10 resources
turn 2  "and the false-negative rate" → on cooldown, nothing injected
turn 3  "what about precision"         → on cooldown, nothing injected
turn 4  (window passed)                → re-injects the relevant set
```

(That trace is with `recommend_cooldown_turns = 2`; the default is `4`.)

**Compaction resets it.** When Claude Code compacts the conversation,
the injected content is summarized away — so the `PreCompact` hook clears
`recently_injected`, making every file eligible again. This is what keeps
a longer cooldown safe: it can never starve the model of context that
compaction has already discarded.

**Tuning.** `recommend_cooldown_turns` (default `4`) is the window. Raise
it if you find the same files re-appearing too often within a focused
session; lower it (or set `0` to disable) if you want routing to re-inject
more eagerly. Suppression is distinct from abstention — an all-suppressed
turn logs `on cooldown, nothing injected` and does **not** trigger the
recency fallback.

Watch it live from a **second terminal** (it stays out of Claude's
context entirely):

```bash
tail -f <workspace>/.multiplai/data/logs/activity.log
```

`activity.jsonl` mirrors the same events as one JSON object per line,
for tooling and the health audit (rotated the same way).

An `inject` record carries the attribution the human line has no room for:

| Field | Meaning |
|---|---|
| `files` | every injected name, memory + skills + resources, in that order |
| `files_by_corpus` | the same names split by corpus |
| `sections_by_file` | memory only, keyed by **bare filename**: which `##` sections were loaded. An **empty list means the whole file** — a file with nothing loaded has no key at all |
| `bytes_by_file` | memory only, same keys: characters injected for that file, summed over its picks |
| `bytes` | the whole assembled context block |

`sections_by_file` and `bytes_by_file` are what let you answer "did that
180 KB file cost me 180 KB this turn, or 6 KB?" — see
[Section-level retrieval](#section-level-retrieval-filemdsection).

### Debug mode — see every script

Logging level is environment-driven. Launch Claude with:

```bash
MULTIPLAI_DEBUG=1 claude          # everything at DEBUG, all scripts
MULTIPLAI_LOG_LEVEL=WARNING claude # quieter
```

`MULTIPLAI_DEBUG=1` makes every hook and script (context routing, diary,
learnings, catalog rebuilds, session lifecycle) emit DEBUG detail to its
per-component log **and** stderr — visible under `claude --debug`.

### Log layout & retention

- `<component>.log` — current per-component file; rotates to
  `<component>-YYYY-MM-DD.log` on UTC day change.
- `activity.{log,jsonl}` — current curated activity stream; rotates to
  `activity-YYYY-MM-DD.{log,jsonl}` the same way.
- `hook-errors.log` — every ERROR+ across all components, append-only.
- Retention: `MULTIPLAI_LOG_RETENTION_DAYS` (default **7**, `0` = keep
  forever) — applies uniformly to every rotated `*-DATE.{log,jsonl}`.
  The rejected `<name>.log.DATE` form is auto-migrated to the standard
  `<name>-DATE.log`.
- **One log sits outside all of the above:** `scripts/.warmup-deferred.log`,
  where `hooks/run.sh` records a lifecycle hook it had to drop because the
  dependency install was still running. It cannot live in `data/logs/` —
  `run.sh` exists precisely for the window in which `multiplai_core.paths` is
  not importable, so it cannot ask where that directory is. It is bounded by
  size instead of by age: capped at **256 KB**, rotated to
  `.warmup-deferred.log.1` by an atomic rename, one generation kept, so the
  pair never exceeds 512 KB. `MULTIPLAI_LOG_RETENTION_DAYS` does not reach it.

Every line follows the project logging standard:
`[<UTC ISO-8601>Z] [<component>] [session:<8-char id>] LEVEL: message`.

## Troubleshooting

Symptom → fix. When in doubt, start with `/multiplai-context:health` — it
checks the plumbing mechanically and names what's missing.

| Symptom | Fix |
|---|---|
| **Nothing runs at all — no diary, no injection, no logs.** Hooks disable themselves silently when `uv` is missing. | Install [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh \| sh`), restart the session. |
| **First session says "building its Python environment" and hooks are quiet.** Cold start: the first hook fire builds the environment from `uv.lock` in the background (~1 min). Expected exactly once; hooks go live on the next prompt after it finishes. | Just keep working — or pre-warm ahead of time with `claude --init-only`, or from a shell: `sh <plugin-dir>/hooks/run.sh --warm`. |
| **A message says the environment FAILED to build.** The build's error is the only thing that matters here. | Read `<plugin-dir>/scripts/.warmup.log` — the build's own output — fix what it names (usually no network, or no git for the `multiplai-core` clone), then rebuild with `claude --init-only` or `sh <plugin-dir>/hooks/run.sh --warm`. It also retries by itself within 15 minutes. |
| **The environment is built but hooks still say "first run".** A bare `uv sync` builds the environment but leaves the in-flight marker the hooks gate behind. | Run `sh <plugin-dir>/hooks/run.sh --warm` (or `claude --init-only`), which clears it. Deleting `<plugin-dir>/scripts/.warmup/` by hand does the same. |
| **Hooks appear to do nothing.** No visible effect, unsure if anything is installed correctly. | Run `/multiplai-context:health` — it verifies the model client, directories, memory freshness, and diary/learnings counts, and names the broken piece. |
| **Memory not injected.** You told setup things, but a new session doesn't know them. | Two checks: (1) your `settings.json` key must be the compound `pluginConfigs["multiplai-context@multiplai"]` form — a bare `multiplai` key fails **silently** (see [Configuration](#configuration)); (2) read the `[context]` routing line in the activity log — [Observability](#observability) explains how to tell a healthy route from an abstention or a fallback. |
| **Settings changed but nothing happened.** | Options are read at session start — restart Claude Code after any `settings.json` change. |
| **Where are the logs?** | `<workspace>/.multiplai/data/logs/` — `activity.log` is the human-readable stream (`tail -f` it from a second terminal); `hook-errors.log` collects every error. Layout and retention in [Observability](#observability). |

## Uninstall

An easy exit is part of the deal — your data is plain markdown on your own
disk, and removing the plugin is two commands:

```
/plugin uninstall multiplai-context@multiplai
/plugin marketplace remove multiplai
```

(Skip the second command if you're keeping other multiplai packs — it
removes the marketplace they all install from.)

That removes the code and all hooks (nothing runs after the next restart).
**Your data stays yours**, untouched, at `<workspace>/.multiplai/` (or
`~/.multiplai/`): `memory/`, `diary/`, `learnings/`, `now/` are all
human-readable markdown — keep them, grep them, or delete the directory
and it's as if the plugin was never here. Derived state (catalogs, logs,
session state) lives under `.multiplai/data/` and in
`~/.claude/` catalogs — safe to delete any time; the plugin recreates
what it needs if you come back.

## Development

```
cd plugins/multiplai-context
uv run --all-packages --project ../.. --with pytest --with pytest-asyncio \
  --with pytest-timeout python -m pytest tests/ -q
```

No venv to create: the repo-root uv workspace supplies `multiplai_core` and
friends, and `--with` adds the test toolchain for the run (same command CI
uses).

Tests live in `tests/` and are dev-only — never loaded by the plugin
runtime.

## License

MIT — see [LICENSE](../../LICENSE).
