# Changelog

All notable changes to the **multiplai-dev** plugin, as seen by someone
installing or updating it.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are this plugin's version in the marketplace manifest
(`.claude-plugin/marketplace.json`); a released version is tagged
`multiplai-dev@<version>`.

Recorded history starts at **0.1.1**; anything earlier is in `git log` only.

Of the 25 versions recorded here, `0.1.1` and `0.5.0`–`0.5.3` carry a git tag;
tagging then lapsed until `0.9.1`, and from there on every release is tagged
when it lands. Dates on untagged versions are the release dates recorded at the
time, not derived from a tag.

## [Unreleased]

## [0.19.0] - 2026-09-26

### Added

- **`review-viewer` opens a PR, a branch, your unpushed commits or a commit
  range, and walks you through the change.** Give it a PR number or URL,
  `owner/repo#N`, a branch name, a worktree path, `a..b` or `a...b`. A PR or
  branch is shown against its merge-base, the same commits
  `/multiplai-dev:review` would review. A worktree shows only the commits not
  yet pushed to its upstream.
- **A written walkthrough of the change.** The Claude Code session reads the
  commits and the whole diff and writes an overview and ordered reviews: what the change is for, the core change, its
  callers, tests, then config and generated files. Each review points at the
  lines it explains; clicking it jumps the code there, and `[` / `]` move
  between reviews. Reviews appear while the session is still writing them. A
  review can carry a mermaid diagram when the change alters a flow or a data
  shape.
- **Review findings load when a review of the same commits exists**, and each
  entry on the Reviews tab shows the findings in its code. A review of an older head of the same
  PR or branch is named in a banner and not loaded.
- The page header shows a PR's title, author and GitHub link.
- **The sidebar can be resized.** Drag the line between it and the code, or
  focus it and use the arrow keys; double-click resets it. The width is
  remembered in that browser.
- **Summary, Reviews and Findings tabs.** Summary shows the overview, the
  PR description, and badges for judging the change at a glance. Some badges
  are measured from git and GitHub: files and lines changed, size (small up
  to 400 lines, large past 1000 or 30 files, not counting lock files), tests
  changed against code, commit hygiene, TODOs and lock files added, and for
  a PR its checks, merge conflicts, draft and review state. The rest are
  the session's own assessments: whether the commit messages explain the
  change, and whether the new code is tested. Click a badge for its
  reasoning. Reviews holds the walkthrough steps. Clicking a file there
  opens the review that explains it, and a review's files are marked in the
  sidebar.
- **A 💡 button above every block of changed lines** asks the session to
  explain just that block. The answer appears in a strip above the block and
  is still there after a reload. Nothing is explained until you ask, so no
  tokens are spent on blocks you already understand. "Follow up" puts the
  block into the message box. These explanations stay out of the chat.
- **Questions and answers are a chat in the footer.** Collapsed, it is a
  one-line message box. A spinner beside it shows while the session is
  answering, and a dot marks answers you have not read. Clicking the box
  opens the whole conversation above it, oldest first. Each message is
  labelled with what it was about: a finding, a review, lines, or the whole
  change. Esc or clicking elsewhere collapses it. Clicking a code
  line adds its reference without opening the chat, so more lines can be
  picked; typing opens it. Answers no longer appear in the right-hand panel.
- Clicking a line or block adds a reference only once. A reference to lines
  that overlap or touch one already in the box widens it: a line and then its
  block leave one reference. The label above the box appears only when the
  message will be linked to the open review or finding.
- **The message box is a footer, always in view.** Type `@` for a list of
  the changed files, and add `:12` or `:12-20` for lines. Clicking an added,
  removed or changed line adds its whole block of changed lines as a
  reference, which you can edit. A block that only removes lines is written
  `@path:base:40-52`. The first reference with lines is sent with the
  question.
- **Unchanged lines fold away, as on GitHub.** A file opens showing its
  changes with three lines around each, plus any cited or picked lines; the
  rest folds into a bar that shows all of it, or 20 lines at a time from
  either end. Jumping to lines inside a fold opens it.
- **Scrolling past the end of a file opens the next one**, and past the top
  the previous one, in the sidebar's order; a bar at the bottom names the
  next file and can be clicked. The fling that reaches the end does not count:
  it takes a fresh push. The open file is highlighted in the sidebar and kept
  in view there.

### Changed

- For a PR target, `review-viewer` now runs `gh pr view`, and fetches the
  PR's commits from `origin` when the clone does not have them (objects only;
  no branch or remote-tracking ref is created). `--fetch` runs `git fetch
  origin` first. Other targets still make no network call; the page also
  loads mermaid from cdnjs.
- Diff colours follow GitHub's. Added lines are green and removed lines red,
  with a darker line-number cell, and removed lines are no longer struck
  through. Lines a finding cites get a blue bar in the margin and are never
  recoloured, so a cited added line stays green. Lines a review points at
  flash when you jump to them.
- The changed-files list groups files under their directory and shows each
  file by name; a long directory is shortened to `…/<last two folders>/`. Hover
  a name or a directory for its full path. The filter still matches full
  paths.

### Fixed

- **`review` and `review-viewer` no longer write into the repository you run
  them from.** Without a workspace `INBOX/`, a review went to `./reviews/`
  and a viewer's files to `./review-viewer/`, which is usually the working
  tree of the repository under review. Both now go under `~/.multiplai/`
  (`reviews/`, `review-viewer/`). `--out` and `--reviews-dir` still override
  it.
- **`review-viewer`'s three panes now scroll on their own.** On a long file the
  whole page scrolled instead, so jumping to a finding or a step's lines could
  leave them off screen.
- **The right-hand panel no longer scrolls sideways.** A wide table or a
  long code span in a PR description could push the whole panel's text out
  of view. Wide tables and images now scroll inside their own box, and long
  words wrap.
- The files no review explains are listed on the Summary tab. On the
  Reviews tab they sat between the list of reviews and the one you clicked,
  so every review looked like it said "Not explained". Clicking a review now
  scrolls its text into view.
- The Summary, Reviews and Findings tabs stay at the top of the right-hand
  panel while it scrolls.

## [0.18.0] - 2026-09-25

### Added

- **`review` reviews a branch, PR or commit range, and rejects in code any
  finding or fix it cannot ground in the reviewed commit.** Five finders look
  at the change from different angles; a fresh agent verifies each finding;
  another proposes a fix; a last one checks the fix against the code that
  consumes what it touches. Between the stages, Python re-reads every cited
  line range with `git show` and drops what is not there. A fix premise about
  what a setting means must cite a line that *uses* the setting, not the line
  that defines it; a premise about anything outside the repo is shown as
  "Assumption: … Ask: …". Output is a markdown review per target, a short
  summary per target that the session pastes into chat when the run ends (one
  line per HIGH and MEDIUM finding with its fix status, one line per dropped
  finding), HIGH / MEDIUM / LOW rollups across targets, and a `findings.json` that opens in
  `review-viewer`, where questions go to the session that ran the review.
  `post` puts the findings you accepted in the viewer into one PR comment,
  only when you say yes in the terminal. Agents get `Read`, `Grep` and `Glob`
  only, and the run stops at a per-target cost ceiling (`--max-cost-usd`,
  default 10).

## [0.17.0] - 2026-09-24

### Added

