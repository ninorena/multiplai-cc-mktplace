# Changelog

All notable changes to the **multiplai-context** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-context@<version>`.

Recorded history starts at **0.1.0**; anything earlier is in `git log` only.

Of the 31 versions recorded here, `0.6.4`, `0.6.5` and `0.8.0` carry a git tag
— the tagging convention started partway through. Dates on untagged versions
are the release dates recorded at the time, not derived from a tag.

## [Unreleased]

## [0.55.2] - 2026-09-28

### Changed

- **A hook about to be killed now logs what it was doing.** 0.55.1 shipped
  the watchdog but left it off, because the multiplai-core it installed
  (0.13.0) could not run it. This release moves to multiplai-core 0.15.0. At
  26s into the per-prompt hook, `context_manager.log` now gets a
  `HOOK_WATCHDOG` line with the stage still running, and a
  `HOOK_WATCHDOG_DETAIL` line with a timed DNS lookup and TCP connect to the
  API host and the last lines of the CLI's debug log. Each model call's CLI
  debug output is also written to `<logs_dir>/sdk/` as it arrives. Fast
  successful calls delete their file; failed, timed-out and slow calls keep
  it.
- multiplai-core 0.15.0 also brings `anthropic` 1.x (from 0.120) and stops
  sending `temperature` on direct Messages API calls.

## [0.55.1] - 2026-09-27

### Fixed

- **A slow memory-router call no longer costs the prompt its memory, and no
  longer runs the hook into its 30s timeout.** When the router's model call
  passed its 25s deadline and the call would not cancel cleanly, the hook kept
  waiting until Claude Code killed it ("Raise the hook's timeout"). The
  prompt got no memory and the log said nothing after `HOOK_ENTRY`. The
  router now gives up at the deadline, falls back to the offline
  token_overlap ranking, and logs why.

### Added

- **`context_manager.log` now says why a router call was slow.** The router's
  own warnings and multiplai-core's SDK lines (call start, `CLI ready after
  Ns`, `first reply after Ns`, the path of the CLI's debug log) now reach the
  file; before, they went only to the hook's stderr. If the hook is still
  running at 26s, it logs `HOOK_WATCHDOG` with the stage it is stuck in, then
  `HOOK_WATCHDOG_DETAIL` with a timed DNS lookup and TCP connect to the API
  host and the last lines of the CLI's debug log. The watchdog needs a
  multiplai-core with `hook_run(watchdog_s=)`; on an older core it stays off.

## [0.55.0] - 2026-09-17

### Added

- **`/multiplai-context:memory-health-audit` now reports skill-routing
  precision.** New `scripts/skill_routing_precision.py` reads each main
  session transcript: the prompt's `UserPromptSubmit` hook attachment says
  which skills the context hook actually showed the model, and the Skill tool
  calls and slash commands that follow say which were used. It reports the
  share of suggestion events where a suggested skill was then invoked before
  the session's next prompt, per-skill suggested/invoked counts, and the
  skills suggested repeatedly but never used. Read-only; `--days N` (default
  30, 0 = every transcript), `--json`, and `--json-out FILE` for the
  assessment copy from the same run. Reading transcripts rather than the
  `ROUTING` log means cooldown-suppressed suggestions are not counted, slash
  commands on the suggesting prompt are credited, and the window is not
  capped by the 7-day log retention. Motivation: a skill pool's retrieval
  precision decays as it grows (arXiv 2608.14036: 29.6% → 3.3% from 5 to 100
  skills), and until now nothing here measured it. First live reading on
  2026-09-17: 24.9% over 277 suggestion events in 30 days.

## [0.54.0] - 2026-09-14

### Changed

- **`collect_costs.py` refreshes list prices from the official pricing page
  before pricing a pass.** Every pass calls `multiplai_core.costing.refresh_pricing()`
  (a soft dependency: a core without that function logs one INFO line and
  keeps the bundled table). The fetch is skipped while the cached table under
  `<data_dir>/costs/pricing.json` is under a day old and never fails the
  pass. `--no-refresh` opts out. Motivation: the bundled table priced
  Sonnet 5 at the Sonnet 4.6 rate for weeks (50% too high), and Opus 5 and
  Fable 5.1 at the fallback rate; a hand-copied table goes stale on every
  model launch, so the collector now reads the source.
- **The pass summary says when the numbers are untrustworthy.** `run_collect()`
  stats gain `fallback_records` and `fallback_models`; the CLI prints a
  WARNING naming the models that had no list price, and another when the
  active pricing table is more than 30 days old.

## [0.53.0] - 2026-09-14

### Added

- **Dream proposals now carry a `## Rules Re-learned` section: the rules memory
  already held that a later session worked out again.** When the judge finds a
  proposed item already in a memory file it drops it as redundant, which is the
  right call for the memory line and hides the interesting part. A redundant
  *rule* is a rule that was written down, that a session did not follow, that
  you corrected, that extraction captured — and that consolidation then deleted
  precisely because memory already contained the rule nobody obeyed. The count
  was the signal, and the drafting prompt says outright not to keep it.

  Each group carries a verdict, because the three ways a rule fails take
  opposite repairs: **not routed** (its file was never injected on the day —
  add a routing keyword, or promote it to always-loaded), **routed but unused**
  (it was in the context window and did not land — reword it, or delete what
  contradicts it), and **inconclusive** (the file was used, or there is no
  telemetry — read it yourself). The evidence is a same-day join against
  session telemetry, never the exact session, and the section says so rather
  than implying a precision it does not have.

  It reads the rejection log from *previous* runs, so a first run reports
  nothing and says that in words instead of rendering an empty section.

- **`relearn_report.py` runs the same report on its own**, without a dream:
  `uv run --project <plugin>/scripts <plugin>/scripts/relearn_report.py`.
  Read-only, seconds, writes nothing. `--limit 0` for the full list,
  `--all-kinds` to include re-derived facts, `--json` for the grouping,
  `--since YYYY-MM-DD` to ignore older drops.

## [0.52.5] - 2026-08-31

### Fixed

- **A memory section the router asked for that no longer exists injected the
  whole file — once per pick.** Falling back to the whole file is deliberate
  and unchanged: better too much context than none. Falling back *twice* was
  not. Two stale anchors on one file loaded that file twice, and the injection
  log recorded it as "two sections" rather than "the file, twice", so the cost
  was invisible in exactly the measurement meant to expose it. Measured on a
  real corpus: two `ai-agent-patterns.md` section picks logged 64,817 bytes
  each against a file of roughly 65 KB. The fallback now happens once per file
  and is recorded as a whole-file load.
- **A renamed section is now named in the log, once, whatever shape the pick
  took.** The warning fires whether the stale anchor arrives alone or beside a
  bare pick of the same file, names the file bank-and-all (`team/dev.md`, not
  just `dev.md`), and — like every other warning this hook emits — is logged
  once per session rather than on every prompt for as long as the rename
  stands. It is the cue to run
  `/multiplai-context:refresh-catalogs --only memory` after renaming a section.
  A file with no `##` headers at all no longer trips it: nothing drifted
  there, and regenerating the catalog would fix nothing.
- **A whole-file fallback no longer defeated the injection cooldown.** Memory
  already injected is suppressed for a few turns, keyed on the pick the router
  made. A fallback is keyed on the file, so the absorbed picks were never
  recorded as injected, the router re-picked them, and the whole file went in
  again on the very next prompt — every prompt, for the life of the session.
  Both kinds of absorbed pick, the stale anchor and the section covered by a
  bare pick of its own file, are now recorded.
- **Picking the same section twice dropped the file's opening lines.** The
  second copy overwrote the first, and only the first carries the text above
  the file's first `##` — the last-updated stamp and instructions such as
  "load `core-voice.md` first". A repeat is now one pick, both here and where
  the router's answer is read.
- **A section name from the router can no longer forge a line in the log.**
  Only the filename half of `file.md#Section` is checked against the catalog;
  the rest is free text the routing model wrote, and a newline in it wrote a
  second, invented record into `context_manager.log` — the file `/log-doctor`
  reads. Non-printing characters are now stripped where the answer is parsed.

## [0.52.4] - 2026-08-23

### Fixed

- **Seven of 28 memory files had no section anchors, so the router had nothing
  to pick but the whole file.** The size floor for anchoring was 8 KB; every one
  of the seven had 3–10 `##` sections and failed only that bar — the largest by
  162 bytes, while being injected whole 57 times. The floor is now 2 KB, which
  is what it was actually for: not indexing a file smaller than its own anchor
  block. Measured on the corpus that prompted this, anchor coverage went from
  21/28 entries to 28/28. Expect a slightly larger router catalog (~37 extra
  anchor lines there) in exchange; the injected side is where the saving lands.

### Added

- **A regeneration that loses a hand-authored catalog field now says so.**
  `bundle` and `co_retrieve_for` are documented, consumed by the routing layer,
  and were absent from all 29 memory entries with nothing logged — these fields
  have no source outside the catalog, so a value that vanishes cannot come back.
  Each generator now declares its `preserved_fields`, and the run compares the
  catalog on disk against the one about to replace it: a field populated before
  and absent (or emptied) after is logged as `HAND_FIELD_LOST` and returned in
  `GenerationResult.errors`. `section_anchors` is deliberately outside the guard
  — it is derived from the file's headers, so losing it can be correct.

## [0.52.3] - 2026-08-22

### Fixed

- **The utilisation judge's extended thinking had switched off by accident, and
  it moved the number 5x.** `lib/thinking.py` shipped in 0.48.0 (2026-08-16) and
  swept the judge in with the other mechanical call sites, so it went from
  thinking-on (the SDK default — no thinking module existed before) to
  thinking-off. Not one line of the judge changed, so nothing in that diff said
  a measurement had moved. Re-measured on a fixed 30-session subset with the
  prompt held constant and only the toggle moved: **14.5% of sections credited
  with thinking off against 2.8% with it on.**

  Thinking is now **pinned on** in `lib/utilisation_judge.py`, as a named
  constant with the measurement written next to it, rather than inherited from
  a default tuned for hook latency. On is the deliberate choice: ruling on
  dependence is the same "hold two things side by side and decide" work the
  memory doctor's contradiction pass keeps thinking for, spot-checks show the
  thinking-off judge buying its higher number with topical false positives, and
  the latency argument does not reach an offline pass sampled five sessions a
  night on a 180s budget. `utilisation_thinking=false` still turns it off — the
  pin is a default, not a lock.

- **Every judge verdict now records what produced it** — plugin version, model,
  thinking on/off — and **the report can no longer average across the
  discontinuity.** Only stamped verdicts count toward the `judge` column;
  everything written before the stamp existed is carried per row as
  `legacy_judge`, labelled `comparable_to_judge: false`, and the human table
  prints a warning naming how many sessions are held aside. Nothing is
  backfilled or rewritten: no existing record says which instrument produced
  it, and a guessed provenance is worse than an absent one. The practical cost
  is that the judge column restarts from empty and rebuilds as the nightly pass
  accumulates stamped verdicts.

- **The judge parser stopped dropping verdicts silently.** Measured across 63
  judged sessions, only 558 of 595 injected section-keys came back with a
  verdict; the other 37 vanished with no trace. Dropping is still correct — a
  missing section is recorded as *not observed*, never as unused, so the rates
  stay honest — but each drop now logs a warning naming the sections, split by
  cause (no verdict returned, malformed tag, a section that was never injected,
  a duplicate). The prompt asks for one verdict per injected section, so a
  shortfall is model non-compliance and has to be visible to be fixed.

### Changed

- **The two memory-utilisation prompts no longer tell the model the answer
  before asking the question.** Both the extraction pass and the sampling judge
  opened by stating the conclusion as fact — "Most injected memory goes unused"
  / "Most injected memory is not used" — and then told the model not to look
  for a reason to disagree. The metric those prompts produce is the evidence
  for that claim, so the claim was making itself true. Both lines are now
  neutral: an empty answer is explicitly permitted without being predicted, in
  either direction. Measured effect is real but an order of magnitude smaller
  than the thinking flip above (~1.5x against 5x).

- **"Dependence, not topical similarity" now names the failure it exists to
  stop.** Spot-checks found the neutral prompt crediting `python.md` on the
  evidence "the session executed Python scripts extensively" and `git-policy.md`
  on "the session checked git status" — neither file was consulted. Both
  prompts now say outright that the session *doing* the thing a file is about
  is not evidence, and give the counterfactual test to use instead (would
  anything have gone differently had the section not been there?). The rule is
  symmetric — it also says a section that shaped a decision counts even when
  the session was nominally about something else — and predicts no answer.

  Everything that made the measurement rigorous is untouched: the evidence
  requirement, the untrusted-content fencing, and the output tags the regex
  parsers match on.

## [0.52.2] - 2026-08-18

### Changed

- **Simplification pass over the hook scripts — no behaviour change intended,
  a little less work per prompt.** The per-prompt context hook now skips the
  memory-bank resolution and the dev-reference doc reads on turns that cannot
  inject them (task notifications, already-announced projects), the resources
  retrieval no longer runs its keyword-search ladder twice when both search
  arms come up empty, and the PreCompact hook reads the transcript tail once
  instead of twice. Several small marker files that were written with a bare
  temp-and-rename now go through the shared atomic writer, so a crash mid-write
  can no longer truncate them — this includes the shared-banks catalog. Dead
  code, duplicate helpers, and unused imports were removed along the way.

  Review of that pass found five places where the behaviour did change, all
  corrected before release:

  - **The expensive fleet collectors stopped staying off the hook path.** A
    shared timestamp parser had moved into `lib/fleet_sources.common`, and
    importing any name from that package runs its `__init__` — so
    `import lib.fleet`, which every session start does, loaded all four
    collectors and `subprocess` with them (measured 16-22 ms and four
    process-shelling modules resident, for a six-line function). The parser
    now lives in a leaf module both sides import, and
    `tests/test_hook_path_imports.py` fails if anything on the hook path ever
    pulls the collectors in again.
  - **Marker files stay world-readable.** Routing them through the shared
    atomic writer swapped `write_text` for `mkstemp`, which creates 0600.
    `claude.sh` on the Mac drains the extraction markers a *container* wrote;
    a marker its uid cannot read is indistinguishable, to that drain's error
    handling, from no marker at all — a silently lost day of diary and
    learnings. The writer chmods to 0644, and restores the trailing newline
    the hand-rolled `json.dumps(...) + "\n"` calls produced.
  - **A diary file that vanishes mid-run no longer aborts the catalog.** The
    word-count helper had its existence guard removed in favour of the
    caller's check, leaving a window — `dream` triage and manual cleanup both
    delete under `.multiplai/diary/` — in which a `FileNotFoundError`
    propagated out of the whole generation pass.
  - **The bank list is resolved fresh, and cannot raise.** `configured_banks`
    had moved to the cached path accessor, pinning the bank list to whatever
    the environment said the first time anything in the process asked; and its
    fallback call sat unguarded inside its own `except`, so a resolution
    failure escaped a function documented as never raising.
  - **A malformed options file is parsed once at session start, not three
    times.** The shared parse was hoisted out of its timing stage and handed
    `None` to both launchers on failure, each of which re-parses on `None`.
    The transcript-tail read in PreCompact moved back inside the stage whose
    budget it spends, for the same reason: work between stages is
    unattributed wall-clock, and those timings are the only instrument for
    that hook's 270-second ceiling.

## [0.52.1] - 2026-08-17

### Fixed

- **Task notifications no longer trigger memory routing.** When a background
  task finishes, Claude Code re-invokes the model with a synthetic
  `<task-notification>` prompt, and the context hook used to route it like a
  real question — each notification could inject several memory files whose
  context the conversation already had. The hook now recognises these prompts
  and injects nothing, the same way a bare "yes"/"go ahead" continuation is
  skipped.
- **`memory/CLAUDE.md` is no longer cataloged or injected as memory.** It is
  the memory system's own index — meta-documentation, not personal context —
  yet the catalog generator swept it up with the rest of `memory_dir` and the
  router could recommend it (ambiguously labelled just "CLAUDE.md"). The
  generator now skips it, the next catalog refresh prunes the existing entry,
  and the recency fallback skips it too. Memory-management skills keep reading
  it by explicit path.
- **`eval_router.py --strategy` now accepts `llm_hybrid`.** The strategy
  shipped in 0.40.0 but the eval harness's argument list was never extended,
  so the hybrid could not be scored against a golden set.
- **`eval_router.py` scores section-anchored picks at file level.** Since
  section-level retrieval went live the router returns picks like
  `file.md#Section`, and the eval's exact string comparison scored every one
  of them as a miss against file-level golden labels.

## [0.52.0] - 2026-08-17

### Removed

- **The resources catalog generator is gone. qmd is now the only way a
  resources corpus is retrieved.** Setting `enable_resources` used to summarise
  every file under `resources_dir` with one LLM call each, write a
  `resources.json`, and route prompts against those summaries — the same
  mechanism memory, banks, diary and skills use. It was the wrong mechanism for
  this corpus. A catalog summary works when you wrote the file and will
  recognise it from one line; a research archive is collected rather than
  written, is long and heterogeneous, and answers a prompt with a *passage*
  rather than a whole document. Routing on whole-document summaries cannot
  express that, which is what qmd's chunk-level hybrid index is built for.

  Concretely: `generators/resources.py` and the `resources` entry in the
  dispatcher are deleted, `--only resources` now fails by name instead of
  silently doing nothing, and a `resources.json` left on disk by an older
  install is ignored rather than injected — its summaries are stale by
  construction now that nothing regenerates them. You may delete that file.

- **The `resources_retrieval` option is gone**, because there is no longer a
  choice to make. If you had set it to `qmd`, delete the line — behaviour is
  unchanged. If you had it at the old default (`catalog`, including by never
  setting it) **and** `enable_resources` was on, resources now come from qmd,
  which needs an index: run
  `skills/qmd-search/scripts/setup_qmd.sh --workspace <ws> --resources-dir <dir>`
  on the host. Until you do, no resources are injected. Nothing breaks —
  retrieval is fail-open — but nothing arrives either.

- **`/multiplai-context:setup` no longer offers to configure a resources
  directory.** The interview could set `enable_resources` but could not build
  the qmd index it needs, which left new installs with an option switched on
  and nothing behind it. Resources are now set up deliberately, through
  `/multiplai-context:qmd-search` and its `setup_qmd.sh`.

### Changed

- `catalog_thinking` now names the generators that actually exist (memory,
  banks, skills, diary). The per-prompt `Router … picked:` log line drops its
  always-zero `resources=` field; the machine-read `ROUTING` line is unchanged
  and still carries `resources=`, which qmd fills.

## [0.51.0] - 2026-08-16

### Fixed

- **The per-language review checklists now reach a session.** Session startup
  looked for the engineering standards in exactly one directory,
  `reference/dev/`, hardcoded — so `reference/review/`, the checklists that
  ship with multiplai-kit, was never injected, and the failure was silent (a
  miss is a log line, not an error). Both halves are fixed, because either
  alone changes nothing: `reference/review/` is now searched after
  `reference/dev/`, **and** the stack→doc map names those files, which it did
  not. A project now also gets the checklist for its language —
  `python-review.md` for Python, `javascript-review.md` for Node/TypeScript,
  `ios-review.md` for Swift, `go-review.md` for Go — plus the cross-language
  `valid-patterns.md` in every case. Go and Rust projects, which mapped to no
  docs at all and so were skipped before any lookup happened, now get one.

  `c-embedded-review.md` is still unreachable: nothing detects a C project, so
  there is no stack to attach it to. That needs a detection change, not a map
  entry.

- **Only `dev/` and `review/` are searched, in that order.** Briefly, every
  subdirectory of `reference/` was, which made "which copy of a doc wins" an
  alphabetical accident — an old copy parked in `reference/archive/` would beat
  the current one in `reference/review/` the day the `dev/` copy was renamed,
  and be injected with no sign anything was wrong. A hidden directory counted
  too, so a `reference/` that was a bare git checkout read as "the kit is
  installed" and then found nothing. The list is now fixed and its order is the
  precedence.

- **A standards doc the map names but nobody wrote is reported again.** When a
  project's docs resolved to nothing, the log line meant to say so — the only
  thing that makes a missing standard visible — crashed, and the crash also
  abandoned every other project in the same turn. So a session whose working
  directory held one project with no docs got no standards for the project it
  was actually pointed at either. The line is back, and now names the
  directories it searched; one project failing no longer costs the others.

- **A doc that cannot be read no longer hides a readable copy of itself.** A
  broken symlink or a file with bad bytes in `reference/dev/` used to abandon
  that filename entirely, including a perfectly good copy of it in
  `reference/review/`. Unreadable is now treated like absent — the next
  directory is tried — and it is logged as a warning rather than passed over.

- **The duplicate-detection tokenizer no longer discards code spans.** Text
  inside backticks was deleted before comparison, which is exactly where the
  distinctive words of a tool-usage rule live — `gh pr merge --squash
  --delete-branch` against a memory line describing that same command scored
  **0.00**, a total miss, because after stripping both sides had almost nothing
  left to compare. Code spans are now kept and tokenized; that pair scores
  **0.80**.

  Two things read this tokenizer, and they are different comparisons, so they
  were measured separately.

  *Near-duplicate flagging* — the `## Routing Warnings` section of a proposal
  and the draft-time gate in `dream_prescreen.py` — scores every backlog item
  against the whole memory corpus. Backtested over the real 602-item backlog
  against memory before and after consolidation, it goes from 317 true / 24
  false to **332 true / 25 false**: +15 real duplicates caught for one extra
  false positive, with the threshold unmoved.

  *Supersede edits*, at the top of every proposal, compare one learning against
  the lines of its one target file and take the single best match. The backlog
  backtest says nothing about that path. Measured on its own against the live
  4,651-line memory corpus at the same threshold: **24 new matches, 6 matches
  lost, and 0 edits retargeted onto a different line**. More conflicts
  surfaced, and none silently repointed at something else.

  Two things were tried and rejected on the same data, recorded so they are not
  retried: collapsing plural to singular scored *worse* at every threshold
  (12.7:1 against 13.2:1 baseline), and lowering the threshold to 0.30 scores
  better on this measure but spends precision on supersede edits, which is the
  trade this change exists to avoid.

## [0.50.0] - 2026-08-16

### Fixed

- **A section that exists no longer reads as missing.** The dream routing gate
  indexed only `##` headings, and memory files keep most of their sections at
  `###` — measured on one real corpus, 302 H2 against 354 H3/H4, so **54% of
  real sections were invisible to it**. It reported `section "X" does not exist`
  for sections plainly present, and then suggested rerouting the item to
  whichever *other* file happened to own an `##` by that name. Since a flagged
  item is never applied unreviewed, each false positive turned an
  auto-applicable item into a manual routing question pointed at the wrong file
  — on one proposal, 3 of 5 routing warnings were this bug. Existence checking
  now spans `##`–`####`. Collision checking stays `##`-only, because the
  uniqueness invariant it enforces is about `##` specifically; widening it
  would have traded the old false positives for new ones.

  When a section genuinely is misrouted, the **reroute now names the `##`
  owner**. Widening existence to `##`–`####` also widened the suggestion, which
  took whichever owner sorted first at any depth: on one real corpus 8 section
  names had more than one owner, and in 6 of them the alphabetically-first
  owner held the name at `###` while a *different* file held the `##` — so the
  suggestion pointed away from the only file the routing invariant says the
  item can live in. Every owner is still listed in the warning.

- **Conflict resolutions can now be decided.** Items under
  `## Conflict Resolutions` were the only kind with nowhere to record a
  decision: `--mark-processed --kind` took `update` and `action` only. A
  reviewer read one, decided, and the decision evaporated — and the conflict
  with it. The section is regenerated, but only from learnings blocks that have
  not been consolidated yet, and the run that drafts a conflict is the run that
  records its source block; so once the proposal was archived or folded forward
  there was nothing left to re-derive it from. `--kind conflict` now marks
  them, keyed by the memory file and line in the item's heading
  (`--kind conflict --file dolcebot.md --index 453`). `/dream-remember` gained
  a step telling you to do it, and what the three outcomes mean; previously the
  skill did not mention the section at all.

- **A failed memory write during unattended consolidation no longer aborts the
  run.** `dream --auto` wrote each memory file inside a loop with no per-file
  error handling, so an `OSError` on the second file left the first already
  rewritten, the state never stamped, and nothing recording either. It now
  counts a failed write as a failed apply, which keeps every source learnings
  file on disk so the next run retries from intact material — the same handling
  the triage path next to it already had.

### Added

- **`dream.py --reconcile`** — finds proposals that were fully decided but
  never filed, and finishes them: archive, stamp, then collect spent learnings.
  A proposal with zero pending items sitting in the dreams root is
  indistinguishable from a pending one; it keeps the dream gate nudging, and
  because learnings are only collected once their proposal is archived, it also
  pins the learnings files so the next run re-drafts items already in memory.
  One real proposal sat like that for two days and contributed 54 re-drafted
  items to its successor. Nothing caught it because `/dream-remember` only ever
  inspects the newest proposal, so re-running the skill could not fix it — the
  skill now runs `--reconcile` as its first step. Pair with `--dry-run` to see
  the list before anything moves.

  Three things it is careful about, because it runs *before* you read anything
  and what it authorizes cannot be undone. It counts an **undecided conflict
  resolution** as pending, so a proposal whose updates are all decided but
  whose conflicts are not stays in the root instead of being archived, stamped
  and having its source learnings collected — after which no later run could
  re-derive the conflict. It **files by what the review decided**: a proposal
  every item of which was rejected lands in `dreams/rejected/`, not
  `dreams/applied/`, matching what `/dream-remember` Step 6 has always asked
  for by hand. And a proposal it **cannot read** is named in the output,
  counted as still pending and left where it is, with a non-zero exit — the
  same fail-closed handling `--gc-learnings` gives an unreadable proposal.

