"""The daily learnings review: a few entries a day, per target file, by tick.

Learnings pile up faster than anyone reviews them. This module turns the pile
into one short queue per target file and a review file with the top two of each
queue. Nothing reaches a memory or project file unless the user ticked "yes".

Everything here is deterministic code. No model call.

Where an entry lives
--------------------
``<learnings>/queues/<slug>.md``   up to ``QUEUE_MAX`` waiting entries for one
                                   target file (the slug is the target path).
``<learnings>/archived/reserve.md``  entries that did not fit in a queue.
``<learnings>/archived/rejected.md`` entries the user said no to, and reserve
                                   entries older than ``RESERVE_DAYS``.
``<learnings>/archived/applied.md``  entries the user said yes to.

**An entry is never deleted.** It is only moved from one of those files to
another. Every move writes the destination first and removes the source second,
so a crash leaves the entry in both places (the next run merges the copies by
key) and never in neither. Raw learnings files are moved to ``archived/`` only
after every entry in them is safely stored; a file with anything this module
does not understand stays where it is.

The review file
---------------
``<dreams>/review-YYYY-MM-DD.md`` holds, for each queue, the top
``PER_FILE_PER_DAY`` entries. Under each entry is the exact text that will be
added, the section it goes under, and three boxes. :func:`apply_review` reads
the ticks and applies only the ``yes`` entries. The text under ``Add:`` is what
gets written, so the user can edit it before ticking.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

QUEUE_MAX = 10
PER_FILE_PER_DAY = 2
RESERVE_DAYS = 90
# Two wordings of one fact, measured on 452 real learnings: true paraphrases
# scored Jaccard 0.35-0.44 and containment 0.56-0.69 once stop words were
# dropped; different facts on the same topic scored 0.27-0.31 and 0.48-0.5.
# Both bars must pass. A miss costs one extra review. A false merge costs
# nothing, because the merged entry keeps every wording.
DUPLICATE_JACCARD = 0.33
DUPLICATE_CONTAINMENT = 0.55
_STOP = frozenset(
    "the and for with that this from are was has have not but when into its their they "
    "been will also should which than then only each any all one use used using".split()
)

_LEARNING_RE = re.compile(
    r"^-\s+\*\*\[(?P<label>[^\]]+)\]\*\*\s+"
    r"(?:(?P<type>OBSERVATION|PREFERENCE|CORRECTION|PATTERN|RULE-PROPOSAL|RULE|DECISION|INTENTION)\s+)?"
    r"(?P<desc>.*?)\s*→\s*Target:\s*(?P<target>\S+)"
    r"(?:\s+—\s+(?P<action>.*))?\s*$"
)
_HEADING_RE = re.compile(r"^##\s+Session Learnings\s+—\s+(?P<ts>\S+)")
_SESSION_RE = re.compile(r"^Session:\s*(?P<sid>\S+)")
_ENTRY_HDR_RE = re.compile(r"^<!-- entry (?P<json>\{.*\}) -->\s*$")
_CAP_RE = re.compile(r"^>\s*Cap:\s*(?P<n>\d+)\s*KB", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]{3,}")


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """One learning, with what it takes to rank and merge it."""

    line: str                       # the learning line, verbatim
    meta: dict                      # key, target, trust, correction, first, last,
                                    # sessions, src, seen, reserved, reason
    also: list[str] = field(default_factory=list)   # wordings merged into this one

    @property
    def key(self) -> str:
        return self.meta["key"]

    @property
    def target(self) -> str:
        return self.meta["target"]

    @property
    def description(self) -> str:
        m = _LEARNING_RE.match(self.line)
        return (m.group("desc") if m else self.line).strip()

    @property
    def action(self) -> str:
        m = _LEARNING_RE.match(self.line)
        return ((m.group("action") or "") if m else "").strip()

    def render(self) -> str:
        out = [f"<!-- entry {json.dumps(self.meta, sort_keys=True, ensure_ascii=False)} -->", self.line]
        out += [f"  - also: {a}" for a in self.also]
        return "\n".join(out)


def _norm(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def entry_key(description: str, target: str) -> str:
    return hashlib.sha256(f"{target}\n{_norm(description)}".encode()).hexdigest()[:16]


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP}


def is_duplicate(a: Entry, b: Entry) -> bool:
    if a.target != b.target:
        return False
    if a.key == b.key:
        return True
    ta, tb = _tokens(a.description), _tokens(b.description)
    if not ta or not tb:
        return False
    both = len(ta & tb)
    return (both / len(ta | tb) >= DUPLICATE_JACCARD
            and both / min(len(ta), len(tb)) >= DUPLICATE_CONTAINMENT)


def rank_sorted(entries: list[Entry]) -> list[Entry]:
    """Corrections first, then seen in 2+ sessions, then verified, then newest."""
    newest_first = sorted(entries, key=lambda e: e.meta.get("last", ""), reverse=True)
    return sorted(
        newest_first,
        key=lambda e: (
            not e.meta.get("correction"),
            len(e.meta.get("sessions", [])) < 2,
            e.meta.get("trust") != "verified",
        ),
    )


def parse_entries(text: str) -> list[Entry]:
    entries: list[Entry] = []
    cur: Entry | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _ENTRY_HDR_RE.match(lines[i])
        if m:
            try:
                meta = json.loads(m.group("json"))
            except json.JSONDecodeError:
                raise ValueError(f"unreadable entry header on line {i + 1}")
            i += 1
            line = lines[i] if i < len(lines) else ""
            cur = Entry(line=line, meta=meta)
            entries.append(cur)
        elif cur is not None and lines[i].startswith("  - also: "):
            cur.also.append(lines[i][len("  - also: "):])
        i += 1
    return entries


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".tmp-{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# The store: queues, reserve, rejected, applied
# ---------------------------------------------------------------------------


def slug(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", target.strip("/"))


class Store:
    def __init__(self, learnings_dir: Path):
        self.root = Path(learnings_dir)
        self.queues = self.root / "queues"
        self.archive = self.root / "archived"

    # -- paths ---------------------------------------------------------
    def queue_path(self, target: str) -> Path:
        return self.queues / f"{slug(target)}.md"

    @property
    def reserve_path(self) -> Path:
        return self.archive / "reserve.md"

    @property
    def rejected_path(self) -> Path:
        return self.archive / "rejected.md"

    @property
    def applied_path(self) -> Path:
        return self.archive / "applied.md"

    # -- io ------------------------------------------------------------
    def read(self, path: Path) -> list[Entry]:
        if not path.exists():
            return []
        return parse_entries(path.read_text(encoding="utf-8"))

    def write(self, path: Path, entries: list[Entry]) -> None:
        _atomic_write(path, "\n\n".join(e.render() for e in entries) + ("\n" if entries else ""))

    def queue_targets(self) -> list[str]:
        if not self.queues.exists():
            return []
        out = []
        for p in sorted(self.queues.glob("*.md")):
            entries = self.read(p)
            if entries:
                out.append(entries[0].target)
        return out

    def queue(self, target: str) -> list[Entry]:
        return self.read(self.queue_path(target))

    def reserve(self, target: str | None = None) -> list[Entry]:
        es = self.read(self.reserve_path)
        return [e for e in es if target is None or e.target == target]

    # -- moves ---------------------------------------------------------
    def add(self, path: Path, entry: Entry) -> None:
        """Append to *path*, merging into an entry already there."""
        entries = self.read(path)
        for ex in entries:
            if is_duplicate(ex, entry):
                merge_into(ex, entry)
                self.write(path, entries)
                return
        entries.append(entry)
        self.write(path, entries)

    def remove(self, path: Path, key: str) -> None:
        entries = self.read(path)
        kept = [e for e in entries if e.key != key]
        if len(kept) != len(entries):
            # An emptied file is rewritten empty, never unlinked: nothing here deletes.
            self.write(path, kept)

    def move(self, key: str, src: Path, dest: Path, **meta_updates) -> Entry | None:
        """Move one entry. Destination first, then source. Never deletes."""
        entries = self.read(src)
        found = next((e for e in entries if e.key == key), None)
        if found is None:
            return None
        found.meta.update(meta_updates)
        self.add(dest, found)
        self.remove(src, key)
        return found

    def find_anywhere(self, entry: Entry) -> Path | None:
        """Where a duplicate of *entry* already lives, if anywhere."""
        for p in (self.rejected_path, self.applied_path, self.reserve_path, self.queue_path(entry.target)):
            if any(is_duplicate(e, entry) for e in self.read(p)):
                return p
        return None


def merge_into(existing: Entry, new: Entry) -> None:
    """Fold *new* into *existing*. Loses no wording and no provenance."""
    m, n = existing.meta, new.meta
    m["sessions"] = sorted(set(m.get("sessions", [])) | set(n.get("sessions", [])))
    m["src"] = sorted(set(m.get("src", [])) | set(n.get("src", [])))
    m["seen"] = m.get("seen", 1) + n.get("seen", 1)
    firsts = [x for x in (m.get("first", ""), n.get("first", "")) if x]
    m["first"] = min(firsts) if firsts else ""
    m["last"] = max(m.get("last", ""), n.get("last", ""))
    if n.get("trust") == "verified":
        m["trust"] = "verified"
    m["correction"] = bool(m.get("correction") or n.get("correction"))
    if new.description != existing.description and new.description not in existing.also:
        existing.also.append(new.description)
    for a in new.also:
        if a not in existing.also and a != existing.description:
            existing.also.append(a)


# ---------------------------------------------------------------------------
# Ingest: raw learnings files -> queues
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    entries: int = 0
    files_archived: list[str] = field(default_factory=list)
    files_held: list[tuple[str, str]] = field(default_factory=list)


def _parse_raw_file(name: str, text: str) -> tuple[list[Entry], str | None]:
    """Entries in one raw learnings file, and a reason to hold the file, if any."""
    entries: list[Entry] = []
    ts = ""
    sid = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or re.match(r"^-{3,}\s*$", line):
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            ts, sid = hm.group("ts"), ""
            continue
        sm = _SESSION_RE.match(line)
        if sm:
            sid = sm.group("sid")
            continue
        m = _LEARNING_RE.match(line)
        if not m:
            return [], f"line {lineno} is not a learning this review understands"
        label = m.group("label")
        typ = m.group("type") or ""
        desc = m.group("desc").strip()
        target = m.group("target")
        trust = "verified" if "verified" in label.lower() else (
            label.split(":", 1)[1].strip().lower() if label.lower().startswith("trust:") else ""
        )
        meta = {
            "key": entry_key(desc, target),
            "target": target,
            "trust": trust,
            "kind": typ,
            "correction": typ == "CORRECTION" or label.upper().startswith("CORRECTION"),
            "first": ts,
            "last": ts,
            "sessions": [sid] if sid else [],
            "src": [f"{name}:{lineno}"],
            "seen": 1,
        }
        entries.append(Entry(line=line.rstrip(), meta=meta))
    return entries, None


def ingest(store: Store) -> IngestReport:
    """Move every entry in the raw learnings files into the store."""
    rep = IngestReport()
    for f in sorted(store.root.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            rep.files_held.append((f.name, f"unreadable ({exc.__class__.__name__})"))
            continue
        entries, hold = _parse_raw_file(f.name, text)
        if hold:
            rep.files_held.append((f.name, hold))
            continue
        try:
            for e in entries:
                dest = store.find_anywhere(e)
                if dest is None:
                    dest = store.queue_path(e.target)
                store.add(dest, e)
        except (OSError, ValueError) as exc:
            rep.files_held.append((f.name, f"could not store its entries ({exc.__class__.__name__})"))
            continue
        rep.entries += len(entries)
        try:
            _archive_raw(f)
        except OSError as exc:
            rep.files_held.append((f.name, f"entries stored, file not moved ({exc.__class__.__name__})"))
            continue
        rep.files_archived.append(f.name)
    return rep


def _archive_raw(f: Path) -> Path:
    dest_dir = f.parent / "archived"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f.name
    n = 2
    while dest.exists():
        dest = dest_dir / f"{f.stem}-{n}{f.suffix}"
        n += 1
    os.rename(f, dest)
    return dest


# ---------------------------------------------------------------------------
# Rebalance: cap 10, reserve, refill, 90-day drop
# ---------------------------------------------------------------------------


@dataclass
class RebalanceReport:
    to_reserve: int = 0
    refilled: int = 0
    expired: int = 0


def expire_reserve(store: Store, *, today: str) -> int:
    """Reserve entries older than RESERVE_DAYS go to rejected.md."""
    cutoff = (date.fromisoformat(today) - timedelta(days=RESERVE_DAYS)).isoformat()
    n = 0
    for e in store.reserve():
        if e.meta.get("reserved", today) < cutoff:
            store.move(e.key, store.reserve_path, store.rejected_path,
                       reason=f"in reserve since {e.meta.get('reserved')}, over {RESERVE_DAYS} days")
            n += 1
    return n


def rebalance(store: Store, *, today: str) -> RebalanceReport:
    rep = RebalanceReport()
    rep.expired = expire_reserve(store, today=today)
    for target in store.queue_targets():
        qp = store.queue_path(target)
        ranked = rank_sorted(store.read(qp))
        for e in ranked[QUEUE_MAX:]:
            store.move(e.key, qp, store.reserve_path, reserved=today)
            rep.to_reserve += 1
        room = QUEUE_MAX - min(len(ranked), QUEUE_MAX)
        if room > 0:
            for e in rank_sorted(store.reserve(target))[:room]:
                e.meta.pop("reserved", None)
                store.move(e.key, store.reserve_path, qp, reserved=None)
                rep.refilled += 1
    return rep


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass
class Dirs:
    workspace: Path
    memory: Path
    learnings: Path
    dreams: Path


def resolve_target(target: str, dirs: Dirs) -> Path | None:
    """Where *target* lives, or None when it would fall outside the workspace.

    A bare file name is a memory file. A path with a directory in it is relative
    to the workspace root. ``..`` is refused outright.
    """
    p = Path(target)
    if ".." in p.parts:
        return None
    if p.is_absolute():
        base = p
    elif len(p.parts) > 1:
        base = dirs.workspace / p
    else:
        base = dirs.memory / p
    try:
        resolved = base.resolve()
        roots = [dirs.workspace.resolve(), dirs.memory.resolve()]
    except OSError:
        return None
    return resolved if any(resolved == r or r in resolved.parents for r in roots) else None


def sha(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def cap_bytes(text: str) -> int | None:
    for line in text.splitlines()[:10]:
        m = _CAP_RE.match(line)
        if m:
            return int(m.group("n")) * 1024
    return None


# ---------------------------------------------------------------------------
# The review file
# ---------------------------------------------------------------------------

_TICK_RE = re.compile(r"^- \[(?P<x>[ xX])\] (?P<what>yes|no|later)\s*$")
_REVIEW_HDR_RE = re.compile(r"^<!-- review (?P<json>\{.*\}) -->\s*$")


def review_path(dirs: Dirs, today: str) -> Path:
    return dirs.dreams / f"review-{today}.md"


def _default_add(e: Entry) -> str:
    return f"- {e.description}"


def render_review_block(n: int, e: Entry, dirs: Dirs, *, expected_hash: str | None = None,
                        changed_note: str = "") -> str:
    tp = resolve_target(e.target, dirs)
    h = expected_hash if expected_hash is not None else (sha(tp) if tp else "unresolved")
    hdr = json.dumps({"key": e.key, "target": e.target, "hash": h}, sort_keys=True)
    sessions = len(e.meta.get("sessions", []))
    warn = []
    if e.meta.get("kind") in ("RULE", "RULE-PROPOSAL") or Path(e.target).name == "CLAUDE.md":
        warn.append("Warning: this edits a standing rule. A yes applies it.")
    lines = [
        f"### {n}. {e.target}",
        f"<!-- review {hdr} -->",
        f"Entry: {e.line}",
        f"Seen: {e.meta.get('seen', 1)} time(s) in {sessions} session(s), last {e.meta.get('last', '')[:10]}.",
    ]
    lines += [f"Also said: {a}" for a in e.also]
    if e.action:
        lines.append(f"Note (not written): {e.action}")
    lines += warn
    if changed_note:
        lines.append(f"CHANGED: {changed_note}")
    lines += [
        "Section: END",
        "Add:",
        "```text",
        _default_add(e),
        "```",
        "- [ ] yes",
        "- [ ] no",
        "- [ ] later",
    ]
    return "\n".join(lines)


def build_review(store: Store, dirs: Dirs, *, today: str, per_file: int = PER_FILE_PER_DAY) -> tuple[Path | None, int]:
    """Write today's review file. Returns (path, entries). An existing file is kept."""
    path = review_path(dirs, today)
    if path.exists():
        return path, len(parse_review(path.read_text(encoding="utf-8")))
    blocks: list[str] = []
    n = 0
    for target in store.queue_targets():
        for e in rank_sorted(store.queue(target))[:per_file]:
            n += 1
            blocks.append(render_review_block(n, e, dirs))
    if not blocks:
        return None, 0
    head = (
        f"# Learnings review {today}\n\n"
        "Tick one box per entry: yes, no, or later. Edit the text under `Add:` first if you\n"
        "want different wording. Then run `/multiplai-context:dream-remember --daily`.\n"
        "No tick means later. Nothing is applied without a yes.\n"
    )
    _atomic_write(path, head + "\n" + "\n\n".join(blocks) + "\n")
    return path, n