- **`review-viewer` shows a code review's findings next to the code, in the
  browser.** Point it at a `findings.json` and it opens a local page: findings
  grouped by severity, each one beside its file in full at the reviewed
  commit, with the diff marked and the cited lines shaded. A question typed
  under a finding reaches the Claude Code session that started the viewer
  within about a second, and the answer appears in the page as rendered
  markdown. Accept, reject and defer buttons record a decision per finding in
  `decisions.json`. With `serve --repo <path> --range <base>..<head>` it shows
  a plain diff and takes questions about selected lines. The page needs a
  per-start token that is never printed, and refuses requests from other web
  origins. The `findings.json` v1 contract ships as
  `skills/review-viewer/schema/findings.v1.schema.json`. Ported from the
  team's diffview tool.

## [0.16.0] - 2026-09-14

### Changed

- **The plan skill stops and asks when recon shows a mixed scope.** When the
  requested work spans mechanisms with different risk and effort profiles (a
  config change beside a protocol rewrite, a no-tools call path beside a
  tool-using harness), the planner now puts the split to the user as explicit
  choices before writing scope into the plan, instead of picking a piece or
  silently planning all of it. Pieces that can only ship together stay one
  scope.

## [0.15.0] - 2026-08-19

### Changed

- **buildme's reviews now need `--trust-repo`.** The per-block code review and
  the design and tasks audits run as subagents that can `Read`, `Grep` and
  `Glob` your repository, so they cross the same trust gate every other buildme
  agent crosses. A full `build` run already required `--trust-repo` (or
  `BUILDME_TRUST_REPO=1`); a `spec-generate` or review-only invocation did not,
  and now does. Without the flag those steps stop with the message that names
  it. The reviewer holds no `Write`, `Edit`, `Bash` or `NotebookEdit` in any
  step, and stops after 30 turns.
- **The reviewer reads the code, not just the diff.** Because it holds `Read`
  and `Grep` it can open the file a changed line calls into, and check whether
  a rule it wants to cite actually says what it thinks. A finding must now
  quote the rule and name the file it came from.
- **The reviewer reads your project's own `CLAUDE.md`.** buildme collects the
  repo-root `CLAUDE.md` plus the `CLAUDE.md` (and `CLAUDE.local.md`) in every
  directory that is an ancestor of a changed file, and hands them to the
  reviewer as the conventions the change must satisfy. A sibling directory's
  file is never included — its rules do not govern this change, and feeding
  them in manufactures violations. This is automatic and separate from
  `standards_files` in `specs/config.yaml`, which is still the way to push in a
  style guide that is not a `CLAUDE.md`.
- **The review names what a deleted line used to guarantee.** For every line
  the change removes or replaces, the reviewer states the invariant that line
  enforced and points at where it is re-established — or reports that it is
  not.
- **The tasks audit stopped second-guessing the implementer.** It flags gaps on
  the contract surface (a function signature, a file format, a wire protocol,
  an error the caller must handle) and no longer flags helper names, file
  layout or control flow inside a block. Those are the implementing agent's
  call.
- **Rubric generation falls back to twelve named code smells** — Fowler's, via
  the `mattpocock/skills` code-review checklist — when your project documents
  no standards of its own. A standard your repo does document always wins over
  the baseline.
- **The whole-change refactor pass looks for wasted work** — redundant
  computation and repeated I/O across blocks — and reports a wrong-altitude
  implementation without acting on it.
- **The board doc's In Review row now matches the protocol decided on
  2026-08-11**: In Review means a real PR into staging, for everyone, not a
  reviewer fetching a branch and diffing it. No code changed — `board.py`
  already stops at In Review.
- **The reviewer's standards block is capped per document.** Each `CLAUDE.md`
  and each `standards_files` entry is trimmed on section boundaries at 12,000
  characters, with the full section index always emitted so the reviewer (which
  holds `Read`) knows what it did not receive. Uncapped, a large conventions
  chain was rebuilt for every block and then sent to every panel member.
- **A change created before `use-cases.md` existed gets its plan rewritten.**
  Resuming such a change generates `use-cases.md`, and now regenerates
  `tasks.md` and `rubric.md` against it — otherwise the plan the build runs was
  written without the use cases it is required to cover, and the plan review
  spent its one regeneration reporting that.
- **The plan review's rewrite of `tasks.md` is re-audited.** The rewritten plan
  goes through the same vertical-slice shape audit the original does, and
  `rubric.md` is regenerated from it, so per-block review no longer scores new
  blocks against the rubric for the plan they replaced.
- **`[buildme.plan_review]` now actually follows `code_review.model` and
  `BUILDME_REVIEW_MODEL`.** The plan reviewer's model was frozen before either
  override was read, so a project that set `code_review: {model: opus}` still
  got the base model. `[buildme.plan_review] MODEL=` continues to win over both.
- **The oversized-plan check stopped reading English as file paths.** "read/write
  access" and "pass/fail gate" were counted as three top-level packages, firing
  the package-spread trigger on plans that named no path at all. A match now has
  to look like a path — an extension, a third segment, or backticks around it.
- **Blocks written as `def name(...)` no longer collapse into one group.** The
  interface graph keyed on the first token of a signature, so every such block
  looked like it produced the same thing and both split checks went silent.
- **A failed plan review is retried on a resume.** The phase pointer used to
  advance past it regardless, which meant an LLM failure skipped the review for
  the rest of the build.
- **The `many-files` split trigger is gone.** It could never fire (no caller has
  a file count before anything is built) and could not have changed the verdict
  if it had. Plan size is what the block-count and package-spread triggers
  answer.

### Added

- **A plan review runs between the design audit and the prototype.** A
  read-only reviewer reads `tasks.md` against `rubric.md`, the project's
  constraints and the use cases, and reports under six headings:
  `rubric-conflict`, `block-contradiction`, `constraint-violation`,
  `over-prescription`, `use-case-coverage`, `oversized-plan`. Critical and
  major findings drive **one** regeneration of `tasks.md`, then the review
  re-runs report-only and the plan stands. It prints
  `PHASE:PLAN_REVIEW:COMPLETE` and keeps the card in the Planning column. An
  `oversized-plan` finding *proposes* a cut and names the signature boundary it
  crosses; nothing here ever splits a plan, creates a ticket or moves a card.
  A failed plan review never fails the build.
- **`use-cases.md`, a new spec artifact** between `proposal.md` and `tasks.md`.
  It carries `## Personas` (who they are, the job they are doing, the
  constraint they are under) and `## Use cases` (`<persona> wants <goal> so
  that <observable outcome>`), and `tasks.md` now requires it alongside
  requirements, design and unknowns — so the work breakdown is written with the
  use cases already on disk, and the plan review can check that every use case
  is delivered by some block. `proposal.md` keeps Why / What Changes /
  Capabilities / Impact, with What Changes now stating Goals and Non-Goals
  explicitly; `design.md` dropped its `## Goals / Non-Goals` section, which
  restated them. This is a third artifact rather than two more proposal
  sections, decided on 2026-08-19: it diverges from the written plan, which
  called for putting personas and use cases inside `proposal.md`.
