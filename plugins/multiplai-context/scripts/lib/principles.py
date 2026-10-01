"""Principles: review one sentence that stands for several learnings.

The daily review shows single facts, and single facts are too small to judge
quickly. This module asks a model to group queued learnings into candidate
principles. The user ticks a principle, not the facts behind it.

Where things live
-----------------
``<memory>/principles.md``            the book. One line per principle. Loaded
                                      every session, so it is capped.
``<memory>/principles-examples.md``   1 or 2 learnings per principle, under the
                                      principle's number. Read only on request.
``<learnings>/archived/rolled-up.md``  every learning behind an accepted
                                      principle, with the principle's number.
``<learnings>/archived/rejected-principles.md``  principles the user said no to.
                                      The model is shown this list.
``<learnings>/principles/pending.json``  candidates waiting for a tick.
``<learnings>/principles/declined.json``  learnings the user said are not
                                      examples of a principle.

**Nothing is deleted.** A yes moves the learnings behind a principle to
``rolled-up.md``, except those the model marked as also carrying a fact for their
own target file. Those stay in their queues and come up as facts. A no leaves them in their queues, where the facts part of the
review shows them one at a time.

The model step is the only part that can fail for reasons outside this code.
A failure leaves the review with its facts part only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lib.daily_review import (
    Dirs,
    Entry,
    Store,
    _TICK_RE,
    _atomic_write,
    rank_sorted,
    read_note,
)

BOOK = "principles.md"
EXAMPLES = "principles-examples.md"
# Files the per-prompt router must not load: the book is loaded every session
# by another route, and the examples are read only on request.
NOT_ROUTED = frozenset({BOOK, EXAMPLES})

BOOK_CAP = 40          # principles in the book
MAX_NEW = 3            # new principles shown in one review
MIN_ENTRIES = 3        # learnings behind a new principle; a first guess
MAX_EXAMPLES = 2
MAX_SENTENCE_CHARS = 240
MAX_FEEDBACK = 20      # the user's notes shown to the model, newest first

_BOOK_LINE_RE = re.compile(r"^- (?P<n>P\d+)\.\s+(?P<s>.+?)\s*$")
_EX_HEAD_RE = re.compile(r"^##\s+(?P<n>P\d+)\.")
_PRINCIPLE_HDR_RE = re.compile(r"^<!-- principle (?P<json>\{.*\}) -->\s*$")

_BOOK_HEAD = (
    "# Principles\n\n"
    f"> Cap: {BOOK_CAP} principles. Loaded every session. To add to a full book, name the one that leaves.\n"
    f"> Examples for each principle are in `{EXAMPLES}`, under its number.\n\n"
)
_EXAMPLES_HEAD = (
    "# Principle examples\n\n"
    f"> Not loaded every session. Each section holds the learnings behind one principle in `{BOOK}`.\n"
)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    sentence: str
    keys: list[str]                 # every learning behind it
    examples: list[str]             # 1 or 2 of those keys
    existing: str = ""              # "P3" when these are more examples for P3
    proposed: str = ""              # date first proposed
    facts: list[str] = field(default_factory=list)  # keys that also carry a fact
                                    # worth keeping in their own target file
    notes: list[str] = field(default_factory=list)  # the user's notes on it
    id: str = field(default="")

    def __post_init__(self):
        if not self.id:
            basis = self.existing or self.sentence
            raw = basis + "\n" + "\n".join(sorted(self.keys))
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:12]


def principles_dir(store: Store) -> Path:
    return store.root / "principles"


def _pending_path(store: Store) -> Path:
    return principles_dir(store) / "pending.json"


def _declined_path(store: Store) -> Path:
    return principles_dir(store) / "declined.json"


def rolled_up_path(store: Store) -> Path:
    return store.archive / "rolled-up.md"


def rejected_principles_path(store: Store) -> Path:
    return store.archive / "rejected-principles.md"


def load_pending(store: Store) -> list[Candidate]:
    p = _pending_path(store)
    if not p.exists():
        return []
    return [Candidate(**c) for c in json.loads(p.read_text(encoding="utf-8"))]


def save_pending(store: Store, pending: list[Candidate]) -> None:
    _atomic_write(_pending_path(store), json.dumps([asdict(c) for c in pending], indent=2) + "\n")


def load_declined(store: Store) -> dict[str, list[str]]:
    """key -> principle numbers the user said this learning is not an example of."""
    p = _declined_path(store)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_declined(store: Store, declined: dict[str, list[str]]) -> None:
    _atomic_write(_declined_path(store), json.dumps(declined, indent=2, sort_keys=True) + "\n")


def locate(store: Store, key: str) -> tuple[Path, Entry] | None:
    """Where a waiting learning is now: a queue or the reserve.

    A pending candidate can outlive a rebalance that moved one of its learnings
    from a queue to the reserve, so a key is looked up, never assumed.
    """
    for t in store.queue_targets():
        qp = store.queue_path(t)
        for e in store.read(qp):
            if e.key == key:
                return qp, e
    for e in store.read(store.reserve_path):
        if e.key == key:
            return store.reserve_path, e
    return None


def prune_pending(store: Store, pending: list[Candidate]) -> list[Candidate]:
    """Drop learnings that are no longer waiting. Drop a candidate left too thin."""
    out = []
    for c in pending:
        keys = [k for k in c.keys if locate(store, k) is not None]
        if not keys:
            continue
        if not c.existing and len(keys) < MIN_ENTRIES:
            continue
        c.keys = keys
        c.examples = [k for k in c.examples if k in keys] or keys[:MAX_EXAMPLES]
        c.facts = [k for k in c.facts if k in keys]
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------


def read_book(memory: Path) -> list[tuple[str, str]]:
    """(number, sentence) for each principle in the book."""
    p = memory / BOOK
    if not p.exists():
        return []
    out = []
    for l in p.read_text(encoding="utf-8").splitlines():
        m = _BOOK_LINE_RE.match(l)
        if m:
            out.append((m.group("n"), m.group("s")))
    return out


def _next_number(memory: Path) -> str:
    used = [int(n[1:]) for n, _ in read_book(memory)]
    ex = memory / EXAMPLES
    if ex.exists():
        for l in ex.read_text(encoding="utf-8").splitlines():
            m = _EX_HEAD_RE.match(l)
            if m:
                used.append(int(m.group("n")[1:]))
    return f"P{max(used, default=0) + 1}"


def _example_line(e: Entry) -> str:
    src = ", ".join(e.meta.get("src", [])[:2])
    return f"- {e.description} (for {e.target}" + (f"; {src}" if src else "") + ")"


def _add_examples(text: str, number: str, sentence: str, lines: list[str]) -> str:
    """Append *lines* under the section for *number*, creating the section if needed."""
    body = text if text.endswith("\n") else text + "\n"
    rows = body.splitlines()
    start = next((i for i, l in enumerate(rows) if (m := _EX_HEAD_RE.match(l)) and m.group("n") == number), None)
    if start is None:
        return body.rstrip("\n") + f"\n\n## {number}. {sentence}\n\n" + "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(rows)) if rows[i].startswith("## ")), len(rows))
    while end > start + 1 and not rows[end - 1].strip():
        end -= 1
    have = set(rows[start:end])
    new = [l for l in lines if l not in have]
    return "\n".join(rows[:end] + new + rows[end:]) + "\n"


# ---------------------------------------------------------------------------
# The model step
# ---------------------------------------------------------------------------

SYSTEM = f"""\
You read short learnings that an AI coding assistant recorded about working with one
person (Nick) and his projects. Your job is to find principles: general rules that
several learnings are instances of.