- **Triage now logs which side won each contested label.** Where it previously
  logged only *how many* items the extractor and the judge disagreed about, it
  now records per item — at debug level — both pairs, the resolved pair, and
  which side each half came from, plus a per-kind summary. Resolution is
  strictly one-way (the more cautious half always wins), so every disagreement
  can only push an item toward manual review, and the bare count could not
  distinguish label noise from genuine caution. On one proposal, 39% of items
  had a contested label and 32% ended `kind: RULE`, which never auto-applies —
  the new breakdown answers whether those items are `RULE` because both passes
  said so (the rubric working) or because one said `FACT` and lost (a labelling
  cost). No behaviour change: `kind: RULE` still never auto-applies.

## [0.49.2] - 2026-08-16

### Changed

- **The log-retention docs now name the one log retention does not reach.**
  README §"Log layout & retention" said `MULTIPLAI_LOG_RETENTION_DAYS` "applies
  uniformly", which is true of everything in `data/logs/` and silent about
  `scripts/.warmup-deferred.log` — the file 0.49.1 capped at 256 KB with one
  rotated generation. Bounded by size, not by age, and for a reason worth
  stating: `hooks/run.sh` exists for the window in which `multiplai_core.paths`
  is not importable, so it cannot ask where `data/logs/` is. Docs only.

## [0.49.1] - 2026-08-16

### Fixed

- **`scripts/.warmup-deferred.log` no longer grows without a bound.** It is the
  one log the plugin writes outside `data/logs/`, so the retention sweep that
  honours `MULTIPLAI_LOG_RETENTION_DAYS` never saw it, and every lifecycle hook
  dropped during a build window appended to it forever.

  It stays where it is: `data/logs/` is resolved by `multiplai_core.paths`
  through several fallbacks, and `hooks/run.sh` exists precisely because that
  package may not be importable yet — so it cannot ask where the directory is
  without guessing. Instead the file is capped at 256 KB and rotated to
  `.warmup-deferred.log.1` by rename, which is atomic and safe against the
  detached subshells that may be appending at that moment. One generation is
  kept, so the pair is bounded at 512 KB. A log under the cap is left alone: a
  dropped session is rare and the record of the last one must survive the next.

Standalone (vanilla Claude Code) hardening: this release is about the
plugin working properly with no kit, no `CLAUDE_CONFIG_DIR`, and no
options configured.

### Fixed

- **First session start can no longer be killed mid-install.** On a fresh
  install the first hook fire must build the plugin's Python environment
  (a full `uv` resolution, including cloning `multiplai-core`) — and the
  hook timeouts (60s SessionStart, 30s/10s per-prompt) could kill that
  build mid-flight, leaving a half-built environment and silent hooks.
  The first fire now starts the build **in the background**, tells you in
  one line that hooks go live on the next prompt (~1 min), and gets out
  of the way. Concurrent hooks on a cold start share one build; a failed
  build retries at most every 15 minutes and logs to
  `scripts/.warmup.log`.
- **Closing a tab during that first minute no longer loses the session.**
  A `SessionEnd` or `PreCompact` firing while the environment is still
  building has work with no second chance — it writes the marker that
  carries the session's diary entry and learnings. Those hooks now run
  as soon as the build lands instead of being skipped, and if the build
  never lands the loss is written to `scripts/.warmup-deferred.log`
  rather than going unrecorded.
- **A build that already failed now says so.** For 15 minutes after a
  failed build every prompt was told a build was in progress and that
  hooks would go live on the next prompt — neither true — and nothing
  named `scripts/.warmup.log`, where the error is. The message now says
  the build failed, names that log, and says how to retry.
- **A relocated environment is no longer read as a permanent cold
  start.** The plugin guessed where `uv` puts its virtualenv. With
  `UV_PROJECT_ENVIRONMENT` set — or a sideloaded copy sitting under an
  unrelated project — the guess never matched, so every prompt started
  another background build and no hook ever ran, behind a "first run"
  banner that never went away. It now asks `uv` the same question its
  own commands ask.
- **Building the environment by hand no longer leaves the hooks gated.**
  Pre-warming is now `sh <plugin-dir>/hooks/run.sh --warm` (what
  `claude --init-only` and `/multiplai-context:setup` run): it builds
  *and* clears the in-flight marker. A bare `uv sync` builds but leaves
  the marker, and the hooks stayed quiet for another 15 minutes — which
  was exactly the recovery the old troubleshooting notes gave you.
- **A slow first build is no longer interrupted by a second one.** The
  15-minute "the builder died" timer read a timestamp nothing refreshed,
  so a first resolution over 15 minutes (it clones `multiplai-core`; a
  slow link makes that ordinary) had a second `uv sync` started
  alongside it, truncating the first one's log. Liveness of the running
  build decides now, not its age.
- **`autoCompactEnabled: false` is now respected on vanilla installs.**
  The checkpoint nudges read the user-level `settings.json` to tell
  disabled auto-compaction from steered auto-compaction, but only looked
  at `$CLAUDE_CONFIG_DIR` — which vanilla Claude Code never sets — so the
  disable was invisible everywhere but kit installs. Every
  `CLAUDE_CONFIG_DIR` read now falls back to `~/.claude`, through one
  shared resolver. The file is read only when auto-compaction steering is
  actually configured, so it stays off the every-prompt path.
- **`/multiplai-context:qmd-search` degrades honestly when qmd isn't set
  up.** With no `resources_dir` configured, or with the configured mode
  unreachable, the skill now answers "resources search isn't set up" —
  naming what is missing and the fix — and offers a plain grep search
  instead of running commands that cannot work. The check matches the
  mode you configured: `http` mode is checked against the daemon's
  health endpoint, not against a `qmd` binary it never uses.

### Added

- **A `Setup` hook: pre-warm with `claude --init-only`.** Claude Code's
  Setup event (`--init-only`, or `--init`/`--maintenance` in `-p` mode)
  now builds the plugin environment synchronously — the right entry point
  for CI and scripted installs. `/multiplai-context:setup` also warms the
  environment as its step 0, so onboarding never races the background
  build. Unlike a session hook, it **fails** when `uv` is missing: its
  whole job is to leave a working environment behind, so passing would
  green-light an install that cannot run a single hook.

## [0.48.0] - 2026-08-15

### Changed

- **Extended thinking is now off at the mechanical model calls**, not just the
  memory router's: diary/learnings extraction, the checkpoint writer, the
  memory doctor's duplication confirmation, the offline utilisation judge,
  per-project `now/` summaries, and catalog generation. These calls are
  parse/classify/summarise work over input the model sees in full; a cold
  no-tools call drops 18.4 s → 2.9 s with thinking disabled (measured
  2026-08-09), so the background pipelines stop paying ~15 s of latency per
  call for deliberation they never needed. Each subsystem has its own opt-back
  option — `extraction_thinking`, `checkpoint_thinking`,
  `duplication_thinking`, `utilisation_thinking`, `now_thinking`,
  `catalog_thinking` — with the same contract as `router_thinking`: default
  off, set `true`/`1`/`yes`/`on` to restore the SDK default. Values are read by
  the same parser as every other boolean option here, so a value that is
  neither true nor false logs a warning and leaves the default standing
  instead of being guessed at.

  **Two calls deliberately keep extended thinking on.** Dream proposal
  generation, as before; and now the memory doctor's **contradiction** pass,
  which reads as mechanical from the outside and is not — it has to hold two
  statements side by side and decide whether they can both be true, which is
  why it already carried a 600 s timeout against the duplication pass's 180 s.
  A missed contradiction is silent, and none of these calls runs against a
  deadline that 15 s would blow, so the trade only made sense where the
  reasoning was not being used. There is no `doctor_thinking` option: the
  switch that ships is `duplication_thinking`, named for the one pass it
  actually moves.

- **The old-dependency warning now names a fix you can actually perform.** If
  the installed `multiplai-core` cannot carry a `thinking=` setting, it is
  dropped with one warning per call path and behaviour degrades to today's
  (thinking on), never to an error — and that warning now tells you to
  reinstall the plugin from the marketplace, rather than to re-resolve a
  lockfile that does not exist on an installed plugin. No version number is
  quoted at you either: the capability is what matters, and the version that
  the old text named was never published.

## [0.47.0] - 2026-08-12

### Fixed

- **`/dream --triage` can now write to memory files that carry a
  `**Last Updated:**` line.** It never could: the applier was told to refresh
  the date, and the safety check that makes an unreviewed write reviewable
  refuses any result where an original line changed — so the two cancelled and
  the whole file was left unapplied. 18 of 29 memory files in the reporting
  workspace carry that line, including every high-traffic one, and a real run
  cleared 48 items and applied 0. The date is now restamped in code after the
  check passes, and the applier is told to leave the stamp alone. The check
  itself is unchanged: an applier that edits the stamp anyway is still refused.
- **A `**Last Updated:**` written as an example inside a memory file is no
  longer restamped.** Only the file's own header — the first one, the one
  freshness reads — is touched. Files documenting the marker (there is at least
  one) had their sample dates silently moved to today.
- **`/dream --auto` writes memory files atomically**, the way `--triage`
  already did. It deletes the source learnings once every target has applied,
  so a write interrupted half-way was the one loss with nothing left to retry
  from.

### Added

- **The routing gate now catches duplicates that were reworded, not just
  copied.** Its existing check needs eight consecutive words to match, so an
  item restating a rule in its own words passed clean — and the drafter is
  shown only each memory file's section headers, never its bullets, so it
  re-proposes what is already there. On the 2026-08-10 backlog that was 1 of 14
  items on `python.md`, 6 of 39 on `git-policy.md`, and 12 of 17 rejections on
  `claude-code-tools.md` restating a rule from an always-loaded `CLAUDE.md`.
  Each item is now also compared line-by-line against the whole corpus — memory
  files, always-loaded `CLAUDE.md`s, shared banks — and a hit is written into
  `## Routing Warnings` quoting the line to open.
- **Items in one proposal that restate each other are grouped.** Near-duplicates
  sourced from different days were surviving both merge passes and being applied
  as separate bullets. Entries aimed at the same file are now clustered, one
  warning per cluster (`#3, #9, #21 are near-duplicates of each other`), so a
  batch restating one rule twenty times reads as one merge rather than 190
  pairwise warnings. Every item in a cluster is held for review, not just the
  first one named.
- **`dream_prescreen.py` now runs both halves of that screen**, so a review-time
  re-run re-detects items restating *each other* and not only items restating
  the corpus. It previously ran the corpus half alone.

Both are **warnings, not filters** — nothing is dropped without you. Warnings
name which kind of file they hit: an always-loaded `CLAUDE.md`, a shared bank,
another memory file, or the target itself. Recall on the reworded kind is
roughly half (the measure ignores text inside backticks), so a clean gate still
is not a certificate; `dream_prescreen.py` re-runs the same screen at review
time, which is what you want anyway once the corpus has moved under a long
triage.

Headings, table rows and anything inside a fenced code block are not treated as
corpus lines, so a warning never points at a code sample. Past the first 40
near-duplicate warnings the quoted line is dropped from the warning text (the
location and score stay, and every flagged item still gets its own line) — the
section is read inside the review whose context window it exists to protect.

## [0.46.0] - 2026-08-11

### Added

- `/dream-remember` now **decides the post-triage remainder itself** instead of
  handing it back as a review queue. A new resolve pass works one target file at
  a time: it rejects corpus duplicates, merges within-batch repeats into a single
  entry, settles contradictions on source date and specificity, and verifies
  checkable claims against the actual source before writing them. You are asked
  only about genuine policy calls — batched into one question per file, not one
  per item. On a 583-item backlog this was the difference between a walk that
  gets abandoned partway and one that finishes.

  **What the resolve pass may never apply, whatever it decides:** `kind: RULE`
  items under any provenance (including a correction from you, and including
  every item with no `**Provenance:**` pair, which reads as `INFERENCE`/`RULE`),
  `[RULE-PROPOSAL]` items, and anything targeting a `CLAUDE.md`/`AGENTS.md` or
  revising an existing line. Those still come to you. The resolve pass writes
  with the editor rather than through `dream.py`, so the in-code floor that
  refuses them does not run on its writes — the restriction has to hold by
  instruction, and it is now stated where the decision is made. The pass is
  skipped entirely under `memory_write_mode: review` and `/dream-remember
  review`, which mean nothing is applied without you.

  The `## Routing Warnings` gate is also read **before** anything is applied,
  not after. It moved to the start of the resolve pass; Step 3 now reports what
  it held and what was done about each warning.
- `dream_prescreen.py` — shows the reviewer which pending items look like they
  already exist somewhere in the corpus: every memory file, the **always-loaded
  `CLAUDE.md` files** (the global one under `$CLAUDE_CONFIG_DIR`, the workspace
  one), and the **shared memory banks**. The drafter is shown none of those, so
  it re-proposed their rules on every run.

  It prints **one line per lead** — score, item, and the corpus location to
  open — because it runs inside the review whose context window it exists to
  protect. Measured on the real 602-item backlog against a 4,922-line corpus:
  31 KB of leads, 2.6s. Printing every item with its body and neighbours,
  which is what `--verbose` still does, costs 412 KB (~103k tokens). An item
  whose insert text does not parse is reported `UNSCREENABLE`, never as a
  clean 0.00.

  It screens on symmetric Jaccard over content words (`lib.conflict_edits`),
  the measure that module already calibrates, rather than a new one. That was
  chosen by measurement, not taste: scoring the 602 items against memory after
  consolidation (they are in there) and against memory one commit before (they
  are not) separates 316 true from 25 false at threshold 0.35, where a
  containment measure separates 523 from 417.

### Changed

- **The routing gate's dedup half now screens the always-loaded `CLAUDE.md`
  files and the shared memory banks**, not just the memory files. A proposed
  line that restates one of those is flagged in `## Routing Warnings` as
  *"already present in an ALWAYS-LOADED file"* at draft time. Those files are
  dedup evidence only — they never join the H2 section registry, so an ordinary
  heading in a `CLAUDE.md` cannot become a phantom misroute. Gate runtime on the
  602-item proposal is unchanged at 0.1s (the per-file n-gram index is now
  cached instead of being rebuilt once per entry).
- Step 3 no longer opens with "review the file and tell me `all`/`none`/numbers".
  The normal ending is a report of what was applied, merged, and rejected, with
  the reasoning. The decision-list prompt remains for an explicit
  `/dream-remember review`, for the item classes the resolve pass may not apply,
  and for the specific items that need a policy call.

### Fixed

- Picking "the" pending proposal is now one implementation, in
  `lib.dream_processed.latest_pending_proposal`, and it selects by **mtime**.
  Lexical order picks the *oldest* same-day proposal, because a re-run's `-2`
  suffix sorts before the base name (`-` is 0x2D, `.` is 0x2E).

## [0.45.0] - 2026-08-11

### Changed

- **Dream proposal entries are numbered `1..N` across the whole file.** They used
  to restart at `1` under every `## Updates for` heading, so `#1` named as many
  entries as you had target files and you had to say the filename out loud to
  refer to one. Now a bare number identifies exactly one entry, and
  `/multiplai-context:dream-remember` takes `skip 14` at face value.

  This holds after the critic runs, not just at merge time. `_renumber_all` in
  `lib/proposal_edits.py` re-sequences entries after every applied directive, so
  a counter that reset per section there would have quietly restored per-file
  numbering on any proposal the critic touched — nearly all of them.

  Proposals generated before this version keep their old per-file numbering.
  They are **not** renumbered on upgrade — a number is the identity the review
  hub records decisions against, so rewriting it would move receipts that may
  already be queued against an in-flight proposal.

### Removed

- **Action items are gone.** Dream no longer emits an `## Action Items` section,
  and `/multiplai-context:dream-remember` no longer writes
  `PLANS/dream-actions-{date}.md`. In practice that file was a directory of
  stale to-dos nobody worked from: a change worth making gets made, and one that
  isn't doesn't need a record.

  The **disposition survives** — a learning that asks the toolchain to change its
  own code, config, or file structure still never becomes memory. It is now
  dropped into `## Filtered Out` under **Toolchain change-request**, so the
  reviewer still sees it and still sees why. The one carve-out is unchanged: if
  such a learning also carries a general principle that outlives the change, the
  principle is kept as a memory entry.

  Reviewing an archived proposal that predates this version still works — the
  `A{N}` parsing and the `--kind action` decision path are retained for exactly
  that. There is simply no file to write.

  A `TO-ACTION` directive from the critic is now parsed as a `DROP` carrying the
  reason `toolchain change-request`. The critic is a model rather than a parser
  and emitted that shape for months; left alive it would have built an
  `## Action Items` section into a fresh proposal, which the review skill now
  says cannot exist. The dropped entry still appears in `## Filtered Out` with
  its source citation inline, so nothing loses provenance.

## [0.44.0] - 2026-08-11

Fixes from the 2026-08-10 hooks code review — every finding verified
against source before changing anything. Nothing here adds a feature; all
of it makes the hooks fail less, fail loudly, or clean up after themselves.

### Fixed

- **A killed PreCompact can no longer block checkpointing for ten minutes.**
  The synchronous pre-compaction writer used to be granted 600s inside a
  300s hook: the harness kill skipped the marker release, and until the
  marker aged out no new checkpoint writer would spawn — right after
  compaction, when context grows fastest. The wait is now clamped under the
  hook budget, and the writer runs under process-group supervision, so a
  timeout actually stops the work instead of orphaning it.

- **A slow checkpoint or extraction child is no longer double-launched.**
  The drain's stale window (900s) sat inside a child's legitimate worst
  case (600s x 2 attempts), so a slow-but-alive writer could be declared
  dead, requeued, and run twice against the same state — paying the model
  cost twice. The window is now derived from the real worst case (1800s),
  and the checkpoint drain also defers to a writer that is visibly
  mid-write.

- **A stalled memory router degrades instead of costing the prompt its
  memory.** The llm router's timeout now covers client creation (the CLI
  spawn), so a stall there falls back to the offline ranking rather than
  running silently into the hook kill with no memory and no log line.

- **Subagent sessions no longer leak into your records.** All seven hooks
  now skip SDK child sessions uniformly. The gap that mattered: a
  SessionEnd firing for a subagent transcript queued a full LLM extraction
  of it into your diary. Routing, registry stamps, and session-start spawns
  are guarded too.