- **Per-step model and effort, set from `multiplai.conf`.** Three sections take
  effect today: `[buildme.review] MODEL=` picks the reviewer's model, and
  `[buildme.plan_review] MODEL=` / `EFFORT=` tune the plan review — its effort
  inherits `[buildme.review]` rather than the pipeline-wide value, so raising
  the reviewer raises both. Two more resolve and are readable on the config
  object but **no call site consumes them yet**: `[buildme.spec] MODEL=` and
  `[buildme.agent] MODEL=` change nothing today. `specs/config.yaml` still
  outranks `multiplai.conf`, which is a machine-wide default.
- **Two knobs for the oversized-plan check**, both under
  `[buildme.plan_review]`: `PLAN_SPLIT_BLOCK_TRIGGER` (default 8) and
  `PLAN_SPLIT_PACKAGE_TRIGGER` (default 3). The defaults are placeholders — no
  run archive has been measured — which is why they are knobs and not
  constants.
- **Part of the reviewer's prompt is borrowed, and says so.** Four blocks come
  from `anthropics/claude-plugins-official`'s `pr-review-toolkit` under
  Apache-2.0. They live under `build_pipeline/prompts/vendored/` with the
  upstream `LICENSE` beside them, each pinned by blob SHA, each header naming
  its source path and the fact that it was modified.
  `scripts/check_vendored_prompts.py` re-fetches every pin and reports drift;
  run it before a release.
- **`deepen` records where it left upstream.** It was re-synced against
  `mattpocock/skills` at `885e2ca` (2026-08-19) and now carries a "Divergence
  from upstream" section naming that commit and mapping every file to its
  upstream path.

### Removed

- **The security review step is gone from the pipeline.** The unwired
  `run_security_review()` function, its prompt and the `security_review` config
  toggle are deleted. Security review is out of scope for buildme — use Claude
  Code's built-in `/security-review` on the branch instead.

## [0.14.1] - 2026-08-17

### Fixed

- **`skill-creator`'s copy of the degradation contract no longer names a
  directory that was retired.** Its output-location rule told you never to
  write to `INBOX/`, `PLANS/`, or a workspace directory relative to the session
  cwd; `PLANS/` was retired on 2026-08-11. A skill author following the rule
  was being warned off a directory that does not exist. The rule itself is
  unchanged.

## [0.14.0] - 2026-08-14

### Removed

- **The `swift-build` skill moved to the new `multiplai-apple` plugin**,
  unchanged. It is mac-only capability — the Swift/Xcode toolchain, and from
  the multiplai-kit container the opt-in host SSH bridge — and multiplai-dev
  is a cross-platform pack, so the mac-only skill now ships as an explicit
  add-on. If you use `swift-build`, run
  `/plugin install multiplai-apple@multiplai` to keep it.

## [0.13.0] - 2026-08-13

### Removed

- **The `code-review` and `security-review` skills.** Claude Code ships
  built-ins under the same slash names, so both collided with them; typing
  `/code-review` or `/security-review` now resolves to exactly one skill —
  the built-in. The six language review checklists from
  `code-review/references/` live on in multiplai-kit under
  `dotfiles/reference/review/`. The two `security-review` references
  (`owasp-checklist.md`, `language-security.md`) are retired with the skill.

## [0.12.0] - 2026-08-06

### Fixed

- **buildme's agents can no longer reach a tool nobody remembered to deny.**
  Every buildme LLM call denies the tools it did not explicitly ask for, but
  the list it computed that denial from named ~18 tools out of the ~44 the CLI
  now ships. Everything unnamed stayed available — and auto-approved, because
  these calls run with permissions bypassed. That included a second
  code-execution tool (`REPL`), two ways to send text off the machine
  (`Artifact`, `SendMessage`), and the whole background/scheduled-execution
  surface (`TaskCreate`, `Workflow`, `CronCreate`, …), which is what a denied
  `Bash` is worth nothing against. The list is re-derived from the CLI's own
  schema, and its test now names the tools that carry the risk instead of
  comparing the list to itself.
- **A crash after the TDD build no longer re-runs the whole build.** The TDD
  engine deleted the build's checkpoint on success even when it was running as
  one phase of `buildme` rather than on its own. Everything after it — the
  documentation pass, the respec proposal, the push and PR — then had nothing
  to resume from: a crash there restarted at `tdd_build` with no block state
  and rebuilt every block from scratch. A crash in the moment between marking
  the build complete and deleting the checkpoint was worse still: the resume
  read "complete" and skipped the remaining phases silently, so the docs, the
  respec proposal and the PR never happened. Resuming a build now picks up
  where it stopped.
- **A crash while the checkpoint is being written can no longer corrupt it.**
  `.build-state.json` was written in place, so an interruption mid-write left a
  half-written file that no resume can read — and it is the only record a build
  has. It is now written to a temp file and renamed into place, so a resume
  always finds either the previous checkpoint or the new one.

### Changed

- **`python -m build_pipeline` exposes only the subcommands it documents.** The
  `migrate` (legacy `openspec/` layout) and `apply` (single-agent, non-TDD)
  subcommands are gone — both were undocumented, unreachable from the
  `/buildme` skill, and untouched by the last year of pipeline work. If you
  scripted either of them directly, use `build` / `spec-generate` / `tdd`
  instead.

## [0.11.0] - 2026-08-05

### Fixed

- **buildme loads the Django and React standards again.** Its stack map named
  `django-best-practices.md` and `react-best-practices.md`; the docs
  multiplai-kit ships are `django-drf-best-practices.md` and
  `react-nextjs-best-practices.md`. A name with no file on disk is skipped with
  only an INFO line, so for a month every Django and React build generated its
  specs with **no conventions to build to** while looking like it had them. If
  you have a `reference_docs:` override in `specs/config.yaml` naming the old
  filenames, update it too — an override replaces the built-in list.
- **A long reference doc no longer arrives cut mid-sentence.** The per-doc
  ceiling for inlining into spec-generation prompts was 8000 chars, set when the
  docs were ~20k; the Django doc is 61k, so 87% of it was dropped and the
  surviving 13% ended in the middle of a rule — which reads as a complete rule.
  The ceiling is now 24000 and overflow is reduced on section boundaries: whole
  sections, plus an index of *every* section in the doc so the generator knows
  what it did not receive and the TDD phases (which hold `Read`) can go get it.

### Added

- **FastAPI and Next.js projects resolve their standards.** A `fastapi`
  dependency now adds `fastapi-best-practices.md`, and a project that lists only
  `next` (with React arriving transitively) counts as React instead of resolving
  no frontend standards at all.

## [0.10.3] - 2026-08-05

### Fixed

- **The promotion gate no longer reports a cold dependency cache as a hung
  script.** Every command got the same 30-second budget, and the first
  `uv run` of a run has to build the environment first — resolve the lock,
  clone the git-sourced `multiplai-core` — which regularly exceeded it. The
  gate said `timed out`, which reads as a defect in the skill and was not one;
  about 40% of runs in one batch failed this way. The first uv-mediated command
  now gets a separate, generous budget for that one-off cost. Everything after
  it is still held to 30 seconds, so a script that genuinely hangs still fails,
  and now fails distinguishably — the message names the budget it blew.

## [0.10.2] - 2026-08-05

### Security

