"""Block-level ledger of which LEARNINGS dream has already consolidated.

Named `learnings_ledger`, not `dream_ledger`, on purpose. A `dream_ledger.py`
was proposed once before as a **sidecar decision record** — which item of a
proposal was applied or rejected — and abandoned when the move-to-processed
contract shipped instead (`## Processed` in the proposal file itself, mirrored
in multiplai-gui's `hub/src/multiplai_hub/dreams.py`). That decision stands and
this module does not revisit it: it tracks dream's **input**, never its output.
Nothing here records a decision, and nothing here is read by the GUI.


Dream used to glob ``.multiplai/learnings/*.md`` and process whatever it found,
all or nothing. Feeding it a subset therefore meant physically moving the other
files out and back, guarded by a shell ``trap … EXIT`` — which is not reliable:
a killed run on 2026-07-31 left four files (204 KB) stranded in a
``.parked-backup/`` directory, invisible to every later run until someone
noticed. Each hand-batched slice also produced its own proposal, so the reviewer
was left reconciling several overlapping documents.

The ledger replaces all of that. This module **never moves or deletes** a learnings file; instead each ``## Session Learnings`` record is hashed, and dream
consolidates only the records whose hash it has not seen. "Process what's new"
becomes a set difference, a killed run resumes instead of redoing, and repeated
runs are idempotent.

Nothing deletes a learnings file. ``dream --auto`` after a successful apply, and
``dream --gc-learnings``, move a file into ``learnings/archived/``; the latter
only once every record in it is recorded here **and** no proposal citing it is
still pending. :func:`prune` then drops the orphaned keys.

Concurrency: callers hold dream's exclusive run lock, so the read-modify-write in
:func:`record` needs no locking of its own. The write is still atomic (temp file
+ ``os.replace``) because a kill mid-write would otherwise corrupt the one file
that says what has already been consolidated — losing it silently re-processes
the entire backlog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 2 — keys are hashed from a normalized projection rather than the raw record
# text, so a change to how a learning is *rendered* no longer changes its key.
LEDGER_VERSION = 2

# A learnings record opens with this heading and runs to the next one. The
# writer (lib/extraction.py) appends records separated by a `---` rule; the
# separator and any preamble before the first heading belong to no record.
_BLOCK_START_RE = re.compile(r"^##\s+Session Learnings\b")
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")

# The label markers extraction writes ahead of a learning's description:
#   - **[CORRECTION/FACT]** <desc> …            (two-axis taxonomy)
#   - **[trust: verified]** CORRECTION <desc> …  (legacy single axis)
# Both are stripped for hashing. What identifies a learning is what it SAYS —
# its description, target and action — not how it was labelled, so relabelling
# the same knowledge must not present it to the reviewer a second time.
# The legacy type word is stripped only after the `trust:` arm, because it only
# ever followed that arm. Applying it after both meant a *new*-form learning
# whose description happened to begin with one of those six caps words —
# "**[EMPIRICAL/FACT]** OBSERVATION about the router." — projected to the same
# string as the same learning without it, so the two hashed identically and the
# second was silently treated as already consolidated.
_LEARNING_MARKER_RE = re.compile(
    r"^(?P<bullet>-\s+)\*\*\[(?:"
    r"[A-Z?][A-Z?-]*/[A-Z?][A-Z?-]*\]\*\*\s+"
    r"|"
    r"trust:\s*\w+\]\*\*\s+"
    r"(?:(?:OBSERVATION|PREFERENCE|CORRECTION|PATTERN|RULE-PROPOSAL|INTENTION)\s+)?"
    r")"
)

# 64 bits of key. The corpus is thousands of records, not billions, and a key
# collision would silently drop one learning — 16 hex chars keeps the ledger
# readable while leaving that risk far below the odds of the extractor writing
# the same record twice.
_KEY_CHARS = 16


@dataclass(frozen=True)
class Block:
    """One ``## Session Learnings`` record, located in its source file.

    ``start_line``/``end_line`` are 1-indexed and inclusive, matching what an
    editor shows and what dream's ``**Source:**`` provenance cites. They are
    carried so a chunk can be rebuilt with its ORIGINAL line numbers — renumbering
    a chunk would silently corrupt every provenance line drawn from it.
    """

    file: str
    start_line: int
    end_line: int
    text: str
    key: str
    legacy_key: str = ""


def _normalize(text: str) -> str:
    """Canonical form for hashing: trailing whitespace stripped per line.

    Keys must survive a whitespace-only reformat of a learnings file, or a
    stray editor save would orphan every key in it and re-process the lot.
    Content changes still change the key — an edited record is a new record,
    which is the conservative direction (re-consolidate rather than skip).
    """
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _projection(text: str) -> str:
    """The part of a record that identifies it: everything but its labels.

    Each learning line keeps its description, target and action and loses its
    ``**[…]**`` marker. Everything else in the record — the heading, the
    ``Session:`` line — is passed through unchanged.

    This is what makes the key survive a rendering change. Without it, adding
    the provenance/kind marker to the line format would have re-keyed every one
    of the pending records at once, and dream would have re-proposed the entire
    consolidated backlog as though it were new.
    """
    return "\n".join(
        _LEARNING_MARKER_RE.sub(r"\g<bullet>", line)
        for line in _normalize(text).splitlines()
    )


def block_key(text: str) -> str:
    """Stable key for a record, hashed from :func:`_projection`.

    Deliberately does NOT include the source filename: the same record text in
    two files is the same learning (the extractor can duplicate one across a
    day boundary), and consolidating it twice would put a duplicate entry in
    front of the reviewer.
    """
    digest = hashlib.sha256(_projection(text).encode("utf-8")).hexdigest()
    return digest[:_KEY_CHARS]


def legacy_block_key(text: str) -> str:
    """The key scheme used before :data:`LEDGER_VERSION` 2 — raw record text.

    Every key written by an earlier version was computed this way, and those
    keys are the record of a backlog that has already been consolidated. They
    are recognised on read (see :func:`unprocessed`) so an upgrade does not
    re-propose work a human has already reviewed.
    """
    digest = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()
    return digest[:_KEY_CHARS]


def parse_blocks(file_name: str, text: str) -> list[Block]:
    """Split one learnings file into its records, in file order.

    Anything before the first ``## Session Learnings`` heading is preamble and
    is dropped: it carries no learning, and including it would make every key in
    the file depend on the file's header.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _BLOCK_START_RE.match(line)]
    if not starts:
        return []

    blocks: list[Block] = []
    bounds = starts + [len(lines)]
    for start, next_start in zip(bounds, bounds[1:]):
        end = next_start - 1
        # Trim the separator rule and blank lines that belong to the gap
        # between records, not to this one.
        while end > start and (
            not lines[end].strip() or _SEPARATOR_RE.match(lines[end])
        ):
            end -= 1
        body = "\n".join(lines[start:end + 1])
        blocks.append(
            Block(
                file=file_name,
                start_line=start + 1,
                end_line=end + 1,
                text=body,
                key=block_key(body),
                legacy_key=legacy_block_key(body),
            )
        )
    return blocks