@dataclass
class ReviewItem:
    start: int
    end: int                # exclusive line index
    key: str
    target: str
    hash: str
    section: str
    add: str
    ticks: list[str]
    result: str


def parse_review(text: str) -> list[ReviewItem]:
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("### ")]
    items: list[ReviewItem] = []
    for s, nxt in zip(starts, starts[1:] + [len(lines)]):
        hdr = next((_REVIEW_HDR_RE.match(l) for l in lines[s:nxt] if _REVIEW_HDR_RE.match(l)), None)
        if not hdr:
            continue
        j = json.loads(hdr.group("json"))
        section, add_lines, in_add, ticks, result = "END", [], False, [], ""
        for l in lines[s:nxt]:
            if l.startswith("Section:"):
                section = l.split(":", 1)[1].strip() or "END"
            elif l.strip() == "```text" and not in_add and not add_lines:
                in_add = True
            elif l.strip() == "```" and in_add:
                in_add = False
            elif in_add:
                add_lines.append(l)
            tm = _TICK_RE.match(l)
            if tm and tm.group("x") in "xX":
                ticks.append(tm.group("what"))
            if l.startswith("Result:"):
                result = l[len("Result:"):].strip()
        items.append(ReviewItem(s, nxt, j["key"], j["target"], j["hash"], section,
                                "\n".join(add_lines).rstrip("\n"), ticks, result))
    return items