- **buildme no longer installs `cryptography` 49.0.0 (CVE-2026-69247, high).**
  If you have buildme installed, updating to 0.10.2 is the fix — the next run
  resolves `cryptography` 50.0.0. There is nothing to change in your own
  config.

  What went wrong: `skills/buildme/scripts/` shipped its own `uv.lock`, left
  behind when this repo consolidated onto a single workspace lock. After that
  consolidation nothing could regenerate it — `uv lock` run from inside the
  skill resolves the whole workspace and rewrites the *root* lock, never the
  nested one — so it stayed frozen on the day's versions while the root lock
  moved on. In this repo that was invisible, because in-repo runs use the root
  lock. On *your* machine there is no workspace above the plugin, so
  `uv run --project` found the frozen lock and used it. Dependabot filed the
  advisory correctly and opened patches; those patches edited the nested lock,
  which is the one file that could not be updated.

  The nested lock is now deleted, so an installed buildme resolves against the
  declared dependency ranges and picks up patched versions on its own. A new
  repo gate (`lint_workspace.py` → nested lockfiles) fails CI if a lockfile
  ever reappears outside the workspace root.

## [0.10.1] - 2026-08-04

### Fixed

- **`buildme`'s pipeline resolves on an installed copy of the plugin.** Its
  `scripts/pyproject.toml` now declares its own git source for
  `multiplai-core`, so `uv run --project <skill>/scripts` works standalone —
  an install is a copy of the plugin subtree only, with no marketplace
  workspace above it to resolve through.

### Added
- **A plugin README** (`plugins/multiplai-dev/README.md`) — what the pack
  contains, what each skill needs, and how it degrades without the kit.

## [0.10.0] - 2026-08-04

### Fixed
- **The skill promotion gate stopped working on skills in a uv workspace.**
  `promote_skill.py` decided how to run a bundled script by looking for a PEP
  723 header: found one, ran it under `uv run --script`; found none, fell back
  to bare `python3`. For a skill whose dependencies live in a `pyproject.toml`
  instead, that fallback meant every script died on `ModuleNotFoundError`
  before argument parsing, and the gate reported the *skill* as broken.

  It now looks upward for a uv workspace root and runs through it. The PEP 723
  path is unchanged, so single-file skills from elsewhere still work.

### Changed
- **`buildme` and `deepen` take their dependencies from the repo-root uv
  workspace.** Their `scripts/uv.lock` files are gone, replaced by one lock at
  the root. Run them with `uv run --project <repo-root>`; running from inside
  the script directory no longer resolves a project of its own.

## [0.9.1] - 2026-08-04

### Fixed
- **`/buildme` now gets its Claude Agent SDK version floor from
  `multiplai-core`, instead of stating its own.** Nothing was broken for you:
  buildme's hand-written pin and core's floor happened to be the same
  (`>=0.2.116,<0.3`), so the right SDK was installed either way.

  It was a latent version of a bug that already bit `deep-research`, which
  declared the SDK the same way and resolved 0.1.56 — the release that
  misparses the terminal message from modern Claude CLIs and fails a finished
  run with `Claude Code returned an error result: success`. Because every
  buildme LLM call goes through `multiplai_core.run_agent()`, core is the only
  place that floor is meaningfully known; a second copy in buildme's own
  manifest could only ever go stale, and would have kept installing an old SDK
  the next time core raised its floor. Buildme now depends on
  `multiplai-core[sdk]` and names no SDK version of its own.

## [0.9.0] - 2026-08-04

### Added

- **`/buildme` now updates your documentation as part of the build, so it
  arrives in the same pull request as the code.** After the TDD build and
  before the respec proposal, a new documentation phase reads the whole build
  diff plus the implementation notes, takes inventory of the documents your
  project actually keeps (`README*`, `CHANGELOG*`, `docs/**`), and updates
  whatever the change made stale — described behavior, flags, defaults, file
  layouts, and usage examples that would no longer work. Where your project
  keeps a changelog it adds a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
  entry written from the user's point of view; it creates a `CHANGELOG.md` only
  when your project's conventions show one is expected. Previously a build
  shipped code and left the docs for whoever reviewed the PR.

  What it writes lands in its own `docs(<change>): update documentation` commit,
  and the files are named in the PR body under **Documentation** so a reviewer
  knows the docs were rewritten rather than assuming they were untouched.

  The phase is **always on** — there is no flag and no `specs/config.yaml`
  toggle — and it is **non-fatal**: if the model call fails, the build still
  completes with the code it produced. It also does not pad. "Nothing needed
  documenting" is a legitimate outcome for a change with no user-visible delta,
  and the agent says so explicitly.

- **A new `DOCS_WARNING:` line when the documentation may be stale.** When the
  build changed source files, your project keeps a changelog, and the phase
  updated nothing, `/buildme` prints a warning naming the changed files and the
  changelog to check before merging. It is a **warning and never a failure** —
  a finished build is not failed over a judgment call about documentation.

- **A second `DOCS_WARNING:` for anything the documentation phase changed but
  did not report.** The agent can edit any file in the project, but only the
  files it *names* are committed — so an unreported edit (a source file touched
  by accident, a document it forgot to list) would otherwise sit in the working
  tree, absent from the PR and from the diff you review. `/buildme` now names
  those files instead. They are still not committed: the documentation commit
  stays limited to explicitly-named paths, which is what makes it safe to build
  from an agent's own report.

### Changed

- **`PHASE:DOCS_UPDATE:COMPLETE` joins the progress protocol**, and the kanban
  card stays in **In Development** through the documentation phase (updating the
  docs is part of producing the change, not a separate review stage).
- Builds resumed from a checkpoint written by an earlier version load and run
  the new phase; a build already past the respec proposal does not go back for
  it.
- The staleness warning no longer treats *code* that happens to live under a
  `docs/` directory as documentation. A build that only changed
  `docs/generator.py` used to report "nothing to document"; a file under
  `docs/` now has to read like a document (`.md`, `.rst`, `.txt`, …) to count
  as one.

## [0.8.0] - 2026-08-04

### Added

- **`/buildme` now refactors every block, on every tier — and proves it was
  safe.** The refactor step used to be off entirely on the advanced tier and
  unverified on the standard one, so the TDD loop effectively stopped at
  "the tests pass". It now runs after every block on both tiers, and the
  pipeline checks the result rather than taking the agent's word for it: the
  suite is re-run and every test file is re-hashed. If the suite is not green,
  or a single test file changed, the whole refactor is thrown away — the block
  is reset to its `impl(block-N)` commit and the build carries on with the code
  that was already green. A refactor can improve your block or do nothing to
  it; it can no longer break one.
- **A refactor that survives verification is committed**, as
  `refactor(block-N): <name>`, so you can see it in the branch history and
  review it separately from the implementation. Previously the refactor's edits
  were left uncommitted and got folded silently into the next commit.
- **One conservative whole-change refactor pass runs after the last block**,
  before the final review, over the cumulative diff. It exists to catch what
  is invisible while you are building block by block: the same helper written
  twice in two blocks, code nothing calls any more, a wrapper that only
  forwards, one concept named three ways. Its charter stops there — module
  boundaries and public signatures stay as designed, and behavior is preserved.
  It lands as `refactor: simplify across blocks`, carries the same
  suite-and-tests verification (a failure reverts the whole pass), is checked
  against your token budget like any block boundary, and is recorded in the
  checkpoint so resuming a build does not pay for it twice.

### Changed

- **The implementer prompt no longer tells the advanced tier "there is no
  refactor phase".** It says a conservative refactor pass follows, and still
  asks for production-quality code the first time — the later pass tidies, it
  does not rescue.