- **The uv-missing warning can no longer steer compaction.** The warning
  used to reach PreCompact's stdout, which is appended to the compaction
  prompt as instructions; it is now stderr-only there. And before declaring
  uv missing, hooks check `~/.local/bin/uv` (uv's default install dir), so
  a PATH-less login shell no longer disables every hook on a machine where
  uv is installed.

- **Routing quality on real conversations.** The "last assistant response"
  used for disambiguation now skips subagent replies and is capped at 2000
  chars, so a giant previous turn cannot out-vote a short prompt. Both
  transcript tail readers retry with a wider window when one oversized
  tool-result line swallows the tail — previously that read as "empty
  session" and silently skipped a checkpoint pass.

- **State files that must not tear are written atomically** (nudge
  cooldowns, the `!keepgoing` override watermark, degraded-writer alerts),
  dead checkpoint markers quarantine under `failed_checkpoints/` instead of
  masquerading as failed extractions, orphaned atomic-write temp files are
  swept, and DEV REFERENCES re-announce after a compaction removes them.

### Changed

- **Hook output is now only `hookSpecificOutput.additionalContext`.** The
  legacy duplicate top-level `context` / `*_files` keys are gone; they had
  no consumer left and doubled hook stdout on every prompt at large
  injections. If you scripted against them, read `additionalContext`.

- The seven byte-identical uv-guard preambles in `hooks.json` are one
  shared `hooks/run.sh`; timeouts and the once-per-24h warning behaviour
  are unchanged.

### Removed

- Dead internal API (no external callers): `checkpoint.marker_name`,
  `checkpoint.session_marker_name`, `TokenOverlapRouter._score_corpus`,
  `extraction.extract_units_and_disposition`,
  `context_manager._read_memory_files`, `session_stop._spawn_writer`.

## [0.43.0] - 2026-08-10

### Added

- **Every hook now logs when it starts and when it finishes.** All seven hooks
  (`session_start`, `context_manager`, `checkpoint_nudge`,
  `session_notification`, `session_stop`, `session_end`, `pre_compact`) write a
  `HOOK_ENTRY` line before doing any work and a `HOOK_EXIT` line with the total
  duration, the interpreter startup cost, and a per-stage breakdown.

  This exists because of a failure you cannot otherwise see. Claude Code kills
  a hook that exceeds its timeout, and a killed process cannot log its own
  death — so when a `UserPromptSubmit` hook was killed at its 30s ceiling on
  2026-08-10 and the prompt lost its injected memory, the logs held **nothing
  at all** for that session. An `ENTRY` with no `EXIT` is now the tombstone.

- **`log_doctor --hooks`** — a per-hook report: run count, runs killed at their
  timeout (with the session id and timestamp of each), p50/p95/max duration,
  startup cost, and which stage inside the hook spent the time. Each hook's
  latency is priced against the ceiling declared in `hooks/hooks.json`, so
  "p95 is 60% of budget" is a number rather than a hunch. Documented in the
  `log-doctor` skill.

  Three things to know before you gate on it:

  - It scans the **last 7 days** unless `--days N` says otherwise. A kill is a
    point-in-time event; with no window one old kill is re-counted forever.
  - It exits **1 when a run was killed that was not expected to be**.
    `session_end` is excluded — the harness kills SessionEnd hooks within
    seconds by design, which is why that hook defers its real work. Its orphans
    are labelled in the report, not counted as failures.
  - A killed run has no duration of its own, so it enters the latency sample at
    its ceiling and the finding says the p95 is a **lower bound**. Left out, a
    hook timing out on half its runs would show a p95 drawn only from the fast
    half and read healthiest exactly when it is worst.

  `--json` carries the same defanged text and untrusted-content notice as the
  markdown report: hook names, stage names and outcomes all come from log text.

- **`--probe-check --scenario hook-timing`** verifies the instrumentation is
  alive. It requires both halves of the pair, for both `context_manager` and
  `checkpoint_nudge`, on purpose: a probe that accepted `HOOK_ENTRY` alone would
  pass on exactly the failure this is meant to catch, and one that named a hook
  without asserting on it would pass on a hook that stopped firing entirely.

  What it cannot see: a hook killed *before* Python starts — during `uv`
  dependency resolution or interpreter startup — writes no line at all, not even
  the `ENTRY`. Zero lines for a session you know had one is that symptom, and
  the `startup p50` column does not cover it.

- **Hooks that mostly stay silent now say why.** `checkpoint_nudge` records an
  `outcome` (`disabled`, `under_threshold`, `cooldown`, `auto_compact_pending`,
  `hard_stop`, `nudged`), `context_manager` records one on the inject-nothing
  path too (`no_match`, `cooldown`, `abstained`, `continuation`, `injected`),
  and `pre_compact` records whether it queued. A hook that has gone quietly
  dead and one that is working as designed used to look identical in the logs.

## [0.42.0] - 2026-08-10

### Added

- **`memory_router: llm_hybrid`** — a third strategy. The LLM picks the files,
  then the offline router's two gates decide which survive: the breadth gate
  (a file needs ≥2 distinct `intent_domains` tokens in your prompt, or one
  verbatim multi-word domain phrase) and the `anti_domains` veto.

  Why you might want it: backtested on 300 real prompts, `llm` finds the right
  file 2.4× more often than `token_overlap` (F1 48.6 vs 20.0) but does not know
  when to stop — on the first prompt of a session it injects 170,940 bytes
  against `token_overlap`'s 93,001, and correctly injects nothing only 51.3% of
  the time against 76.9%. `llm_hybrid` is the attempt to have both.

  It is **opt-in and unmeasured.** The gates can only remove, so every drop is a
  recall risk — a file the LLM was right about, phrased in words your catalog
  does not carry, gets filtered. Dropped picks are logged (`HYBRID_GATE`) rather
  than discarded silently, and `ROUTING_SCORES` still reports scores, so you can
  see what the gate cost you. Score it on a golden set before trusting it.

- **`router_thinking` option** — extended thinking on router calls, default
  **off**. This is the change that makes `memory_router: llm` usable at all: a
  cold routing call takes 18.4 s with thinking on and 2.9 s with it off, against
  a 30 s hook budget. Set it to `1`/`true` to restore thinking.

  Needs `multiplai-core` ≥ 0.14.0. Against an older core the plugin logs a
  warning, keeps thinking on, and still works — it does not crash and does not
  silently stop routing.

### Fixed

- **A router reply that isn't JSON no longer reads as "nothing is relevant."**
  It now degrades to the offline router, like every other failure on that path.
  Before, a model that answered with prose produced an empty pick, which the
  context manager treats as a deliberate abstention — and abstention suppresses
  its own recency net, so those prompts got **no memory at all**, indistinguishable
  in the log from correct behaviour. An empty but well-formed reply is still a
  genuine abstention and still injects nothing.

- **A genuine abstention now says so in the log** (`LLMRouter abstained`).
  Three different outcomes previously printed the same `picked: memory=0` line —
  abstained, replied unparseably, timed out — which made an `llm` router
  regression invisible.

### Changed

- The latency notes in `memory_router.py` said the LLM router's cost was a ~12 s
  SDK cold start. That does not reproduce: the subprocess spawn is worth 4–6 s,
  `effort` is inert on this path, and extended thinking was the real cost. The
  comment now carries the measured numbers and the backtest results, because the
  old figure is what justified the previous default.


## [0.41.0] - 2026-08-10

The rebuild pointer 0.39.0 started writing for every session was being filed
under the wrong name, in two independent ways, and nothing was ever collecting
the checkpoints it pinned.

### Fixed

- **A checkpoint written after your tab closed can now be found again.** The
  pointer's filename included the hostname of whatever process wrote it. For a
  tab that is *closed* rather than `/clear`-ed, that writer is the host drain —
  a throwaway container whose name (`ab5d84093a24`) no session will ever have —
  so the pointer was orphaned the moment it was written. Since closing a tab is
  the common case, the end-of-session checkpoint 0.39.0 added was reaching
  almost nobody. The pointer is now keyed to the container the session itself
  ran in.
- **A session that worked inside a sub-project no longer files its pointer
  under that sub-project.** Claude Code's working directory follows shell
  navigation, so a session rooted at your workspace that spent an hour inside
  `PROJECTS/something` recorded its rebuild pointer under `something`. A
  `/clear` from the workspace root then looked under the workspace, missed, and
  fell back to an older pointer — which on 2026-08-10 belonged to a completely
  different session, silently rebuilding the wrong window. The project is now
  pinned when the session starts and does not move.
- **`Written on host:` in `now/<project>.md` names the session's machine, not
  the drain's.** Same root cause; the header was attributing summaries to
  containers that never ran anything.
- **The end-of-session log line carries its session id.** `session_end.log`
  printed `session:--------` for the checkpoint half of the hook, so
  `grep session:<id>` silently omitted it.
- **A leftover `fleet.txt` is deleted again.** The one-line status count was
  retired in 0.18.0, which also deleted leftovers; 0.19.0 dropped the deletion
  while the changelog kept promising it. Any workspace that ever ran ≤0.17.1
  has been carrying the file since.

### Added

- **Checkpoints are collected.** A session's saved working state is deleted
  once nothing has touched it for `checkpoint_gc_days` (default 7) — measured
  from its files *and* its last session event, so a quiet-but-open tab keeps
  its checkpoint. Expired rebuild pointers are cleared on the same pass.
  Previously nothing was ever collected: 216 directories had accumulated on one
  workspace, the oldest from Jul 7. Set `checkpoint_gc_days: 0` to keep
  everything, as before. This deletes intent, files touched and errors from
  past sessions — that is the point; it was never meant to be permanent.
- **The `checkpoint_*` options appear in the plugin's settings.** All eleven
  were documented and read by the code but declared nowhere, so nothing
  validated their types or defaults and no settings UI could show them. Three
  of the documented defaults were also stale (`checkpoint_stale_hours` is 0.5,
  not 3; `checkpoint_timeout_s` is 600, not 240).

### Known limitation

A checkpoint from a closed tab is still only claimable by a session in the same
container. Under one-container-per-session that means a new tab will not find
it; the pointer is now correct rather than nonsensical, which is a smaller
claim than "closed tabs are restorable". Tracked separately.
## [0.39.0] - 2026-08-09

### Changed

- **Your working state is now saved about every 30 minutes instead of about
  once a session.** Each save covers only the half-hour since the last one, so
  it finishes in seconds. The old 3-hour cadence let a session's unwritten
  backlog grow until the save could no longer finish at all — and because the
  bookmark only moved on success, every failed attempt was handed a bigger
  backlog than the one before it. On 2026-08-08 that cost eight consecutive
  failed saves over 18 hours and a `/clear` that restored a different window's
  work. Set `checkpoint_stale_hours` back to `3.0` for the old volume.
- **Sessions between 100K and 200K tokens get a save cadence at all.** The
  token-based refresh used to apply only above the 200K handoff threshold, so
  a session sitting at 150K had nothing but the one-off band crossings. It now
  applies at every level: grow 25K tokens past your last save and one is
  written.
- **A save that fails no longer leaves you with nothing.** The previous save is
  kept verbatim and the window the model could not summarise is appended to it
  raw — your turns and the tools that ran — under an `## Unsummarised since`
  heading, rather than discarded. The save timeout also went from 4 to 10
  minutes.
- **Two failed saves in a row now tell you.** Previously they were a line in a
  log file; the eight failures that caused the incident above reached nobody.
- **The diary reads only what is new.** A session that extracts twice (once at
  compaction, once at the end) used to re-read its whole transcript the second
  time. It keeps appending, as before — nothing in the permanent record is
  overwritten.

### Fixed

- **`/clear` saves your work before discarding it.** Nothing ran on that edge
  at all, so everything since the last save went with the window. Closing a
  tab queues the save instead, and it is written after the container exits.
- **Any session with a save on disk can now be restored.** The restore pointer
  was written only above 200K tokens and only after a successful save, so a
  clean 143K-token session left a perfectly good save that nothing could find.
- **Two windows open on the same project stop overwriting each other's restore
  pointer.** The pointer is keyed by project *and* window, so `/clear` restores
  the window you actually cleared — not whichever one saved last.
- **`now/<project>.md` says which window wrote it.** One file per project and
  several windows on it meant the last writer won silently; on 2026-08-08 a
  freshly-cleared window read another window's summary as its own prior work.

<!-- MERGE ORDER: this entry is 0.38.0 and 0.37.0 belongs to the memory-doctor
     branch, which is a SIBLING of this one rather than an ancestor. Both branch
     off 0.36.0, so this file has no 0.37.0 section until the doctor branch lands.
     Merge the doctor first and the history is continuous; merge this one first
     and `main` ships with 0.37.0 missing. `check_changelog.py` cannot catch it —
     it tests `old != new`, which is inequality only (and would also pass a
     downgrade). Re-numbering this to 0.39.0 does not help: it moves the hole to
     0.38.0 and makes it permanent. -->
## [0.38.0] - 2026-08-09

### Fixed

- **A slow router no longer costs a prompt its memory.** With
  `memory_router: llm`, a model call that timed out returned no picks — and a
  router that *ran* and picked nothing is read downstream as a deliberate "no
  memory is relevant", which suppresses the recency safety net. So a slow
  machine produced prompts with **no memory injected at all**, recorded as one
  warning in the log and otherwise indistinguishable from correct behaviour.
  Measured on five real prompts replayed under load: five of five injected
  nothing.

  A failed call now answers with the `token_overlap` router instead — the same
  degradation the plugin already applied when no model client was available at
  all, for the same stated reason. The worst case is now the offline ranking
  you would have had anyway, not silence. A model that genuinely runs and
  chooses nothing still abstains; only failures degrade.

  **Do not lower `router_timeout_seconds` to compensate.** It looks like you
  should — a timeout now costs you only the offline ranking, so waiting for an
  answer you are about to discard reads as pure latency. It was measured, on
  the same five real prompts, and it does not work. Uncapped, those prompts
  take a **median 27.4 s** (range 14.8–52.6 s), so there is no ceiling below
  the median that buys anything: at 15 s, three fell back and the two that
  finished returned no picks at all. The shipped 25 s default already sits just
  under the median, which is the worst place for it — roughly half of all
  prompts lose their routing and pay the full ceiling to do so.

  The cost here is the Agent SDK's ~12 s per-call subprocess spawn, not the
  model, so no timeout value fixes it. Until that cold start is gone,
  `token_overlap` is the better setting on most machines.

### Added

- **Shared memory banks.** A *bank* is a git repository of memory files. Point
  the plugin at one and its files route and inject exactly like your own — the
  router picks a team file over a personal one on relevance alone, because
  relevance is relevance. You declare banks in `memory-banks.yaml` at your
  workspace's `.multiplai/` root:

  ```yaml
  memory_banks:
    - name: dolcebot-team
      remote: git@github.com:you/memory-bank.git
      mode: propose        # ro = read only · propose = contribute by PR
      sync: session-start
  ```

  **If you configure nothing, nothing changes.** No file, no banks, and every
  path behaves exactly as it did in 0.36.0.

- **You can always tell whose note you are reading.** Content from a shared
  bank is injected under a heading naming the bank and its last-updated date,
  and its body sits inside an `<untrusted-content>` fence. That fence is the
  point, not decoration: a bank is memory *other people write*, arriving over
  the network on a schedule, so it is reference material about how your team
  works — never a standing instruction to your agent. If a bank contradicts
  your own memory, Claude is told to say so rather than pick a side quietly.

- **Contributions leave as a pull request, never as a write.** In every write
  mode, including `auto`. An item the dream pipeline routes to a shared bank is
  refused a local write by the same code floor that refuses path traversal, and
  shows up in your review pile labelled *"belongs to a shared memory bank"*.
  Turning it into a PR is a separate, explicit command:

  ```
  memory_bank.py contribute --proposal <dream proposal> --apply
  ```

  No model is involved in producing the contribution — the text in the pull
  request is the text you read in the proposal, byte for byte.

- **`BANK.md` and a leak check.** Each bank declares its owners, its review
  rules, and its **no-go domains** (compensation, health, finances, credentials
  — a sensible default list applies when a bank declares none). Anything
  naming a no-go domain, or containing something shaped like an API key or
  token, is blocked before a PR is opened and stays in your review pile. A
  blocked item is not rejected memory; it is refused *sharing*.

- **`/memory-bank` — list, sync, check, contribute, adopt.** `list` shows every
  bank and its state; `sync` fast-forwards them (also run automatically and
  detached at session start, TTL-gated — a bank you cannot reach is *stale*,
  never a session-start error); `check` reports cross-bank collisions.

- **`adopt` — the migration that stops you injecting the same fact twice.** The
  rule banks are built on is that a fact lives in **exactly one** bank, so
  joining one means moving your overlapping content into it and deleting your
  local copy. `adopt` shows you which of your files the bank overlaps and why,
  then deletes only files you name one by one — there is no adopt-everything.
  **Nothing is deleted that is not already in the bank, line for line**: if the
  contribution PR has not merged and pulled yet, the file is skipped and the
  missing lines are named. Every adoption writes a receipt carrying the exact
  `git revert` that undoes it.

- **Cross-bank collision reports.** When two banks claim the same filename, the
  same routing domains, or the same section heading, catalog generation says
  so — in `bank-collisions.md` beside the catalogs — instead of letting both
  copies reach a prompt. It reports; it never silently resolves.

### Changed

- **Two new settings:** `memory_banks_file` (where your bank declarations live,
  if not the default) and `bank_sync_ttl_hours` (how often session start
  fast-forwards a bank; default 6).

- **Your workspace is found without the launcher telling it.** If neither
  `workspace_dir` nor `WORKSPACE` is set, the plugin now walks up from the
  project directory to the nearest `.multiplai/` folder. Previously that case
  fell through to `~/.multiplai` and quietly wrote your memory outside the
  workspace it was plainly sitting in. Explicit settings still win, so nothing
  that works today changes. **Requires `multiplai-core` ≥ 0.14.0.**

## [0.37.0] - 2026-08-08

### Added

- **A weekly memory doctor that reads your whole corpus and tells you what is
  wrong with it.** Triage judges one new item against one file, which means it
  cannot see the failures that only exist across the corpus: the same fact
  arriving five times over three months in slightly different words across three
  files, or a note that quietly contradicts one written in May. Nobody was
  watching for either. Now something is, once a week, unattended, and it writes
  what it finds to `.multiplai/dreams/doctor-YYYY-MM-DD.md`.

  Three sections, each finding numbered and citing `file:line` so you can check
  it:

  - **Duplication** — every pair of near-identical bullets, first shortlisted by
    a plain text-similarity measure (no new dependency; the corpus is under
    1 MB) and then confirmed by a model, which also drafts the merged wording.
    Only confirmed pairs are reported.
  - **Contradiction** — pairs of statements *within one file* that cannot both
    be true, each with both quotes and a one-line explanation. A file you have
    not edited since the last run costs nothing, and its earlier findings are
    carried forward rather than dropped.
  - **Dead weight** — sections nothing seems to use, split into three findings
    that mean different things: never retrieved at all, retrieved but estimated
    unused, and expensive per estimated use.

- **The doctor never edits your memory, and there is no flag that changes
  that.** Triage writes *additions*, which a receipt records and `git revert`
  undoes cleanly. The doctor would be proposing *deletions and merges*, where a
  wrong call destroys something no receipt can reconstruct. So it hands you
  suggestions with the evidence attached and you decide. The report is also
  built so that no existing tooling can pick it up: `dream --triage` refuses it
  outright.

### Notes on how much to trust it

- **Dead weight is honest about being an estimate.** Every number names which
  estimator produced it and shows its sample size. A section is only called
  unused when *both* estimators independently saw it enough times and each put
  its use at or below the threshold — a missing estimate is never read as "not
  used", and the two are never averaged into one score. Anything below the
  sample-size floor is not reported at all, and the floor is printed in the
  report.

  **That both-estimators rule covers the "unused" finding only.** The
  "expensive per estimated use" list ranks from whichever estimator has data, so
  a telemetry gap cannot make live memory look *unused* but can still shape that
  list off one surviving estimator. Every row names its basis, and the report now
  says this where you read it rather than only in the source.
- **It will propose nothing for a while, and that is correct.** The utilisation
  telemetry needs weeks to accumulate. Until it has, the dead-weight section
  says so in place of ranking noise.
- **A section that reads as a behavioural rule is never proposed for removal on
  usage grounds.** Rules are retrieved rarely by construction, and they are
  exactly the content whose absence you would not notice. They are listed
  separately as withheld, with the reason.
- **Cross-file contradictions are not looked for yet.** The report says so, so
  that an empty contradiction section is not mistaken for a clean bill of
  health.
- **A failure never widens what you are shown.** A model call that times out or
  comes back garbled contributes nothing at all, and the report tells you how
  many did. With no model available the deterministic dead-weight pass still
  runs and the other two say they were skipped. In the same spirit: a section
  whose text cannot be read back out of the corpus at all is **withheld** rather
  than proposed, and listed with that reason — an unscreened candidate is not a
  pruning candidate.
- **A partly-read file says so.** One contradiction call reads at most 60,000
  characters of a file, and four files in a corpus this size are longer than
  that — including the largest, of which about two thirds sits past the limit.
  Those files are now named in the report, because "no contradictions found" in a
  file that was only two-thirds read is a different statement from a clean one.
- **A doctor run that fails leaves the weekly gate open**, so the next
  maintenance run retries instead of going quiet for seven days on the strength
  of one log line.
- **Quoted text in the report cannot restructure the report.** The suggested
  merge wording and the quoted lines are model output derived from your memory
  files; they are now neutralised on the way out, so a stray code fence in a
  memory file cannot break out of its bullet and rearrange everything below it.

## [0.36.0] - 2026-08-08

### Changed

- **Triage now judges what an item *says*, not what it looks like.** The old
  classifier was eight regex-and-allowlist gates, and on the real 194-item
  proposal it sent 120 items to you — **90 of them for one reason**: the text
  contained a word like "always" or "never". That gate fires on "the API always
  returns UTF-8", because the difference between a fact and an instruction is
  semantic and a pattern match is not. It is gone, along with the 18-name
  hand-maintained list of "files that are safe to write to", which went stale
  and could not see any memory file added after it was written.

  What replaces them is a separate model call whose only job is to find reasons
  to escalate. It is never told it is grading another pass's output. Per item it
  re-derives the provenance/kind labels, checks whether the cited source
  actually supports the claim, and checks whether the target file already says
  it — three questions no pattern could ever answer.

- **What may be applied without you reading it is now a table you can read.**
  Provenance sets confidence, kind sets blast radius, and only the intersection
  applies:

  | | fact | decision | rule |
  |---|---|---|---|
  | you corrected it / you stated it | apply | apply | **review** |
  | observed while working / read in a source | apply, if the citation holds | review | **review** |
  | Claude inferred it | review | review | **review** |

  **A rule never applies automatically. Not in any mode, not even when you
  yourself said it.** That is about blast radius, not trust: a wrong fact is one
  you notice later; a wrong rule changes what you notice.

  The model cannot move an item *up* this table. It can only escalate for
  review, or drop. So talking the judge into approving a rule changes nothing.

### Added

- **`memory_write_mode`** — a new setting, defaulting to **`triage`**.
  - `review` — nothing is ever applied automatically. This is exactly the old
    behaviour, one word away.
  - `triage` *(default)* — the judge runs and the table decides; everything else
    waits for you.
  - `auto` — also applies plain facts the table would have held back. Rules
    still never apply.

  The nightly maintenance pass now applies under `triage` and `auto`. **This is
  a real change: until now nothing unattended ever wrote to your memory files.**
  Every applied item is in a receipt, memory is a git repository, and the
  receipt ends with the exact `git revert` command that undoes the whole batch.

  **Read this before you upgrade, because the default turns it on.** With
  `memory_write_mode: triage`, the whole chain — learnings → proposal → judge →
  memory write → git commit — runs on the daily maintenance cadence with no
  human present and without you invoking `/dream-remember` at all. The
  maintenance pass can even generate the proposal it then triages, in one run.
  Set `memory_write_mode: review` to keep the old behaviour exactly.

  One related narrowing worth knowing: destination protection used to be a list
  of 18 filenames that could never be written unattended. It is now two —
  `CLAUDE.md` and `AGENTS.md` — because a filename is a poor proxy for "this
  file holds behaviour". What protects `preferences.md`, `git-policy.md`,
  `technical-pref.md` and the voice guides now is the classification: an item
  whose **kind** is `RULE` is never applied unattended in any mode. That is a
  better rule and a *model's* judgement rather than a filename match, so it is a
  genuine trade rather than a straight improvement.

- **A rejection log at `.multiplai/data/rejections.jsonl`.** When the judge
  drops an item — usually because your memory already says it — the item is
  written here in full: its text, its labels, its source citation and the
  judge's own one-line reason. Dropping means "not promoted to memory", never
  "deleted": the source learning is untouched and any drop can be read back and
  overruled. The receipt shows every rejection while there are 25 or fewer, and
  grouped counts above that.

- **The receipt now has both an `Applied` and a `Rejected` section**, each item
  carrying the labels it was judged under and the judge's reason.

### Fixed

- **A model failure can no longer widen what gets written.** A timed-out batch,
  a rate limit, an unparseable reply, or no SDK at all now yields *zero*
  verdicts — and with zero verdicts nothing is applied at all, which is
  identical to `review` mode. The count of items that kept a conservative
  default because their batch failed is printed and logged rather than passed
  over in silence.

- **Verdicts are cached, so re-runs are stable.** Triaging the same proposal
  twice produces the same partition and costs nothing the second time, and a
  killed run resumes instead of re-judging. The cache key covers everything the
  judge was shown — including the citation the item carried, whether the routing
  gate had flagged it, and the judge's own prompt — so a cache hit is always the
  answer to the same question. It is not reused when any of those change.

- **Two items numbered the same can no longer be applied on each other's
  judgement.** A proposal that numbers two entries `### 3.` under one file is
  now refused outright, with both labels named, rather than triaged: the number
  is how an item is identified end to end, so two items sharing one would each
  have been written on the other's verdict. Renumber them, or regenerate.

- **A stricter reading of the judge's reply.** A verdict line for an item that
  was not in the batch it answers is discarded, and a reply that answers twice
  for one item is thrown away whole. This closes a route by which text inside a
  learning could pose as a verdict for a *different* item — most cheaply as a
  forged `drop`, which would have removed a legitimate learning from your queue.

- **`--dry-run` really does write nothing.** It still calls the model (the
  partition *is* the judge's answer, so a preview that skipped judging could only
  report "nothing would apply"), but it no longer saves the verdict cache — so a
  preview cannot leave behind something a later real run applies from. The help
  text now says all of this, and no longer describes gates that were removed.

- **An unsupported citation now holds back every fact it should.** The rule is
  "a factual claim about the world needs a citation the judge could corroborate";
  it was being applied to facts from research and observation but *skipped* for
  the weakest class of all — a conclusion the model merely inferred. Under
  `auto` that meant an uncited inference was applied while a cited fact was held.
  Corrections and your own stated preferences remain exempt: neither is a claim
  about an external source.

- **Memory files are written atomically, and a failed write cannot lose a
  receipt.** The write was truncate-then-write with no error handling, so a
  failure on the second of two files left the first rewritten with no receipt,
  no commit and nothing marked processed — the one state the receipt ordering is
  designed to make impossible. A file that cannot be written is now reported like
  any other refusal and its items stay pending.

- **The write floor checks where the file actually is.** A symlink sitting in the
  memory directory used to satisfy every filename check while sending the write
  somewhere else entirely — including to `CLAUDE.md`. The resolved destination is
  now checked too, and a symlink is refused outright. Also: a target with a
  trailing newline is no longer a valid filename (Python's `$` matches before
  one, so `"CLAUDE.md\n"` had been passing).

- **The rejection log is capped at 5,000 records.** It holds every dropped
  item's text verbatim and was designed to grow forever — and since
  `/dream-remember` deletes the source learnings files, it was the one permanent
  copy. It is not redacted, deliberately (a regex screen over free text fails
  open while reading as protection); it is bounded, git-ignored, and documented
  as holding what it holds.

- **`.multiplai/memory/CLAUDE.md` (and `AGENTS.md`) can never be written
  unattended**, whatever an item claims to be. Neither can a filename that tries
  to escape the memory directory, an item that revises rather than appends, or
  one that did not parse. These checks run *after* the model's verdict and can
  only refuse it.


## [0.35.0] - 2026-08-08

### Added

- **You can now see which memory is earning its place — as an honest estimate,
  never a measurement.** Retrieval counts were always available and always
  misleading: a section injected on every prompt and relevant on none of them
  looks maximally valuable counted that way. Nothing can observe *use*
  directly, so this release estimates it two independent ways and shows both
  side by side.
  - **Self-report**, free. The end-of-session pass already reads your whole
    transcript; it is now also handed the list of sections that were injected
    and asked which it relied on — with a quote or a concrete reference as
    evidence. A claim with no evidence is recorded as unsupported and never
    counts. "None of them" is offered as a normal answer, because otherwise a
    model finds a reason for everything.
  - **An offline judge**, sampled. A new cheap-tier pass in the nightly
    maintainer compares the injected list against a distilled transcript for a
    handful of sessions per run (`utilisation_judge_sample`, default 5; `0`
    turns it off). It is independent of the session's own reasoning, so where
    the two estimators disagree, that disagreement is itself the signal.
- **A ranked table, ordered by cost per estimated use.** Run
  `scripts/utilisation_report.py` (add `--json` for a machine-readable form),
  or read it inside `/multiplai-context:memory-health-audit`. The expensive
  and seldom-used rows sort to the top, which is what makes a pruning
  candidate obvious.
- **Records accumulate in `<data_dir>/utilisation.jsonl`** — one record per
  session, written atomically, with detail older than 90 days collapsing into
  per-section running totals so the file cannot grow without bound. Totals
  survive compaction exactly; only the per-session detail goes.

### Notes on reading the numbers

- **Everything on this surface is labelled *estimated*, everywhere, and there
  is deliberately no single blended "utilisation score".** Two estimates with
  opposite biases averaged into one number would be exactly the fabricated
  precision this feature exists to avoid. Where they disagree by more than 35
  points the row is marked, not reconciled.
- **Rows with too few observations are not ranked at all.** They appear under
  "insufficient data" with their sample size, rather than being sorted into a
  position they have not earned. "Never retrieved" is kept as a separate list
  again — a section that never reached a prompt tells you about routing, not
  about value.
- **Nothing is pruned, edited, or auto-applied by any of this.** The table is
  evidence for you to act on; `/multiplai-context:dream-remember` is still the
  only path that writes to memory.
- **A blank is not a zero — on both estimators.** If either pass fails, times
  out, or comes back without an answer, nothing is written, leaving that half
  unestimated — so a bad night can never make your corpus read as dead weight.
  Concretely: the judge writes no verdict rather than an empty one, and the
  in-session self-report is skipped unless the whole transcript was read *and*
  every chunk actually answered. That second condition matters more than it
  sounds: the self-report rides on the end of a longer reply, so a truncated one
  used to record "used none of them", which is what puts a section you rely on
  at the top of a pruning list. A missing estimate now shows as `—`, and only a
  section genuinely reported as unused shows a zero.

  With no model client available, no estimate is recorded; what was injected is
  still recorded, because that part needs no model.
- **"Never retrieved" says when it does not know.** The list needs some history
  behind it before "never" means anything — on a fresh install it is simply
  everything you have. Below ten recorded sessions both the report and the
  `--json` output now say so, and the JSON carries
  `never_retrieved_sufficient: false` so anything reading it unattended cannot
  mistake a thin log for evidence.
- **Two sessions ending at once no longer lose each other's telemetry.** The log
  is read-modify-written, and without a lock the later writer silently discarded
  the earlier one's whole record. With parallel containers and extraction
  draining at the next session start, that overlap is ordinary rather than rare;
  measured before the fix, 40 simultaneous writers left 2 records.
- **The judge's coverage report distinguishes an outage from nothing to judge.**
  `kept_default` lumped "the model call failed" together with "this session has
  no transcript any more", and since transcripts age out faster than the 90-day
  retention window the second is a permanent, growing baseline — which buried the
  first. The report now breaks the total into `unavailable`, `not_judgeable` and
  `empty_verdicts`, and only `unavailable` warns.

- **A blank is not a zero.** If the judge pass fails or times out it writes no
  verdict at all, leaving the session unjudged — so a bad night can never make
  your whole corpus read as dead weight. With no model client available, no
  estimate is recorded; what was injected is still recorded, because that part
  needs no model.

## [0.34.0] - 2026-08-08

### Added

- **Each learning now says where it came from, separately from what it is.**
  Lines in `.multiplai/learnings/` open with a pair instead of a trust rating:

  ```
  - **[CORRECTION/RULE]** Stage with an explicit pathspec. → Target: dev.md — Add to the Git section.
  ```

  The first half is the **provenance** — `RESEARCH`, `EMPIRICAL`, `CORRECTION`,
  `DECLARATION` or `INFERENCE` — which is where the knowledge came from and so
  the only thing that says how it could be checked again. The second is the
  **kind** — `FACT`, `RULE`, `DECISION` or `INTENTION` — which is what sort of
  claim it is, and how far a wrong one reaches.

  These used to be one `type` field answering both questions at once, which
  meant a correction *about a fact* and a correction *about how Claude should
  behave* wore the same label. They need opposite handling: the first is the
  most trustworthy input in the system, the second is the one you most want to
  read before it applies. `INFERENCE` in particular had no representation at
  all — a conclusion Claude drew and nobody confirmed looked, one consolidation
  later, exactly like a verified fact.

  When the extractor genuinely cannot tell, it answers `INFERENCE` and `RULE` —
  the cautious end of each axis, so unclear items land in front of you rather
  than sliding past. README → *Learning lifecycle* has both tables with a
  worked example each.

- **Dream proposal items carry the pair.** Each `### N.` entry gains a
  `**Provenance:** PROVENANCE/KIND` line naming what the entry was distilled
  from, and the auto-apply receipt repeats it — so an item applied without
  anyone reading it can still be traced back to the kind of evidence behind it.

### Changed

- **Learnings you already have are left exactly as they are.** Records captured
  before this release keep their `**[trust: …]** TYPE` line and are read
  through a fixed mapping rather than relabelled on disk. Nothing back-fills a
  provenance onto an old note: guessing where a month-old line came from would
  manufacture the very signal these labels exist to make trustworthy. Both
  forms coexist in the same file for as long as your backlog does.

- **`trust:` is deprecated.** It is still recorded and still accepted, but it no
  longer appears on the rendered line — two confidence-ish markers on one line
  is what made the old format ambiguous. Provenance answers the question `trust`
  was reaching for, and with an actual source behind it.

### Fixed

- **Upgrading does not re-propose your whole consolidated backlog.** Dream
  decides what is new by hashing each learnings record, and that hash used to
  include the label markers — so changing the line format would have made every
  pending record look brand new. Hashes are now computed from what a learning
  *says* (its description, target and action), and existing ledger entries are
  converted in place on the first run after the upgrade. Nothing you have
  already reviewed comes back.

  Two details of that conversion, both of which cost a learning when they were
  wrong. The old key is kept as an alias rather than replaced, because a run
  that crashed mid-draft records raw key strings beside its staged draft — if
  those stop resolving, the draft is discarded *and* the learning stays marked
  consolidated, so its content is gone with nothing to show for it. And a
  learning whose description happens to begin with a word from the old
  vocabulary (`OBSERVATION`, `PATTERN`, …) now keeps its own identity; it used
  to hash the same as the same learning without that word, so one of the two was
  silently treated as already reviewed.

- **One thing does read the new labels: conflict detection.** When a learning
  contradicts a line already in memory, dream surfaces it under
  `## Conflict Resolutions` for you to accept or reject. That used to fire on
  any learning marked `trust: verified`; it now fires on provenance
  `CORRECTION` or `EMPIRICAL` — the two that mean "the world was observed to be
  otherwise". `RESEARCH`, `DECLARATION` and `INFERENCE` do not: read somewhere,
  asserted, or reasoned to, none of them observed here. This is deliberately the
  same set of learnings as before rather than a narrower one; `EMPIRICAL` is what
  most of your verified learnings become, so leaving it out would have quietly
  turned the feature off.

  Routing and injection are unchanged, and dream's triage still does not read
  the pair.

- **Nothing acts on the new labels yet.** Routing, injection and dream's own
  triage behave exactly as before. This release only records the information.
## [0.33.0] - 2026-08-08

### Added

- **Routing can now load one section of a memory file instead of the whole
  file.** The machinery for `file.md#Section` picks has shipped since 1.2.0, but
  the field that switches it on — `section_anchors` — was marked hand-authored,
  so unless you wrote it yourself it was empty and every picked memory file was
  injected whole. Catalog generation now writes it for you: each memory file of
  at least 8 KB with at least three `##` sections gets one entry per section,
  carrying the section name and a one-line description of what is in it. The
  router reads those descriptions and asks for the sections it needs — naming
  sections is now its **default** for an anchored file, with the whole file
  reserved for when most of it is relevant. On a 180 KB file with 30 sections
  that is roughly 6 KB in place of 180. Measured over 17 routing prompts against
  a 921 KB corpus, injected memory fell 68% (814 KB → 260 KB).

  Section names are extracted from your file in code and handed to the model as
  a fixed list to describe — it never writes the name — so an anchor cannot
  drift from the header it points at. If one is wrong anyway, the loader still
  falls back to the whole file, which is what happens today, so no prompt can
  come out with less context than before.

  Two things that follow from that promise and are worth knowing, because both
  are behaviour you would otherwise have to discover:

  - **Everything above your file's first `##` heading rides along with every
    section pick.** That is where your `**Last Updated:**` stamp, the
    `**Purpose:**` line, `Boundaries — route elsewhere:` routing notes, and
    cross-file instructions like `> **Load core-voice.md first.**` live. It is
    added once per file, not once per section, and it is why "a pick never
    carries less than the whole file would have" is true rather than
    aspirational.
  - **`##` lines inside fenced code blocks are not sections.** A markdown
    example pasted into a memory file used to cut the surrounding section in
    half and add a phantom section name to the catalog. Memory files are exactly
    where markdown examples get pasted.

  Files under either threshold get no anchors. That is deliberate, not a
  failure: a 4 KB file is already about one section's worth of context.

  **On upgrade, anchors appear on the next catalog refresh** — you do not have
  to touch your memory files or pass `--force`. Regeneration is normally gated
  on a file's content hash, which an upgrade does not change; a memory file that
  is anchorable but has no anchors yet is regenerated once regardless, then
  falls back to the hash gate like everything else.

- **`memory_lint` reports duplicate `##` section names across your memory
  files.** Your memory `CLAUDE.md` has always asked for these to be unique
  corpus-wide; nothing checked. It matters now, because two files both offering
  `## Overview` make a `#Overview` section pick ambiguous. Warn-only and it
  renames nothing — which of the two should change is a judgement about what the
  memory means. Findings appear in the maintainer's lint report and in
  `/multiplai-context:health` under `memory_validity.duplicate_h2`.

  A name repeated **within one file** is reported too, as `duplicate-h2-in-file`.
  A `#Name` pick for it loads every occurrence, so the slice is bigger and
  vaguer than the name suggests — retitling one is the fix.

- **`router_timeout_seconds`** — how long the `llm` router waits before giving
  up, previously fixed at 25s in the source. Worth knowing about because a
  timeout injects **no** memory for that turn, and section anchors give each
  routing call a longer catalog to read and a finer choice to make. If you see
  `LLMRouter timed out` in the logs, this is the knob.

### Changed

- **`section_anchors` is regenerated rather than preserved across catalog
  rebuilds.** It used to sit with the hand-authored fields, which meant the
  first list ever written was frozen: rename or delete a section and the catalog
  kept advertising the old name forever, silently, because a missed anchor just
  loads the whole file. It is now derived from the file each time that file
  changes. If you had hand-written anchors, they will be replaced on the next
  regeneration of that file — any that name a real `##` header will come back
  with a description attached. `sections`, `bundle` and `co_retrieve_for` are
  untouched and still preserved.

- **The injection log records which sections were sent, and what each file
  cost.** `inject` events in `activity.jsonl` gain `sections_by_file` (an empty
  list meaning the whole file) and `bytes_by_file`. Without them `files` alone
  cannot tell you whether a large file cost you 180 KB or 6 KB this turn.
  `files` and `files_by_corpus` are unchanged.

### Known issue

- **If you run `memory_router: llm`, routing calls got slower.** The catalog the
  router reads roughly doubled (30 KB → 61 KB on a 29-file corpus) because every
  section now carries a description, and the reply is longer too. Median routing
  latency in our measurement went from 16 s to 22 s, with one 35-section file
  reaching 84 s. The router's ceiling is **25 s**, and a call that hits it
  injects **no memory at all** for that turn — the pre-existing failure mode,
  now easier to reach. The default router (`token_overlap`) makes no model call
  and is unaffected.

## [0.32.2] - 2026-08-07

### Fixed

- **The fleet view shows your tab names again, not `claude-personal-07213856`.**
  Every board — `AGENTS.md`, `/multiplai-context:fleet-status`, `fleet.json` and
  anything reading it — labelled agents with their container name, because the
  only place it looked for a tab name was `tmux/panes.json`, and the kit's
  launcher had been writing `"window": ""` into every entry. (That half is fixed
  in multiplai-kit: it read `automatic-rename` window-locally, so a global
  `set -g automatic-rename off` returned empty and the name was never recorded.)

  The name was already on disk the whole time. Each `tmux/viewed/<n>` marker
  carries the window name on its second line, rewritten by the
  `after-rename-window` hook every time you rename a tab — so it is both fresher
  than the pane map and available in cases the map is not. The marker now
  supplies the label, with the pane map as fallback, and neither requires a
  guess about whether a human chose the name: it is simply what the tab is
  called.

  A tab renamed mid-session now relabels on the next render instead of at the
  next launch.

  The marker is read through the same guarded lookup `seen` uses, so both agree
  about which pane they are describing — **tmux recycles pane ids per server**,
  and a label taken from one tab while attention is credited to another would be
  worse than either being absent.

## [0.32.1] - 2026-08-07

### Fixed

- **A skill the router recommends can now actually be invoked.** When the
  routing hook suggested a relevant skill it wrote `Invoke with /<name>`,
  taking the name from the catalog's source key — which is the skill's
  *directory* name. For every skill shipped by a plugin that is not a valid
  identifier: the real one is `/<plugin>:<name>`, and the bare form fails with
  `Unknown skill`. So the hook was reliably teaching a name that does not work.

  An audit of 111,780 real tool calls on 2026-08-07 measured the consequence:
  the `Skill` tool failed **23.2%** of the time, and every single failure was
  an unqualified name — `extract-insights` (24 attempts), `youtube-transcript`
  (19), `deep-research` (13). Those are three of this suite's own skills,
  unreachable at the exact moment the router had decided they were the right
  thing to reach for.

  The hint now names the invocable identifier:
  `Invoke with /multiplai-research:extract-insights when relevant.` A skill in
  your own `skills_dir` is still suggested bare, because that is genuinely how
  it is invoked.

## [0.32.0] - 2026-08-07

### Added

- **`checkpoint_hard_stop_tokens` — enforce the handoff instead of suggesting
  it.** Set it, and past that many context tokens the plugin stops accepting
  new prompts, showing you the count and what to do about it. Off by default;
  the handoff stays advisory unless you ask for a wall.

  It is for the setup where you have disabled native auto-compaction
  (`DISABLE_AUTO_COMPACT=1` or `autoCompactEnabled: false`) because you would
  rather hand off to a fresh session than be summarized. With compaction off,
  nothing between the handoff threshold and the model's real context ceiling
  actually *does* anything — the nudges are advice, and a session drifts deep
  into the degraded zone the checkpoint system exists to avoid.

  Ways out, so it is a wall with a door: slash commands always go through
  (`/clear` and `/compact` are the point), `!keepgoing` in a prompt overrides
  it for one refresh band, and any internal failure falls through to not
  blocking. Subagents are never blocked.

### Removed

- **The compaction summary stub.** Since 0.6.9 the PreCompact hook told the
  built-in summarizer to emit one sentence instead of a full summary, since
  the checkpoint re-injection carries the state anyway. It never worked, and
  could not: to outrank the summarizer's own instructions the directive was
  phrased as a priority override telling it to ignore them — which, from
  inside the summarizer, is indistinguishable from a prompt injection hidden
  in the conversation being summarized. Sessions refused it and wrote the full
  summary regardless; one flagged it to its user as an attack on their own
  tooling, which was the right call on the evidence it had.

  Compaction now produces its normal summary. If compaction cost is what you
  are avoiding, `checkpoint_hard_stop_tokens` addresses it at the source by
  handing off instead.

### Fixed

- **Disabled auto-compaction is no longer mistaken for steered
  auto-compaction.** `autocompact_trigger_tokens()` decided "auto mode is on"
  from `CLAUDE_CODE_AUTO_COMPACT_WINDOW` / `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
  alone, and never checked whether compaction was disabled. Leaving those
  vars in place while setting `DISABLE_AUTO_COMPACT=1` (or
  `autoCompactEnabled: false`) is the normal shape of that config — and the
  plugin read it as "compaction will handle this", suppressing the handoff
  advice for the one setup where nothing else would act. It now checks
  `DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`, and the `autoCompactEnabled`
  setting first, in the CLI's own order.

### Changed

- The pending rebuild marker — which makes a manual `/compact` below the
  handoff threshold still re-inject your checkpoint — is now written whenever
  a valid checkpoint exists. It was previously gated on the checkpoint being
  *fresh*, a check that only existed to protect the removed summary stub;
  a checkpoint lagging by a band is strictly better than no injection.

## [0.31.0] - 2026-08-07

### Added

- **The fleet view now knows which agents you have already looked at.** With
  six tabs running, "what is everyone doing" is not the question — "which of
  these has done something since I last looked" is. An agent whose tmux tab was
  on your screen more recently than its last event is **seen**; anything it
  does afterwards makes it unseen again, automatically, with nothing to dismiss
  or mark read.

  What this changes where you look:

  - `AGENTS.md` marks a seen entry (`### mktplace — 5m · seen 2m ago`) and
    leaves everything else exactly as it was;
  - within every group, **unseen agents sort first** — ordering only, nothing
    is hidden by being seen;
  - the digest's in-flight line gains `N unseen`;
  - each agent in `fleet.json` gains `seen` and `seen_at`.

  It reads `$WORKSPACE/.multiplai/data/tmux/viewed/`, written by the kit's
  `fleet-viewed.sh` from tmux's pane-selection hooks. **Those hooks are four
  lines you add to your own `~/.tmux.conf`** — see the kit's
  `docs/TMUX-FLEET-BOARD.md`. Without them nothing here activates and the board
  renders exactly as it did before: no marker directory means *nobody is
  recording attention*, which is deliberately distinct from *you have looked at
  nothing*, and only the second one licenses printing a count.

  A marker is ignored unless its tmux server matches **that pane's** — the
  socket recorded on the pane's own map entry, not the map's document-level
  one, which belongs to whichever launch wrote the file last. tmux recycles
  pane ids per server, and crediting one tab's attention to an unrelated
  session would make the board confidently wrong — worse than saying nothing.
  If you run two tmux servers, the document-level comparison got it wrong in
  both directions at once: it credited attention across servers and denied it
  within them.

  `seen` is a **third axis**, not a new status. `status` stays
  `working | waiting_input | idle | ended` per the hub's API contract, and
  `disposition` stays how you left a session. Attention is neither: an agent's
  state must not change because you glanced at a tab.

## [0.30.0] - 2026-08-07

### Added

- **The fleet view now labels a session with the name you gave its tmux tab.**
  `AGENTS.md` entries carry `tab \`pi-eval\`` alongside the container name, and
  a collision line reads `mktplace@pi-eval` instead of `mktplace@feat/x` — the
  string you are actually scanning your tab bar for. Each agent in `fleet.json`
  gains `tmux_pane`, `tmux_window` and `tmux_server`.

  The data comes from `$WORKSPACE/.multiplai/data/tmux/panes.json`, written by
  the kit launcher. It has to: this plugin's hooks run inside the container and
  tmux runs on your Mac, so `$TMUX_PANE` is not merely missing in here, it is
  unknowable. The join key is the container name, which is the one identifier
  that survives a `/clear`.

  A pane id is only meaningful alongside the tmux server that issued it — tmux
  recycles them per server — so the server path is carried with it, **per
  entry**, and anything joining to a pane id is expected to compare servers
  first. Per entry because the map merges across tabs and two tabs can be on
  two servers; a map written by an older launcher has one socket for the whole
  document, and that is used as the fallback.

  A tab keeps its auto-generated name out of this: the launcher records a
  window name only when you pinned it (`automatic-rename off`), so a label
  never reads `mktplace@bash`, and an unnamed tab falls through to the
  worktree-or-branch qualifier as before.

  **Without the kit, or without tmux, nothing changes.** No map, a malformed
  map, or a payload that does not declare itself as host-observed tmux data is
  refused wholesale and the three fields stay empty, which every surface
  already renders as no label. Pinned against a golden capture taken before any
  of this work landed.

## [0.29.0] - 2026-08-07

### Changed

- **`fleet.json` is now refreshed by every session start and every post-exit
  drain, not only when you run `/fleet-status`.** It was written by
  `/fleet-status` alone, so anything reading it was as old as the last time you
  ran that command by hand — on 2026-08-06 `AGENTS.md` was stamped 21:23 while
  `fleet.json` still carried the previous day. The two files are two renderings
  of one truth; they are now written together, by the same function, at the
  same moment.

- **Sections a session start cannot collect are carried forward instead of
  blanked, and now say how old they are.** The hook path reads local files; PRs,
  repo state, background jobs, the backlog and scheduled routines need `git` and
  `gh`, which only `/fleet-status` runs. So a session start keeps whatever the
  last `/fleet-status` saw for those five, together with a new `collected_at`
  stamp per section — enough for a consumer to render `PRs 3 open · 14m ago`
  rather than implying it just looked.

  Without this, wiring the JSON into the hook path would have been a
  regression rather than a fix: you run `/fleet-status`, you get your PRs, and
  the next session start — seconds later, and with ten tabs open they are
  constant — overwrites them with "not collected".

  A carried section reverts to *not collected* after **one hour**. That bound
  is the point: `null` means "I didn't look" and `[]` means "I looked and there
  was nothing", and a board showing yesterday's PR state indefinitely while
  looking confident about it is the failure that distinction exists to prevent.
  An empty list is carried as an empty list, never converted to `null`.

  `/fleet-status` remains the deliberate refresh: what it actually collected
  always wins over what a previous pass remembered. It carries the rest
  forward on the same terms, which matters when it *cannot* collect — `--offline`
  skips GitHub outright, and any source that errors reports nothing. An
  offline run no longer erases a PR reading a run ten minutes earlier took.

  Both files remain pure caches — delete them, run anything that renders, and
  they come back. The `fleet.json` version stays at `1`: `collected_at` is
  additive and no existing field changed meaning.

## [0.28.0] - 2026-08-06

### Changed

- **`/dream-remember` triage now auto-applies about a third of a proposal, not
  most of it.** 0.26.0 claimed 175 of 194 items applied and 19 left for you.
  That number came from a classifier with two holes, and the honest split on
  the same proposal is **74 applied, 120 left for you**. Expect a third, and
  read the 120 — they were always yours to decide.

  Two gates were wrong:

  - The file check was a denylist naming `CLAUDE.md` alone, so every other
    memory file was auto-appliable — including `preferences.md`,
    `git-policy.md`, `technical-pref.md` and the voice and workflow guides,
    which the agent reads *to decide how to act*. It is now an allowlist of the
    files that **record** rather than instruct, so a memory file added next
    month waits for you instead of being auto-appliable until someone
    remembers to classify it.
  - Nothing looked at what an item actually *said*. A rule carried into a facts
    file ("never run `uv sync` against a shared venv") was an additive entry to
    a non-behavioural file and sailed through. Items **phrased as standing
    instructions** now go to you wherever they land — 104 of the 120 on the
    measured run. The check is deliberately trigger-happy: it fires on factual
    sentences that merely contain "always"/"must"/"prefer", and each false
    positive costs one line of reading.

### Fixed

- **A proposal with no `## Routing Warnings` section is now refused outright**
  rather than fully auto-applied. The section is absent whenever the routing
  gate failed — it is fail-open — and an absent section was indistinguishable
  from a clean one, so exactly the items the gate exists to catch were the ones
  applied unattended (6 of them on the measured proposal). Triage exits 1 and
  writes nothing; review that proposal by hand or regenerate it.

- **An applier result that is not purely additive is refused.** Every
  auto-applied item is an `add`, so the rewritten file must still contain every
  original line and grow no more than the proposed items account for. The
  previous guard accepted a result that had discarded 40% of the file — on a
  path where, by construction, nobody read the diff.

- **A proposal cannot target a file outside your memory directory.** The target
  comes from a model-written heading and was joined onto the memory path
  as-is, so `../../CLAUDE.md` resolved to the workspace file — and the
  "does it exist?" check was no guard, because the interesting traversal
  targets are precisely the files that exist.

- **The triage commit names only the files it rewrote**, instead of staging
  `*.md` across the whole memory directory — an unrelated hand-edit is no
  longer swept into a commit captioned as an automatic apply. Triage also takes
  dream's exclusive run lock, like `--auto` does, so two concurrent runs cannot
  both write memory.

- **`--dry-run` without `--triage` is now an argument error.** It was silently
  ignored, so `dream.py --dry-run` ran a full consolidation and wrote a
  proposal — the opposite of what the flag promises.

- The receipt's per-item **source citation** is now populated. It was always in
  the template and always blank, because the parser dropped the field.

## [0.27.0] - 2026-08-06

### Changed

- **Session extraction is materially cheaper.** The extraction prompt was one
  large user message with the transcript spliced into its middle, so no two
  calls shared a cacheable prefix and the whole prompt was cache-*written* on
  every run — measured at a 59% write share over 1,086 calls in 30 days, the
  worst in the ledger. The static instructions (including the memory-file
  charters) now travel in the `system` prompt, leaving only today's date and
  the transcript in the user message. Same instructions, same output format —
  the stable half is now a prefix that caches once and reads thereafter.
- **`catalog_model` now defaults to `claude-sonnet-5`** (was
  `claude-sonnet-4-6`). Sonnet 5 is the current generation and is on
  introductory pricing through 2026-08-31. Set `catalog_model` explicitly to
  keep the old model.

### Security

- Extraction's instructions now arrive over the `system` channel rather than
  inline in the same message as the untrusted transcript. The transcript is
  still bracketed by a closing instruction block, per
  `docs/untrusted-content.md`; this strengthens the separation, it does not
  replace it.

## [0.26.0] - 2026-08-06

### Added

- **`/dream-remember` now triages a proposal before showing it to you.** A
  proposal was never too long to *review* — it was too long to review item by
  item. At ~190 items the walk costs a whole context window and gets abandoned
  partway, which is how a backlog grows instead of shrinking.

  So the skill now runs `dream.py --triage` first. It splits the proposal
  **deterministically** — no model judgement — and applies only the items with
  no decision in them: an additive entry, to a non-behavioural memory file,
  that no gate flagged. On a real 194-item proposal that is 175 applied and
  **19 left for you**.

  What always reaches you, and never gets auto-applied:

  - anything marked `[RULE-PROPOSAL]` — it changes how the agent behaves;
  - anything landing in `CLAUDE.md`, for the same reason, however additive;
  - anything the routing gate flagged (wrong file, section collision, duplicate);
  - anything the drafter marked low confidence;
  - anything that **revises or replaces** existing memory rather than appending
    — an `update` can destroy a line that was right, an `add` cannot;
  - anything whose block did not parse cleanly. The classifier is deliberately
    pessimistic: a false "needs review" costs you one line of reading, a false
    "auto" writes something you never agreed to.
  - Conflict Resolutions, which live in their own section and by definition
    revise existing lines.

  **Auto-applied is not unreviewed.** Every item is written to a receipt at
  `.multiplai/dreams/applied/<date>-auto-apply-receipt.md` with its target,
  section, text and source citation, and memory is under git — so the receipt
  plus `git -C .multiplai/memory diff` is the whole audit trail, and reverting
  is a `git checkout`. Skim the receipt; that is what it is for.

  Run `dream.py --triage --dry-run` to see the split without writing anything.
  If a file's applier returns something unsafe, nothing is written for that
  file and its items stay pending — a partial apply nobody can describe is
  worse than none.


## [0.25.0] - 2026-08-05

### Changed

- **A session whose container is gone is now dropped from the registry within
  the hour, instead of sitting there for a month.** Entries that never fired
  `SessionEnd` — a closed terminal, a reboot, `docker kill`, the OOM killer —
  were kept for 30 days on the grounds that nothing could prove they were not
  still running. Since 0.22.0 something can: the kit launcher records the
  host's running containers, and `AGENTS.md` has been reading it to retire
  those sessions on sight. Now the registry collector reads the same evidence,
  so the files go too and the graveyard stops accumulating (140 entries for 4
  live sessions, on the registry that prompted this).
- Two things still protect an entry, unchanged: **parking it** keeps it
  forever, since a parked session's process being gone is the normal case, and
  a **queued or in-flight extraction** keeps it until that extraction has
  written its disposition. Without the kit — so without a roster — collection
  is exactly what it was: 7 days after a clean exit, 30 days otherwise.

## [0.24.0] - 2026-08-05

### Changed

- **The fleet digest now counts every agent you have, not just the quiet
  ones.** `RUNNING (2)` was the count of sessions in the `Working` group
  alone — but a session that stops to ask you something leaves that group for
  `Needs you`, so four live containers reported as two, and the line was wrong
  in the same direction every time. It now reads
  `IN FLIGHT (4) · 2 waiting on you · 1 working · 1 parked`: one total for
  "how many agents do I have", then what each is doing. Idle and stale prompts
  keep their counts on the same line, in the same lowercase form.
- **`AGENTS.md` no longer prints each entry's involved files.** Six paths per
  entry, under every heading, wrapped across the terminal and pushed the next
  agent's heading off screen — the same bulk 0.17.0 trimmed the file to
  remove. Nothing is lost that you were reading: the paths are still collected
  absolute, `fleet.json` still ships them, and collision detection still reads
  them, so the one question the line stood in for — *is another agent holding a
  file I am about to write?* — is still answered, on the `Collisions` line.

## [0.23.0] - 2026-08-05

### Fixed

- **Your plugin configuration had no effect before this version.** Claude Code
  hands options to hooks as `CLAUDE_PLUGIN_OPTION_<KEY>` with `<KEY>`
  **uppercased**; every read in this plugin used the lowercase key, so nothing
  you set in `pluginConfigs` was ever seen. Each option silently fell back to
  its default — and because the defaults are mostly "off", the symptom was a
  feature that just never ran, with no error and clean-looking logs.

  **What changes for you on updating:** every option you have configured starts
  working, possibly for the first time. If you set `enable_skills: true`, skill
  routing now actually runs; `enable_costs: true` now actually launches the cost
  collector; `memory_router: llm` now actually selects the LLM router (it had
  been silently using `token_overlap`); `keep_ratio`, `recommend_cooldown_turns`,
  the `qmd_*` knobs, `catalog_model`, `router_model`, `skills_dir`,
  `resources_dir` and the `*_dir` paths likewise. **Worth re-reading your
  `pluginConfigs` before updating** — a value you set months ago and forgot is
  about to take effect. `workspace_dir` is the exception: it kept working by
  accident through the `WORKSPACE` fallback.

- **`checkpoint_timeout_s` from 0.20.0 only takes effect now.** The 0.20.0 fix
  raised the checkpoint writer's timeout and was correct in settings, but the
  code reading it could not see it. Same for the rest of the `checkpoint_*`
  family.

### Added

- **`catalog_concurrency` and `plugins_dir` are now declared options.** Both
  were read by the code and (in `plugins_dir`'s case) announced in an earlier
  changelog, but neither appeared in the plugin's option schema. Same names,
  same defaults (`5` and empty → `$CLAUDE_CONFIG_DIR/plugins`); they are simply
  visible and documented now.

- **One INFO line per prompt naming the options that fell back to defaults.**
  Names only, never values. The reason this bug survived eight days is that a
  *dead* option and a *deliberately off* option looked identical in the logs.
  In `.multiplai/data/logs/context_manager.log`.

### Changed

- Every option read goes through `multiplai_core.plugin_options` (new in core),
  which takes the bare option key and uppercases it once. There is deliberately
  **no lowercase fallback**, and a test fails the build if a lowercase name
  reappears anywhere under `scripts/`. Defaults, names and cascade order are
  otherwise unchanged — only the variable name consulted changed.

## [0.22.0] - 2026-08-05

### Changed

- **A session whose container is gone is now reported as ended, immediately,
  instead of being guessed at after twelve hours of silence.** Nothing inside a
  session can report its own death — a reboot, a closed terminal, a `docker
  kill` or an OOM takes the hooks down with it — so `AGENTS.md` had only silence
  to reason from, and every such session sat as "working" for half a day and
  then as a permanent idle entry. On a real registry that was 49 entries in
  limbo.

  multiplai-kit now writes the running container names to
  `.multiplai/data/live_containers.json` on every launch (kit PR #35, needs
  `git pull && ./setup.sh`). When a reading is *newer* than a session's last
  event and that session's container is not in it, the session is over — no
  inference, no waiting. Sessions still live keep their real status, so a
  long-running agent no longer decays to idle just because it has been quiet.

  **Parked sessions are never retired this way.** Parking is a stated intent;
  the container being gone is exactly what you meant when you parked it.

  Everything about this is conditional on evidence, and its absence changes
  nothing: no roster file (no kit, or a kit that has not launched since) leaves
  the previous behaviour untouched, a roster older than a session's last event
  decides nothing about that session, and a corrupt or unreadable one is
  ignored. The status vocabulary is unchanged — a dead container maps to the
  existing `ended` — so the multiplai hub sees no shape change.

  One knock-on worth knowing: entries that would have lingered for weeks now
  reach `ended` in minutes, so registry GC collects them on the 7-day
  ended-session schedule rather than the 30-day one.

- **Session entries record whether they ran in a container** (`in_container`).
  This is what keeps a bare `--local` session, or one run through
  `claude-wrapped` on the Mac, from being declared dead by a roster that could
  never have listed it: outside a container the recorded hostname is a *machine*
  name, and no string comparison can tell the two apart. Entries written before
  this version simply are not judged by the roster.

## [0.21.0] - 2026-08-05

### Changed

- **`AGENTS.md` no longer lists idle sessions.** On a real registry the file was
  36 idle entries to 17 fronts — the thing you opened it for sat at the top with
  a graveyard under it. Idle is a *guess at death*, not a queue: nothing inside a
  session can report its container being killed, so past the quiet threshold
  those entries are overwhelmingly closed terminals. They are still counted in
  the header (`… · 36 idle, not listed`), so a fleet that has gone entirely quiet
  still says so, and `fleet.json` still carries every entry for the hub.

  Nothing was deleted. The per-session checkpoint — intent, next action, files —
  is still at `data/checkpoints/<sid>/checkpoint.md`, and the diary is where
  "what did that session decide" is answered. If you want something to stay on
  the list, **park it**: parked sessions are listed at any age, which is the
  difference between coming back to something and abandoning it.

- **A session goes idle after 12 hours of silence, was 24.** A day sounds
  conservative until you notice it spans the previous evening: every container
  opened after dinner still claimed a slot under **Working** the next morning,
  which read as nine running agents where there was one. Half a day is about one
  working session — quiet since this morning is plausibly still yours, quiet
  since yesterday is not.

  The collision window stays at 24h and is now a separate constant, because it
  answers a different question (could uncommitted work still land on this file?)
  than the idle threshold does (is this tab still mine?). In practice unparked
  work is bounded by the shorter one, since it leaves `Working` at 12h; parked
  work keeps the full 24.

- **Involved-file lists are readable.** They were absolute paths, all of them,
  one line — one real entry carried 41, which is a 4000-character line that
  pushes the next agent's heading off the screen. Paths inside the agent's own
  checkout now render relative to it, paths elsewhere in the workspace relative
  to the workspace root, anything else as `…/parent/name`, and the list stops at
  six with `_+N more_`. Duplicates that collapse to the same short form are
  shown once. This is display only: the stored paths stay absolute, so
  `fleet.json` and collision detection are unaffected.

## [0.20.0] - 2026-08-05

### Fixed

- **The unattended nightly consolidation can now actually finish.** The memory
  maintainer gave its dream pass 10 minutes, while dream's own budget is 15
  minutes *per chunk* of your backlog (30 for an oversized one). The pass was
  arithmetically unable to complete however fast the model ran, and every
  unattended run for a week timed out — which is why a backlog can sit
  unconsolidated for days even though the maintainer runs nightly. The cap is now
  derived from dream's per-chunk budget (one hour, covering the slowest full run
  measured to date) instead of being a number sitting next to it, so the two can
  no longer drift apart.

  If your learnings have been piling up, the next scheduled run should drain them
  without you doing anything. There is nothing to configure.

- **A timed-out background pass no longer keeps running.** Killing a child
  process killed only the wrapper, so a dream the maintainer had reported as
  "timed out" carried on for another three minutes — writing its proposal, with
  eight CLI subprocesses still under it — after the failure was logged. Children
  now get their own process group and a timeout kills the whole tree, so the log
  and reality agree and nothing burns CPU unsupervised.

- **A rate-limited review batch is retried instead of silently discarded.**
  During dream's second-pass review, a batch that hit an API rate limit lost its
  suggestions and said so only in the log. Because that pass runs eight batches
  at once, one burst could take out *all* of them — leaving a proposal that had
  cost a full drafting run with no review applied at all. A rate limit now costs
  a 60-second wait and one retry; a genuine failure still degrades the same way
  as before, losing one batch rather than the proposal.

- **Dream logs no longer warn about a citation nobody wrote.** Every run reported
  two unverifiable `**Source:**` citations pointing at a file that never existed
  — the citation checker was matching an example in its own source comments.
  Citations naming something that is not a dated learnings file are now logged
  quietly (they are a formatting slip with nothing to check), while citations
  that could send a reviewer to the wrong place still warn.

## [0.19.0] - 2026-08-05

### Added

- **Dev reference injection.** If you keep prescriptive engineering standards in
  `$CLAUDE_CONFIG_DIR/reference/dev/` (the docs multiplai-kit ships — uv/Python,
  Django/DRF, React/Next.js, Swift, FastAPI…), Claude is now told about the ones
  that apply to the project you are in, once per session per project.

  This deliberately does **not** go through the router. Memory is context about
  you and is picked by relevance to your wording; a standards doc applies
  because of what the project *is*. So detection reads the project's manifests
  — `pyproject.toml`, `package.json`, `Package.swift`, `Cargo.toml`, `go.mod`,
  plus framework hints (`manage.py` or a `django`/`fastapi` dependency, a
  `react`/`next` dependency) — and maps the stack to doc filenames.

  What gets injected is **pointers, not contents**: the absolute path plus the
  doc's section index, ~60 tokens. Inlining would cost 15k+ tokens per turn for
  one doc; Claude holds `Read` and only needs to know the doc exists and what is
  in it.

  Works from a workspace root too. If your cwd holds many repos and carries no
  manifest of its own (`knowhere/PROJECTS/<name>/…`), path-like tokens in your
  prompt are resolved to find the project you are actually pointing at.

  Turn it off with the **Dev Reference Injection** (`enable_dev_references`)
  option. It already does nothing on an install with no `reference/dev/`
  directory — no warning, no error.

## [0.18.6] - 2026-08-05

### Fixed

- **The plugin and the multiplai-gui hub now read a dream proposal the same
  way.** Their block-matching patterns are duplicated by design — the cross-tool
  contract is the `## Processed` heading, not a shared library — and they had
  drifted on the first day: a heading like `### 5.` with no summary counted as
  an item here and not in the hub. The stricter form is now used in both, and a
  test pins it, so the two tools cannot disagree about what the same file says.

## [0.18.5] - 2026-08-05

### Fixed

- **The digest's own pointer named a command that does not exist.** It said
  `Full detail: /fleet-status --full`; the invocation is
  `/multiplai-context:fleet-status --full`.
- **A waiting session with no project printed its name twice** —
  `hostname \`hostname\` — …`. It now prints the project and the container when
  both are known, and just the container when only that is.
- **The INBOX count only counted `*.md`.** Screenshots and saved links live
  there too, so a "swept" INBOX could still hold twenty things. Every top-level
  file counts now; subdirectories still do not, since those are your filing.
- **A stalled repo can no longer eat the digest's time budget.** Six bounded
  `git` calls at five seconds each is thirty seconds for one unreachable
  checkout. A repo now has ten seconds total, after which it reports what it
  learned and says the rest is missing.
- **Two concurrent runs could drop each other's cache entries.** The cache
  file is written under a lock.

### Changed

- `collect_backlog()` no longer accepts a `now` argument. It never used it.

## [0.18.4] - 2026-08-05

### Fixed

- **`backfill.py` no longer re-extracts the same sessions on every run.** The
  "have I done this one already?" check demanded a marker in *both* the
  learnings file and the diary. A session that produced only a diary entry —
  or only learnings, or, most commonly, nothing worth recording at all — never
  satisfied both, so every `--since` run read the whole transcript and called
  the model again, and again. Either marker now counts, and a run additionally
  records the *act* of extracting in `processed-sessions.txt`, which is what
  covers the session that legitimately yielded no output.

  A session whose extraction **failed** — a rate limit, a timeout — is
  deliberately not recorded, so a later run still retries it. Losing a
  transcript permanently is worse than reading one twice.

## [0.18.3] - 2026-08-05

### Fixed

- **A `## Citation Repairs` entry can no longer be confidently wrong.** Dream's
  proposals cite provenance as `<learnings-file>:<line>`, and the repair pass
  corrects a filename only when it can *prove* the cited one is wrong. Two ways
  it could get that proof wrong:

  **A file that could not be read looked like a file that did not exist.** When
  `dream` failed to read one of your learnings files — a transient I/O error is
  enough — it simply left it out of what the repairer was given, which is
  indistinguishable from the file being absent. Every citation to that file then
  looked provably broken, and a neighbouring past-midnight record supplied a
  one-candidate match. A *valid* `2026-07-28.md:10` was rewritten to
  `2026-07-29.md:10` and listed as a verified correction — the one outcome the
  pass exists to prevent, since a wrong citation looks wrong and a wrong repair
  looks right. Citations naming an unreadable file are now left exactly as
  written, and the file is reported once so you can see that a check was skipped
  rather than passed.

  **A range citation was only checked at its start.** `2026-07-28.md:1-9999`
  passed silently while the proposal reported every citation as verified, and
  following it ran off the end of the file. Both ends are now checked; a range
  whose tail does not resolve is reported, never repaired — half a verified
  range is not evidence of which file the other half meant.

  Neither changes what a correct citation does, and neither makes the pass
  repair anything it did not repair before.

## [0.18.2] - 2026-08-05

### Removed

- **`scripts/synthesize_agents.py`** — `scripts/fleet_status.py --full` writes
  the same `AGENTS.md` from the same registry and checkpoints, and adds
  everything the registry cannot see. If you invoked the old script by hand,
  use `fleet_status.py --full` instead.
- **`scripts/replay_router_logs.py`** — a one-off ratio sweep over your own
  routing logs, with no caller. `scripts/eval_router.py --keep-ratio R` answers
  the same question against the golden-case harness.
- **The last `fleet.txt` remnants.** The one-line status-bar count was retired
  in 0.18.0, and `write_fleet_view()` has been deleting any leftover copy on
  every session since. That self-heal is now gone too — a runtime that has
  started at least one session on 0.18.0 or later has already been cleaned.
- **`dream.py --run`**, the hidden deprecated alias for `--auto`. Use `--auto`.
- **The legacy single-corpus router path** in `lib/memory_router.py`
  (`CorpusRouter.select`, both implementations, and the `MemoryRouter` alias).
  Routing has gone through `select_multi` since the three-corpus flow landed;
  nothing called the single-corpus entry point. No change to what gets routed.

### Fixed

- **Background work runs again.** Learnings and diary extraction, cost
  collection, the resources (qmd) refresh, the checkpoint writer and the memory
  maintainer had all been silently dead since the dependency consolidation on
  2026-08-04: the plugin launched each of them with uv's project resolution
  turned off, so the child could not read `scripts/pyproject.toml` and died
  immediately on `ModuleNotFoundError: No module named 'multiplai_core'`.
  Nothing surfaced it — every child is launched detached with its stderr
  discarded, and the process *did* start, so no hook logged a failure. The
  deferred-extraction drain was worse than a no-op: it moved each pending
  marker into `processing_extractions/` before launching the child that never
  ran, so every session start re-launched the same dead work. All eight launch
  points now name the plugin's scripts directory as the uv project, through a
  single shared helper so the flag cannot be dropped from one of them again.

  If your diary or learnings have a gap around 2026-08-04, that is why. The
  pending markers were not lost — they are still in the data directory and will
  be picked up once you are on this version.

- **Locks now actually lock across sessions.** Cost collection, the qmd
  refresh, and — most importantly — appends to your diary and learnings files
  put their lock files in the system temp directory. Under the container-based
  runtime each session gets its own `/tmp`, so two concurrent sessions locked
  two different files and both proceeded: concurrent appends to the same diary
  file could interleave. Locks moved to `<data_dir>/locks/`, on the shared
  workspace filesystem, matching what `/dream` already does.

- **`/dream-remember` Step 5 could delete learnings files that were still in
  use.** Two separate ways, both of which destroyed the sources behind work you
  had not finished reviewing. If you run `--gc-learnings` (Step 5 says to run it
  every time), update.

  **A consolidation run's own inputs.** `dream` records each chunk against the
  proposal name as it goes, but only writes the proposal file at the very end —
  minutes later on a long run. In between, the name was fully recorded and the
  file existed nowhere, and gc read "no such proposal" as "already decided", so
  it deleted the learnings the run was still consuming. After a crashed run the
  window never closed: the resumed run would rebuild from its staged drafts and
  produce a proposal citing files that no longer existed. gc now requires the
  proposal to actually be *present* in `applied/`, `rejected/` or `superseded/`
  before treating its sources as collectable.

  **Sources a folded-forward proposal still cites.** When a new run absorbs an
  undecided proposal, the old one moves to `superseded/` and its items — source
  citations and all — move into the new one, but the internal record still
  pointed at the old proposal. The old one looked decided, so gc deleted the
  files while the *new*, still-pending proposal cited them; the `**Source:**`
  lines in your open review stopped resolving. gc now reads what the pending
  proposals actually cite, which stays correct across a fold no matter what the
  record says.

  Both paths are now covered by tests, and gc fails closed throughout — a
  proposal it cannot read stops collection entirely rather than licensing a
  delete. If you were bitten by either, learnings files are git-tracked once
  committed, so `git log --diff-filter=D -- .multiplai/learnings/` will find
  them.

## [0.18.1] - 2026-08-04

### Fixed

- **`/multiplai-context:fleet-status` no longer reports collisions that are not
  collisions.** A real reading showed 16, of which 0 were real. Two causes.
  Every pair of agents working in one repo collided on a **blank path**,
  because a checkpoint may list the checkout root as an involved entry and
  stripping the repo prefix left `""` for both sides. And pairs of sessions
  *stopped at a prompt* collided on a document they had merely both **read** —
  a collision is a claim that someone might write the file while you are in it,
  which a session waiting on an answer cannot do until answered. Directory
  entries are now ignored (a shared directory says "same neighbourhood", not
  "same file"), and a session in `waiting_input` no longer holds a file against
  anyone. **Parked sessions still do**, unchanged: uncommitted work nobody is
  watching is a stronger claim on a file, not a weaker one. A genuine shared
  file between two live agents still reports, as before.
- **The backlog no longer announces a pending dream proposal that does not
  exist.** It counted every `.md` in `.multiplai/dreams/`, which includes
  `memory-lint-latest.md` — a report, not something to apply. It now counts the
  `processed-learnings-*.md` proposals that `/multiplai-context:dream` writes
  and `dream-remember` reads.

**Still known-imprecise, and not fixed here:** the `NEEDS YOU` lines quote each
session's last recorded next action verbatim and nothing checks it against the
world, so a request that has since been satisfied — a PR already merged, an env
var already set — keeps asking. Treat those lines as "what this session thought
when it stopped", not as verified state.

## [0.18.0] - 2026-08-04

### Removed

- **`fleet.txt` — the one-line status-bar fleet count — is gone.** A count
  with no referent (`9 fronts · 4 need you`) tells you there is a fire
  without telling you where; `/multiplai-context:fleet-status` replaced it
  with a ranked, actionable digest in 0.17.0. The fleet view now writes
  `AGENTS.md` only, and **deletes a leftover `fleet.txt`** from an earlier
  release so a status line still reading it goes blank instead of showing a
  frozen count. If your status bar shows a fleet segment, it disappears —
  that is this change, not a breakage; run the skill for the real reading.
  (The kit's status-line reader is removed in a paired `multiplai-kit`
  release.)

## [0.17.1] - 2026-08-04

### Fixed

- **Installed copies of the plugin work again.** 0.16.0 moved the hooks onto
  the repo's uv workspace via `--project "${CLAUDE_PLUGIN_ROOT}/../.."` — a
  path that only exists in the marketplace repo checkout. On a real install
  (a copy of the plugin subtree only) every hook failed to resolve its
  dependencies. Hooks and documented commands now use
  `uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts"`, which resolves through
  the workspace in-repo and standalone on an install (the scripts project
  declares its own git source for `multiplai-core`). Same ~0.06s warm hook
  latency.

## [0.17.0] - 2026-08-04

### Added

- **`/multiplai-context:fleet-status` — one ranked snapshot of everything in
  flight.** Agent sessions waiting on an answer, open pull requests with CI and
  review state, dirty or unpushed checkouts, background jobs, and the pending
  backlog, in one reading of under twenty lines. Read-only: it merges nothing,
  closes nothing, kills no session and deletes no branch.

  The list is ranked by **what is blocked on a decision only you can make** —
  an approved PR waiting for a merge click, then red CI on a PR you own, then a
  stack of PRs collapsed to one line with its merge order, then a session that
  asked you a question, then two sessions holding the same file. It is capped
  at eight items; past that you get the count, because an unbounded urgent list
  is the same overwhelm in a new font.

  `--full` prints the whole `AGENTS.md` report, `--json` emits `fleet.json` for
  another program, `--fresh` re-queries GitHub, and `--offline` skips it.

- **`AGENTS.md` now carries pull requests, repo hygiene, background jobs and
  backlog sections** when the new skill generates it, so the file the digest
  points at is the same file the digest summarizes. Sessions-only generation
  from the session hooks is unchanged, byte for byte.

- **`fleet.json`** — the fleet as structured data, including the derived
  `group` / `front` / `age_seconds` fields so another consumer cannot re-derive
  them differently.

### Notes

- **Nothing that is not collected is ever reported as zero.** Scheduled
  routines live server-side and print `not tracked`; an unavailable `gh` prints
  `not read`; a repo outside your GitHub token's reach is listed apart from
  real failures, because it is a standing fact about the credential rather than
  something that might work next time.
- **`gh` and `git` are both optional.** Each source degrades on its own and the
  rest of the reading still stands.
- Measured on a 27-repo workspace: about 9 seconds cold, half a second against
  the 5-minute PR cache.

## [0.16.0] - 2026-08-04

### Fixed
- **The prompt hooks no longer time out.** If your sessions have been printing
  `UserPromptSubmit hook timed out after 10s` / `after 30s`, this is that.
  Every prompt you typed fired two hooks, and each one re-resolved
  `multiplai-core` against GitHub before doing any work — measured at 12-68
  seconds per invocation, against timeouts of 10s and 30s. When the hook timed
  out, routing was skipped: no memory injected, silently.

  The cause was `multiplai-core` being pinned by git *tag* in each script's
  PEP 723 inline metadata. A tag is a mutable ref, so uv could not cache the
  resolution and asked GitHub every single time. Dependencies are now declared
  once in `scripts/pyproject.toml` and resolved from a committed lockfile —
  the same command now takes ~0.05s and touches the network on install only.

### Changed
- **Scripts are launched with `uv run --project` instead of
  `uv run --no-project`.** If you invoke any of them by hand — from a SKILL.md,
  a cron job, or a shell — the old form now fails with
  `ModuleNotFoundError: multiplai_core`, because the dependencies moved out of
  the script files. Every SKILL.md in this plugin shows the new form. There is
  still no venv for you to create or activate.
- **`multiplai-core` is no longer pinned per script.** It tracks `main`, with
  a lockfile recording what that resolved to, so updates arrive as a reviewed
  Dependabot PR that CI has run against. 25 of the 26 scripts were already on
  the same version, so the per-script pin was granularity nobody used.

### Removed
- **`requirements-dev.txt`.** The test suite no longer installs from it — CI
  supplies pytest per-run (`uv run --with pytest ...`) and `multiplai_core`
  comes from the workspace — so keeping it would have been exactly the kind of
  unwatched pin drift this release eliminates. The Dependabot pip entry that
  scanned it is gone too.

## [0.15.1] - 2026-08-03

### Fixed
- **The fleet reading counted dead sessions.** A status line saying
  `36 fronts · 5 need you · oldest 19d · 9 collisions` over a fleet of one
  running session is not a reading, it is noise you learn to ignore — and
  that is what the real registry produced: 117 entries, 34 of them counted
  as fronts, every one of the nine collisions between pairs of sessions that
  had been dead for three to eighteen days.

  Two causes, both fixed:

  - **Idle sessions counted as fronts.** `AGENTS.md` still lists every tab
    that is on the board, idle ones included — that is where you go looking
    for the one you forgot about. But `fleet.txt` has room for one number,
    and it now counts only what has a claim on you: **Needs you**, **Working**
    and **Parked**. The full read's header follows suit (`N front(s) … · N
    idle`), so the two cannot disagree.

  - **Collisions were reported between long-dead sessions.** A collision is
    the claim that two agents might *now* write the same file. Both holders
    must therefore be fronts and have been heard from within 24 hours; a file
    two sessions both touched last week is shared history.

### Changed
- **Session state is now documented in one place** — README → *Session
  accounting*. It had accreted across two half-sections and a lot of code
  comments, which is how you end up with three overlapping notions of "is
  this session alive" and no single page saying which is which. The new
  section covers what is on disk and who may write it, the three independent
  fields (`status` / `disposition` / group), how a session's end is detected
  (and why only a clean quit is ever *observed*), which groups are listed
  versus counted, and the GC cutoffs. The suite `ARCHITECTURE.md` links here
  rather than restating it.

- **There is now a single walkthrough of what a session actually does** —
  README → *The life of a session*. Checkpointing, extraction and the
  registry were each documented well and separately, which left no page
  answering the question people actually ask: what happens, in what order,
  and where does it fork. Ten stages from `docker run` to the deletion of
  the last registry entry, each fork stated as a branch — the four values
  of `SessionStart.source` and which two re-inject a checkpoint, the three
  outcomes of the Stop-hook checkpoint decision, the four ways out of a
  full context window, the ways a session can stop, the two drains and
  the atomic rename that keeps them from colliding, and the retry ladder
  down to `failed_extractions/`. It closes with every branch point in one
  table, including the two that turn features off entirely (`uv` missing,
  no model client). It ends with a worked example — two sessions, one quit
  and one left open over lunch — tracing what `fleet.txt` reads at each
  point and why the abandoned one is listed but never counted.

  Also corrects *Session accounting* → *When it refreshes*, which credited
  the fleet-view regeneration to `synthesize_agents.py` running alongside
  the drain. `SessionStart` writes it in-process; the host drain writes it
  again after a container exits; the script is the on-demand entry point.

## [0.15.0] - 2026-08-01

### Added
- **A tab you left open for days now has a checkpoint saying where it stopped.**
  Checkpoints used to be triggered by context size alone — bands at 100K and
  200K tokens — so a session that sat at 40K for three days had none at all.
  That is backwards: the session whose state you have most thoroughly lost track
  of was the one the fleet view had least to say about, because `AGENTS.md`
  reads its intent, next action and files-in-hand from the checkpoint.

  A third trigger now fires on age instead of size. It writes when a session is
  at least **30 minutes** old and either has never checkpointed or last did so
  over **3 hours** ago.

  **What it costs:** a four-hour session writes twice instead of once or twice.
  Checkpoint writes distill only the transcript since the last one and overwrite
  a single file, so writing more often makes each write smaller rather than
  making the total larger. There is a test asserting the two-writes-in-four-hours
  figure, so the cadence cannot drift silently.

  Both numbers are configurable:
  `CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours` and
  `CLAUDE_PLUGIN_OPTION_checkpoint_min_session_minutes`. Setting `stale_hours`
  to `0` turns the age trigger off and restores the previous size-only
  behaviour exactly. These are **new** settings —
  `checkpoint_ttl_hours` still means what it always meant (how long a `/clear`
  handoff marker stays valid) and is unaffected.

  A session idle for three days fires no hooks at all, so nothing runs during
  the quiet stretch. That is fine: the last completed turn *is* the moment the
  work stopped, so the checkpoint it leaves behind is at most `stale_hours`
  behind where the session actually ended up.

## [0.14.0] - 2026-08-01

### Changed
- **Checkpoints are now cleaned up once the diary has been written.** A
  checkpoint is *live state* — roughly where a session is right now, so the next
  context window can pick it up. The diary is the permanent record of what the
  session actually did. Those are two artifacts with two lifetimes, but only the
  diary ever had an ending: every session that crossed a token band left a
  directory in `.multiplai/data/checkpoints/` forever. There were **182 of them**
  on one machine, one per session ever run, none ever collected.

  Extraction now deletes a session's checkpoint directory once that session's
  diary entry exists — and only then. Six things keep one alive:

  - **The extraction was not fully successful.** A partial LLM failure can
    still write a diary entry from the chunks that survived, but that entry
    covers only part of the session — a diary that does not fully supersede
    the checkpoint never deletes it.
  - **The session is still running.** An extraction deferred by context
    compaction runs against a live session; its checkpoint is working state,
    not a leftover, and is kept untouched.
  - **The session is `parked`.** `AGENTS.md` renders a parked session's intent,
    next action and files-in-hand from its checkpoint, and a parked session is
    precisely the one still listed weeks later. Deleting it would leave an entry
    you deliberately kept with nothing to say.
  - **The diary write failed** — nothing has superseded anything.
  - **A checkpoint writer is still running** for that session.
  - **An unconsumed `/clear` rebuild marker still points at it** — you crossed
    the handoff threshold and closed the tab instead of clearing, so that
    checkpoint is what tomorrow's session rebuilds from.

  Nothing you can see changes: no checkpoint is removed while it is still the
  best available answer to "where was this?", and if the cleanup itself fails,
  the diary entry is already written and unaffected.

## [0.13.0] - 2026-08-01

### Added
- **Say "park it for now" and the session stays on the list.** There is no new
  command to remember — which is the point, because the moment you want to park
  something is the moment you are overloaded and walking away, and that is
  exactly when you will not remember a `/park`.

  The extraction pass that already reads your whole transcript for the diary now
  also reads how you *left*, from the closing exchange alone, and labels the
  session one of three ways:

  - **`done`** — you said so. "we're done", "ship it", "merged, thanks".
  - **`parked`** — you said you were stopping without finishing. "park it for
    now", "let's pick this up tomorrow", "shelve this".
  - **`active`** — everything else, including a session that just stops
    mid-work. This is the default, and anything ambiguous lands here.

  Three things follow. A parked session shows in `AGENTS.md` under its own
  **Parked** heading — between **Working** and **Idle**, since it is not urgent
  but it is the pile you *chose* to return to — quoting your own closing words
  as the reason. It stays there after its container exits, which is the whole
  point of parking it. And a `done` session drops off the list even if its
  container is still running.

  Most importantly, **a parked session is never garbage-collected.** Registry
  entries normally age out in 7 to 30 days, while the transcripts behind them
  survive a year — so a parked idea used to stay `--resume`-able while becoming
  invisible, which is the failure this fixes. Nothing is copied anywhere; the
  session record simply stops expiring.

  It costs no extra model call — the label rides along on the response the
  diary extraction was already making — and it is written to its own registry
  key, so a session's liveness (`working`/`idle`/`ended`) is untouched: a
  session can be both `ended` and `parked`. If the model says nothing useful,
  or extraction fails outright, the session is `active` and everything else
  behaves exactly as before.

  The label's lifecycle holds up at the edges, too. Resuming a session clears
  its old departure label — a resumed parked session groups by what it is
  *doing* again (including "Needs you"), and a resumed done session comes back
  onto the list. The label survives a long absence: registry cleanup skips any
  session whose deferred extraction is still queued, so coming back on day 8
  to a session parked on day 0 still finds it labelled. And on a long
  transcript, a mid-session extraction hiccup no longer costs the label — only
  the closing exchange's own chunk decides it.

## [0.12.0] - 2026-08-01

### Added
- **A fleet view: one file that says what every agent is doing.** If you run
  several Claude Code sessions at once, the state of each one has until now
  lived in your head — the plugin knew *where* each session was (the session
  registry) and *what* it was doing (the per-session checkpoint), but nothing
  put the two together where you could read it.

  `.multiplai/data/AGENTS.md` now does. Each session appears under **Needs
  you** / **Working** / **Idle** / **Ended**, carrying its project, container,
  branch (worktrees say which one), how long it has been quiet, what it is
  doing, what it will do next, and which files it has in hand. Below that, a
  **Collisions** section lists every file two live sessions are both holding —
  the overlapping-work question answered without any agent talking to another.

  A second file, `.multiplai/data/fleet.txt`, carries the same reading as one
  short line — `6 fronts · 2 need you · oldest 3d · 1 collision` — cheap enough
  for a terminal status line to `cat` on every prompt.

  It refreshes by itself: at every session start, and on the host right after
  your last tab closes. To render it by hand:

  ```
  uv run --no-project <plugin>/scripts/synthesize_agents.py --stdout
  ```

  **No model call and no network** — it is pure aggregation of files that
  already exist, so it costs a few file reads and is safe to run at any time.
  Both files are a **cache**: delete them and the next run rebuilds them
  identically. Nothing writes into `AGENTS.md` as state, and anything you add
  to it by hand is overwritten on the next refresh.

  Sessions that never wrote a checkpoint (most of them — checkpoints only
  trigger past a context-size band) still appear, with the fields the registry
  does have.

## [0.11.0] - 2026-08-01

### Added
- **The diary can now be written the evening you stop working, instead of the
  next time you open a session.** When a session ends, the plugin leaves a
  marker and something else has to pick it up and run the (multi-minute)
  extraction. Until now the only thing that ever picked one up was the *next*
  `SessionStart` — so closing your last tab on a Friday produced Friday's diary
  entry on Monday, and a fleet of long-lived tabs could sit on unwritten
  history for days.

  There is now a standalone `scripts/drain_extractions.py` that drains the same
  queue from outside a session:

  ```
  uv run --no-project drain_extractions.py --data-dir <workspace>/.multiplai/data
  ```

  It takes an optional `--data-dir` (defaulting to the usual path cascade),
  `--wait` to block until each extraction finishes with its errors visible, and
  `--verbose` for a one-line summary. Container launchers can call it right
  after the container exits; you can also just run it by hand when you suspect
  a session never got written up.

  Session start drains through exactly the same code, so the two paths cannot
  drift apart. Two drains racing is handled end to end: the dequeue is an
  atomic rename that also refreshes the marker's mtime (staleness is measured
  from *launch*, so a marker written Friday and drained Monday is not
  instantly "stale" and re-launched by a concurrent session start), and
  staleness recovery claims a marker atomically before rewriting it, so two
  recoverers cannot double-count its retry attempts.

  `--wait` exits nonzero when any extraction child fails, so
  `drain … --wait && echo ok` actually proves extraction worked. Errors (a
  missing `extract_learnings.py`, failed children) always print to stderr;
  `--verbose` gates only the success summary.

  The script's header documents which environment it needs: notably
  `CLAUDE_PLUGIN_OPTION_workspace_dir` (or `WORKSPACE`), without which the
  diary silently lands in `~/.multiplai/` instead of your workspace, and
  `CLAUDE_CONFIG_DIR` so the Agent SDK finds your existing credentials.

  The startup line reports **both** queues — `(N pending, M in flight)`. A
  session whose container died mid-extraction leaves its marker in
  `processing_extractions/`, where the drain can still rescue it but the
  pending count cannot see it; reporting pending alone made such a run
  announce `0 marker(s) pending` and then say it had drained one, which reads
  as a malfunction rather than a recovery.
## [0.10.2] - 2026-07-31

### Fixed
- **A big memory file no longer runs out of time while being updated.** Writing
  approved changes back into a memory file was given the same fixed time limit
  no matter how much there was to write. Your largest files are exactly the ones
  that collect the most updates — a recent backlog had 41 KB of pending changes
  for `claude-code-tools.md` and 38 KB for `multiplai.md` — and on a slow call
  those could run out of time and be skipped, silently leaving the file
  untouched while smaller files updated fine. The time limit now scales with how
  much is actually being written. A file that still runs out is left exactly as
  it was, never half-written, and the log now names the file and its size.

### Changed
- **`/dream-remember` now applies a review per target file instead of per
  item.** A large proposal used to cost one script cold start and a fresh
  read of the same memory file for every single decision — a 70-item review
  across 14 files ran out of context part-way through and had to hand off,
  having applied five. The skill now reads each memory file once, applies all
  of its approved edits, updates `Last Updated` once, and records every
  decision for that file — approved *and* rejected — in one call. Reviews that
  previously needed a compaction handoff now finish in one sitting. Nothing
  about *consent* changed: `[RULE-PROPOSAL]` items are still presented and
  answered one at a time, and items you neither approved nor rejected stay
  pending.

### Added
- **`dream.py --mark-processed --decisions -`** takes a JSON array of
  decisions on stdin — `{"kind","file","index","status","target"}` per item —
  and marks them all in one read and one write of the proposal, printing
  `marked N processed, M unchanged`. The write is atomic, so an interrupted
  or failed call leaves the proposal exactly as it was rather than
  half-decided. The existing single-item flags are unchanged and still
  supported.
- **`dream.py --gc-learnings`** replaces the skill's judgement call about when
  it is safe to delete consolidated learnings files. Pure code, no model call:
  a file is removed only when every `## Session Learnings` record in it has
  been consolidated **and** no proposal citing it is still pending, so a
  review you left half-finished keeps the sources its `**Source:**` citations
  point at. It prints what it removed and why it kept the rest. Step 5 of
  `/dream-remember` now runs this instead of deciding by hand.

## [0.10.1] - 2026-07-31

### Changed

- **`/dream` now runs 8 consolidation calls at once instead of 4.** On a large
  backlog four was simply too few to finish in a reasonable time: a measured
  283 KB backlog is about 98 minutes of model work, which four workers cannot
  get through in under ~24 minutes no matter how it is scheduled — the actual
  run took 38. Eight roughly halves that. The trade is that `/dream` now uses
  more of your machine while it runs; if you would rather it stayed out of the
  way, `MULTIPLAI_DREAM_CONCURRENCY=4` (or any number) still wins.

### Fixed

- **`/dream` no longer cites the wrong file for some of its sources.** Every
  entry in a proposal ends with a `**Source:**` line naming the learnings file
  and line it came from, so you can check an entry before applying it. When a
  session ran past midnight its notes are saved into the *next* day's file, and
  `/dream` sometimes cited it under the earlier date — sending you to a file
  where that line doesn't exist. On a large backlog about 2% of citations were
  affected. They are now corrected automatically wherever the right file can be
  identified beyond doubt, and a `## Citation Repairs` section at the end of the
  proposal lists every correction made. Citations that can't be verified are
  listed there too, and left exactly as written rather than guessed at.

- **`/dream` no longer loses a chunk of learnings on its first run.** The very
  first run on a machine had to guess how fast drafting goes, and it guessed
  nearly twice too fast — so it built chunks bigger than it could finish inside
  the deadline. On a 283 KB backlog one chunk of twelve ran out of time, retried,
  ran out again, and was skipped. Nothing was lost permanently (a skipped chunk's
  learnings stay pending and come back next run), but the run took 30 minutes
  longer than it should have and quietly consolidated less than it reported. The
  starting guess is now the measured rate. From the second run onward `/dream`
  has always calibrated itself from your own machine, and still does.

- **The second-pass reviewer now actually runs on large backlogs.** `/dream`
  drafts a proposal and then re-reads it to merge duplicates, drop
  point-in-time noise, and re-route items filed under the wrong memory file. On
  a big backlog that review was handed the entire proposal in one piece — far
  more than it could read in the time allowed — so it timed out and was skipped,
  every time, while the log said only "keeping the merged draft". The review is
  now done in batches, so it runs on backlogs of any size. Its edits are applied
  to the whole proposal together, exactly as before. If one batch fails, the rest
  of the review still lands instead of the whole pass being lost.

- **`/dream` no longer reports a partial run as a complete one.** When some of a
  run's work failed — a rate limit, a slow call, a network blip — it still
  printed the number of learnings it *set out* to process, not the number it
  actually did, and said nothing about the second-pass review having been
  skipped. A run that consolidated 122 of 231 learnings announced "231 new
  learning block(s)", which reads as "your backlog is done" when half of it is
  still queued. It now says `122 of 231 ... consolidated`, names how many chunks
  did not finish, and states that the deferred learnings come back on the next
  run. Nothing about the recovery behaviour changed — those learnings were always
  safe and always came back; only the reporting was wrong.

- **`/dream --check` now tells you its estimate is a minimum, and says so
  honestly.** The old estimate assumed chunks run in synchronised batches,
  waiting for the slowest of each group before starting the next. They don't —
  a chunk starts as soon as a slot frees up, so the old answer changed depending
  on nothing more than the order the chunks happened to be listed in (a 16%
  swing on a measured 283 KB run). The new number can't do that. It is a floor
  rather than a guess, so it now prints as `est. ≥24m` and "at least 24 min":
  the one run measured against it came in 13% above its own prediction, and a
  bare figure would have promised a precision it doesn't have. The plan line
  also drops the "wave" count, which never described how the work was
  scheduled.

## [0.10.0] - 2026-07-31

### Changed
- **`/dream` now handles a backlog of any size, in one run, without moving
  anything.** It used to read every learnings file and consolidate them in a
  single model call. That call emits proposal text at a flat rate regardless of
  how much you feed it, so the run time was essentially your backlog size
  divided by a constant — and past roughly 150 KB it hit the internal ceiling
  and failed every time. The only workaround was to move learnings files out of
  the directory and back a few at a time, which produced one proposal per slice
  and, on at least one killed run, left files stranded in a hidden backup
  directory where no later run could see them.

  Dream now splits the backlog into chunks sized from the timeout, drafts them
  concurrently (4 at a time by default), and merges the results into one
  document. **No learnings file is moved or deleted by this path anymore.**

- **`/dream` reports what it is about to do before spending anything.** A
  planning line names how much is new, how many chunks it will take, and the
  estimated wall clock. `dream.py --check` prints the same plan without
  starting a run. The estimate self-calibrates from your own observed
  throughput as runs complete.

- **Re-running after a crash resumes instead of starting over.** Dream keeps a
  ledger of which learnings it has already consolidated, keyed by content, so a
  killed run costs only its in-flight chunks. Whitespace-only edits to a
  learnings file do not orphan its ledger entries.

- **A second `/dream` while one is running is now a no-op**, not a competing
  writer. It names when the running one started and exits cleanly.

- **You end up with exactly one proposal to review.** An earlier proposal you
  have not decided on is folded into the new one and moved to
  `.multiplai/dreams/superseded/`.

  Two kinds of proposal are never touched: one you have already decided items in
  (it has a `## Processed` section), and **one you have curated by hand**.
  Curation is detected by content, not modification time — so a `git checkout`
  or `git stash` in your workspace cannot make dream mistake an untouched
  proposal for an edited one, and editing a proposal cannot be undone by dream.

- **`## Filtered Out` is now one line per dropped item** instead of a
  multi-line block, so the section stays skimmable. It is still per-item on
  purpose: a filtered learning is marked consolidated and will not resurface,
  so each drop has to stay visible.

### Requires
- `multiplai-core` **v0.12.0** (up from v0.10.0), for the per-call timeout that
  lets one oversized chunk get a longer ceiling without affecting the others,
  and for the `alive` heartbeat that shows a long call is still producing. Set
  `MULTIPLAI_AGENT_HEARTBEAT_S=0` to silence the heartbeat.

### Tuning
- `MULTIPLAI_DREAM_CONCURRENCY` (default `4`) — chunks drafted at once. Each is
  a CLI subprocess; raise it only if your machine has headroom.
- `MULTIPLAI_DREAM_THROUGHPUT` — override the bytes-per-second estimate.

## [0.9.0] - 2026-07-27

### Fixed
- **`/setup` now writes plugin options to the key Claude Code actually
  reads.** It previously wrote `pluginConfigs.multiplai.options`; the
  correct key is the compound form
  `pluginConfigs["multiplai-context@multiplai"].options`. The wrong key
  fails silently — every option quietly falls back to its default — so if
  you ran setup before this release and set a custom workspace, check your
  `settings.json` and move the `options` block under the compound key.
- The plugin README no longer claims Claude Code prompts for `userConfig`
  values at enable time (it doesn't —
  [anthropics/claude-code#39455](https://github.com/anthropics/claude-code/issues/39455)).
  Options are collected by `/setup` or set manually; the README now shows
  the correct settings key.

### Changed
- **`/setup` is now a 2-question quick path by default** — your name and
  your workspace directory, then one restart at the end (the two mid-flow
  restart notices are gone). It finishes by walking you to your "first
  recall": ask a fresh session what it knows about you and see the memory
  arrive. The complete interview (identity/technical/general preferences,
  routing scope, project identity, git for memory) is unchanged and now
  runs via `/multiplai-context:setup full`.
- The README quickstart now ends at the first recall — what to do, what
  you should see, and how to read the `[context]` routing line that proves
  it — instead of ending at configuration tables.
- `/dream-remember`'s docs no longer name an unreleased GUI; the
  `## Processed` cross-tool contract is unchanged.

### Added
- **Troubleshooting** section in the README: `uv` missing, slow first
  start, hooks apparently doing nothing, memory not injected, settings
  changes not taking effect, where the logs live.
- **Uninstall** section in the README: the two commands, what stays on
  disk, and how to remove everything.
- **What it costs** section in the README: real ledger-derived figures for
  the unattended passes (dream proposal ≈ $1, catalog refresh ≈ $0.05,
  background extraction/rebuild calls ≈ $0.16 — medians, July 2026), whose
  quota unattended calls draw on (the Agent SDK, i.e. your own Claude Code
  auth; an `anthropic_api_key` only as explicit fallback), and how to
  switch the background work off.

## [0.8.2] - 2026-07-27

### Changed
- **`/log-doctor`'s untrusted-text handling now comes from `multiplai-core`**
  instead of a copy kept in `log_doctor.py`. Digest output is unchanged, with
  two improvements inherited from the shared version: a log line saying "ignore
  **the** previous instructions" is now marked `⟪INJECTION?⟫` (the local copy
  only matched "ignore all/any previous instructions", so that phrasing slipped
  through unmarked), and a `source` label containing a `"` can no longer close
  the fence's tag attribute. Core pin moves `v0.9.0` → `v0.10.0` across all 23
  scripts and `requirements-dev.txt` together.

## [0.8.1] - 2026-07-26

Post-merge review fixes for memory evolution (findings 7.1–7.4 of
`INBOX/pr-review-batch-2026-07-26.md`, against #62). No new features.

### Fixed
- **The condition sweep could silently skip a whole cycle**
  (`scripts/lib/prospective.py`). The sweep fired on
  `(today - captured).days % 30 == 0` — only on exact 30-day multiples from
  capture. Miss that single day (no session, a run either side of the UTC
  midnight rollover, a closed laptop) and the intention waited another 30 days.
  For the one memory channel whose stated failure mode is *being silent*, a
  scheduler that can skip is the wrong shape. Now each intention carries a
  `last_surfaced` stamp in `prospective_sweep.json` (plugin data dir — derived
  state, deliberately NOT in the human-edited `prospective.md`) and the test is
  elapsed-time: `today - last_surfaced >= 30`. Because that condition stays
  true until something records the surface, an un-stamped sweep now re-fires
  next session — noise, never silence.
  - Keyed on the intention's condition + text, not its line number, so
    reordering or reflowing `prospective.md` doesn't reset the clock.
  - An intention with neither stamp nor capture date now surfaces once and is
    stamped, instead of being ignored forever.
- **The maintainer was spawned on every session start, gate or no gate**
  (`scripts/session_start.py`). `_launch_maintainer` unconditionally `Popen`'d
  `uv run … memory_maintainer.py`; the child then checked its own 24h gate and
  exited. The gate check is a timestamp read, so it now happens **in-process
  before the spawn** — the child stays authoritative, but ~95% of sessions no
  longer pay a `uv run` startup (and, on a cold uv cache, a network fetch for
  the PEP 723 git dependency) to accomplish nothing. A test pins the restated
  gate constants to the maintainer's own.
- **Unattended dream proposals accumulated one file per day**
  (`scripts/memory_maintainer.py`). Generating a proposal correctly does not
  stamp the dream gate (nothing was applied), so a backlog left unconsolidated
  for a week produced seven dated proposals — seven proposal + critique model
  calls — and handed `/dream-remember` a pile to choose between. The pass is now
  skipped while any un-archived `processed-learnings-*.md` is still waiting.
- **An ancient `as of` with no `review by` was permanently clean**
  (`scripts/lib/memory_lint.py`). `(as of 2019-01)` matched the annotation
  regex and was accepted as complete, so nothing could ever expire it — while
  the linter's whole premise is that facts rot on their own schedule. Reported
  after 12 months as a **third kind**, `undated`, not folded into `expired`:
  nothing has passed, and blurring a missed review date with a missing one would
  misdescribe both. `--expired-only` still means only genuinely-passed dates.
- **A trailing `<!-- comment -->` rode into the surfaced intention text**
  (`scripts/lib/prospective.py`). Closed comments are now stripped from a line
  before it is matched.

### Changed
- **Core pin moved `v0.8.1` → `v0.9.0`** across all 23 PEP 723 script headers
  and `requirements-dev.txt`. The maintainer resolving one core version while
  the hook that launches it imports another is two versions of the same library
  in one workflow.
- README documents the three linter kinds, the end-of-month meaning of a
  `YYYY-MM` stamp (previously only in a docstring), the elapsed-time sweep, and
  the pre-spawn gate. `templates/prospective.md` states the sweep cadence and
  the comment-stripping rule.

## [0.8.0] - 2026-07-26

Memory evolution (#62), untrusted-log handling (#60), behavioural contracts
(#61), and outcome-based cost reporting (#59).

### Added
- **Prospective memory — intentions that fire later** (`scripts/lib/prospective.py`,
  `templates/prospective.md`, seeded by `/setup`). Every other memory file
  answers "what is true?"; `memory/prospective.md` answers "what did I say I'd
  come back to?". One intention per line, two trigger kinds:
  - `- [due: 2026-09-01] Re-check X (captured 2026-07-26)` — a date. Surfaced
    as a `SessionStart` nudge from a week before, and for as long as it stays
    overdue.
  - `- [on: the runtime moves past v0.5] Re-run the audit (captured 2026-07-26)`
    — a condition in prose. **Never machine-evaluated.** No code decides
    whether "the runtime updated" has happened, because a wrong guess fires the
    reminder at the wrong time; these surface through normal memory routing and
    on a periodic sweep instead.
  - Nothing expires automatically and nothing writes to the file behind your
    back — capture goes through extraction → dream → `/dream-remember` like
    every other learning. Remove acted-on intentions yourself.
- **Proactive memory maintainer** (`scripts/memory_maintainer.py`), launched
  **detached** from `SessionStart` and gated to **at most once per 24h**. Four
  passes: staleness lint → dream proposal (only when the dream gate is open and
  a backlog exists) → catalog refresh (only when memory is newer than the
  catalog) → `now/` status rebuild for the active project (on a Haiku tier —
  unattended work shouldn't spend the session's model budget). Silent, never
  blocks session start, and a failed run costs one duplicate pass next session.
  - **It never writes to `.multiplai/memory/`.** That is the whole safety
    story: passes 1–2 write proposals to `.multiplai/dreams/` and the health
    log; 3–4 write derived files that are rebuilt from source.
    `/dream-remember` remains the only path that edits memory.
  - Run by hand with `uv run --no-project scripts/memory_maintainer.py
    [--force] [--dry-run]`. State: `<data_dir>/maintainer_state.yaml`.
- **Per-fact validity windows** (`scripts/lib/memory_lint.py`). Memory files
  carry one file-level `**Last Updated:**` stamp, which says when the file was
  touched, not whether a given fact is still true. Facts may now carry
  `(as of 2026-07)` and `(as of 2026-07, review by 2026-10)`; the linter reports
  `expired` (a passed `review by`) and `unmarked` (a volatile-class fact with no
  annotation). **Warn-only and non-rewriting on purpose** — the volatile-class
  patterns are heuristics, so a false positive must cost one noisy report line,
  never a silently rewritten fact. Surfaced by the maintainer and in
  `/health` (`memory_validity`), and the dream prompt now asks for the
  annotation on newly proposed volatile lines.
- **Conflict-triggered supersede edits** (`scripts/lib/conflict_edits.py`). For
  each CORRECTION / `trust: verified` learning, a deterministic pass finds the
  existing memory line that learning is about and prepends a
  `## Conflict Resolutions` section **above** the model's own proposal output,
  so review sees corrections before new information. Precision over recall by
  design (`MIN_OVERLAP`): it emits nothing when unsure, and it says plainly that
  text overlap cannot distinguish "contradicts" from "restates" — both want the
  same handling (edit in place, don't append a near-duplicate), and which one it
  is stays the reviewer's call. Fail-open: a crash here never loses a proposal.
- **Behavioural contracts for skills** (`skills/costs/CONTRACT.md`,
  `skills/log-doctor/CONTRACT.md`) — the two pilots for the `--contract` mode of
  `multiplai-dev`'s `promote_skill.py`. Each case is a shell command plus a
  substring that must appear in its output; they pin the *shape* of the
  interface (the `--by` dimensions, `--json` being parseable JSON), never the
  numbers, since a cost ledger changes hourly.
- **Cost reporting per outcome, not per token** (`scripts/costs_report.py`):
  - `--group task --pr-join` — a task is a branch whose PR state resolves via
    `gh` (merged / closed / open / no-pr); the summary divides total cost by
    merged-PR count with per-task median/p90. A repo `gh` cannot reach degrades
    to `no-pr` instead of failing the report.
  - `--group build` — cost per DONE **and** per FAILED buildme block, joined
    from `specs/changes/*/.build-state.json`. The failed-block figure is the
    retry-and-loop spend.
  - `--report cache [--cache-threshold R]` — per project/skill/component cache
    hit ratio `cr / (in + cr)` and write share, flagging rows under the
    threshold. Computed from fields already in every ledger record, so it works
    on historical months. Baseline measured 2026-07-25: **99.7% overall hit
    ratio**, every SDK component at 100.0% — "already optimal", not a fix.
  - **Framing rule:** cross-model comparisons are per-outcome only. Different
    models tokenize differently, so a cheaper per-token model that loops twice
    is more expensive.

### Changed
- **Log text is now treated as untrusted input** (`scripts/log_doctor.py`).
  Anything that reaches a log — an echoed HTTP response, a filename, a
  traceback carrying a remote payload — can be authored by someone who wants the
  agent *reading the digest* to act on it. Two defences applied in the script
  rather than trusted to the reader: (1) log text can no longer break out of its
  container (C0/C1 controls, ANSI escapes, zero-width and bidi characters
  stripped; code fences and `untrusted-content` tags defanged), and (2)
  instruction-shaped spans are marked in place as `⟪INJECTION?⟫` — the analyst
  still sees the original words, which is the forensic signal, but they arrive
  labelled. Every digest carries an `UNTRUSTED_NOTICE` stating that fenced
  content is data, never instructions, and that imperative text inside a fence
  is a finding to report, not an order to follow.
- **Config-audit nudge cadence tightened 90 → 60 days** (`session_start.py`,
  `config_audit.py`, `skills/config-audit/SKILL.md`). The cadence is now pinned
  to how often *models* ship, not to how fast configuration rots: what the audit
  removes is scaffolding a newer model no longer needs, and rules written for a
  model two releases ago still cost tokens and still constrain a model that
  outgrew them. The skill gained a "delete scaffolding aggressively" section —
  the test is "would a current model do this correctly without being told?", and
  a ruleset that *shrinks* after a model upgrade is the expected outcome.
- `/health` now reports per-fact `memory_validity` findings alongside the
  file-level mtime freshness. Non-fatal: a lint failure must not take down the
  report people run when something else is already broken.

## [0.7.0] - 2026-07-23

### Added
- **In-file decision record for dream proposals (`## Processed`).** When
  `/dream-remember` applies, edits, or rejects an item, the item's block is now
  **moved into a `## Processed` section** at the end of the proposal `.md`
  instead of the proposal being reviewed all-or-nothing. Items under
  `## Processed` are no longer pending, so a proposal can be reviewed partly in
  the multiplai-gui GUI and finished in the CLI (or vice versa) with no
  double-applies. The `## Processed` heading is the entire cross-tool contract —
  no sidecar file, no key scheme. Mirrors the multiplai-gui hub.
  - New `scripts/lib/dream_processed.py`: `move_to_processed` / `mark_processed`
    (idempotent, group-aware block relocation via write-then-rename) and
    `has_pending_items`.
  - New `dream.py --mark-processed --proposal … --kind {update|action} --index N
    [--file …] --status {applied|edited|rejected} [--target …]` verb, called by
    the skill per item so decisions are recorded mechanically, never hand-edited.
  - `dream.py --archive` now **refuses** a proposal that still has pending items
    (exits non-zero), so a partially-reviewed proposal is left pending instead of
    silently discarding the remainder.

### Changed
- `dream-remember` SKILL: Step 3 presents only pending items (skips
  `## Processed`); Step 4/4b record each decision via `--mark-processed`; Step 5
  cleans up learnings only when the proposal is fully decided; Step 6 archives
  only when nothing is left pending (undecided items stay for a later run/GUI).

## [0.6.17] - 2026-07-17

### Fixed
- **Single-token false positives in `token_overlap` routing.** Smoothed IDF
  over a small catalog values any df=1 domain term at `log((N+1)/2)+1` —
  ≈3.74 on a 30-entry catalog, above `MIN_SIGNAL=2.0` — so ONE incidental
  generic token ("search", "browser", "skill", "data") injected an unrelated
  file (live-reproduced: a travel prompt pulled in three files, each on a
  single token; "data" alone injected five). Two levers, both scoring-side
  (`keep_ratio` / relative-cutoff logic untouched):
  - **Match-breadth eligibility gate** (`MIN_DOMAIN_MATCHES = 2`): an entry
    can clear the NONE floor only if it matched ≥ 2 distinct `intent_domains`
    tokens or a multi-word domain phrase appears verbatim in the prompt.
    Ineligible entries still rank in diagnostics but are never picked
    (`select_multi` only; `select()` keeps its pure rank+cap contract).
    Hardcoded, not a plugin option — 2 is the smallest breadth that kills
    one-token noise, and no catalog geometry favors another value.
  - **Per-term IDF cap** (`MAX_TERM_IDF = 2.5`) bounds the small-catalog
    inflation so no single locally-rare term can dominate a score (the
    historical `audiovideo.md`=23.5 spike mechanism).
  Seed golden set: NONE-accuracy 66.7% → 100% with recall and precision held
  at 100%; production replay unchanged (28.4% cap-hit at `keep_ratio=0.30`).
- **`ROUTING_SCORES` accuracy under the gate.** Picks are no longer a
  contiguous prefix of the ranking, so the log line now emits the router's
  actual injected set (`picked_scored`) and computes `floor_excluded` as the
  best non-injected score.
- **The other contiguous-prefix consumers of the ranking.** A shared
  `_picked_ranking` helper now feeds everything that reports or reasons
  about "what was actually injected":
  - The post-cooldown re-floor anchors on the top *pick* instead of the raw
    ranking's top — which, under the gate, can be an ineligible entry that
    never injected and thus never appears in `suppressed`, silently
    disabling the re-floor exactly when weak cooldown survivors most need
    re-checking. Intended side effect (matches the documented contract):
    bundle/co_retrieve expansion picks are no longer bar-checked via their
    incidental raw-pool scores.
  - The activity-line score hint reads top/floor from the picked set, and
    abstention distinguishes "best N lacks match breadth" (raw best may
    clear MIN_SIGNAL — the single-token trap) from a genuine
    "best eligible N < floor" (via a new `top_eligible` diag field).
- **`replay_router_logs.py`** imports `MIN_SIGNAL` from `lib.memory_router`
  instead of mirroring the constant (drift-proof, and satisfies the sys.path
  wiring test).

## [0.6.16] - 2026-07-17

### Fixed
- **Routing cap-saturation / filler injection.** The `token_overlap` router
  was hitting the 10-file memory cap on ~45% of routes (measured by replaying
  439 real routing calls, 2026-07-10→16). Root cause: `domain_score` is an
  unnormalized sum of matched IDF weights, so a long/rich prompt inflates the
  whole ranking and the shallow `0.20×top` relative cutoff admits a fat filler
  tail — median 16 candidates cleared it and the cap chopped to 10, so the
  *cap*, not relevance, did the filtering. The relative-cutoff ratio is raised
  `0.20 → 0.30` (halves cap-saturation to ~28%; production-replay is exact for
  a tighter policy since it can only trim). Top matches are always retained;
  only the sub-30%-of-top tail is dropped. On the seed golden set, `0.30`
  raised precision 96.8%→100% and false-positives 3.6%→0% with recall held at
  100% (multi-domain matches preserved).

### Added
- **`keep_ratio` plugin option** (default `0.30`) exposes the cutoff as a
  live-tunable knob, so it can be dialed in production without a code release
  while a full golden eval set is rebuilt. Clamped to `(0, 1]`.
- **`scripts/replay_router_logs.py`** — label-free, real-traffic eval that
  sweeps `keep_ratio` against your own `ROUTING_SCORES` logs.
- **`eval_router.py --keep-ratio`** for golden-case sweeps at a given ratio,
  plus a **synthetic** eval fixture (`evals/synthetic-fixture-catalog.json` +
  `evals/synthetic-cases.jsonl`, a fictional persona — no real user data) so
  the harness runs out of the box for CI / reviewers (the previously-cited
  50-case golden set was absent from the repo). Real, user-supplied golden
  sets live privately under `<workspace>/.multiplai/data/evals/`, which
  `eval_router.py` now discovers by default.

## [0.6.15] - 2026-07-16

Fixes from the 2026-07-12→16 PR audit (mktplace PRs #24–#39).

### Fixed
- **PreCompact freshness gate (data-loss guard).** The summarizer-stub
  directive is now emitted only when the checkpoint on disk is *fresh* —
  the synchronous pre-compaction checkpoint succeeded this invocation, or
  the checkpoint's recorded token watermark / file mtime is close to the
  live context size. Previously a *stale-but-valid* checkpoint (writer
  timed out, script missing, tokens unreadable) still stubbed the native
  summary, silently losing the tail of the session. `_sync_checkpoint`
  now returns explicit success/failure (all four silent-fail paths return
  False and are covered by tests), a writer exiting non-zero counts as
  failure, and a context size of 0 always keeps the native summary.
- **Dream: last invisible failure path.** `create_client()` in
  `dream_report()` now sits inside the logged try/except, so an
  SDK-unavailable `RuntimeError` lands in `dream.log`/`hook-errors.log`
  like every other dream failure instead of dying only on task stdout.
- **Session-registry GC races.** GC now takes the same per-entry flock
  the writers use and re-checks staleness under it before unlinking, so
  an entry can no longer be deleted out from under a hook/hub mid
  read-merge-write. `.lock` files are removed only while their flock is
  held (lock-then-unlink); when the lock is unavailable they are left
  for a new orphan sweep that collects aged `.adopt`/`.lock` files whose
  entry is gone (non-blocking flock probe, re-check under the lock).
- **uv-guard marker fallback.** When `$CLAUDE_CONFIG_DIR` is unwritable,
  the missing-uv warning marker now degrades to `$TMPDIR` instead of
  re-warning on every hook event.

### Added
- **CLI-version canary for the summarizer-steering channel.** The
  PreCompact hook reads the CLI version from `AI_AGENT` and logs a
  warning when the major is newer than the last verified one (2.x /
  2.1.207) — the directive is still emitted (worst case is the native
  summary). Documented in README with the residual risk.
- **qmd http exposure documented where it's configured.** The
  `qmd_http_url` option description and README now state plainly that a
  `0.0.0.0`-bound daemon is an unauthenticated read-only HTTP endpoint
  over the whole indexed corpus, reachable from the LAN unless scoped,
  with a concrete macOS pf rule scoping `:8181` to the OrbStack subnet.
  The `0.0.0.0` bind instruction itself is unchanged (required for
  container→host reach under OrbStack).

### Changed
- `qmd_candidate_limit` is now capped at 50 so the HTTP timeout it
  scales (`http_timeout()`) is bounded (~38s worst case).
- `qmd_refresh.py` shell-quotes the workspace path in the remote SSH
  maintenance command.
- Test hygiene: the 90-day config-audit boundary test pins `>=` at
  exactly 90 days under a frozen clock; the config-audit skill's
  "never applies" test uses an explicit sentence whitelist instead of an
  80-char negation-proximity heuristic.

## [0.6.14] - 2026-07-16

### Added
- **Charter-based extraction targets.** The Stop-hook extractor (and
  backfill) no longer routes learnings against a bare filename list: each
  valid target now renders as `file — purpose. NOT: …`, with `purpose`
  taken from the first sentence of the file's memory-catalog summary and
  the `NOT:` note from its `anti_domains`. New shared loader
  `lib/extraction.load_target_charters()` (replaces the two drifting
  `_list_valid_targets` copies); degrades gracefully to bare names when
  the catalog is absent/unreadable. The prompt's "use the closest match"
  instruction is gone — learnings that fit no file's domain are now tagged
  `target: unknown` for downstream consolidation to reroute or filter,
  instead of being forced into the closest broadly-named file.
- **Deterministic routing-validation gate on dream proposals**
  (`lib/routing_validation.py`, pure code, no LLM). Every generated
  proposal now ends with a `## Routing Warnings` section — `(none)` when
  clean — produced by two checks: a section-registry check (H2 section
  names are unique across memory files, so an entry whose `Section:`
  lives in a different file than its target is a misroute, and a "new
  section" colliding with another file's section breaks the invariant)
  and a cross-file duplicate check (normalized 8-gram token overlap of
  each proposed insert against *all* memory files, ≥50% flags). The gate
  only warns — it never rewrites the proposal — and is wired fail-open +
  loud: a gate crash logs an exception and the proposal is written
  without the section.
- **dream-remember consults the warnings.** The skill now surfaces
  Routing Warnings at presentation time and gates application: flagged
  items are never silently applied — misrouted sections propose a reroute
  to the section's owner file, new-section collisions ask for a
  rename/reroute, and cross-file duplicates are read at the flagged
  location and skipped/merged unless the user confirms an intentional
  update.

### Changed
- **Dream routing prose is data-driven, not hardcoded.** `dream.py`'s
  proposal prompt now renders `NOT HERE:` per candidate file from the
  catalog's `anti_domains`, and the routing principles in
  `_PROPOSAL_SYSTEM` / the critic's mis-routing check (`_CRITIC_SYSTEM`)
  are genericized to work from the per-file PURPOSE/OWNS DOMAINS/NOT HERE
  blocks instead of naming specific memory files that drift. The critic
  now receives the same memory-domain blocks as the proposal pass.

## [0.6.13] - 2026-07-15

### Fixed
- **Bumped every `multiplai-core` pin to `@v0.8.1`** (across all PEP 723
  scripts and `requirements-dev.txt`). `v0.8.1` floors `claude-agent-sdk`
  to `>=0.2.116,<0.3`; the previously-pinned `v0.6.0` capped the SDK at
  `<0.2`, forcing the 0.1.x line. Against a modern Claude CLI (`>=2.x`)
  that SDK misparses the terminal result message and raises the
  deterministic `Claude Code returned an error result: success` *after* a
  full generation — the failure that silently broke `dream` (and which any
  other `[sdk]` consumer — `extract_learnings`, `backfill`,
  `context_manager`, `generate_catalog`, `checkpoint_writer`,
  `synthesize_now` — would have hit next). Restores pin consistency (the
  `test_core_pin_consistency` drift-guard was red: `dream` had moved to
  `v0.8.0` while every other script lagged at `v0.6.0`).

### Added
- **`dream` SDK-call failures are now diagnosable from persistent logs**
  (folds in the unreleased 0.6.12 work): `multiplai-core` `v0.8.1`'s
  `setup_logging(propagate_loggers=…)` attaches the dream file handler to
  the `multiplai_core` package loggers, and `dream.py` wraps the
  `_generate_proposal` call so a failed report logs an exception and exits
  non-zero instead of vanishing silently.

## [0.6.11] - 2026-07-14

### Added
- **qmd `http` execution mode.** A third `qmd_mode` (`http`) POSTs an
  authored, typed query to a resident `qmd mcp --http` daemon on the host
  instead of shelling out per prompt. The daemon keeps the embedding and
  rerank models warm in VRAM (no ~12s cold start per prompt) and does the
  fusion + rerank itself; the request is a JSON `searches` array, so the
  ssh bridge's shell-quoting and newline limits no longer apply. New
  options: `qmd_http_url` (default `http://host.docker.internal:8181`),
  `qmd_candidate_limit` (rerank latency dial, default 10), and
  `qmd_min_score` (weak-match cutoff, default 0.30, now applies to all
  modes). `qmd_strategy` is ignored in `http` mode.

### Changed
- **Authored typed queries replace raw-prompt pasting (http mode).**
  Rather than sending the user's whole sentence — which qmd's lexical arm
  ANDs (stopwords and all) at 2× RRF weight, electing junk to rank 1 — the
  lexical arm now carries only the IDF-rarest content words (document
  frequency read from the project-local `.qmd/index.sqlite`), with the
  full prompt on the vector arm and passed as `intent`. Degrades to
  stopword-filtered word order when the index is unreadable.
- **Stopword list extended** with intent/quantifier fillers (`learn`,
  `more`, `understand`, `explain` and morphological variants) that were
  leaking into the lexical keyword arm.

## [0.6.10] - 2026-07-14

### Added
- **`/multiplai-context:config-audit` — subtractive config/rules review**
  (gap B1 of the AI-coding-insights analysis). The skill enumerates the
  active config surface (`$CLAUDE_CONFIG_DIR/CLAUDE.md`, workspace
  `CLAUDE.md`s, `settings.json` env/permissions blocks, hook
  registrations, memory-file standing rules), classifies every standing
  rule as *still-serving*, *obsolete*, or *model-constraining*, and
  writes a removals-first proposal to
  `.multiplai/dreams/config-audit-YYYY-MM-DD.md` for user review. It
  never applies changes — same propose-then-review UX as dream. A new
  90-day SessionStart gate (`_config_audit_gate_open`, state file
  `config_audit_state.yaml` beside the dream state) nudges
  `/multiplai-context:config-audit` when the review falls out of
  cadence. The stamp is deterministic: the skill's final step runs
  `scripts/config_audit.py --stamp` (mirroring `dream.py --stamp`),
  which resolves the data dir via the same `get_paths()` cascade the
  gate uses — never hand-written YAML. Gate semantics: a **missing**
  state file (fresh install) is seeded with `last_run: now` and does
  NOT nudge — the 90-day clock starts at install; a stale (>=90d) or
  existing-but-corrupt state opens the gate (fail-open recovery, like
  the dream gate).

## [0.6.9] - 2026-07-14

### Added
- **Near-instant compaction via summarizer steering.** The PreCompact hook
  now prints a directive to stdout when a valid checkpoint exists — Claude
  Code appends PreCompact stdout to the compaction summarization prompt as
  custom instructions (verified in the CLI 2.1.207 binary; the background
  precompute path honors them too), so the native summarizer emits a
  one-sentence stub instead of a multi-KB summary. The checkpoint rebuild
  (SessionStart source=compact) carries the real state. Safety gates: only
  when checkpointing is enabled, the session is not a child, and
  `checkpoint.md` validates; the pending rebuild marker is written first so
  even a manual /compact below the handoff threshold gets its checkpoint
  re-injected. Any doubt → native summary (silent stdout).

## [0.6.8] - 2026-07-12

### Added
- **Hub session registry.** The lifecycle hooks now maintain a per-session
  JSON entry at `<data_dir>/sessions/<session_id>.json` implementing the
  "hub input contract" of the multiplai hub (spikelab/multiplai-gui,
  `docs/api-contract.md`): identity fields (`session_id`, `hostname`,
  `cwd`, `project`, `workspace`, `started_at`) plus a `last_event`
  stamp — `start` (SessionStart), `stop` (turn finished, session idle
  and adoptable), `notification` (waiting for user input — the hub's
  push trigger; a new Notification hook, the plugin's sixth event), and
  `end` (SessionEnd). Writes are atomic (tmp+rename), read-merge-write
  so hub-written keys survive, and best-effort throughout — the hooks
  never raise. SessionStart GCs entries whose session ended more than
  7 days ago (removing orphaned `.adopt` markers with them) and the
  data-dir `*` gitignore keeps registry files untracked by mechanism.
  Degradation: works identically with or without docker or the kit;
  with no hub installed the files are simply never read.

## [0.6.6] - 2026-07-10

### Added
- **Memory-vs-session conflict surfacing.** Every injected `=== MEMORY ===`
  block now opens with a directive requiring the model to cross-check the
  retrieved memory against the rest of the session's context (documents,
  pasted files, other injected context, user statements) and, on any
  disagreement, explicitly surface it to the user — naming the memory
  file, presenting both versions, and stating which source it follows
  (newer/in-session wins by default). Each memory file is additionally
  stamped with its last-updated date so the model has a concrete recency
  signal to judge staleness: the in-content `**Last Updated:**` header
  (maintained by the dream tooling) is preferred, falling back to
  filesystem mtime — mtime alone lies after a re-clone/checkout, which
  would stamp stale facts as fresh. Applies to both injection paths
  (router picks and the recency fallback), fused in a single renderer so
  the directive and the stamps can't ship separately. Opt out via the
  new `memory_conflict_preamble` option (default `true`; ~90 tokens per
  memory-carrying turn). Detection lives in the model rather than the
  hook because only the model sees the full session context; covered by
  unit tests plus an opt-in live-LLM E2E test (`MULTIPLAI_E2E_LLM=1`)
  that demonstrates a stale memory fact being flagged against a
  contradicting in-session document.

### Internal
- Consolidated the context_manager E2E test harness (sandbox layout,
  catalog writer, subprocess hook runner) into `tests/conftest.py` —
  three test suites carried drifting near-copies.

## [0.6.5] - 2026-07-10

### Added
- **Reviewed dream proposals are archived out of the dreams root.**
  `dream.py --stamp` takes `--archive <proposal-path>` (with
  `--archive-as applied|rejected`, default `applied`): after stamping
  dream state it moves the reviewed proposal into `dreams/applied/` or
  `dreams/rejected/`, collision-safe (`-2`/`-3` suffix instead of
  overwriting a previously archived same-name file) and via plain rename
  (`git mv` would fail on the typically-untracked fresh proposal).
  `--auto` runs self-archive their audit-trail proposal after a fully
  successful apply. Previously applied proposals sat in
  `.multiplai/dreams/` indistinguishable from pending ones.

### Changed
- **dream-remember Step 6 runs on every review, including `none`.** The
  skill now stamps with zero counts and archives the proposal as
  `rejected` when the user declines everything — a fully rejected
  proposal was previously left looking pending forever (its source
  learnings already deleted), so the next run re-presented it. Step 1
  explicitly scopes proposal discovery to the dreams root (never
  `applied/`/`rejected/`) and pins the exact proposal path for Step 6 so
  a concurrent session's newer proposal can't be archived by mistake;
  the Step 8 summary no longer claims an archive on runs where none
  happened.

## [0.6.4] - 2026-07-09

### Added
- **Config knobs now do what the README promises.** `catalog_model_diary`
  is wired: the diary catalog generator runs on its own model
  (`effective_diary_model`) instead of inheriting the generic
  `catalog_model`. `catalog_ttl_hours` is wired on the read path:
  `context_manager` emits a once-per-session advisory staleness warning
  when a loaded catalog's `generated_at` exceeds the TTL (a cheap
  timestamp compare — it never regenerates inline; run
  `/refresh-catalogs` for that).
- Drift-guard tests: the `multiplai-core` pin is now asserted consistent
  across every script's PEP 723 metadata and `requirements-dev.txt`, and
  every shipped skill is asserted to have a README command-table row.

### Changed
- **`session_state.json` writes are atomic and key-preserving.**
  `session_start` and `session_stop` now go through the atomic
  temp+rename helper and read-merge rather than overwrite, so a session
  start/stop no longer drops a concurrent session's `turn_index` /
  `recently_injected` cooldown state.
- README command table completed with the `now`, `log-doctor`, and
  `costs` skills; `marketplace.json` version realigned with `plugin.json`
  (had silently lagged at 0.6.1).

### Fixed
- **qmd SSH remote string fully sanitized.** `build_argv` now vets and
  single-quotes the workspace path (from the hook cwd) and the collection
  name (from config), not just the query — an unsafe value now yields no
  command (fail-open) instead of risking shell injection over the bridge.

### Removed
- **`catalog_reasoning_effort`** config option (and its README/schema
  entries). It was validated and documented but never consumable: the
  `multiplai-core` `ModelClient.query()` interface has no reasoning/thinking
  parameter, so honoring it would require an out-of-scope cross-repo
  change. Removed rather than left as a false promise.

### Internal
- Added `pytest-timeout` to the dev toolchain (`--timeout` is assumed by
  docstrings/reviewer commands). Defused ~12 dormant
  `pytest.skip("...does not exist yet")` guards for files that now exist —
  a renamed/removed file now fails loudly. Tidied dispatcher
  signature-introspection tests (behavioral return-value check; dropped a
  tautological assertion) and removed a misleading unused `create_client`
  import from `context_manager`.

## [0.6.3] - 2026-07-09

### Changed
- **qmd retrieval entries now carry the matching chunk's line number.**
  qmd matches chunks, not whole documents; injected resource entries
  render as `(score 0.72, line 5) Title` so the model (and the reader)
  can jump straight to the matching chunk instead of skimming the whole
  file. The `qmd-search` skill documents the chunk semantics (`line`,
  `@@` snippet context headers, best-chunk-per-file dedup, `qmd get`).

## [0.6.2] - 2026-07-09

### Fixed
- **log-doctor injection traces use the embedded prompt.** Decision traces
  now print the `prompt` key that 0.5.3 ROUTING_SCORES payloads carry, and
  the footer note claiming "prompts are not logged by context_manager" —
  stale as of 0.5.3 — only appears when the scanned lines actually predate
  the embedded prompt (and now says so).

## [0.6.1] - 2026-07-09

### Added
- **Cost ledger + `/costs` skill.** A new collector
  (`scripts/collect_costs.py` over `scripts/lib/costing_collector.py`)
  incrementally scans Claude Code session transcripts, prices every API
  call (per-model rates + cache tiers, unknown model → fallback), and
  appends to a monthly append-only JSONL ledger under `<data_dir>/costs/`.
  Span attribution follows Skill / Agent / Workflow invocations (sidechain
  subagent traffic flagged and attributed to the innermost agent span;
  nested/ambiguous spans marked `approx`). Offsets are checkpointed and
  records dedup by `msg_id`, so re-runs append nothing new.
- **`costs` skill + `scripts/costs_report.py`** — reports month-to-date
  totals and breakdowns `--by session|skill|project|model|day|component`,
  with `--session` for a per-chat itemized bill and `--json` output.
- **SDK cost tap.** SDK-driven pipelines tag their runs with a `component`
  (buildme, deep-research, dream) so their spend lands in the same ledger.
- **Automatic collection at session start.** With the new `enable_costs`
  option on, `session_start.py` fires the collector detached (like the qmd
  refresh) so the ledger stays current with no manual step. The collector
  self-guards with an flock, so racing session starts can't double-append.
  Opt-in (default off); local-only, nothing leaves the machine.

### Changed
- Pinned `multiplai-core` to **v0.6.0** plugin-wide (adds the `costing`
  module and the `component` cost-ledger tap in `run_agent`). The
  `buildme` and `deep-research` pipeline `pyproject.toml` pins moved to
  v0.6.0 to match their new `component=` call sites.

## [0.6.0] - 2026-07-08

### Added
- **qmd resources retrieval backend (`resources_retrieval=qmd`).** When
  `enable_resources` is on, resources can now be retrieved through a
  [qmd](https://github.com/tobi/qmd) hybrid index instead of the
  catalog+router path: BM25 keyword ladder + vector search fused by
  reciprocal rank (`scripts/qmd_retrieval.py`), per prompt, no LLM in the
  loop. Results render as path + excerpt entries in the existing
  `=== RESOURCES ===` section and respect the re-recommendation cooldown.
  Fail-open throughout: any qmd error/timeout/missing binary means "no
  resources this turn", never a blocked prompt. `catalog` remains the
  default — nothing changes for existing users.
- **New plugin options** — `resources_retrieval` (`catalog`|`qmd`),
  `qmd_mode` (`local`|`ssh` for container→host bridge execution),
  `qmd_ssh_host`, `qmd_collection`, `qmd_strategy`
  (`fused`|`hybrid`|`fts`).
- **Incremental index refresh at session start.** When the qmd backend is
  active, `session_start.py` fires a detached, per-workspace flock-guarded
  child (`scripts/qmd_refresh.py`): `qmd update` + `qmd embed` retry
  passes (embedding can die mid-run but is incremental).
- **`qmd-search` skill** — manual/deep searches against the same index
  (semantic + keyword + hybrid rerank), config-aware for both `local` and
  `ssh` modes. Ships `scripts/setup_qmd.sh`, the one-shot host setup
  (bun + qmd install, `qmd init`, collection add, index + embed, smoke
  query). Container setups additionally need the qmd allowlist in the
  multiplai-container SSH-bridge gateway.
## [0.5.3] - 2026-07-07

### Fixed
- **Post-cooldown relevance re-floor — weak co-picks no longer injected once
  their anchor is suppressed (injection forensics, session 351388d2).** The
  router admits everything within `KEEP_RATIO` (20%) of a corpus's top score,
  so near-floor files ride in as companions of a strong match; when the
  cooldown then suppressed that top scorer, the weak survivors were injected
  alone (e.g. life.md at 3.335 after its 10.8/9.9 anchors were suppressed —
  perceived as "it injected stuff that makes no sense"). Now, when the
  top-ranked pick is itself cooldown-suppressed, survivors must re-clear
  `POST_COOLDOWN_KEEP_RATIO` (0.5) × the suppressed top score or nothing is
  injected; drops are logged as `COOLDOWN_REFLOOR`. Design: chosen over the
  alternative of running cooldown *before* the floor/cap pick because the
  surviving weak tail then forms a fresh ranking whose top can still clear
  the absolute `MIN_SIGNAL` floor — exactly the observed failure. Behavior is
  unchanged when the top pick survives cooldown, when scores are unavailable
  (LLM router), and for unscored bundle/co_retrieve expansion picks.

### Added
- **`ROUTING_SCORES` lines carry the prompt.** Each line's JSON payload now
  includes a whitespace-collapsed, 80-char-truncated `"prompt"` key so
  score→prompt attribution no longer requires digging through session
  transcripts. Embedded in the JSON (not a trailing `key=value`) so existing
  `memory={...}$`-anchored parsers (/health, log tooling) keep working. The
  prompt is already session context per the logging standard's PII rule.
- **`ROUTING_SCORES` emitted for skills and resources corpora.** Previously
  only `memory=` was logged, leaving skill/resource injections with no score
  trail. Memory still logs unconditionally (the /health contract); skills and
  resources log whenever their corpus is enabled.

## [0.5.2] - 2026-07-07

### Added
- **`/log-doctor` skill + `scripts/log_doctor.py`.** Scans the runtime logs
  directory (`paths.logs_dir()`), clusters ERROR/WARNING/INFO entries by
  normalized signature (with traceback tails, first/last seen, counts), runs
  cross-cutting health checks (oversized append-only logs, format drift,
  missing session ids), and supports per-subsystem focus (`--subsystem`),
  recency windows (`--days`), and JSON output. The skill guides root-cause
  verification against source code before writing a fix-recommendation
  report to `INBOX/`. Read-only; the scanner has no LLM dependency.
- **log-doctor probe mode.** Exercise a functionality and assert its expected
  log entries appeared: `--probe-start` snapshots per-file byte offsets,
  `--probe-check --scenario <name>` evaluates only content appended since the
  baseline. Ships grounded scenarios (session-start/end/stop, routing,
  extract-learnings, generate-catalog, synthesize-now, backfill, dream,
  deep-research) plus ad-hoc `--expect SUBSYSTEM:LEVEL:REGEX` expectations;
  unexpected ERRORs from the involved subsystems fail the probe (exit 1).
  Entries now carry their parsed `[component]`, so errors that only surface
  in `hook-errors.log` are attributed to the right subsystem.
- **log-doctor injection forensics (`--injections`).** Reconstructs each
  context-routing decision by joining `context_manager` ROUTING_SCORES /
  COOLDOWN lines with `activity.jsonl` inject events: per-file
  picked/injected/suppressed counts with score stats, cap-hit and abstain
  rates, and `--trace N` full decision traces (`--file X` to focus on one
  file). Explains "why did it inject that" cases — e.g. cooldown suppressing
  the top scorers so near-floor files fill the slots.

## [0.5.1] - 2026-07-07

### Fixed
- **Learning extraction no longer depends on resolution luck (log-doctor F1).**
  All scripts that create a model client now declare `multiplai-core[sdk]`
  in their PEP 723 headers — uv script envs get no host-injected
  `claude-agent-sdk`, so extraction silently lost every session whenever a
  re-resolved env happened to omit it. Core pin bumped to v0.5.1 across all
  scripts (brings in the pytest log-dir guard and hook-errors.log oversize
  truncation from multiplai-core 0.5.1).
- **Diary catalog now actually regenerates during backfill (log-doctor F2).**
  The post-pass called `generate_catalog.main()`, whose `asyncio.run()`
  always raised `RuntimeError` inside backfill's running event loop — the
  "Regenerated diary catalog" branch had never executed (failure was logged
  as a non-fatal warning). Backfill now awaits `generate_catalogs()`
  directly; regression test added.
- **Tests can no longer write into real workspace logs (log-doctor F3).**
  Scripts configure logging at module import, which pytest runs during
  collection — before any fixture. conftest now pins `WORKSPACE` to a
  throwaway temp dir at import time and also scrubs
  `CLAUDE_CODE_AUTO_COMPACT_*` / `CLAUDE_AUTOCOMPACT_*` (ambient
  autocompact steering flipped checkpoint hooks into silent auto mode,
  failing 5 checkpoint tests on steered hosts).
- **Standard-format log lines carry real session ids (log-doctor F5).**
  Hook entry points re-bind `setup_logging` with the payload's session id
  instead of leaving `[session:--------]` on every WARNING+ line.

## [0.5.0] - 2026-07-07

### Added
- **Context checkpointing & rebuild (MiMo-style long-horizon support).** One
  logical chat can now span many physical context windows. The Stop hook
  measures real context footprint from the transcript tail and spawns a
  detached `checkpoint_writer.py` at token bands (default 100K/200K, tuned
  for 1M-window models) producing an incremental 11-field `checkpoint.md`;
  above the handoff threshold (200K) the checkpoint auto-refreshes every
  25K tokens, the user is advised to `/clear` via `systemMessage`, and a new
  `checkpoint_nudge.py` UserPromptSubmit hook tells Claude to wind down at a
  natural boundary. SessionStart consumes a TTL-gated pending marker and
  re-seeds the fresh session from the checkpoint. Goal-safe by construction:
  no `decision` output ever (cannot block /goal loops), child/subagent
  sessions fully excluded, writer failure never blocks the session. New
  `lib/checkpoint.py` core + config via `checkpoint_*` options; docs in
  README ("Context checkpointing"). Verified by a simulated >700K-token
  multi-rebuild E2E suite plus a live hook-subprocess smoke run.
- **Fully-automatic rebuild via steered auto-compaction.** Setting
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`/`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (README
  "Activation") makes native auto-compaction fire near the handoff threshold;
  `SessionStart(source="compact")` then injects the checkpoint into the
  compacted window — same session id, same terminal, `/goal` loops survive,
  zero user action. Same-session marker consumption is permitted only on the
  compact path; band counters reset after every rebuild so each new physical
  window re-checkpoints. In auto mode the `/clear` nudges are suppressed
  (they return only if compaction is overdue/misconfigured).
## [0.4.3] - 2026-07-06

### Changed
- **Bumped `multiplai-core` pin to `@v0.4.0`.** Picks up the library's security
  fix (the no-tools SDK client now also blocks Read/WebFetch/etc. under
  `bypassPermissions`, closing a prompt-injection exfiltration path) plus
  correctness fixes (malformed-timeout env var no longer crashes at import,
  atomic state writes, robust JSON extraction). All entry-point scripts and
  `requirements*.txt` updated.

## [0.4.2] - 2026-07-05

### Fixed
- **Every dispatcher run crashed the diary generator** — the 0.3.x
  `--only`-override feature made the dispatcher pass `force_enable` to every
  generator, but `DiaryGenerator.run()` still had the old signature
  (`TypeError: unexpected keyword argument`). Signature updated; a new
  contract test asserts every registered generator accepts the dispatcher's
  full run() contract, so future overrides can't regress this.

## [0.4.1] - 2026-07-05

### Fixed
- **refresh-catalogs skill doc contradicted the uv migration** — an Operational
  Note still told the session to invoke `generate_catalog.py` with bare
  `python` via the removed managed-venv self-routing, causing
  `ModuleNotFoundError: multiplai_core`. All skill docs now consistently
  mandate `uv run --no-project`. Also scrubbed stale `venv_dir`/`python
  dream.py` references from the health and dream skill docs.

## [0.4.0] - 2026-07-05

### Changed
- **uv + PEP 723 runtime.** The managed plugin venv (`venv_bootstrap`/`venv_guard`)
  is gone. Every entry-point script carries inline dependency metadata and runs
  via `uv run --no-project`. Shared modules (`paths`, `config`, `log_utils`,
  `model_client`) moved to the `multiplai-core` package, consumed as
  `git+https://github.com/spikelab/multiplai-core@v0.2`.

### Added
- **Installed-plugin skill discovery.** The skills catalog now also indexes
  skills shipped by installed Claude Code plugins (the themed Multiplai packs:
  pm, writing, research, dev, media) via `installed_plugins.json`, in addition
  to `skills_dir`. New `plugins_dir` option (empty = `$CLAUDE_CONFIG_DIR/plugins`).

### Changed
- **`refresh-catalogs --only <gen>` now honors its override contract.** An
  explicit `--only` filter has always been documented as running a generator
  regardless of config gating, but each generator's `run()` still re-checked
  its own `enable_*` flag and silently no-op'd — so `--only resources` did
  nothing when `enable_resources=false`. The dispatcher now threads a
  `force_enable` signal into the gated generators, so an explicitly-named
  generator runs even with its flag off (the `resources_dir` requirement is
  still enforced). This lets you keep a catalog fresh without turning on
  injection. Documented in the `refresh-catalogs` skill and README, with
  operational notes (managed-venv self-routing, `exit 1` = partial errors,
  don't `pkill -f generate_catalog`).
- **Resources catalog indexes Markdown only.** `ResourcesGenerator.discover_sources()`
  now allowlists `.md`/`.markdown` and skips dotfiles and binaries (PDFs,
  images, archives, scripts, raw `.txt`), so the routing surface is no longer
  diluted by placeholder "binary file" entries.

### Added
- **Action Items — a third dream disposition.** A learning that asks the
  toolchain to change its own code/config/file-structure ("split these files",
  "delete this orphan", "use install.sh in the Dockerfile") is no longer
  mis-filed as memory. The dream proposal now has a `## Action Items` section
  (`A{N}` numbering, What/Why/Source). On approval, `/multiplai-context:dream-remember`
  writes them to `PLANS/dream-actions-{date}.md` as unchecked tasks, so they
  survive the learnings cleanup and become durable work. A learning that
  carries a durable general *principle* alongside the change keeps **both** —
  the principle as memory and the change as an action item.
- **Bounded critic second pass over the dream proposal.** After the draft, a
  cheap second LLM pass (over the proposal only, not the raw backlog)
  surgically strips point-in-time residue (commit SHAs, `Decision (date):`
  framing, finished-task imperatives, one-off paths), demotes past-event
  records to Filtered Out, and reroutes mis-filed action items. Falls back to
  the raw draft on failure.
- **`filename:line` provenance.** Each proposal entry ends with a `**Source:**`
  line citing the learnings file and line number it was distilled from
  (pending learnings are now fed to the model with line-number prefixes so the
  citation is accurate, not guessed) — so a report is traceable on re-processing.

### Changed
- **Dream now distills generalized, reusable knowledge — not a session log.**
  The proposal prompt is built around an explicit DIARY-vs-MEMORY distinction:
  the diary already records what happened; memory holds guidance that changes a
  *future, different* task. Entries are generalized ("when X, do Y"), with the
  point-in-time scaffolding stripped. Report noise removed: no per-file
  learning counts, `seen Nx` notes, or trust labels (weak items get a
  `[warning low confidence]` marker instead).
- **`/multiplai-context:dream --auto` uses the same generalization pass as
  report mode.** Auto mode previously ran a thin per-file prompt with none of
  the above discipline. It now generates the same proposal (same prompt +
  critic), writes it to `.multiplai/dreams/` for audit, then mechanically
  applies each file's slice concurrently.
- **`router_model` option for the `llm` router** (default `claude-haiku-4-5`).
  The LLM router now forwards a model to the client; Haiku keeps the
  per-prompt classification cheap. The `UserPromptSubmit` hook timeout is
  raised to 15s (router timeout 12s) so an inline `llm` call can complete.
  See the README "Router latency" note — `llm` runs ~7-10s/prompt via the
  Agent SDK (CLI cold-start per call) and is best treated as a routing-
  quality experiment until routing moves off the blocking hook.
- **Re-recommendation cooldown.** After a file is injected, it is
  suppressed from re-injection for `recommend_cooldown_turns` turns
  (default `4`; `0` disables) — it's already in the conversation, so
  re-injecting wastes context. A turn counter and a `recently_injected`
  map persist in `data/session_state.json`; the `PreCompact` hook clears
  the map so post-compaction every file is eligible again. An
  all-suppressed turn logs `on cooldown, nothing injected` and is
  distinct from router abstention (no recency fallback). New
  `recommend_cooldown_turns` userConfig option.

### Changed
- **Activity-log `[context] inject` lines now group injected files by
  corpus** — `→ memory: … · skills: … · resources: …` instead of one
  flat comma-separated list, so you can tell which files came from which
  corpus. The JSONL mirror gains a `files_by_corpus` field.

### Fixed
- **Skills were routed but never injected.** The loader called
  `read_text()` on the skill *directory* instead of `<name>/SKILL.md`
  (the real Claude Code layout), so the skills count was always 0.
  Skills are now surfaced as lightweight recommendations (catalog
  summary + `/<name>` invocation hint) rather than full SKILL.md bodies.
- **`anti_domains` hard-excluded relevant entries across all corpora.**
  Anti phrases that reuse the entry's own positive vocabulary (e.g.
  "…unrelated to memory routing") nuked the entry on the very tokens
  that made it relevant. Anti tokens that are also the entry's
  `intent_domains` vocabulary are now dropped before the exclusion check.

## [0.3.0] - 2026-05-21

Diary layout aligned with learnings — one file per UTC day.

### Changed (breaking on-disk layout)
- **Diary now uses per-day files**, matching the learnings layout. Each
  ``YYYY-MM-DD.md`` file under your diary dir holds one ``# Diary``
  header plus one ``## Session: <id> — <ts> — <cwd>`` block per session
  that ran on that day. The previous ``YYYY-MM-DD/<sessionId>.md``
  per-session layout is gone.
- Why: easier to browse (`ls diary/` shows ~365 entries/year instead of
  thousands), consistent with learnings, append-only with `fcntl.flock`
  for concurrent SessionStart subprocesses.
- Idempotent on session_id: re-extracting the same session is a no-op.
- The diary catalog generator now iterates ``*.md`` files at the top of
  the diary directory; one catalog entry per day, same schema as before.
- Health check renamed ``diary.entry_count`` → ``diary.day_count`` to
  reflect what it actually measures.

### Migration
- No public users existed on the pre-0.3.0 layout, so no user-facing
  migration tool ships. The internal migration was a one-shot script
  applied to existing on-disk diaries during development and discarded.

## [0.2.1] - 2026-05-21

First public-marketplace-ready release. Focused on safety, transparency,
and onboarding rather than new features.

### Fixed
- **`UserPromptSubmit` hook can no longer crash your session.** The
  context-routing hook now wraps all work in a top-level guard: any
  unhandled error logs to `hook-errors.log` and emits a safe empty
  context. Previously a single failing file read could surface a
  traceback mid-prompt.

### Added
- One-time warning at session start when neither the Agent SDK nor an
  Anthropic API key is configured — so LLM-backed features (extraction,
  dreams, catalogs) silently no-op'ing is no longer a mystery.
- README: **How it works** (4-step lifecycle), **Where your data lives**
  (what gets written where + `.gitignore` snippet), **Quick start: the
  only options you probably need** (cuts the 18-option config wall down
  to 3-4 that matter).
- Platform support note in README — macOS / Linux / WSL on Windows;
  native Windows isn't supported.

### Changed
- `anthropic` dependency pinned to an exact version for reproducible
  installs.

## [0.2.0] - 2026-05-17

Internal release. Highlights for users:

- **Runtime state moved next to your memory.** Logs, catalogs, the
  plugin venv, and dream state now live at `<workspace>/.multiplai/data`
  instead of Claude Code's managed plugin dir. Side effect: routing
  catalogs are now actually loaded by default (they were silently
  unreachable before). New `data_dir` config option to override.
- **Tail-friendly logs.** Every log file rotates daily into
  `<name>-YYYY-MM-DD.log`, with `<name>.log` always pointing at today.
  Retention controlled by `MULTIPLAI_LOG_RETENTION_DAYS` (default 7).
- **`activity.log` curated stream.** One plain-language line per
  meaningful action — context injected, dream nudge, session boundary,
  diary write, learnings capture, catalog rebuild. Tail with
  `tail -f <data>/logs/activity.log`.
- Test suite hardened so it never inherits the host workspace's
  `CLAUDE_PLUGIN_*` / `WORKSPACE` env.

### Observability

- `log_utils.py` rewritten to the project logging standard: UTC ISO-8601
  lines with `[component] [session:xxxxxxxx] LEVEL:` shape, env-driven
  level (`MULTIPLAI_DEBUG=1` or `MULTIPLAI_LOG_LEVEL`), and a shared
  `hook-errors.log` for ERROR+ across all components.
- **Uniform date-rotation across every log.** `<name>.log` is always the
  *current* file; on the first write of a new UTC day it rotates to
  `<name>-YYYY-MM-DD.log` (date infix *before* the extension, per the
  standard). The stdlib `TimedRotatingFileHandler` was emitting the
  rejected `<name>.log.YYYY-MM-DD` form; legacy files in that shape are
  auto-migrated. Retention is `MULTIPLAI_LOG_RETENTION_DAYS` (default 7,
  `0` = keep forever), applied uniformly to every rotated file.
- New `log_event()` curated activity stream — one plain-language line per
  meaningful action in `activity.log` (current), mirrored to
  `activity.jsonl` for tooling, both rotating to `activity-YYYY-MM-DD.*`
  the same way. Previously these were always date-stamped with *today*,
  so there was never a stable current file to `tail -f`. Written
  regardless of log level; never raises into a hook.
- Lifecycle scripts instrumented: context inject/skip/fallback (with the
  exact files loaded), dream nudge, session start/end/pre-compact, diary
  write, learnings capture, catalog rebuild (+entry count and timing),
  deferred-extraction launch.
- README "Observability" section: live-watch command, debug toggle, log
  layout, retention.

### Fixed

- **`data_dir` is now workspace-anchored.** Previously `paths.py`
  resolved `data_dir` from `CLAUDE_PLUGIN_DATA`, so logs/catalogs/venv/
  dream-state landed in Claude Code's per-install managed dir —
  split away from `<workspace>/.multiplai/` where memory/diary/learnings
  live (and contradicting the in-code comment). Now: explicit `data_dir`
  option → `<workspace>/.multiplai/data` → `CLAUDE_PLUGIN_DATA` (only
  when no workspace) → `~/.multiplai/data`. New `data_dir` userConfig
  option. As a side effect this also resolves the router always falling
  back (the managed dir had no `catalogs/`).
- Test suite hardened: an autouse fixture scrubs ambient
  `CLAUDE_PLUGIN_*`/`WORKSPACE` so tests never inherit the host
  workspace.

## [0.1.0] - 2026-05-16

Initial public release of the **multiplai** context-manager plugin,
distributed via the `multiplai` Claude Code marketplace.

### Plugin

- `.claude-plugin/plugin.json` with `userConfig` (workspace/memory/diary/
  now/learnings dirs, sensitive `anthropic_api_key`, catalog & router
  options) and an explicit `hooks` declaration.
- Official Claude Code hooks schema (`hooks/hooks.json`): `SessionStart`
  (venv bootstrap + session init), `UserPromptSubmit` (context routing),
  `Stop` (lightweight checkpoint), `SessionEnd` and `PreCompact`
  (deferred-extraction markers).

### Core

- Path resolver with plugin-env → workspace → standalone cascade; all
  runtime state resolves through it (catalog generators included).
- Model client abstraction: Agent SDK (zero-config) with Anthropic
  API-key fallback; empty-content responses handled gracefully.
- First-run virtualenv bootstrap (`uv` preferred, `pip` fallback).
- Routed, per-prompt memory injection (`token_overlap` or `llm`
  strategy). `SessionStart` no longer dumps the full memory corpus.
- Diary-first learning extraction (brace-safe prompt construction);
  per-session diary, per-day learnings.
- Catalog generation (memory, diary, optional skills/resources) with
  content-hash incremental regeneration.
- Dream consolidation: report mode by default
  (`/multiplai-context:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai-context:setup`, `/multiplai-context:dream`, `/multiplai-context:dream-remember`,
`/multiplai-context:health`, `/multiplai-context:memory-health-audit`,
`/multiplai-context:refresh-catalogs`, `/multiplai-context:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