def default_ledger_path() -> Path:
    """Ledger location: the git-ignored per-skill state bucket.

    ``skill_state_dir`` creates the directory and drops the ``data_dir``
    ``.gitignore`` on first access, so the ledger never lands in a commit.
    Imported lazily so this module stays usable in tests without a resolved
    workspace.
    """
    from multiplai_core.paths import get_paths

    return get_paths().skill_state_dir("dream") / "ledger.json"


def load(path: Path) -> dict:
    """Read the ledger, or an empty one.

    Fail-open on a missing, empty or corrupt file: an unreadable ledger must
    degrade to "nothing processed yet" (re-consolidating is wasteful but safe)
    rather than crash the run and leave the backlog untouched forever.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": LEDGER_VERSION, "processed": {}}
    if not isinstance(data, dict) or not isinstance(data.get("processed"), dict):
        return {"version": LEDGER_VERSION, "processed": {}}
    data.setdefault("version", LEDGER_VERSION)
    return data


def save(path: Path, ledger: dict) -> None:
    """Atomically write the ledger (temp file in the same dir + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(ledger, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def unprocessed(blocks: list[Block], ledger: dict) -> list[Block]:
    """Records not yet consolidated, in input order, de-duplicated by key.

    Two identical records in the same batch collapse to one — otherwise the
    reviewer sees the same proposed memory entry twice.

    A block counts as seen under **either** key scheme. A ledger written before
    version 2 holds :func:`legacy_block_key` values, and those records have
    already been through a human review; treating them as new because the key
    function changed underneath them would put the whole consolidated backlog
    back in front of the reviewer. Old keys are left in place rather than
    rewritten — ``prune`` drops them when their file goes, and until then they
    cost a few bytes and answer correctly.
    """
    seen = ledger.get("processed", {})
    out: list[Block] = []
    batch: set[str] = set()
    for b in blocks:
        if b.key in seen or (b.legacy_key and b.legacy_key in seen):
            continue
        if b.key in batch:
            continue
        batch.add(b.key)
        out.append(b)
    return out


def migrate(ledger: dict, blocks: list[Block]) -> int:
    """Re-key pre-version-2 entries onto projection keys. Returns how many moved.

    Mutates *ledger* in place and does not touch disk — the caller decides
    whether to persist, because writing the ledger outside dream's run lock
    could clobber a key a concurrent run had just recorded.

    Migration needs the records themselves: a legacy key is a hash of the raw
    text and cannot be converted into a projection key without the text it was
    computed from. The learnings files on disk are that text, which is why this
    takes *blocks*. Records whose file has since been deleted keep their legacy
    key and are dropped by :func:`prune` in the ordinary way.

    Idempotent, and safe to run against an already-migrated ledger: a block
    whose projection key is present is left alone.

    **The legacy key is left in place as an alias, not popped.** Every
    ``Block``-keyed read goes through :func:`lookup` and would be fine either
    way, but staged-draft sidecars store *raw key strings* — a crashed run
    wrote ``[b.key for b in chunk.blocks]``, which under a v1 ledger are legacy
    keys — and ``dream._resume_staged_drafts`` tests raw membership. Popping
    the legacy key made that test fail after an upgrade, which **deleted the
    staged draft** while ``unprocessed`` correctly still saw the record as
    consumed: the drafted content was destroyed and the learning stayed marked
    consolidated forever. That is precisely the "silent learning loss" the
    ledger-coverage check exists to prevent, and the same crash-then-resume
    worked before version 2. :func:`prune` removes both keys when the record
    leaves disk, so the alias costs one ledger line until then, and
    ``unprocessed``'s docstring already promised this ("Old keys are left in
    place rather than rewritten") — it was ``migrate`` that contradicted it.
    """
    processed = ledger.get("processed")
    if not isinstance(processed, dict):
        return 0
    moved = 0
    for b in blocks:
        if not b.legacy_key or b.legacy_key == b.key:
            continue
        if b.key in processed or b.legacy_key not in processed:
            continue
        processed[b.key] = processed[b.legacy_key]
        moved += 1
    ledger["version"] = LEDGER_VERSION
    return moved


def lookup(processed: dict, block: Block) -> dict | None:
    """The ledger entry for *block* under either key scheme, or ``None``.

    Callers that need the entry itself — which proposal consumed the record —
    must go through this rather than indexing ``processed[block.key]``, or a
    ledger still carrying pre-version-2 keys reads as "never consolidated".
    """
    entry = processed.get(block.key)
    if entry is None and block.legacy_key:
        entry = processed.get(block.legacy_key)
    return entry


def record(path: Path, blocks: list[Block], proposal: str) -> int:
    """Mark *blocks* consolidated into *proposal*. Returns how many were new.

    Called per chunk, as each draft lands, rather than once at the end: a crash
    then costs only the in-flight chunks, and the next run resumes with the rest.
    """
    if not blocks:
        return 0
    ledger = load(path)
    processed = ledger.setdefault("processed", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for b in blocks:
        if b.key in processed or (b.legacy_key and b.legacy_key in processed):
            continue
        processed[b.key] = {"file": b.file, "proposal": proposal, "at": now}
        added += 1
    save(path, ledger)
    return added


def prune(path: Path, existing_files: set[str]) -> int:
    """Drop keys whose source file is gone. Returns how many were removed.

    ``--auto`` and ``/dream-remember`` delete learnings files once applied; their
    keys would otherwise accumulate forever. Pruning is safe because a deleted
    file's records cannot come back — and if one did (restored from git), it is
    genuinely new input to consolidate again.
    """
    ledger = load(path)
    processed = ledger.get("processed", {})
    stale = [k for k, v in processed.items() if v.get("file") not in existing_files]
    for k in stale:
        del processed[k]
    if stale:
        save(path, ledger)
    return len(stale)