- **`[buildme.agent]` in `multiplai.conf` also tunes the whole-change refactor
  pass**, alongside the test writer, implementer, per-block refactorer and fix
  agents.

## [0.7.0] - 2026-08-04

### Added

- **`/buildme` now reads your codebase before it writes a spec.** A new
  **Codebase Analysis** phase runs between research and spec generation: three
  agents explore the repo in parallel (module structure and entry points;
  naming, error-handling and test patterns; dependencies, config loading and
  integration points) and write what they find to
  `specs/changes/<name>/codebase-analysis.md`. That report is fed into
  `design.md`, so the design extends the modules you already have instead of
  proposing a parallel structure beside them. At the review checkpoint the
  pipeline prints `REVIEW:CODEBASE_ANALYSIS:<path>` — read it before
  `design.md`, because it is where "why does it extend that module" is
  answered.

  The phase is skipped, with the reason logged, for a project that has no
  source files yet and for `--only` runs (which generate no specs), and a
  failure is never a build failure — the design falls back to treating the
  project as new.

- **Reference docs now reach spec generation, not just the TDD agents.** The
  best-practice docs under `$CLAUDE_CONFIG_DIR/reference/dev/` for your stack
  are inlined into the design and task-breakdown prompts (capped at 8,000
  characters each), so the plan is written to your conventions rather than
  corrected to them afterwards. Every run prints one `REFERENCES:<doc names>`
  line (or `REFERENCES:(none)`) naming exactly what reached the generator.

- **Frameworks are detected, not guessed from the manifest name.** A `manage.py`
  or a `django` dependency in `pyproject.toml`/`requirements.txt` adds the
  Django reference docs on top of the Python ones; `react` in `package.json`
  adds the React docs on top of the Node ones. Previously every Python project
  got the same two docs regardless of what it actually was.

- **`reference_docs:` in `specs/config.yaml`** lets you say which docs a project
  builds to. Keys are the detected stack (`pyproject`, `Package`, `package`,
  `Cargo`, `go`) or framework (`django`, `react`), and a key you set replaces
  the built-in list for that key alone:

  ```yaml
  reference_docs:
    pyproject: [uv-python-best-practices.md, our-house-style.md]
  ```

### Changed

- A reference doc named in the mapping but missing from disk is still skipped
  rather than failing the run — but the skip is now logged, and a project whose
  stack is detected while *none* of its docs resolve gets a loud warning
  instead of silently generating specs with no conventions attached.

### Fixed

- The prototype-feedback pass, which regenerates `design.md` and `tasks.md`
  once from the prototype's notes, now carries the same codebase analysis and
  reference docs the first pass had. Without that, a regeneration quietly
  rewrote the design away from the project's conventions.

## [0.6.0] - 2026-08-03

### Changed

- **`/buildme`'s design audit now fixes the specs it criticises, instead of
  printing the criticism and building anyway.** Until now the audit ran after
  spec generation, listed the gaps it found — a spec scenario with no task
  block, a design decision contradicting a requirement — and the build carried
  on against the same unchanged documents. You would find the warnings in the
  log after the fact, next to code built on top of them.

  Every gap the audit rates **critical** or **major** is now handed back to the
  generator, which rewrites `design.md` and then `tasks.md` once with those
  findings in front of it. The rewrite lands as its own commit
  (`docs(specs): regenerate design.md and tasks.md after design audit`), so
  the branch history shows what the critique changed, and `git diff` on that
  commit is a readable summary of it.

  The audit then runs one more time, **report-only**, and says in the log
  whether the critique landed:

  ```
  PHASE: design_audit_feedback_applied — 2 artifact(s)
  PHASE: design_audit_recheck — clean
  ```

  It is deliberately one pass, not a loop. If gaps remain, they are reported
  and the documents stand — a stubborn gap costs one extra audit call, not an
  open-ended spec-generation bill, and the review checkpoint is still where you
  decide. Minor gaps are reported only; they never trigger a rewrite.

- **The design audit also judges the plan, not just its consistency.** It
  previously cross-referenced the artifacts against each other. It now
  additionally reports: abstractions no requirement asks for (the factory with
  one implementation, the config knob nothing reads), task blocks mis-sized for
  review, design decisions with nothing a test could assert on, and edge cases
  no scenario covers — missing input, a dependency timing out, a retry after
  partial completion. Combined with the change above, this is what turns those
  observations into an actually different plan.

### Fixed

- **A resumed build no longer loses the spec-generation checkpoint.** The
  orchestrator kept an in-memory copy of the build state while spec generation
  wrote its own to disk, then overwrote the disk copy — discarding the record
  of which artifacts and which audits had completed. A resumed build could
  therefore re-run work that had already succeeded. The orchestrator now
  re-reads the state after spec generation, the same way it already did after
  the TDD phase.

- **The build no longer pays for a design audit whose answer it already has.**
  A full build reaches the audit stage twice — once inside spec generation,
  once from the orchestrator's own `DESIGN_AUDIT` phase — and the second
  arrival used to re-read all four artifacts through the model just to
  rediscover that the regeneration had already happened. It now checks the
  checkpoint *before* calling the model, so the second arrival is free. On a
  build whose audit found nothing actionable, this removes a whole audit call
  that previously produced only a log line.

- **The phase pointer can no longer move backwards after spec generation.**
  Re-reading the state (above) also picked up a pointer already sitting at
  `design_audit`, which the orchestrator then reset to `spec_generation`. The
  advance is now guarded, so an interrupted build resumes at the phase it
  actually reached rather than at one it had already finished.

## [0.5.4] - 2026-07-30

### Security

- **buildme's locked dependencies carried 21 known vulnerabilities (9 high);
  all are now patched.** If you run `/buildme`, the pipeline resolves its
  environment from `skills/buildme/scripts/uv.lock`, so these were the
  versions actually executing on your machine. Update the pack to pick up the
  fix — there is nothing else for you to do.

  Every advisory was in a *transitive* dependency, reached through
  `claude-agent-sdk → mcp` and its sub-tree; no dependency buildme declares was
  itself affected. The lock had simply gone a long time without regeneration.

  Worth knowing when judging urgency: most of these advisories describe
  **server-side** attack surface — MCP's HTTP and WebSocket transports,
  Starlette request handling, multipart parsing of untrusted uploads. buildme
  is a local CLI that acts as an MCP *client* and never binds a socket, so
  that surface was not reachable in normal use. The two with local relevance
  were a vulnerable OpenSSL bundled in `cryptography`'s wheels and a PyJWT
  algorithm-confusion flaw (CVE-2026-48526). Patch promptly; no emergency.

  Upgraded: `click` 8.3.2→8.4.2, `cryptography` 46.0.7→49.0.0, `idna`
  3.11→3.18, `mcp` 1.27.0→1.29.0, `pydantic-settings` 2.13.1→2.14.2, `pyjwt`
  2.12.1→2.13.0, `python-multipart` 0.0.26→0.0.32, `starlette` 1.0.0→1.3.1,
  `sse-starlette` 3.3.4→3.4.6, `uvicorn` 0.44.0→0.52.0.

  Deliberately *not* upgraded in the same change: `anthropic`,
  `claude-agent-sdk`, `pydantic` and `pytest`. A blanket refresh resolves
  cleanly but moves the pipeline's own runtime, which is a separate risk from
  a security patch and should be reviewable separately. No behaviour change
  and no API change in this release.

