# review-viewer scripts — orientation

A stdlib HTTP server and a static page. The server never calls a model: it
serves the page, answers read-only questions about one review, serves the
walkthrough the session wrote, and carries messages between the page and the
Claude Code session through JSONL files.

Run everything through this member directory:

```bash
uv run --directory plugins/multiplai-dev/skills/review-viewer/scripts python -m review_viewer --help
cd plugins/multiplai-dev/skills/review-viewer/scripts && \
  uv run --project ../../../../.. --package review-viewer --extra dev python -m pytest tests/ -q
```

The page-logic tests need `node`; they fail (not skip) without it.

## Module map

| Module | Does |
|---|---|
| `__main__.py` | CLI: `serve` (findings files, or `--target`), `reply`, `pending`, `list`, `stop`, `walkthrough put\|status`, `validate`, `export-schema`. Calls `setup_logging` once. Owns the stdout contract. `find_review()` looks for a review of the same commits. |
| `models.py` | The `findings.json` v1 and `walkthrough.json` v1 pydantic models (source of truth for both files in `../schema/`), `finding_id()`, and the mailbox row models. |
| `gitdata.py` | Git: `parse_target()` / `resolve_target()` (PR, branch, worktree, `a..b`, `a...b`; same base/head rules as `review_pipeline/target.py`, restated because that member is not importable here), `parse_unified()`, `file_view()`, `allowed_paths()`, `diff_target()`. Fixed argv, no shell, stdin closed. The only writes to a repo are the fetches named in `../SKILL.md`. |
| `stats.py` | measured badges: `classify()` a path (lock, generated, test, docs, code), `change_stats()` from `git diff --numstat` and `git log`, the size/tests/commits thresholds, PR badges. |
| `walkthrough.py` | `check()` a walkthrough against the served target, `coverage()`, `put()` by atomic replace. |
| `mailbox.py` | Append-only JSONL rows, `decisions.json` by atomic replace; the directory is 0700 and every file 0600. |
| `server.py` | `ThreadingHTTPServer` subclass (`allow_reuse_address = False`), request checks, routes, idle watchdog. |
| `registry.py` | Finds live viewers: probes each mailbox's recorded port with that mailbox's token, in parallel. A token is never sent to any other port. |
| `netinfo.py` | Container detection (degradation contract rule 2), bind host, URLs to print. |
| `static/` | `index.html`, `boot.js` (takes the token out of the address bar), `theme.js` (applies the saved theme and light/dark mode before first paint as `data-theme`/`data-mode`, fills the Theme menu, drives the mode button), `logic.js` (pure functions, tested under node), `app.js`, `app.css`, `themes.css` (every rule scoped to `html[data-theme]`), the bundled `font-*.woff2` and `FONTS-LICENSE.txt`. |

After changing `models.py`, run `python -m review_viewer export-schema` and
commit both schemas; `test_models.py` fails while either differs.

## The token rule

A token (`secrets.token_urlsafe(32)`) is created per server start. It exists
only in `<mailbox>/server.token` and `<mailbox>/open.html` (both 0600, deleted
on exit) and in the page's memory. It never reaches stdout, stderr, a log
line, `activity.*`, `server.json`, or any other file. The session's stdout is
its transcript, which is kept. `test_server.py` greps for it.

## Request checks, in order

1. A request with an `Origin` header that does not match its `Host` → 403.
2. `/api/*` without a matching `X-Review-Token` (`hmac.compare_digest`) → 401.
3. A `POST` whose `Content-Type` is not `application/json` → 415. That forces a
   CORS preflight, which is never answered with allow headers.

`GET /` and `/static/*` need no token and contain no review data. The file
route serves only paths in `allowed_paths()` (changed files, finding files,
citation paths) — anything else is 404.

## Protocol 1: `findings.json` v1

See `models.py` or the committed schema. Unknown keys are rejected at every
level. Line numbers are 1-based, at `head_sha`. `finding_id` is the first 10
hex of `sha1(f"{file}\0{line_start}\0{claim}")`.

## Protocol 2: the mailbox (`<dir of findings.json>/viewer/`)