def _set_result(text: str, item: ReviewItem, result: str) -> str:
    """Replace an item's three boxes with a Result line."""
    lines = text.splitlines()
    block = [l for l in lines[item.start:item.end] if not _TICK_RE.match(l) and not l.startswith("Result:")]
    while block and not block[-1].strip():
        block.pop()
    block += [f"Result: {result}", ""]
    return "\n".join(lines[:item.start] + block + lines[item.end:]) + "\n"


def _insert(text: str, section: str, add: str) -> str | None:
    """Return *text* with *add* placed at the end of *section*, or None if the
    section does not exist. ``END`` means the end of the file."""
    body = text if text.endswith("\n") or not text else text + "\n"
    if section.upper() == "END":
        sep = "" if not body or body.endswith("\n\n") else ""
        return body + sep + add.rstrip("\n") + "\n"
    lines = body.splitlines()
    idx = next((i for i, l in enumerate(lines) if re.match(r"^#{1,6}\s+", l) and l.lstrip("# ").strip() == section.lstrip("# ").strip()), None)
    if idx is None:
        return None
    level = len(lines[idx]) - len(lines[idx].lstrip("#"))
    end = len(lines)
    for i in range(idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[i])
        if m and len(m.group(1)) <= level:
            end = i
            break
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    new = lines[:end] + add.rstrip("\n").splitlines() + lines[end:]
    return "\n".join(new) + "\n"


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    left: list[tuple[str, str]] = field(default_factory=list)   # (target, why)
    written_files: list[Path] = field(default_factory=list)


