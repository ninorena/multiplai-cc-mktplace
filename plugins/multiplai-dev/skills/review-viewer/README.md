# review-viewer — how it works

`review-viewer` opens a web page for a code change: a GitHub PR, a branch,
the commits you have not pushed yet, or a commit range. The page shows every
changed file in full with the diff marked, and a walkthrough of the change
that your Claude Code session writes. When `/multiplai-dev:review` has
reviewed the same commits, the page shows its findings beside the code too.
You can ask questions in the page and accept, reject or defer findings; the
session answers, and the answers appear in the page.

`SKILL.md` is the session's instructions. `scripts/CLAUDE.md` is for people
changing the code. This file explains how the parts fit together.

## Using it

Ask the session, for example:

- "walk me through PR 245"
- "show me the branch `feature-x` in the viewer"
- "open the review of my unpushed commits"

What you can name:

| You say | The page shows |
|---|---|
| `245`, `#245`, a PR URL, `owner/repo#245` | the PR's head against the point where it branched from its base |
| a branch name | that branch against the point where it branched from the default branch — what a PR would show |
| a worktree path, or nothing | that worktree's commits not yet pushed |
| `a..b` | exactly those two commits |
| `a...b` | `b` against the point where `a` and `b` branched |

A PR needs `gh`, logged in, and a local clone of the repository. Nothing
else needs the network, apart from the page loading its libraries from
cdnjs.

The session prints an `open: file://…` link. Open it from the machine that
runs the browser: click it, or run `open <path>` on a Mac. The page loads
already authorised. The `url:` lines it also prints are for reference;
opened directly, they show a "not authorised" banner.

The walkthrough fills in while the session writes it: first the overview and
the first steps, then the rest. Click a step's file reference to jump the
code there; `]` and `[` move between steps, `j` and `k` between findings.

The **Theme** menu in the header changes colours and type without moving
anything: Default; Phosphor and Amber are CRT terminals; Turbo is a DOS
text-mode IDE; 1-bit is black and white; Game Boy uses the handheld's four
greens; Solarized is Ethan Schoonover's palette. The button next to it
cycles **Auto → Light → Dark**: every theme has a light and a dark version,
and Auto follows the system setting, changing when it does. Both choices are kept per
browser. The fonts ship with the viewer (`static/FONTS-LICENSE.txt`, all
SIL Open Font License), so themes load nothing from the network.

## The parts

```
 browser (your Mac)                 container or machine running Claude Code
 ------------------                 ----------------------------------------
                                     ┌──────────────── mailbox dir ─────────────┐
 page ── HTTP + token ──► server ───►│ inbox.jsonl      questions, decisions    │
   ▲                        │        │ outbox.jsonl     answers                 │◄── session
   └──── polls ─────────────┘◄───────│ decisions.json   accept/reject/defer     │    (Monitor
                                     │ ../walkthrough.json                      │     + CLI)
                                     └──────────────────────────────────────────┘
```

- **The server** is a small Python HTTP server started by the session. It
  reads git, serves the page and the code, and reads and writes the files in
  the mailbox directory. **It never calls a model.** Every answer and every
  walkthrough comes from the Claude Code session that started it; the page
  header names that session.
- **The session** watches `inbox.jsonl` with a Monitor, answers with
  `python -m review_viewer reply`, and publishes the walkthrough with
  `python -m review_viewer walkthrough put`.
- **The page** is static HTML and JavaScript. It asks the server for data
  and polls for answers and for the walkthrough.

## How a question reaches the session and comes back

1. You type a question, or click accept, reject or defer. The page sends it
   to the server (`POST /api/ask`).
2. The server appends one line to `inbox.jsonl`. A decision is also
   recorded in `decisions.json` at once.
3. The session's Monitor sees the new line and wakes the session.
4. The session reads the code, then runs `reply`, which appends the answer
   to `outbox.jsonl`.
5. The page polls the server, which reads `outbox.jsonl`, and shows the
   answer under your question.

If the session has ended, questions wait in `inbox.jsonl` and no answer
comes. A Monitor lasts 30 minutes; the session re-arms it and then runs
`pending` to answer anything that arrived in between.

## How the walkthrough gets there

The session reads the diff and writes `walkthrough.json`: an overview, then
steps, each pointing at the lines it explains, with an optional mermaid
diagram and links to findings. `walkthrough put` checks it in code before
the page sees it, and refuses it (exit 2, every problem listed) when:

- it is for different commits than the ones served;
- a step points at a file that did not change, or at lines that do not
  exist;
- it links a finding that is not loaded;
- it says it is complete but a changed file is neither explained nor listed
  as skipped, or a confirmed finding is not linked from any step.

It passes, and the page picks it up on its next poll, without a restart.

## The token

Each time the server starts it creates a random token. It lives only in:

- `server.token` and `open.html` in the mailbox directory, readable only by
  you and deleted when the server stops;
- the open page's memory. `open.html` sends the browser to the page with the
  token in the address, and the page's first script removes it from the
  address bar at once, so it is not bookmarked, synced or shown.

It is never printed, logged or written anywhere else, because what the
session prints becomes its transcript, which is kept.

**Why it exists.** Inside a container, the server listens on every network
interface so that the browser outside can reach it. Anything else that can
reach that port could then read the diff and the review, and could type
"questions" that the session reads as your words. Every data request must
carry the token or it gets 401. Two more checks stop other websites open in
the same browser: a request from another web origin gets 403, and every
`POST` must be JSON, which makes the browser ask the server first — and the
server never says yes.

The page also runs under a content security policy: scripts only from the
server and cdnjs, fonts only from the server, no inline scripts or styles. Diagrams are drawn by mermaid,
cleaned with DOMPurify and shown as images, so a diagram cannot run code.

## Stopping

The session stops the viewer with `python -m review_viewer stop --box
<mailbox>`. It also stops by itself 30 minutes after the last page is
closed. Stopping deletes the token files, so an old `open:` link stops
working.