| File | Writer | Row |
|---|---|---|
| `inbox.jsonl` | server | `{"v":1,"id":"q-<utc>-<4 hex>","ts","target","kind":"question"\|"decision","finding_id","anchor":{"path","side":"head"\|"base","line_start","line_end"}\|null,"text","decision","step_id","explain":bool}` (`explain`: from a block's 💡 button; needs `anchor`) |
| `outbox.jsonl` | `reply` | `{"v":1,"reply_to","ts","text","done"}` |
| `decisions.json` | server | `{finding_id: {"decision","note","ts"}}` |
| `server.json` | server | `{"url_path_only","port","pid","session_id","started","targets"}` |
| `server.token`, `open.html` | server | 0600, deleted on exit |

One writer per file; each row is one `write()` under 64 KiB on an `O_APPEND`
descriptor. Readers skip lines that do not parse. `reply` splits answers that
would exceed the row limit into several rows; only the last one can carry
`done: true`. Rows are split on `\n` only: they are written with
`ensure_ascii=False`, so U+2028 can appear raw inside a string.

`step_id` is null unless the question was asked while a walkthrough step was
open in the page; then it names that step (`^[a-z0-9-]{1,40}$`), and the
session answers in the context of that step.

`pending` prints the inbox rows whose latest reply is missing or not
`done`. The session runs it after arming (or re-arming) the Monitor.

## Protocol 3: the walkthrough (`<mailbox>/../walkthrough.json`)

The session writes it; the server never calls a model. `walkthrough put
--box <mailbox> --file <f>` parses it against `Walkthrough` and checks, in
code, against `<mailbox>/target.json` (the served target, written by `serve`
at publish time and left in place — it holds no secret):

1. `base_sha`/`head_sha` equal the served target's.
2. Every anchor path is a changed file, not binary, and its line range exists
   at `head_sha` (`side: head`) or `base_sha` (`side: base`).
3. Every `finding_ids` entry is a loaded finding.
4. With `complete: true`: every changed file is anchored or in `skipped`, and
   every `confirmed` or `unverifiable` finding is linked from a step.
5. Step ids are unique. (`skipped` paths must be changed files too.)
6. At most one assessment per topic (except `other`); with `complete: true`,
   the `commits` and `tests` assessments exist.

Any failure → exit 2, every problem listed with its step id, nothing written.
Otherwise the file is replaced atomically (0600). `GET
/api/targets/<slug>/walkthrough` returns it (404 while absent); the page polls
it, so a new walkthrough never restarts the server. `walkthrough status`
prints the target's shas, what is not yet covered, the missing assessments,
the measured badges and each commit's subject.

`/api/targets/<slug>` also returns `pr` (number, title, author, url, body,
head/base ref, check counts, mergeable, draft, review decision — from the one
`gh pr view` call, in memory and `target.json` only, never in
`findings.json`), `notice` (a review exists for other commits) and `stats`
(`stats.change_stats`: line counts by file kind, commits, and the measured
badges with their thresholds; null when git cannot read the range).

## Git output

Every git call runs with `-c color.ui=never -c core.quotepath=off`, and every
`git diff` with `--no-color --no-ext-diff --no-textconv`, with
`GIT_EXTERNAL_DIFF` removed from the environment: user or repo config must
not change what `_walk()` parses. Hunk bodies are counted from the `@@`
lengths, never recognised by their first characters (`--- comment` is a
deleted line, not a file header). File text is split on `\n` only, which
is how git numbers lines.

## Idle and reuse

Only page activity resets the idle timer; `/api/whoami` (used by `list`,
`serve` and `stop`) does not. `serve` on a mailbox whose server is alive
reuses it when the session and the findings digests match, restarts it when a
findings file changed, and exits 3 for another or an unidentified session.

Plain-diff mode (a `--target` with no review of the same commits) puts the
mailbox under `<workspace INBOX or ~/.multiplai>/review-viewer/<slug>/viewer/`. A
target whose review is found uses that review's `viewer/`. `serve` looks in
`--reviews-dir` (default: `<workspace INBOX or ~/.multiplai>/reviews`, where
the review skill writes — `output_root()` and the review skill's
`default_out()` must agree) for `*/findings.json` with the same base and head.

## Logging

`setup_logging("review-viewer", propagate_loggers=("review_viewer", "multiplai_core"))`
writes `review-viewer.log`; every module logs to `logging.getLogger(__name__)`.
Request lines go to DEBUG. Unexpected handler errors log at ERROR with
`exc_info` (so they reach `hook-errors.log`); the client gets `500
{"error": "internal"}`.

`log_event("review-viewer", …)` fires for exactly these events. No field ever
holds question, answer or walkthrough text.

| event | message (example) | fields |
|---|---|---|
| `start` | `viewer started for <slug> on port 8765` | `port, targets, host, container` |
| `reuse` | `viewer already running for <slug>; reused it` | `port, owner_session` |
| `question` | `question q-… on finding 3fa2c91b0e` | `target, finding_id, chars` |
| `decision` | `finding 3fa2c91b0e rejected` | `target, finding_id, decision` |
| `reply` | `reply to q-… (final)` | `target, reply_to, chars, done` |
| `walkthrough` | `walkthrough for <slug>: 5 steps (complete)` | `target, steps, complete` |
| `idle_stop` | `viewer stopped after 30 min with no open page` | `idle_minutes` |
| `stop` | `viewer stopped by stop --box` | `reason` |
| `rejected_request` | `refused request: bad token` (WARNING, at most once a minute per status) | `status, route` |

## Tests

`tests/fixture_repo.py` builds the two-commit repository the tests review
(fixed author and dates, so the shas in `fixtures/findings.example.json` are
stable). `python tests/fixture_repo.py <dir>` builds it anywhere.
`build_remote()` adds a bare origin, a branch `main` moved past, an unpushed
commit and a PR head under `refs/pull/7/head`, for target resolution;
`test_gitdata.py` fakes `gh` with a script first on `PATH`.

| File | Covers |
|---|---|
| `test_gitdata.py` | diff parsing, file views, `parse_target`/`resolve_target` |
| `test_stats.py` | path classes, numstat parsing (renames, binary), each badge's thresholds, check counts, stats on the fixture repo |
| `test_walkthrough.py` | each `walkthrough put` rule, the CLI, the route, `step_id` questions |
| `test_serve_targets.py` | review lookup (match, stale, none) and the `walkthrough:` stdout line |
| `test_server.py`, `test_mailbox.py`, `test_models.py`, `test_logging.py`, `test_netinfo.py` | the server, mailbox, contracts, logs, container detection |
| `logic.test.js` (via `test_logic_js.py`) | the page's pure functions, under node |