def apply_review(path: Path, store: Store, dirs: Dirs, *, today: str,
                 refresh: Callable[[str, str], str] | None = None) -> ApplyReport:
    """Apply the ticked entries of one review file. Never deletes an entry.

    Item positions in the file move as results are written, so each item is
    looked up again by its key in the current text rather than by an offset.
    """
    rep = ApplyReport()
    text = path.read_text(encoding="utf-8")

    for snap in parse_review(text):
        label = f"{snap.target} [{snap.key[:8]}]"
        item = _refind(text, snap.key) or snap
        if item.result:
            continue
        if not item.ticks or item.ticks == ["later"]:
            continue
        if len(item.ticks) > 1:
            rep.left.append((label, "more than one box ticked"))
            continue
        choice = item.ticks[0]
        qp = store.queue_path(item.target)
        entries = store.read(qp)
        entry = next((e for e in entries if e.key == item.key), None)
        if entry is None:
            rep.left.append((label, "no longer in its queue"))
            continue

        if choice == "no":
            store.move(item.key, qp, store.rejected_path, reason=f"said no on {today}")
            text = _set_result(text, item, f"rejected {today}")
            rep.rejected.append(label)
            continue

        tp = resolve_target(item.target, dirs)
        if tp is None:
            rep.left.append((label, "target path is outside the workspace"))
            continue
        if not tp.exists():
            rep.left.append((label, "target file does not exist; create it first"))
            continue
        if not item.add.strip():
            rep.left.append((label, "the Add text is empty"))
            continue
        current = tp.read_text(encoding="utf-8")
        if sha(tp) != item.hash:
            number = _number_of(text, item)
            fresh = render_review_block(
                0, entry, dirs,
                changed_note=f"{tp.name} changed after this review was written. "
                             "Check the edit below, then tick again.",
            ).replace("### 0.", f"### {number}.", 1)
            lines = text.splitlines()
            text = "\n".join(lines[:item.start] + fresh.splitlines() + [""] + lines[item.end:]) + "\n"
            rep.left.append((label, "target changed since the review was written; edit shown again"))
            continue
        before_hash = sha(tp)
        if item.add.strip() in current:
            new_text = current
        else:
            new_text = _insert(current, item.section, item.add)
            if new_text is None:
                rep.left.append((label, f"section '{item.section}' not found in {tp.name}"))
                continue
            cap = cap_bytes(current)
            if cap is not None and len(new_text.encode()) > cap:
                over = len(new_text.encode()) - cap
                rep.left.append((label, f"{tp.name} would be {over} bytes over its {cap // 1024} KB cap. "
                                        "Name what leaves, then tick again"))
                continue
            if refresh:
                new_text = refresh(new_text, today)
        try:
            if new_text != current:
                _atomic_write(tp, new_text)
                rep.written_files.append(tp)
            store.move(item.key, qp, store.applied_path, applied=today)
        except OSError as exc:
            rep.left.append((label, f"write failed ({exc.__class__.__name__})"))
            continue
        text = _set_result(text, item, f"applied {today}")
        # Our own write changed the file. Other waiting entries for it were
        # written against the same old hash, so re-stamp those: otherwise a half-ticked
        # review would call its own earlier apply "someone changed this".
        text = _restamp_hash(text, item.target, before_hash, sha(tp))
        rep.applied.append(label)

    _atomic_write(path, text)
    return rep


def _restamp_hash(text: str, target: str, old_hash: str, new_hash: str) -> str:
    out = []
    for l in text.splitlines():
        m = _REVIEW_HDR_RE.match(l)
        if m:
            j = json.loads(m.group("json"))
            if j.get("target") == target and j.get("hash") == old_hash:
                j["hash"] = new_hash
                l = f"<!-- review {json.dumps(j, sort_keys=True)} -->"
        out.append(l)
    return "\n".join(out) + "\n"


def _refind(text: str, key: str) -> ReviewItem | None:
    return next((i for i in parse_review(text) if i.key == key), None)


def _number_of(text: str, item: ReviewItem) -> str:
    first = text.splitlines()[item.start]
    m = re.match(r"^###\s+(\d+)\.", first)
    return m.group(1) if m else "0"