A good principle:
- is one sentence, at most 25 words, in plain English, in the imperative ("Check X before Y.");
- applies beyond one project, one file, or one incident;
- would change how an assistant behaves next time;
- is a fair guess even if no single learning states it outright.

Rules:
- A new principle needs at least {MIN_ENTRIES} learnings behind it. If fewer fit, do not propose it.
- Each learning can sit behind at most one principle. Most learnings will fit none. That is fine.
- Pick the 1 or 2 learnings that show the principle most clearly as examples.
- Under "facts", list the learnings behind a principle that also state a specific
  fact worth keeping in their own target file: a path, a name, a setting, a source of
  truth for one project. Leave out learnings the principle fully covers.
- Do not restate a principle already in the book, or one the person rejected.
- If learnings are new instances of a principle already in the book, list them
  under "support" with that principle's number instead.
- Propose at most {MAX_NEW} new principles. Prefer the ones with the most learnings behind them.
- Nick's notes on earlier review items say what he wants. Follow them. A note can
  apply beyond the one item it was written on.

Answer with JSON only, no prose, in this shape:
{{"principles": [{{"sentence": "...", "entries": ["e1", "e4", "e9"], "examples": ["e4"], "facts": ["e9"]}}],
 "support": [{{"principle": "P3", "entries": ["e2"], "examples": ["e2"], "facts": []}}]}}