## [0.5.3] - 2026-07-27

### Changed
- **buildme validates effort names against `multiplai-core`'s table instead of
  a copy of it.** 0.5.2 moved the effort *logic* into core's `pick_effort` but
  left the list of valid names hand-mirrored in `build_pipeline/config.py`.
  Core v0.11.0 exports that table, so the copy is gone. No behaviour change
  today — the two lists agreed — but the drift this removes had a nasty shape:
  had they ever disagreed, a `[buildme] EFFORT=<name>` that buildme thought
  valid and core did not would have been resolved to `high` instead of falling
  back to your default, with no warning. Nothing to do on your side.
- **Pin: `multiplai-core` v0.9.0 → v0.11.0** (`pyproject.toml` + `uv.lock`) for
  the exported table. buildme is the only skill in this pack that pins core.

## [0.5.2] - 2026-07-26

### Changed
- **buildme: conf-file effort overrides now delegate to `multiplai-core`'s
  `pick_effort`** instead of a local duplicate, and the pipeline's core pin
  moves `v0.7.0` → `v0.9.0` (additive releases only; the vendored `uv.lock`
  moves with it). One behavior change: a `MULTIPLAI_EFFORT` *global in
  `multiplai.conf`* now caps `[buildme] EFFORT=` overrides the same way the
  `MULTIPLAI_EFFORT` env var always did — the two ceilings no longer disagree.
  Defaults are untouched: with no conf `EFFORT=`, effort stays unset and the
  SDK decides, uncapped, exactly as before.

## [0.5.1] - 2026-07-26