"""


def build_prompt(entries: list[Entry], book: list[tuple[str, str]],
                 rejected: list[str], feedback: list[str] | None = None) -> tuple[str, dict[str, Entry]]:
    """The user message, and the short id -> entry map used to read the answer."""
    idmap: dict[str, Entry] = {}
    rows = []
    for i, e in enumerate(entries, start=1):
        sid = f"e{i}"
        idmap[sid] = e
        rows.append(f"{sid} [{e.target}] {e.description}")
    parts = ["## The book (principles already accepted)\n"]
    parts += [f"{n}. {s}" for n, s in book] or ["(empty)"]
    parts += ["", "## Rejected principles (do not propose these again)\n"]
    parts += [f"- {s}" for s in rejected] or ["(none)"]
    parts += ["", "## Nick's notes on earlier review items\n"]
    parts += [f"- {s}" for s in (feedback or [])] or ["(none)"]
    parts += ["", "## Learnings\n"] + rows
    return "\n".join(parts) + "\n", idmap


def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the model's answer")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the model's answer is not a JSON object")
    return data


def parse_candidates(text: str, idmap: dict[str, Entry], *, book_numbers: set[str],
                     declined: dict[str, list[str]], max_new: int, today: str) -> list[Candidate]:
    """Read the model's answer. Anything that breaks a rule is dropped, not repaired."""
    data = _json_object(text)
    used: set[str] = set()
    out: list[Candidate] = []

    def keys_of(ids) -> list[str]:
        ks = []
        for sid in ids if isinstance(ids, list) else []:
            e = idmap.get(str(sid))
            if e is not None and e.key not in used and e.key not in ks:
                ks.append(e.key)
        return ks

    def examples_of(ids, keys) -> list[str]:
        ex = [k for k in keys_of(ids) if k in keys][:MAX_EXAMPLES]
        return ex or keys[:MAX_EXAMPLES]

    def facts_of(ids, keys) -> list[str]:
        return [k for k in keys_of(ids) if k in keys]

    new = 0
    for p in data.get("principles") or []:
        if new >= max_new or not isinstance(p, dict):
            continue
        sentence = " ".join(str(p.get("sentence", "")).split())
        if not sentence or len(sentence) > MAX_SENTENCE_CHARS:
            continue
        keys = keys_of(p.get("entries"))
        if len(keys) < MIN_ENTRIES:
            continue
        out.append(Candidate(sentence=sentence, keys=keys,
                             examples=examples_of(p.get("examples"), keys),
                             facts=facts_of(p.get("facts"), keys), proposed=today))
        used.update(keys)
        new += 1

    for s in data.get("support") or []:
        if not isinstance(s, dict):
            continue
        number = str(s.get("principle", "")).strip()
        if number not in book_numbers:
            continue
        keys = [k for k in keys_of(s.get("entries")) if number not in declined.get(k, [])]
        if not keys:
            continue
        out.append(Candidate(sentence="", keys=keys, examples=examples_of(s.get("examples"), keys),
                             facts=facts_of(s.get("facts"), keys), existing=number, proposed=today))
        used.update(keys)
    return out


def model_input(store: Store, pending: list[Candidate]) -> list[Entry]:
    """Queued learnings not already behind a waiting candidate, best first."""
    taken = {k for c in pending for k in c.keys}
    out: list[Entry] = []
    for t in store.queue_targets():
        out += [e for e in store.queue(t) if e.key not in taken]
    return rank_sorted(out)


def rejected_sentences(store: Store) -> list[str]:
    """Each rejected principle, with the user's reason when one was given."""
    p = rejected_principles_path(store)
    if not p.exists():
        return []
    return [l[2:] for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def feedback_notes(store: Store, pending: list[Candidate], limit: int = MAX_FEEDBACK) -> list[str]:
    """The user's notes on single learnings and on waiting principles, newest first.

    Notes on rejected principles already reach the model with the rejected list.
    """
    dated: list[tuple[str, str]] = []
    for path, verb in ((store.rejected_path, "said no to"), (store.applied_path, "said yes to")):
        for e in store.read(path):
            for n in e.meta.get("notes", []):
                dated.append((e.meta.get("last", ""), f'On a learning he {verb} for {e.target} ("{e.description[:100]}"): {n}'))
    for t in store.queue_targets():
        for e in store.queue(t):
            for n in e.meta.get("notes", []):
                dated.append((e.meta.get("last", ""), f'On a waiting learning for {e.target} ("{e.description[:100]}"): {n}'))
    for c in pending:
        for n in c.notes:
            dated.append((c.proposed, f'On the waiting principle "{c.sentence or c.existing}": {n}'))
    dated.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in dated[:limit]]


# ---------------------------------------------------------------------------
# The review blocks
# ---------------------------------------------------------------------------


def render_block(letter: str, c: Candidate, store: Store, book: dict[str, str]) -> str:
    hdr = json.dumps({"id": c.id}, sort_keys=True)
    found = [locate(store, k) for k in c.keys]
    entries = {e.key: e for _, e in (f for f in found if f)}
    targets = sorted({e.target for e in entries.values()})
    if c.existing:
        lines = [f"### More examples for {c.existing}", f"<!-- principle {hdr} -->",
                 f"{c.existing}. {book.get(c.existing, '(no longer in the book)')}"]
    else:
        lines = [f"### Principle {letter}", f"<!-- principle {hdr} -->",
                 "Principle:", "```text", c.sentence, "```"]
    lines.append("Examples:")
    lines += [f"- {entries[k].description}" for k in c.examples if k in entries]
    lines.append(f"Behind it: {len(c.keys)} learning(s), for {', '.join(targets) or '?'}.")
    if c.facts:
        lines.append(f"After a yes, {len(c.facts)} of them still come up as facts for their own file.")
    lines += [f"Earlier note: {n}" for n in c.notes]
    lines += ["Note:", "- [ ] yes", "- [ ] no", "- [ ] later"]
    return "\n".join(lines)


def render_section(pending: list[Candidate], store: Store, memory: Path) -> str:
    if not pending:
        return ""
    book = dict(read_book(memory))
    blocks = []
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for c in pending:
        blocks.append(render_block("" if c.existing else next(letters), c, store, book))
    return (
        "## Principles\n\n"
        "Each one is a guess at a rule behind several learnings. A yes adds it to the book,\n"
        f"`{BOOK}`, which loads every session. Edit the sentence first if you want.\n\n"
        + "\n\n".join(blocks) + "\n"
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class PItem:
    start: int
    end: int
    id: str
    sentence: str
    ticks: list[str]
    result: str
    note: str = ""


def parse_blocks(text: str) -> list[PItem]:
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("### ") or l.startswith("## ")]
    items = []
    for s, nxt in zip(starts, starts[1:] + [len(lines)]):
        hdr = next((m for l in lines[s:nxt] if (m := _PRINCIPLE_HDR_RE.match(l))), None)
        if not hdr:
            continue
        sent, in_text, done, ticks, result = [], False, False, [], ""
        for l in lines[s:nxt]:
            if in_text:
                if l.strip() == "```":
                    in_text, done = False, True
                else:
                    sent.append(l)
                continue
            if l.strip() == "```text" and not done:
                in_text = True
                continue
            tm = _TICK_RE.match(l)
            if tm and tm.group("x") in "xX":
                ticks.append(tm.group("what"))
            if l.startswith("Result:"):
                result = l[len("Result:"):].strip()
        items.append(PItem(s, nxt, json.loads(hdr.group("json"))["id"],
                           " ".join(" ".join(sent).split()), ticks, result, read_note(lines[s:nxt])))
    return items


def _set_result(text: str, item: PItem, result: str) -> str:
    lines = text.splitlines()
    block = [l for l in lines[item.start:item.end] if not _TICK_RE.match(l) and not l.startswith("Result:")]
    while block and not block[-1].strip():
        block.pop()
    block += [f"Result: {result}", ""]
    return "\n".join(lines[:item.start] + block + lines[item.end:]) + "\n"


@dataclass
class PrincipleReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    left: list[tuple[str, str]] = field(default_factory=list)
    written_files: list[Path] = field(default_factory=list)