### Added
- **swift-build: `uitest` + `screenshots` verbs** for GUI self-verification
  (#55) — run XCUITest bundles and capture app screenshots from the container,
  with rm-rf guards on result-bundle and screenshot output paths.

### Fixed
- **buildme: explainer detector precision** (#74) — dotted module paths
  collapse to their declared distribution prefix, and builtins/stdlib members
  no longer surface as "new dependencies".

## [0.5.0] - 2026-07-26

Verification overhaul for buildme. The theme: reviewers **propose** and the
orchestrator **disposes**; the tests that gate a block must still be the tests
that gated it; and spend is bounded by money, not just by iteration counts.

The same release carries the skill-engineering gate (#61) — a proposed skill is
executed before it is installed — and the model × effort config axis for buildme
(#59).

### Added
- **Test-integrity gate** (`gates.unchanged_tests_gate`). The implementer's
  tool grant is per-tool, not per-path, so test files stay writable for the
  whole implement phase — and the review-fix agent is the same implementer, so
  every fix iteration re-opens the window. Buildme now sha256-hashes the
  block's test files when the RED gate passes and re-checks them at both
  windows. A silent change fails the block. `TEST CHANGE REQUIRED: <reason>`
  in the implementer's report downgrades it to a flag, re-baselines the
  hashes, and hands the reason to the reviewer as an unverified claim.
- **Structured findings + orchestrator adjudication.** Reviews emit discrete
  `ReviewFinding` claims (claim, severity, confidence, evidence, location).
  `_adjudicate_review_findings` re-judges each one on the main model with the
  full build context that a fresh-context reviewer structurally lacks;
  rejected findings are recorded and never reach a fix agent. Fails open — an
  adjudicator error keeps every finding. `code_review.adjudicate: false`
  **drops** findings rather than applying them blind.
- **Reviewer panel** (`code_review.panel`). Each member reviews the diff in
  its own fresh context, concurrently, and the results merge: dimension scores
  average with confidence scaled down by panel disagreement; identical
  findings combine confidence (noisy-or) and keep the harshest severity
  anyone assigned; spec verdicts are unioned, not intersected.
- **Graded review gate** (`code_review.gate` → `ReviewGatePolicy`). Dimension
  scores are discounted toward neutral by reviewer confidence, so an unsure
  reviewer moves the needle less. Spec compliance stays a hard floor. The
  3.5/1.0 thresholds now live in one place instead of being duplicated
  between `review_score_gate` and `ReviewResult.passed`.
- **Trajectory judgment in the final review.** The final reviewer receives a
  per-block history (review iterations, final weighted scores, rejected
  findings, declared test changes) and is asked to judge drift, scope creep,
  erosion, and test gaming across the whole build — the failures that are
  invisible to per-step review because they are spread thin.
- **Per-build budget** (`budget.max_tokens` / `budget.max_usd`). Every other
  loop bound is an iteration count, which does not bound spend. Spend is
  recorded per phase on every SDK call, checked at block boundaries, warns
  once at 80%, and stops with a breakdown of where the tokens went. It rides
  in `.build-state.json`, so a resumed build does not get a fresh budget.

- **Promotion gate for `/propose-skill` — run the draft, don't vouch for it**
  (`skill-creator/scripts/promote_skill.py`, #61). An approved skill is now
  written to a **draft** directory (`/tmp/skill-draft/<name>/`) and installed
  only after the gate passes. The gate checks frontmatter (required keys, known
  `model`/`effort` values — a typo there is silently ignored at runtime, so the
  skill quietly runs on the wrong tier) and **executes every bundled entry point
  with `--help`, expecting exit 0**. Until this existed, the first person to
  discover a bad import or a raising `--help` was whoever hit it mid-task, days
  later, with the authoring context gone.
  - `--contract` mode runs a skill's `CONTRACT.md` assertions: each case is a
    shell command plus a substring that must appear in its output. Pilots ship
    with multiplai-context's `costs` and `log-doctor`.
  - `quick_validate.py`, `init_skill.py` and `package_skill.py` updated to the
    same frontmatter rules; bundled `.sh` entry points across the marketplace
    now answer `--help` with exit 0 because the gate executes them.
- **`/propose-skill --from-session` — draft from a real trajectory** (#61).
  Diary entries and learnings are *summaries*: they record that a workflow
  happened, not the commands, flag values and dead ends it went through, so a
  skill drafted from them reads plausibly and fails on first use. This mode reads
  the raw transcript and splits what it finds into **stable steps** (literal
  commands) and **judgment points** (criteria for deciding, never a hardcoded
  answer). A step counts as stable only if it was seen done the same way for a
  *different* input — one occurrence is an anecdote. Dead ends are recorded too:
  the command that failed, and why, cannot be reconstructed from the successful
  path.
- **Model × effort as two config axes for buildme** (`config.py`, #59). Both are
  set from `multiplai.conf` with no code edit: `[buildme]` (`MODEL=`, `EFFORT=`)
  for the whole pipeline, plus `[buildme.spec]`, `[buildme.review]` and
  `[buildme.agent]` for per-step `EFFORT=`. A step section falls back to
  `[buildme]`, which falls back to the SDK default. The `MULTIPLAI_MODEL` /
  `MULTIPLAI_EFFORT` ceilings still cap the result, so a conf override cannot
  escape a budget run.
- **Model-upgrade re-test checklist** (`e2e-test/SKILL.md`, #61). Skills are
  prompts, and a prompt tuned against one model is not automatically correct
  against the next — a bump can change output format, verbosity or how literally
  an instruction is followed, none of which raises an error. Run on any
  `MULTIPLAI_MODEL` ceiling change, `model:`/`effort:` frontmatter change, or
  Claude Code default-model move: smoke-invoke every script, run the `CONTRACT.md`
  assertions, re-check the frontmatter tier against the cost report.

### Changed
- `ReviewScore` gained `confidence` (default 1.0) and `effective_score()`.
  Confidence shrinks a score *toward neutral* rather than scaling it down —
  scaling would make an unsure reviewer look harsher, inverting the meaning of
  low confidence. Existing fixtures at the default confidence score exactly as
  before.
- All `llm_call`/`agent_call` sites pass a `budget_label`, so a budget stop can
  name the phase that spent the money.

### Fixed
- **Test-integrity claims are scoped to one window.** `block.implementer_report`
  accumulates every agent's output, so a `TEST CHANGE REQUIRED:` declared during
  the implement phase kept passing the claim parse at every later review-fix
  iteration — one declared change authorized every later silent one, defeating
  the re-baseline it sits next to. Each window now sees only its own agent's
  report.
- **An unavailable test-file list no longer fails the block.** `git` failing
  (index.lock contention, timeout on a large repo) made every snapshotted file
  read as deleted and accused the agent of deleting the suite. The snapshot now
  distinguishes "listed nothing" from "could not list" and reports the gate as
  not-checked.
- **The integrity gate covers non-Python projects.** The path pattern was
  Python-only, so on a Swift/Go/TS repo nothing matched, the snapshot was empty,
  and the gate reported "not checked" for every block. Hashing now uses a
  language-agnostic pattern; the `def test_*` weak-test scan keeps the
  Python-only one.
- **A failing reviewer panel member no longer fails the block.** The panel
  gathered without `return_exceptions`, so N members meant N× the chance that
  one unreachable backend marked the block FAILED. Failed members are dropped
  with a warning and only an all-members-failed panel raises.
- **Failed `llm_call`s are charged to the budget.** Only `agent_call` recorded
  partial usage on failure, so a review that timed out after a 150k-char prompt
  spent real money invisibly.
- `ReviewResult.passed_with(policy)` added; the bare `passed` property is
  documented as the default-policy view and `run_code_review` now logs the
  configured verdict. With `code_review.gate` set, the logged verdict and the
  gate's decision no longer disagree.
- **`EFFORT=xhigh` is accepted.** `config.KNOWN_EFFORTS` mirrors core's private
  `_EFFORT_TIERS` and omitted `xhigh`, which sits between `high` and `max` — so a
  valid SDK effort was rejected with a warning and silently downgraded to the
  default. (Core's own table carries a comment warning against exactly this
  omission when the set is copied.)
- `_merge_scores` rounds half-up explicitly (bare `round()` is banker's
  rounding, so a 2/3 split went to 2 and a 3/4 split to 4) and discounts a
  dimension only one panel member scored — zero spread otherwise read as
  unanimous agreement.

### Notes
- Every new behavior is opt-in via `specs/config.yaml`. With no `panel`, no
  `gate`, and no `budget:` section, the pipeline behaves as it did in 0.4.0.

## [0.4.0] - 2026-07-20

Ports enforcement mechanisms from the `superpowers` plugin's methodology skills
into buildme's code-driven pipeline. The theme: buildme structurally enforces
red-green TDD, never marks failing work done, and reviews against a hardened
two-verdict rubric.

### Added
- **RED gate** (`gates.red_gate`, from *test-driven-development*). Between the
  test-writer and implementer phases the pipeline runs the suite and requires a
  non-zero exit failing for the right reason. A passing suite means the tests
  prove nothing (`rewrite_tests`); a collection/syntax error means broken test
  files (`fix_tests`). One retry each, then the block fails. RED and GREEN
  output are captured as block evidence and fed to the reviewer.
- **Two-verdict review** (from *subagent-driven-development*'s task reviewer).
  `ReviewResult` gained a spec-compliance verdict (`missing`/`extra`/
  `misunderstood`) alongside the scored dimensions; passing requires both.
  `CODE_REVIEW_PROMPT` now treats the implementer's report as unverified
  claims, verifies against the ground-truth diff, cites file:line with
  why-it-matters and how-to-fix, and acknowledges strengths first.
- **Integration circuit breaker** with escalation (from *systematic-debugging*).
  Three fix attempts; the third switches to a question-the-architecture prompt
  and escalates to `config.review_model`. The fix prompt carries the four-phase
  debugging protocol (read the complete error, reproduce, one hypothesis, one
  variable at a time). Exhaustion writes a diagnosis to `build-progress.md`.
- **Global Constraints + Interfaces threading.** `design.md` carries a REQUIRED
  `## Global Constraints` section; task blocks carry `Interfaces:`
  (`Produces:`/`Consumes:` exact signatures). Both are injected verbatim into
  agent and review prompts, plus earlier blocks' Produces signatures, so
  implementers use exact signatures rather than re-deriving them. Generated
  tasks are scanned deterministically for placeholders (TBD/TODO/"add
  appropriate error handling"/"similar to block N"), and the tasks audit gained
  spec-coverage traceability and cross-block signature-consistency checks.
- **REQUIRED report slots.** Test-writer and implementer reports close with
  `STATUS:`/`TESTS_RUN:`/GREEN evidence, plus explicit permission to stop
  ("bad work is worse than no work"). `NEEDS_CONTEXT`/`BLOCKED` fails the block
  with the agent's stated reason surfaced.
- **Testing anti-patterns** (from *testing-anti-patterns*). Mechanical
  detection of mock-assertion-only and mock-setup-dominant tests; the
  test-writer prompt and rubric Test Quality dimension gained the five
  anti-pattern checks as positive gate-function criteria.
- `--lenient-review` restores accept-and-continue on review exhaustion for
  unattended overnight runs.

### Changed
- **No silent DONE.** Confirmed-weak tests now fail the block instead of
  warning (the previously-unwired `TEST_QUALITY_PROMPT` auditor adjudicates the
  static scan, with one test-writer retry). Review exhaustion fails the block
  rather than marking it done regardless. `_run_final_review` uses a structured
  verdict over the full-build diff and fails closed — a FAILED verdict fails
  the build, and so does an unverifiable review (an exception yields
  `passed=False` with the error surfaced); neither is marked done, so a resume
  re-runs the review. `_verify_entry_point` actually smoke-runs the detected
  entry point under the repo-trust guard instead of reporting an unverified
  pass.

## [0.3.3] - 2026-07-18

### Added
- **swift-build: `swift`/`xcodebuild`/`xcrun` passthrough.** `swift-host.sh`
  now accepts these as top-level commands and forwards shell-quoted args to
  the host (from the optional `--package-path` dir). Enables host diagnostics
  and repair (`xcodebuild -runFirstLaunch`, `-version`, `-showBuildSettings`)
  and xcodebuild-free simulator builds (e.g. a SwiftPM cross-compile for
  `arm64-apple-ios17.0-simulator`) without leaving the gateway.
  An opt-in `--xcsift` flag (first passthrough arg) pipes the host output
  through the same trusted `2>&1 | xcsift --format toon --quiet` suffix as
  build/test — errors/warnings survive, build noise is dropped. Off by
  default because diagnostic output (`-version`, `-showBuildSettings`,
  `simctl list`) is the answer, not noise, and xcsift would filter it.

## [0.3.2] - 2026-07-17

Fixes from the 07-12→16 PR audit (`INBOX/pr-audit-multiplai-2026-07-12-to-16.md`).

### Fixed
- **buildme: resumed pre-baseline checkpoints no longer mis-baseline the
  review diff.** `run_block_tdd` stamps `baseline_commit` only when the block
  is genuinely starting (PENDING/TESTING). Resuming an old mid-block
  checkpoint (IMPLEMENTING/REVIEWING with no baseline) previously stamped
  current HEAD — hiding the block's own commits from the quality reviewer;
  now it keeps the documented `git diff HEAD` fallback and logs a warning.
- **buildme: one unreadable standards file no longer fails the block.**
  `BuildConfig.standards_text()` now catches per-file read errors
  (OSError/UnicodeDecodeError), logs, and skips — as its docstring always
  promised.
- **buildme: tasks-shape audit completion is recorded in checkpoint state,
  not inferred from file existence.** A crash mid-audit used to leave
  tasks.md DONE and silently skip the audit on resume; resume now re-runs it
  (idempotent) and logs when it is skipped as recorded-complete. A non-list
  JSON audit response (e.g. object-wrapped findings) now logs a warning
  instead of silently passing as "no findings".
- **buildme: `claude-agent-sdk` floored+capped** (`>=0.2.116,<0.3`) — 0.1.x
  crashed at import (same bug class as the dream hook crash).
- **swift-build: plain Linux (no container markers) now refuses the SSH
  bridge even when `SSH_BUILD_USER` is set**, matching the SKILL.md support
  matrix — the bridge assumes the container↔host identical-path mount.
  Also fixed the false "caches result" comment (the xcsift probe is now
  actually memoized per invocation).
- **skill-creator: the degradation-contract reference is now resolvable for
  installed plugins** — `docs/degradation-contract.md` is vendored into the
  skill's `references/` (repo copy stays canonical) and SKILL.md points at
  it plus the marketplace GitHub URL.

### Changed
- swift-build SKILL.md documents two current gateway limitations (paths with
  spaces / schemes with parens until the container-side unquote fix ships)
  and the `sim screenshot` host-path caveat.

## [0.3.1] - 2026-07-15

### Fixed
- **`swift-build` was unusable over the container→host SSH bridge.** The script
  appends `2>&1 | xcsift --format toon --quiet` to build/test commands, which
  the host gateway rejected as a shell metacharacter (`DENIED: shell
  metacharacter in command`) whenever the host had `xcsift` installed — so
  every containerized `swift build` / `swift test` failed. Paired with a gateway
  change (multiplai-container) that recognizes this one fixed, trusted suffix.
- **`discover_scheme` sent `2>/dev/null` over the bridge** — a latent `>`
  redirect that the gateway also denied, breaking scheme discovery (and thus
  `build`/`test`) for Xcode-project layouts. Removed; the sed/grep parse
  tolerates stderr.
- Corrected the SKILL.md "Gateway Compatibility" note that falsely claimed pipes
  work because the gateway runs `zsh -lc` on the full command.

### Changed
- **`swift-build` is now model-invocable** (`disable-model-invocation: false`),
  so Claude reaches for the skill instead of improvising raw `swift --version` /
  `ssh` calls that the gateway denies.

## [0.3.0] - 2026-07-14

### Added
- **New skill: `plan`** — author self-contained, executable implementation
  plans with a mandatory completion contract: verifiable "Done means"
  criteria, explicit "Constraints / out of scope" (including stop-and-ask
  gates), and a fresh-session self-containedness test. Plan files are
  directly consumable by "implement the plan", goal/autonomous runners,
  or buildme — no parallel goal document needed. Prompt-only, no scripts.

## [0.2.0] - 2026-07-10

Semantic model tiers for the buildme pipeline (requires multiplai-core ≥ v0.7.0).

### Changed
- **buildme now resolves its model from a semantic tier, not a dated literal.**
  `config.DEFAULT_MODEL` is now `pick_model("opus", task="buildme")` — the model
  family lives in `multiplai_core.env.CURRENT_MODEL` (one place to bump per
  quarter), still capped by the `MULTIPLAI_MODEL` ceiling, and retunable per
  task via a `[buildme] MODEL=...` section in `multiplai.conf` with no code edit.

### Fixed
- **Tier detection (DEV-3).** `detect_tier()` now derives advanced/standard from
  the resolved `DEFAULT_MODEL` instead of the `CLAUDE_MODEL` env var, which
  Claude Code never exports to Bash subprocesses — so the tier was permanently
  stuck on `standard` in production regardless of the pinned model. buildme now
  correctly runs the advanced (per-block) TDD path under an opus ceiling.

## [0.1.1] - 2026-07-09

Correctness fixes for the buildme pipeline from the 2026-07-08 code review.
All ~190 tests pass; each fix ships with a regression test.

### Fixed
- **Design/tasks/audit/rubric are grounded in the generated requirements
  again.** Four readers still globbed the never-written legacy
  `change_dir/specs/*/spec.md` while the only writer emits flat
  `change_dir/requirements/<capability>.md`, so every one of those prompts was
  built against an empty directory (`(no specs yet)`). `_read_specs`,
  `run_design_audit`, and the two rubric gatherers now read `requirements/*.md`
  (stem = capability name), mirroring `tdd_engine.assemble_context`. The
  vestigial `"specs"` artifact-id alias in `_build_prompt` is gone.
- **Agent timeouts are observable.** `agent_call` catches `AgentRunTimeout` and
  returns `AgentResult(success=False)` without raising, so every
  `except LLMCallTimeoutError` in the TDD engine was dead — `block.timed_out`
  never flipped and `EXIT_AGENT_TIMEOUT` (3) was unreachable (real timeouts
  exited 1). `AgentResult` gains a `timed_out` flag, the fatal
  test-writer/implementer paths propagate it to `block.timed_out`, and the five
  unreachable except blocks are removed.
- **Tier detection recognizes newer Opus models.** `detect_tier` used a literal
  allowlist (`opus-4-5/4-6/5/6`) that silently downgraded the skill-pinned
  `opus-4-7` to `standard`; replaced with an Opus `>= 4.5` version-range check.
  See the caveat below.
- **Block state is indexed by list position, not `block.number - 1`**, so
  non-contiguous LLM-generated `tasks.md` numbering can't silently no-op a
  status write.
- **Per-block commits no longer leak buildme bookkeeping.**
  `_git_commit_block_phase` excludes `build-progress.md` and `.build-state.json`
  from staging instead of `git add -A`.
- **`--change` can't escape `specs/changes/`.** The change name is normalized
  when resolving `config.change_dir` (shared `normalize_change_name`), so a
  traversal value can't send `archive()`'s `shutil.move` out of tree.

### Known limitation
- Tier detection is **inert in production**: Claude Code v2.1.x does not export
  `CLAUDE_MODEL` to Bash subprocesses (it exports `CLAUDE_EFFORT` but not
  `CLAUDE_MODEL`), and `SKILL.md` invokes the pipeline via a plain `uv run` with
  no `CLAUDE_MODEL=` prefix — so `detect_tier` always sees an empty model and
  returns `standard`, for every model, not just `opus-4-7`. The version-range
  fix is correct in isolation and future-proofs the day the model is plumbed
  through, but the tier stays inert until the skill propagates the model (e.g.
  `CLAUDE_MODEL="{model}" uv run …`) or the pipeline grows an explicit
  `--tier`/`--model` flag. Documented in the `detect_tier` docstring.