def apply_principles(path: Path, store: Store, dirs: Dirs, *, today: str) -> PrincipleReport:
    """Apply the ticked principle blocks of one review file. Never deletes a learning.

    Order on a yes: the examples file first, then the book, then the learnings
    move. A crash part way leaves an example with no book line, which the next
    yes on the same number repairs, and never a book line with no evidence.
    """
    rep = PrincipleReport()
    text = path.read_text(encoding="utf-8")
    pending = {c.id: c for c in load_pending(store)}

    for snap in parse_blocks(text):
        item = next((i for i in parse_blocks(text) if i.id == snap.id), snap)
        if item.result:
            continue
        c = pending.get(item.id)
        if c is not None and item.note and item.note not in c.notes:
            # Kept on the candidate, so a "later" carries it into tomorrow's
            # review and the next model call sees it.
            c.notes.append(item.note)
            save_pending(store, list(pending.values()))
        if not item.ticks or item.ticks == ["later"]:
            continue
        if c is None:
            rep.left.append((item.id, "no longer waiting; it was already handled"))
            continue
        label = f"examples for {c.existing}" if c.existing else (item.sentence or c.sentence)[:60]
        if len(item.ticks) > 1:
            rep.left.append((label, "more than one box ticked"))
            continue

        if item.ticks[0] == "no":
            if c.existing:
                declined = load_declined(store)
                for k in c.keys:
                    declined.setdefault(k, [])
                    if c.existing not in declined[k]:
                        declined[k].append(c.existing)
                _save_declined(store, declined)
            else:
                rp = rejected_principles_path(store)
                old = rp.read_text(encoding="utf-8") if rp.exists() else ""
                why = f": {item.note}" if item.note else ""
                _atomic_write(rp, old + f"- {item.sentence or c.sentence} (said no on {today}{why})\n")
            del pending[c.id]
            save_pending(store, list(pending.values()))
            text = _set_result(text, item, f"rejected {today}")
            rep.rejected.append(label)
            continue

        # yes
        book = dict(read_book(dirs.memory))
        if c.existing:
            if c.existing not in book:
                rep.left.append((label, f"{c.existing} is no longer in the book"))
                continue
            number, sentence = c.existing, book[c.existing]
        else:
            sentence = item.sentence
            if not sentence:
                rep.left.append((label, "the principle text is empty"))
                continue
            if len(book) >= BOOK_CAP:
                rep.left.append((label, f"the book is full ({BOOK_CAP}). Name the principle that leaves, then tick again"))
                continue
            number = _next_number(dirs.memory)

        found = {k: f for k in c.keys if (f := locate(store, k))}
        if not found:
            rep.left.append((label, "none of its learnings are still waiting"))
            continue
        ex_lines = [_example_line(found[k][1]) for k in c.examples if k in found]
        ex_lines += [f"- Nick's note: {n}" for n in c.notes]
        ex_path = dirs.memory / EXAMPLES
        ex_text = ex_path.read_text(encoding="utf-8") if ex_path.exists() else _EXAMPLES_HEAD
        try:
            _atomic_write(ex_path, _add_examples(ex_text, number, sentence, ex_lines))
            rep.written_files.append(ex_path)
            if not c.existing:
                bp = dirs.memory / BOOK
                bt = bp.read_text(encoding="utf-8") if bp.exists() else _BOOK_HEAD
                _atomic_write(bp, bt.rstrip("\n") + f"\n- {number}. {sentence}\n")
                rep.written_files.append(bp)
            for k, (src, e) in found.items():
                if k in c.facts:
                    # Still a fact for its own file: it stays waiting, tagged, and
                    # the facts part of a later review shows it.
                    e.meta["principle"] = number
                    entries = store.read(src)
                    for x in entries:
                        if x.key == k:
                            x.meta["principle"] = number
                    store.write(src, entries)
                    continue
                store.move(k, src, rolled_up_path(store), principle=number, rolled_up=today,
                           example=k in c.examples)
        except OSError as exc:
            rep.left.append((label, f"write failed ({exc.__class__.__name__})"))
            continue
        del pending[c.id]
        save_pending(store, list(pending.values()))
        text = _set_result(text, item, f"added as {number} on {today}" if not c.existing
                           else f"added to {number} on {today}")
        rep.accepted.append(f"{number}: {sentence[:60]}")

    _atomic_write(path, text)
    return rep
