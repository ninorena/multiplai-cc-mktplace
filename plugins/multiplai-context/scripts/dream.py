"""Dream consolidation script for multiplai plugin.

Default mode (no flags): generates a human-readable change proposal and writes it
to .multiplai/dreams/ for review. Run /multiplai-context:dream-remember to apply.

--auto: fully autonomous — applies changes directly to memory files without review.
--check: report pending learnings count, chunk plan and predicted duration, and exit.
--gc-learnings: move learnings files that are fully consolidated and fully
    decided into learnings/archived/. Pure code, no model call.

The report path is a batching pipeline, not one big call: learnings are parsed
into `## Session Learnings` blocks, filtered against a ledger of what has already
been consolidated, packed into timeout-sized chunks, drafted concurrently, and
merged deterministically into ONE document. The report path never moves or
deletes learnings files — the ledger, not the filesystem, is what says "already
done". Nothing deletes a learnings file anywhere. Two places move one into
learnings/archived/: `--auto` after a successful apply, and the explicit
`--gc-learnings` subcommand.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The per-CALL ceiling, and the number chunk sizes are derived from. Defined in
# `lib.dream_chunking` (pure, import-side-effect-free) so a supervisor can read
# it without importing this module — see that constant's comment for why 900.
#
# Imported here BEFORE model_client, and used to seed the SDK's own env knob in
# the same breath: the SDK reads that variable into a module constant at import
# time, so the setdefault has to precede it. `setdefault` preserves an explicit
# override; deriving the value from the constant means the two can no longer
# drift apart.
from lib.dream_chunking import CHUNK_TIMEOUT_S

os.environ.setdefault("MULTIPLAI_SDK_CALL_TIMEOUT_S", str(int(CHUNK_TIMEOUT_S)))

from multiplai_core.paths import get_paths
from multiplai_core.model_client import create_client
from multiplai_core.config import load_yaml, save_yaml
from multiplai_core.log_utils import setup_logging
from generators.config import load_catalog_config
from generators.dispatcher import generate_catalogs
from lib import citation_repair, daily_review, learnings_ledger, principles, taxonomy
from lib.dream_processed import (
    PROCESSED_HEADING,
    Decision,
    archive_disposition,
    has_pending_conflicts,
    has_pending_items,
    latest_pending_proposal as _latest_pending_proposal,
    mark_many_processed,
    mark_processed,
)

logger = setup_logging("dream", propagate_loggers=("multiplai_core",))

# Each chunk spawns a Claude Code CLI subprocess. Unbounded fan-out over a dozen
# chunks is a subprocess storm, so the gather runs behind a semaphore.
#
# 8, not 4, because 4 cannot finish a real backlog in a tolerable time. Measured
# on the 283 KB fixture: 48.9 B/s over 19 chunks is 5,875 s of model work, which
# four workers cannot do in under 24m28s however it is scheduled — the run took
# 37m55s end to end. Eight halves the floor to ~12m14s (bounded below by the one
# slowest chunk, 556 s), and the critic's to ~4m (bounded by its slowest batch,
# 465 s).
#
# The cost is eight concurrent CLI subprocesses on a machine that is usually also
# running the user's own session. That is the trade being made deliberately; the
# semaphore still bounds it, and MULTIPLAI_DREAM_CONCURRENCY lowers it for anyone
# who would rather wait than share the CPU.
DEFAULT_CONCURRENCY = 8

# One retry, not a ladder. A 429 here is self-inflicted burstiness that clears in
# under a minute; if it is instead a real quota exhaustion, retrying repeatedly
# just converts a fast partial result into a slow one. 60 s is longer than the
# per-minute windows the CLI reports and short against the pass's own budget.
_RATE_LIMIT_ATTEMPTS = 2
_RATE_LIMIT_BACKOFF_S = 60.0

# Matched on the rendered exception text because that is all the SDK guarantees
# to be stable — `multiplai_core` surfaces the CLI's error as a plain exception,
# and the log line this was diagnosed from is the same string. A false positive
# costs one 60 s retry and never a wrong result, so the match errs wide.
_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "too many requests", "429")


def _is_rate_limit(exc: BaseException) -> bool:
    """True when *exc* looks like a rate limit rather than a real failure."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)

# Weight of the newest observation in the throughput EWMA. Low enough that one
# slow chunk (a cold model, a retried call) does not swing the next run's plan.
_THROUGHPUT_EWMA_ALPHA = 0.3
# Guard rails on the calibrated value: an outlier that poisons the stored EWMA
# would mis-size every future chunk, and the failure is silent.
_THROUGHPUT_MIN, _THROUGHPUT_MAX = 10.0, 2000.0

# Set once if the installed multiplai-core predates `query(timeout_s=…)`
# (< v0.12.0), so the fallback is logged once rather than per chunk.
_TIMEOUT_KWARG_UNSUPPORTED = False

# Holds the run lock's fd for the process lifetime. Closing it — including by
# letting it be garbage-collected — releases the lock, so it must stay reachable.
_RUN_LOCK_FD: int | None = None


# ---------------------------------------------------------------------------
# Run state — lock, staging area, throughput calibration
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Dream's git-ignored state bucket: lock, ledger, staged chunk drafts."""
    return get_paths().skill_state_dir("dream")


def _lock_path() -> Path:
    return _state_dir() / "lock"


def acquire_run_lock() -> bool:
    """Take dream's exclusive run lock. ``True`` if this process may proceed.

    Non-blocking ``flock`` on ``skill_state_dir("dream")/lock`` — **in the
    workspace, deliberately not in ``/tmp``**. Every Claude session runs in its
    own OrbStack container, so a ``/tmp`` path is container-local: two sessions
    invoking ``/dream`` concurrently — the exact scenario this exists for —
    would lock two *different* files and both proceed. The workspace is one
    shared filesystem inside the VM's single kernel, so a lock there really does
    exclude across session containers, and it keys by workspace for free.
    (``scripts/qmd_refresh.py`` keys on ``/tmp`` and has this hole; do not copy
    it.)

    The lock is held for the whole run — fold, draft, merge, critic, and the
    proposal write — and is **released by the OS when the process dies**. There
    is therefore no stale-lock class of bug and no cleanup path to get wrong:
    nothing unlinks the file, nothing has to run on the crash path, and a killed
    run leaves a lock that is already free. The fd is parked in a module global
    for exactly that reason — closing it would release the lock early.

    Residual limit: a **host-side** process (a dream run from a Mac terminal, or
    the multiplai-gui hub) locks in a different kernel and is not excluded.
    Accepted — the hub only ever invokes ``--stamp``/``--archive``, which do not
    take this lock at all, and host-terminal dream runs are not part of the
    workflow.

    On contention the holder's start time is read back out of the lock file and
    reported; the caller exits 0, because a second run is a no-op, not an error.
    Fails **open** if the lock file cannot be opened or locked at all (read-only
    or flock-less filesystem): refusing to consolidate is worse than the race
    the lock prevents.
    """
    global _RUN_LOCK_FD
    path = _lock_path()
    try:
        # No O_TRUNC: truncating before the flock attempt would erase the
        # holder's timestamp — the one thing the contention message needs.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        logger.exception("Could not open dream run lock %s — proceeding unlocked", path)
        return True

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 256).decode("utf-8", "replace").strip()
        except OSError:
            holder = ""
        os.close(fd)
        msg = (
            "Another dream run is already in progress"
            + (f" (started {holder})" if holder else "")
            + " — nothing to do."
        )
        logger.info(msg)
        print(msg)
        return False
    except OSError:
        os.close(fd)
        logger.exception("flock unavailable on %s — proceeding unlocked", path)
        return True

    try:
        os.ftruncate(fd, 0)
        os.write(fd, (datetime.now(timezone.utc).isoformat() + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError:
        logger.warning("Could not stamp the run lock with a start time", exc_info=True)
    _RUN_LOCK_FD = fd
    return True


def _dream_state_path() -> Path:
    """Calibration state, kept beside the ledger in the skill state bucket.

    Deliberately NOT ``paths.dream_state_file()``: that file is the *gate* state
    (``last_run``, ``files_updated``) that ``--stamp`` and the session hooks
    read, and a run's throughput measurements have no business landing in a
    record other tools poll for "has dream run recently".
    """
    return _state_dir() / "dream_state.yaml"


def _calibrated_throughput() -> float | None:
    """Stored input-bytes-per-second EWMA, or ``None`` if not calibrated yet."""
    try:
        state = load_yaml(_dream_state_path()) or {}
        value = state.get("throughput_bytes_per_s")
    except Exception:
        return None
    if isinstance(value, (int, float)) and _THROUGHPUT_MIN <= value <= _THROUGHPUT_MAX:
        return float(value)
    return None


def _update_throughput(observed: float) -> None:
    """Fold one chunk's observed B/s into the stored EWMA.

    Self-calibration matters because the 85 B/s default is one machine's
    measurement; a slower model or host silently makes every estimate a lie, and
    the estimate is what decides chunk size.
    """
    if not observed or observed <= 0:
        return
    observed = min(max(observed, _THROUGHPUT_MIN), _THROUGHPUT_MAX)
    try:
        path = _dream_state_path()
        state = load_yaml(path) or {}
        prior = _calibrated_throughput()
        value = observed if prior is None else (
            (1 - _THROUGHPUT_EWMA_ALPHA) * prior + _THROUGHPUT_EWMA_ALPHA * observed
        )
        state["throughput_bytes_per_s"] = round(value, 1)
        save_yaml(path, state)
    except Exception:
        logger.warning("Could not update throughput calibration", exc_info=True)


def _runs_dir() -> Path:
    """Staging area for per-chunk drafts: ``.../dream/runs/<run-id>/``."""
    return _state_dir() / "runs"


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _stage_draft(run_dir: Path, index: int, text: str, keys: list[str], kind: str = "chunk") -> Path:
    """Persist one draft plus the block keys it consolidated.

    Written **before** the ledger records those keys. Crashing in between costs
    a re-draft (the resume path discards a staged draft whose blocks are not
    ledgered), which is the safe direction: duplicated work, never lost input.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    md = run_dir / f"{kind}-{index:02d}.md"
    md.write_text(text)
    (run_dir / f"{kind}-{index:02d}.json").write_text(
        json.dumps({"kind": kind, "keys": list(keys)})
    )
    return md


def _resume_staged_drafts(current_run_dir: Path, ledger: dict) -> list[str]:
    """Drafts left behind by a crashed prior run, ready to join this merge.

    A staged draft is included only when every block it consolidated is recorded
    in the ledger. If it is not, those blocks are still `unprocessed` and are
    being re-drafted right now — keeping the stale draft too would put the same
    learning in front of the reviewer twice, so it is deleted instead.
    """
    out: list[str] = []
    runs = _runs_dir()
    if not runs.is_dir():
        return out
    processed = ledger.get("processed", {})
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        if run_dir == current_run_dir:
            continue
        for md in sorted(run_dir.glob("*.md")):
            side = md.with_suffix(".json")
            try:
                keys = list(json.loads(side.read_text()).get("keys") or [])
            except (OSError, json.JSONDecodeError):
                # An unreadable sidecar means "unknown blocks", not "no draft":
                # keep the content and let the ledger's own dedup handle any
                # overlap. Dropping it would be the one irreversible choice.
                logger.warning("Staged draft %s has no readable sidecar — kept anyway", md)
                keys = []
            if keys and not all(k in processed for k in keys):
                logger.warning(
                    "Discarding staged draft %s — its blocks are not ledgered, "
                    "so they are being re-drafted this run", md,
                )
                md.unlink(missing_ok=True)
                side.unlink(missing_ok=True)
                continue
            try:
                out.append(md.read_text())
            except OSError:
                logger.error("Unreadable staged draft %s — skipped", md, exc_info=True)
                continue
            logger.info("Resuming staged draft from an interrupted run: %s", md)
    return out


def _clear_staging() -> None:
    """Drop every staged run directory once their content is in a written proposal."""
    runs = _runs_dir()
    if not runs.is_dir():
        return
    for run_dir in runs.iterdir():
        if run_dir.is_dir():
            shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Learnings I/O
# ---------------------------------------------------------------------------

def _read_all_learnings(learnings_dir: Path) -> tuple[str, list[Path]]:
    """Read all pending learnings files. Returns (combined_text, source_files).

    Each content line is prefixed with its 1-indexed line number (matching what an editor
    shows for that file) so the model can cite `filename:line` provenance accurately rather
    than guessing — line numbers it can't see are line numbers it would fabricate.
    """
    if not learnings_dir.exists():
        return "", []
    files = sorted(learnings_dir.glob("*.md"))
    if not files:
        return "", []
    parts = []
    for f in files:
        raw = f.read_text()
        if not raw.strip():
            continue
        numbered = "\n".join(
            f"{i}: {line}" for i, line in enumerate(raw.splitlines(), start=1)
        )
        parts.append(f"### File: {f.name}\n\n{numbered}")
    combined = "\n\n---\n\n".join(parts)
    return combined, files


def _proposal_output_path(dreams_dir: Path, today: str) -> Path:
    """Return a non-colliding path for today's proposal.

    A same-day dream run (scheduled, or kicked off in parallel) must never
    silently overwrite a proposal that may be mid-review in dream-remember —
    that's silent data loss and forces a full re-generation (observed
    2026-06-21). If the base name is free, use it; otherwise append an
    incrementing counter so the prior proposal survives untouched.
    dream-remember globs `processed-learnings-*.md` and takes the most recent
    by mtime, so the versioned name is still discovered first.
    """
    base = dreams_dir / f"processed-learnings-{today}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = dreams_dir / f"processed-learnings-{today}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def _archive_proposal(proposal_path: Path, dreams_dir: Path, disposition: str = "applied") -> Path:
    """Move a reviewed proposal out of the dreams root into `applied/` (or
    `rejected/`), so the root holds only pending proposals.

    Collision-safe: `_proposal_output_path` only checks the dreams root, so a
    same-day re-run reuses a base name freed by an earlier archive. Archiving
    that second proposal must not clobber the first — suffix like the root
    naming does. A plain rename is used even in git-tracked workspaces: git
    detects the rename at the next commit, and `git mv` would fail on the
    (typically untracked) freshly generated proposal.
    """
    dest_dir = dreams_dir / disposition
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / proposal_path.name
    if dest.exists():
        stem, suffix = proposal_path.stem, proposal_path.suffix
        n = 2
        while (dest_dir / f"{stem}-{n}{suffix}").exists():
            n += 1
        dest = dest_dir / f"{stem}-{n}{suffix}"
    proposal_path.rename(dest)
    return dest


def _collect_blocks(learnings_dir: Path) -> tuple[list, list[Path]]:
    """Parse every learnings file into ``## Session Learnings`` blocks.

    Returns ``(blocks, files)`` in filename order. Nothing is moved or deleted:
    which blocks are new is a set difference against the ledger, not a question
    about which files happen to be on disk.
    """
    if not learnings_dir.exists():
        return [], []
    files = sorted(learnings_dir.glob("*.md"))
    blocks: list = []
    for f in files:
        try:
            blocks.extend(learnings_ledger.parse_blocks(f.name, f.read_text()))
        except OSError:
            logger.warning("Could not read %s — skipped", f, exc_info=True)
    return blocks, files


# ---------------------------------------------------------------------------
# "dream wrote this, untouched" stamp
# ---------------------------------------------------------------------------
#
# Spike curates a proposal by editing the file directly — deleting whole
# sections and individual items so that what remains is exactly what he wants
# applied. A curated proposal has no `## Processed` items, so the fold in
# `_fold_pending_proposals` would happily absorb it and destroy the curation.
# The stamp is the guard: fold only a document that is byte-identical to what
# dream wrote.
#
# Content hash, NOT mtime: `.multiplai/dreams/` is git-tracked, and
# checkout/stash/restore rewrite mtimes — an mtime guard would read untouched
# proposals as hand-edited and start exactly the `-2` pileup the fold exists to
# stop. "Differs from what dream wrote" is the real criterion, so hash it.

_GENERATED_MARKER_RE = re.compile(r"^<!-- dream:generated sha256=([0-9a-f]{64}) -->$")


def _stamp_generated(text: str) -> str:
    """Append the ``<!-- dream:generated sha256=… -->`` trailer to a proposal."""
    body = text.rstrip("\n") + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{body}<!-- dream:generated sha256={digest} -->\n"


def _is_unmodified_generated(text: str) -> bool:
    """True only if *text* still hashes to the value in its own trailer.

    A proposal written by a pre-ledger dream carries no trailer and can never
    pass — by design. Leaving those alone is the safe default, not a bug.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return False
    m = _GENERATED_MARKER_RE.match(lines[-1].rstrip("\n"))
    if not m:
        return False
    body = "".join(lines[:-1])
    return hashlib.sha256(body.encode("utf-8")).hexdigest() == m.group(1)


# Sections dream re-derives from current state on every run. A folded proposal
# carries yesterday's copies; merging them in would put two `## Routing Warnings`
# sections in one document, the stale one first.
#
# A *decided* conflict resolution is not lost to this, and needs no exemption
# here (issue #201). `_strip_regenerated` runs only inside
# `_fold_pending_proposals`, which folds nothing that `_has_decided_items`
# rejects — and marking a conflict processed moves its block under
# `## Processed`, which is exactly what that predicate looks for. So the
# proposal stops being foldable the moment any conflict in it is dispositioned,
# and the decision survives. Adding a second mechanism here would be a
# same-question-answered-twice hazard, not extra safety.
#
# An *undecided* one is a different story, and the reason `--reconcile` has its
# own predicate (`_reconcile_is_finished`). "Regenerated" does not mean
# "regenerated forever": `_with_conflict_resolutions` is fed `pending_text` —
# only the learnings blocks *not* yet in the ledger — so a conflict can be
# re-derived only while the block it came from is still unconsolidated. The run
# that first drafts it ledgers that block, so from the next run on there is
# nothing left to derive it from. Between that and the strip above, an
# undecided conflict that leaves the dreams root (folded forward, or archived)
# is gone for good. Nothing brings it back.
_REGENERATED_SECTIONS = (
    "## Routing Warnings", "## Conflict Resolutions", "## Rules Re-learned",
)


def _strip_regenerated(text: str) -> str:
    """Reduce a finished proposal back to a draft, ready to merge.

    Drops the ``dream:generated`` trailer (it describes the file it was written
    into, not the content) and any deterministically regenerated section.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = any(line.startswith(h) for h in _REGENERATED_SECTIONS)
        if skipping:
            continue
        if _GENERATED_MARKER_RE.match(line):
            continue
        out.append(line)
    while out and (not out[-1].strip() or out[-1].strip().startswith("---")):
        out.pop()
    return "\n".join(out) + "\n"


def _has_decided_items(text: str) -> bool:
    """True if any item block sits under ``## Processed``.

    That heading is the cross-tool decision contract with multiplai-gui. A
    proposal carrying decisions is off-limits: folding it would discard them,
    and renumbering it would make a decision the hub has already queued apply to
    the wrong entry.
    """
    in_processed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == PROCESSED_HEADING:
            in_processed = True
            continue
        if stripped.startswith("## "):
            in_processed = False
            continue
        if in_processed and stripped.startswith("### "):
            return True
    return False


def _fold_verdict(text: str) -> tuple[bool, str]:
    """Whether a pending proposal may be folded, and why not when it may not."""
    if _has_decided_items(text):
        return False, "it carries decided items under `## Processed`"
    if not _is_unmodified_generated(text):
        return False, (
            "its content differs from what dream wrote (hand-curated, or written "
            "by a pre-ledger dream that left no `dream:generated` stamp)"
        )
    if not has_pending_items(text):
        return False, "it has no pending items to fold"
    return True, ""


def _fold_pending_proposals(dreams_dir: Path) -> list[str]:
    """Absorb undecided, untouched pending proposals into this run.

    Called at the START of the run, under the lock, before any drafting — not at
    write time. The hub writes `## Processed` blocks into a proposal *item by
    item* during an apply, so "has no decided items" is a claim that decays over
    a multi-minute run; checking first shrinks the window from minutes to
    milliseconds. The verdict is re-taken immediately before each individual
    move, inside the same lock hold, so a proposal that gained a decision during
    the scan is skipped rather than swallowed.

    Returns the folded documents' text, in filename order. The files themselves
    move to ``dreams/superseded/`` — a sibling of ``applied/``/``rejected/``,
    with the same collision-safe suffixing — so the hub's non-recursive glob of
    the dreams root stops listing them, exactly as archiving does.
    """
    folded: list[str] = []
    if not dreams_dir.is_dir():
        return folded
    for path in sorted(dreams_dir.glob("processed-learnings-*.md")):
        try:
            text = path.read_text()
        except OSError:
            logger.warning("Could not read pending proposal %s — left in place", path.name)
            continue
        ok, reason = _fold_verdict(text)
        if not ok:
            logger.info("Not folding %s — %s; left pending", path.name, reason)
            continue
        # Re-check against the file as it stands right now: the hub may have
        # written a decision into it since the scan above.
        try:
            fresh = path.read_text()
        except OSError:
            continue
        if fresh != text:
            logger.info("Not folding %s — it changed during the scan; left pending", path.name)
            continue
        ok, reason = _fold_verdict(fresh)
        if not ok:
            logger.info("Not folding %s — %s; left pending", path.name, reason)
            continue
        try:
            dest = _archive_proposal(path, dreams_dir, "superseded")
        except OSError:
            logger.exception("Could not supersede %s — left pending, not folded", path.name)
            continue
        folded.append(_strip_regenerated(fresh))
        logger.info("Folded undecided proposal %s into this run (moved to %s)",
                    path.name, dest)
    return folded


def _read_memory_files(memory_dir: Path) -> dict[str, str]:
    """Return {filename: content} for all .md files in memory_dir."""
    if not memory_dir.exists():
        return {}
    return {
        f.name: f.read_text()
        for f in sorted(memory_dir.glob("*.md"))
        if f.name != "learnings.md"
    }


def _extract_headers(content: str) -> str:
    """Return H1–H3 headers from markdown content."""
    headers = [l for l in content.split("\n") if l.startswith("#")]
    return "\n".join(headers) if headers else content[:300]


def _load_memory_catalog(catalogs_dir: Path) -> dict[str, dict]:
    """Return {filename: {summary, intent_domains, anti_domains}} from the memory catalog.

    The catalog (built by the router) carries each memory file's domain — its
    summary, intent_domains, and anti_domains (what does NOT belong there).
    Routing by that domain is far more reliable than guessing from
    section-header names, which makes broadly-named files (e.g.
    ai-agent-patterns.md) act as catch-alls. Returns {} if the catalog is absent
    or unreadable — the proposal then falls back to headers-only routing.
    """
    import json

    catalog_file = catalogs_dir / "memory.json"
    if not catalog_file.exists():
        return {}
    try:
        data = json.loads(catalog_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("entries", []):
        src = entry.get("source")
        if src:
            out[src] = {
                "summary": entry.get("summary", ""),
                "intent_domains": entry.get("intent_domains", []),
                "anti_domains": entry.get("anti_domains", []),
            }
    return out


# ---------------------------------------------------------------------------
# Report mode (default)
# ---------------------------------------------------------------------------

_PROPOSAL_SYSTEM = """\
You are a memory consolidation analyst for a personal Claude Code memory system.

## The one thing to understand

Every learning has exactly ONE of two dispositions. Choosing correctly IS the job:

- MEMORY (what you write to) — GENERALIZED, REUSABLE KNOWLEDGE: guidance that changes how a
  FUTURE, DIFFERENT task is done.
- FILTERED OUT — everything else, each under a bold reason heading. Two kinds dominate:
  - DIARY (already recorded elsewhere) — WHAT HAPPENED: facts, events, decisions, fixes, in
    chronological order. Never duplicate it.
  - **Toolchain change-request** — a concrete change the TOOLCHAIN ITSELF should make to its
    own code, config, or structure (the memory/dream/plugin system, file layout, scripts).
    That is engineering work, not knowledge to remember. It is not tracked anywhere: a change
    worth making gets made. Filter it out with that reason so the reviewer still sees it.

Your job is NOT to log this session. It is to DISTILL the pending learnings: most are diary
(drop), some are reusable knowledge (memory). A learning that only says "X happened" / "we
decided Y" / "fixed Z" is diary — drop it unless it contains a general lesson you can lift out.

The MEMORY-vs-CHANGE-REQUEST cut: if a learning says the system *should be changed* ("split
these files", "delete this orphan", "this script should also check X"), the change itself is
never memory. Then ask whether it ALSO carries a general principle that outlives the change —
one that would guide a DIFFERENT future situation after this task is done and forgotten:
- NO — pure cleanup with no transferable rule (e.g. "delete this stale orphan file", "remove
  this dead catalog reference") -> filter out, write nothing.
- YES — e.g. "before making ANY repo public, scrub+rotate secrets and strip employer content"
  (useful for the next repo), or "split memory files by retrieval domain, not topic affinity"
  (a design heuristic for the next file) -> keep the PRINCIPLE as a memory entry; the concrete
  change is still filtered out.
Memory is for knowledge that informs work, not a backlog of refactors — but a durable principle
earns its memory place even when the change that revealed it is dropped.

## Generalization transform (apply to every candidate)

Strip the point-in-time scaffolding, keep the transferable rule:

- DROP: commit hashes / SHAs; "committed as ...", "fixed in ...", "decided on <date>";
  finished-task residue ("update file X", "rename Y now"); one-off absolute paths;
  specific project / repo / file names UNLESS the lesson is genuinely scoped to that
  project and useless elsewhere.
- KEEP, phrased as conditional guidance: "When <situation>, do <action>, because
  <outcome>." Prefer this shape over a narrated fact.

## Litmus gate (decide keep vs filter)

For each candidate ask, in order:
1. "Does this ask the toolchain to change its own code/config/structure?" -> FILTER OUT under
   **Toolchain change-request** (unless step 2 also finds a durable principle in it).
2. "Facing a DIFFERENT but similar situation in the future, does this change what I'd do?"
   - YES, and it reads as transferable guidance -> KEEP (generalized) as memory.
   - It only records that something happened / was decided / was fixed -> FILTER OUT.
   - A true general lesson wrapped in specifics -> KEEP THE LESSON, DROP THE SPECIFICS.

## Examples

RAW: "npm install -g @anthropic-ai/claude-code is deprecated. Use
curl -fsSL https://claude.ai/install.sh | bash. Update multiplai-container Dockerfile."
KEEP: "Claude Code is no longer installed via npm; official method is
`curl -fsSL https://claude.ai/install.sh | bash`."
(Dropped "update the Dockerfile" — a one-time task, now done.)

RAW: "pluginConfigs key must be plugin@marketplace compound form; wrong key silently
falls back to the home-directory defaults with no error. Sideloaded plugins ignore
pluginConfigs — use CLAUDE_PLUGIN_OPTION_* env vars. Committed as a8cbec9."
KEEP: "`pluginConfigs` keys use the compound `plugin@marketplace` form; a wrong key
fails silently (falls back to defaults, no error). Sideloaded plugins (`--plugin-dir`)
ignore `pluginConfigs` — pass options via `CLAUDE_PLUGIN_OPTION_*` env vars instead."
(Dropped the specific fallback path and the commit SHA.)

RAW: "Decision (2026-06-15): multiplai-core, mktplace, and kit all going public.
Pre-public: scrub gho_ token from history + rotate; remove scalestack skill;
secret scan. De-personalization machinery deleted; identity moves to memory."
FILTER OUT as written — it's a dated decision + checklist (diary). If a reusable rule
exists, extract ONLY that: "Before making any repo public, scrub secrets from git
history AND rotate them, and strip employer-specific content."

RAW: "multiplai-plugin git fetch not run regularly; origin/main tracking ref weeks
stale. Always fetch before checking ahead/behind or assuming sync with remote."
KEEP: "Always `git fetch` before checking ahead/behind counts or assuming sync with a
remote — local tracking refs go stale."
(Dropped the multiplai-plugin framing; the lesson is general.)

RAW: "Delete the stale `.multiplai/memory/memory-catalog.json` orphan (the live catalog is
`.multiplai/data/catalogs/memory.json`)."
FILTERED OUT under **Toolchain change-request**, no memory entry.
(Pure cleanup — no rule that outlives the deletion.)

RAW: "Mixed-domain memory files (career facts + career strategy) degrade routing precision —
split memory files by retrieval domain, not topic affinity."
MEMORY (design heuristic, guides the next file too): "Split memory files by retrieval domain,
not topic affinity — mixed-domain files degrade routing precision."
(The concrete "split career-history vs career-strategy" is a change-request and is filtered
out; the heuristic behind it is what survives.)

RAW: "Decision: scrub gho_ token from kit history + rotate before going public; remove
scalestack skill (employer content)."
MEMORY (general principle, outlives these one-time tasks): "Before making any repo public,
scrub secrets from git history AND rotate them, and strip employer-specific content."
(The two specific tasks are change-requests — filtered out.)

## Output format

```
# Processed Learnings — {date}

**Sources:** {N} files, ~{M} entries
Generated by Dream. Review with `/multiplai-context:dream-remember` to apply.

---

## Updates for `{filename}`

### {N}. {short_title}
**Section:** {existing section or "New section"}
**Change:** add / update / replace
**Provenance:** {PROVENANCE}/{KIND}
> Exact text to insert (generalized, concise, ideally "When X, do Y")

**Source:** {learnings_file}:{line-number(s)}

---

## Filtered Out ({N} items)

**{reason}**
- {short title} — {reason} (Source: {learnings_file}:{line})
- {short title} — {reason} (Source: {learnings_file}:{line})

**{other reason}**
- {short title} — {reason} (Source: {learnings_file}:{line})
```

`## Filtered Out` is an audit trail, so it is EXACTLY ONE LINE PER DROPPED ITEM — grouped
under its reason, no sub-bullets, no quoted text, no explanation paragraphs. Every dropped
item gets its own line: a dropped learning is marked consolidated and never resurfaces, so
a bare count would make the drop silent and permanent. Reasons: diary/event-only, already
applied, too specific, task residue, superseded, no clear target file.

Title markers (prefix the {short_title}, none in the normal case):
- **[RULE-PROPOSAL]** — a change to CLAUDE.md behavioral rules; requires individual approval.
- **[warning low confidence]** — an item you are including despite weak/unverified support.

## Routing — pick the target file by DOMAIN, not by header keyword

Each candidate file is shown with PURPOSE, OWNS DOMAINS, and NOT HERE (its anti-domains).
Route each entry to the file whose domain actually owns the learning's SUBJECT, then pick a
section within it. The headers only choose the section — never the file. All file-specific
routing knowledge is in those blocks — apply these generic principles to them:
- Respect NOT HERE: when a file's NOT-HERE line names the learning's subject, that file is
  disqualified — route to the file whose PURPOSE owns the subject instead.
- No catch-alls: broadly-named files are never fallbacks. Route by what the lesson is ABOUT,
  not by which tool or agent happened to perform the work — "an agent ran the migration" does
  not make it an agent pattern.
- Portability test: would the knowledge survive switching away from this specific tool or
  platform? Tool/platform-agnostic principles go to the general craft/design file for their
  subject; knowledge about operating a specific tool or platform goes to that tool's file.
- If no file's domain fits, say so (propose a new file or filter) — do not force-fit into the
  nearest broadly-named file.

## Rules

- Group updates by target memory file. Do NOT print per-file learning counts, "seen Nx"
  repetition notes, or trust levels — they cost tokens and serve no reader.
- Each entry carries a **Provenance:** line stating the `PROVENANCE/KIND` pair of the
  learning it came from — where the knowledge came from, and what sort of thing it is.
  Copy the pair from the learning's `**[PROVENANCE/KIND]**` marker. Older learnings show
  `**[trust: …]** TYPE` instead and carry no pair; read those across as
  `CORRECTION` → `CORRECTION/FACT`, `PREFERENCE` → `DECLARATION/FACT`,
  `INTENTION` → `DECLARATION/INTENTION`, `RULE-PROPOSAL` → `?/RULE`,
  `OBSERVATION` and `PATTERN` → `INFERENCE/FACT`. Write `?` for a half you genuinely
  cannot read off the source — never guess one. When an entry merges learnings that
  disagree, state the WEAKEST provenance (weakest to strongest: @PROVENANCE_STRENGTH@)
  and the BROADEST kind (broadest to narrowest: @KIND_BREADTH@). Nothing acts on this
  line yet — it is recorded so the pair stays traceable back to the session it came from.
- Each entry ends with a **Source:** line for provenance: the learnings filename and the
  line number(s) it was distilled from, so the origin is traceable on re-processing. The
  pending learnings are shown with `N: ` line-number prefixes — cite those exact numbers.
  Format `filename:line` or `filename:start-end`; if an entry merges several learnings, cite
  each separated by `; ` (e.g. `2026-06-15.md:42; 2026-06-16.md:10-12`). Cite only numbers
  you actually see — never invent a line number.
- Deduplicate: if the same lesson appears multiple times, merge into one entry. (Don't
  annotate the count.)
- Resolve contradictions: keep the most recent / most reliable version; note what was superseded.
- **Date volatile facts.** If a proposed line makes a claim about the *present* that names a
  changeable value — a price or rate, a version or model generation, who someone works for,
  "the current/latest/best X" — append `(as of YYYY-MM)`, and add `, review by YYYY-MM` when
  you can name a date by which it should be re-checked. Memory files carry only a file-level
  freshness stamp, so an undated volatile fact reads as true forever. Do NOT annotate
  permanent facts that merely mention a number ("Swift 6.3 rejects covariant Self" is not a
  volatile fact), and do NOT annotate a fact already marked historical.
- Most learnings are verified — do NOT label them. Filter out genuinely junk low-trust
  single-occurrence items. If you DO include a weakly-supported item, prefix its title with
  **[warning low confidence]** instead of dropping it.
- Filter out: diary/event-only entries, finished-task residue, already-applied facts,
  one-time fixes with no general pattern, entries with no clear target file. List each
  dropped item on its own single line under its reason heading — one line, never more.
- DROP — into Filtered Out, under the reason heading **Toolchain change-request** — any
  learning that calls for the toolchain to change its own code/config/file-structure ("split
  these files", "delete this orphan", "this script should also check X"). It is not memory and
  it is not tracked anywhere: a change worth making gets made, and one that isn't doesn't need
  a record. Keep it out of memory even so — a change-request filed as guidance tells a future
  session to do work that may already be done.
  The ONE exception: if it also carries a general principle that outlives the change (one that
  would guide a different future situation), keep that principle as a memory entry. If the
  principle is just the change restated, drop it and write nothing.
- Number entries from 1 within your own output and do not worry about collisions with other
  chunks. You are one of several parallel drafters and cannot see the others' counts; the merge
  step assigns the final `1..N` sequence across the whole proposal.
- Keep proposed text concise — one-line bullets over paragraphs. Memory costs tokens.
- Never invent changes not supported by the learnings.
"""

# The two rankings are substituted rather than written out, so this prompt
# cannot teach an ordering the code has moved on from. A sentinel rather than
# str.format: the prompt is full of literal `{N}`-style placeholders the model
# is meant to see.
_PROPOSAL_SYSTEM = (
    _PROPOSAL_SYSTEM
    .replace("@PROVENANCE_STRENGTH@", taxonomy.render_ranking(taxonomy.PROVENANCE_STRENGTH))
    .replace("@KIND_BREADTH@", taxonomy.render_ranking(taxonomy.KIND_BREADTH))
)


_BANK_ROUTING_RULES = (
    "### Shared memory banks — routing rules\n"
    "The files listed below live in a SHARED bank: a git repository other "
    "people also read and write. Three rules, in order:\n"
    "1. **Bias personal.** Route an entry to a shared bank ONLY when that "
    "bank's declared domains actually own the subject. When in doubt, or when "
    "a personal file also fits, choose the personal file.\n"
    "2. **Never route anything personal there.** No names, no compensation, "
    "no health, no finances, no household detail, no credential, nothing "
    "about a specific individual. A shared bank is read by people who are not "
    "the user.\n"
    "3. **A shared-bank entry is a contribution, not a write.** It becomes a "
    "pull request the bank's owners review. Write it so a stranger on that "
    "team can act on it without the surrounding session.\n"
    "Target a bank file by its full reference, e.g. "
    "``## Updates for `teamname/dev.md` ``.\n"
)


def _bank_memory_context() -> str:
    """The shared-bank half of the routing block, fenced as untrusted.

    Only **headers and catalog metadata** are included — never file bodies.
    Two reasons, both load-bearing: a bank can be large and this block is
    repeated in every chunk of a run, and every byte of it is text somebody
    else wrote. What is here is enough to route to a file and a section, which
    is all this block is for.

    The fence is required by ``docs/untrusted-content.md`` and by the threat
    this whole feature introduces: a bank is a repo whose contents arrive on a
    schedule, so a section header is an attacker-authored string reaching a
    prompt. The drafter has no tools, and the write floor refuses a shared
    target regardless of what the drafter decides — the fence is the first of
    those three layers, not the only one.
    """
    from multiplai_core.untrusted import fence, markdown_notice

    from lib.banks import shared_banks

    banks = shared_banks()
    if not banks:
        return ""
    catalog = _load_bank_catalog(get_paths().catalogs_dir())
    body: list[str] = []
    for bank in banks:
        if not bank.path.exists():
            continue
        for path in sorted(bank.path.glob("*.md")):
            if path.name.lower() in ("bank.md", "readme.md"):
                continue
            ref = bank.ref(path.name)
            meta = catalog.get(ref, {})
            lines = [f"### {ref}", f"SHARED BANK: {bank.name} (mode {bank.mode})"]
            if meta.get("summary"):
                lines.append(f"PURPOSE: {meta['summary']}")
            if meta.get("intent_domains"):
                lines.append("OWNS DOMAINS: " + "; ".join(meta["intent_domains"]))
            if meta.get("anti_domains"):
                lines.append("NOT HERE: " + "; ".join(meta["anti_domains"]))
            try:
                lines.append(f"SECTIONS:\n{_extract_headers(path.read_text())}")
            except (OSError, UnicodeDecodeError):
                continue
            body.append("\n".join(lines))
    if not body:
        return ""
    fenced = fence("\n\n".join(body), "shared memory banks — written by other people")
    notice = markdown_notice(
        "file names, section headers and catalog summaries copied out of shared "
        "memory banks",
        "A shared memory bank",
        injection_marker=True,
    )
    return "\n".join([_BANK_ROUTING_RULES, notice, *fenced])


def _load_bank_catalog(catalogs_dir: Path) -> dict[str, dict]:
    """``{bank/file.md: {summary, intent_domains, anti_domains}}`` from banks.json."""
    import json

    catalog_file = catalogs_dir / "banks.json"
    if not catalog_file.exists():
        return {}
    try:
        data = json.loads(catalog_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("entries", []) or []:
        src = entry.get("source")
        if src:
            out[src] = {
                "summary": entry.get("summary", ""),
                "intent_domains": entry.get("intent_domains", []),
                "anti_domains": entry.get("anti_domains", []),
            }
    return out


def _memory_context(memory_contents: dict[str, str]) -> str:
    """Render the memory-domain block the drafting and critic passes both see.

    Each file's catalog domain (summary + intent_domains + anti_domains) drives
    routing; the headers only pick the section WITHIN the chosen file. This block
    is byte-identical across every chunk of a run, so the shared prompt prefix
    stays cacheable — do not vary it per chunk.

    Shared-bank files are appended after the personal ones, fenced (see
    :func:`_bank_memory_context`). With no banks configured that call returns
    an empty string and this block is byte-for-byte what it was before banks
    existed — which also means no cache invalidation for anyone who has none.
    """
    catalog = _load_memory_catalog(get_paths().catalogs_dir())
    blocks = []
    for name, content in memory_contents.items():
        meta = catalog.get(name, {})
        lines = [f"### {name}"]
        if meta.get("summary"):
            lines.append(f"PURPOSE: {meta['summary']}")
        if meta.get("intent_domains"):
            lines.append("OWNS DOMAINS: " + "; ".join(meta["intent_domains"]))
        if meta.get("anti_domains"):
            lines.append("NOT HERE: " + "; ".join(meta["anti_domains"]))
        lines.append(f"SECTIONS:\n{_extract_headers(content)}")
        blocks.append("\n".join(lines))
    banks_block = _bank_memory_context()
    if banks_block:
        blocks.append(banks_block)
    return "\n\n".join(blocks)


def _draft_prompt_prefix(memory_context: str, source_names: str) -> str:
    """Everything in the user message that precedes the learnings themselves.

    Constant for the whole run (including the source-file list, which names the
    run's files rather than the chunk's) so that only the tail of the prompt
    differs between chunks.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Today's date: {today}\n"
        f"Source files: {source_names}\n\n"
        f"## Current memory file structure:\n\n{memory_context}\n\n"
        f"## Pending learnings:\n\n"
    )


async def _query(client, system: str, messages: list[dict], timeout_s: float | None):
    """``client.query`` with a per-call timeout, tolerating an older core.

    ``timeout_s`` on ``query()`` arrives in multiplai-core v0.12.0. Against an
    older pin the keyword does not exist, so the call is retried without it and
    every chunk falls back to the module-wide ceiling. Patching
    ``model_client._SDK_CALL_TIMEOUT_S`` instead is not an option: it is private,
    and it is racy under ``asyncio.gather`` with concurrent chunks.
    """
    global _TIMEOUT_KWARG_UNSUPPORTED
    if timeout_s and not _TIMEOUT_KWARG_UNSUPPORTED:
        try:
            return await client.query(system=system, messages=messages, timeout_s=timeout_s)
        except TypeError as exc:
            if "timeout_s" not in str(exc):
                raise
            _TIMEOUT_KWARG_UNSUPPORTED = True
            logger.warning(
                "Installed multiplai-core has no query(timeout_s=…) (pre-v0.12.0) — "
                "per-chunk timeouts fall back to MULTIPLAI_SDK_CALL_TIMEOUT_S"
            )
    return await client.query(system=system, messages=messages)


async def _generate_proposal(
    client,
    all_learnings: str,
    memory_contents: dict[str, str],
    source_files: list[Path],
) -> str:
    """Single-call proposal generation, kept for ``--auto``.

    Report mode uses the chunked pipeline in :func:`_draft_chunks` instead; this
    stays because ``--auto``'s applier stage is out of scope and shares it.
    """
    source_names = ", ".join(f.name for f in source_files)
    memory_context = _memory_context(memory_contents)
    messages = [
        {
            "role": "user",
            "content": _draft_prompt_prefix(memory_context, source_names) + all_learnings,
        }
    ]

    response = await client.query(system=_PROPOSAL_SYSTEM, messages=messages)
    cleaned = await _critique_proposal(client, response.content, memory_context)
    sourced = _with_repaired_citations(cleaned, get_paths().learnings_dir)
    validated = _with_relearns(_with_routing_warnings(sourced, memory_contents))
    return _with_conflict_resolutions(validated, all_learnings, memory_contents)


# ---------------------------------------------------------------------------
# Chunked parallel draft
# ---------------------------------------------------------------------------

def _concurrency() -> int:
    raw = os.environ.get("MULTIPLAI_DREAM_CONCURRENCY", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY
    return max(1, value)


def _plan_line(*, new_bytes: int, total_bytes: int, chunks: list, throughput: float) -> str:
    """One planning line, logged and printed BEFORE anything is spent."""
    from lib import dream_chunking

    return dream_chunking.format_plan_line(
        new_bytes=new_bytes,
        total_bytes=total_bytes,
        chunks=chunks,
        concurrency=_concurrency(),
        throughput=throughput,
    )


async def _draft_one_chunk(client, chunk, *, prefix: str, sem, run_dir: Path,
                           ledger_path: Path, proposal_name: str) -> str | None:
    """Draft one chunk, stage it, and ledger its blocks. ``None`` on failure.

    Staging and recording happen here rather than after the gather so that a
    crash costs only the in-flight chunks. The order is deliberate: draft on
    disk first, ledger second.
    """
    from lib import dream_chunking

    async with sem:
        started = time.monotonic()
        messages = [{"role": "user", "content": prefix + dream_chunking.render_chunk(chunk)}]
        try:
            response = await _query(client, _PROPOSAL_SYSTEM, messages, chunk.timeout_s)
        except Exception:
            logger.exception(
                "Chunk %02d failed (%d bytes) — skipped; its blocks stay unprocessed "
                "for the next run", chunk.index, chunk.n_bytes,
            )
            return None
        elapsed = max(time.monotonic() - started, 1e-6)

    text = (response.content or "").strip()
    if not text:
        logger.warning(
            "Chunk %02d returned nothing (%d bytes, %.0fs) — skipped; its blocks stay "
            "unprocessed", chunk.index, chunk.n_bytes, elapsed,
        )
        return None

    observed = chunk.n_bytes / elapsed
    logger.info(
        "Chunk %02d complete: %d bytes in, %d bytes out, %.0fs, %.0f B/s",
        chunk.index, chunk.n_bytes, len(text.encode("utf-8")), elapsed, observed,
    )
    try:
        _stage_draft(run_dir, chunk.index, text, [b.key for b in chunk.blocks])
        learnings_ledger.record(ledger_path, list(chunk.blocks), proposal_name)
    except OSError:
        logger.exception(
            "Could not stage/ledger chunk %02d — the draft still joins this merge",
            chunk.index,
        )
    _update_throughput(observed)
    return text


async def _draft_chunks(client, chunks: list, *, memory_context: str, source_names: str,
                        run_dir: Path, ledger_path: Path, proposal_name: str) -> list[str]:
    """Draft every chunk concurrently behind a semaphore, in chunk order.

    A failed chunk is skipped: its blocks are never ledgered, so the next run
    picks them up. Only a run in which EVERY chunk failed re-raises — that is an
    outage, not a bad block, and swallowing it would write an empty proposal and
    look like success.
    """
    prefix = _draft_prompt_prefix(memory_context, source_names)
    sem = asyncio.Semaphore(_concurrency())
    results = await asyncio.gather(*(
        _draft_one_chunk(
            client, chunk, prefix=prefix, sem=sem, run_dir=run_dir,
            ledger_path=ledger_path, proposal_name=proposal_name,
        )
        for chunk in chunks
    ))
    drafts = [r for r in results if r]
    if chunks and not drafts:
        raise RuntimeError(
            f"All {len(chunks)} draft chunk(s) failed — see the per-chunk tracebacks above"
        )
    if len(drafts) < len(chunks):
        logger.warning(
            "%d of %d chunks failed; their blocks stay unprocessed for the next run",
            len(chunks) - len(drafts), len(chunks),
        )
    return drafts


def _enforce_ledger_coverage(ledger_path: Path, staged_run_dir: Path, merged: list[str]) -> None:
    """Un-record any block whose draft did not reach the merge.

    The hard invariant of this pipeline: a block marked consolidated but absent
    from the written proposal is silent learning loss — it never resurfaces and
    nobody ever sees it. Recording is tied to a staged draft, and the merge input
    is built from those same staged drafts, so this should be structurally
    impossible; it is checked anyway, and a violation is repaired by dropping the
    keys so the next run re-consolidates them.
    """
    try:
        merged_texts = set(merged)
        orphan: list[str] = []
        for side in sorted(staged_run_dir.glob("*.json")):
            md = side.with_suffix(".md")
            meta = json.loads(side.read_text())
            keys = list(meta.get("keys") or [])
            if not keys:
                continue
            if not md.exists() or md.read_text() not in merged_texts:
                orphan.extend(keys)
        if not orphan:
            return
        logger.error(
            "Ledger coverage violation: %d block(s) were recorded but their draft did "
            "not reach the merge — un-recording so they are consolidated next run",
            len(orphan),
        )
        ledger = learnings_ledger.load(ledger_path)
        processed = ledger.get("processed", {})
        for key in orphan:
            processed.pop(key, None)
        learnings_ledger.save(ledger_path, ledger)
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not verify ledger coverage")


def _with_conflict_resolutions(
    proposal: str, all_learnings: str, memory_contents: dict[str, str]
) -> str:
    """Prepend the deterministic ``## Conflict Resolutions`` section.

    Generic consolidation *may* notice that a verified correction contradicts an
    existing memory line. Nothing makes it. This pass finds those pairings in
    code and puts them where review starts, because a stale line that
    contradicts a confirmed fact keeps teaching the wrong thing until someone
    spots it.

    Placed above the model's own output rather than appended: the model's
    section headings are what a reviewer skims, and a conflict block below them
    reads as a footnote. Fail-open like the routing gate — a crash here must
    never lose a generated proposal.
    """
    try:
        from lib.conflict_edits import conflict_section_for

        section = conflict_section_for(all_learnings, memory_contents)
    except Exception:
        logger.exception("Conflict-edit pass failed; proposal left unannotated")
        return proposal

    if not section:
        return proposal

    logger.info("Conflict-edit pass: %d resolution(s) prepended",
                section.count("\n### "))
    return f"{section}\n\n---\n\n{proposal}"


def _with_repaired_citations(proposal: str, learnings_dir: Path) -> str:
    """Correct ``**Source:**`` citations that name the wrong learnings file.

    A record that opens ``## Session Learnings — 2026-07-28T20:54`` but lives in
    ``2026-07-29.md`` (its session ran past midnight) is sometimes cited under
    the timestamp's date rather than the file it was rendered from. The line
    number stays right, which is what lets `lib/citation_repair` fix the
    filename deterministically instead of guessing.

    Fail-open like the routing and conflict gates: a crash here must never lose
    a generated proposal. See `lib/citation_repair` for why it repairs only
    provably-broken, unambiguously-resolvable citations.
    """
    try:
        from lib import citation_repair

        blocks, files = _collect_blocks(learnings_dir)
        learnings = {}
        unreadable = []
        for f in files:
            try:
                learnings[f.name] = f.read_text()
            except OSError:
                # Must be passed through, not just skipped: a file absent from
                # `learnings` reads as "does not exist", which makes every
                # citation to it look provably broken and licenses a repair
                # that is wrong. See `repair_citations` for the confirmed case.
                logger.warning("Could not read %s for citation repair", f.name)
                unreadable.append(f.name)

        repaired, findings = citation_repair.repair_citations(
            proposal, blocks, learnings, unreadable
        )
        section = citation_repair.render_findings(findings)
    except Exception:
        logger.exception("Citation repair failed; proposal left as written")
        return proposal

    if not findings:
        logger.info("Citations verified — every **Source:** line resolves")
        return proposal

    fixed = sum(1 for f in findings if f.repaired)
    logger.info(
        "Citation repair: %d corrected, %d left unresolved",
        fixed, len(findings) - fixed,
    )
    for finding in findings:
        if finding.repaired:
            continue
        # An advisory finding names a file that is not a dated learnings file at
        # all — nothing to chase, so it goes at INFO. Everything else is a
        # citation a reviewer may follow to the wrong place, which is the thing
        # WARNING is for; mixing the two taught the reader to ignore both.
        log = logger.info if finding.advisory else logger.warning
        log(
            "Unverifiable citation %s:%d — %s",
            finding.cited_file, finding.line, finding.reason,
        )
    return f"{repaired.rstrip()}\n\n---\n\n{section}"


def _with_routing_warnings(proposal: str, memory_contents: dict[str, str]) -> str:
    """Append the deterministic ``## Routing Warnings`` section to a proposal.

    Pure code (section-registry + cross-file dedup, see lib/routing_validation).
    Fail-open + loud: a crash in the gate must never lose a generated proposal —
    log the failure and return the proposal unvalidated. Never rewrites entries;
    the human reviewing via dream-remember stays the gate.

    The dedup half additionally screens against the always-loaded ``CLAUDE.md``
    files and the shared memory banks (``lib.memory_corpus``). The drafter is
    shown neither, so rules living there were re-proposed on every run; those
    files are evidence only and never join the section registry.
    """
    try:
        from lib.routing_validation import validate_proposal, render_warnings_section

        try:
            from lib import memory_corpus
            dedup_extra = memory_corpus.extra_contents(get_paths())
            if dedup_extra:
                logger.info(
                    "Dedup corpus extended with %d always-loaded/bank file(s): %s",
                    len(dedup_extra), ", ".join(sorted(dedup_extra)),
                )
        except Exception:
            # A missing CLAUDE.md or a malformed bank config must narrow the
            # evidence, never lose the proposal.
            logger.exception("Could not read the always-loaded corpus — dedup runs on memory files only")
            dedup_extra = {}

        warnings = validate_proposal(proposal, memory_contents, dedup_extra=dedup_extra)
        if warnings:
            logger.warning(
                "Routing validation flagged %d issue(s):\n%s",
                len(warnings), "\n".join(f"  - {w}" for w in warnings),
            )
        else:
            logger.info("Routing validation clean — no misroutes or cross-file duplicates")
        return proposal.rstrip() + render_warnings_section(warnings)
    except Exception:
        logger.exception(
            "Routing validation gate failed — proposal written WITHOUT a Routing Warnings section"
        )
        return proposal


def _with_relearns(proposal: str) -> str:
    """Append the deterministic ``## Rules Re-learned`` section to a proposal.

    Pure code over two logs this pipeline already writes: the rejections it
    logged on previous runs, and the router's utilisation telemetry. Fail-open
    and loud, like the other deterministic sections — a crash in a diagnostic
    must never lose a generated proposal.

    **It reports on previous runs, by construction.** This runs at draft time,
    before this run's own triage has rejected anything, so the log it reads ends
    at the last run. That is the right window rather than a limitation: the
    question is which rules memory *already held* and a session derived anyway,
    and this run's proposal has not been judged yet.

    Why this exists at all is in ``lib.relearns``: the drafting prompt tells the
    model to merge repeats and not annotate the count, which is right for the
    memory line and throws away the one signal the pipeline produces about
    whether a written rule is doing any work.
    """
    try:
        from lib import rejections, relearns

        paths = get_paths()
        data_dir = paths.data_dir()
        records = rejections.read(rejections.default_path(data_dir))
        rows = relearns.read_utilisation(data_dir / "utilisation.jsonl")
        groups = relearns.analyse(records, rows)
        if groups:
            logger.info(
                "Re-learn scan: %d rule(s) memory already held were derived "
                "again across %d drop(s)",
                len(groups), sum(g.count for g in groups),
            )
        else:
            logger.info("Re-learn scan clean — no rule was re-derived")
        return f"{proposal.rstrip()}\n\n---\n\n{relearns.render_section(groups)}"
    except Exception:
        logger.exception(
            "Re-learn scan failed — proposal written WITHOUT a Rules Re-learned section"
        )
        return proposal


# Second pass — bounded surgical critic, expressed as DIRECTIVES rather than a rewrite.
# The critic used to regenerate the entire document to change under 1% of it (measured:
# 40846 -> 41252 bytes at 173-630 s a pass), which is both the slowest step and the one
# most able to lose content it was not asked to touch. Emitting one directive per edit
# makes the cost proportional to the number of edits, and makes every change auditable in
# the log. `lib/proposal_edits.py` parses and applies them in pure code.
_CRITIC_SYSTEM = """\
You are a strict editor doing a SECOND PASS over an already-drafted memory proposal. The
analyst that drafted it generalizes most things well but still (a) leaves point-in-time
residue on some KEEP entries, (b) keeps whole past-event records because they embed a
useful fragment, and (c) — because the proposal was drafted in parallel over separate
slices of the backlog — repeats the same lesson in more than one entry.

You do NOT rewrite the document. You emit EDIT DIRECTIVES, one per line, and nothing else.

## Your five jobs

### 1. Strip residue (every '### N.' entry)

Surgically remove any residual:
- commit hashes / SHAs ("committed as abc1234", "fixed in def5678")
- dated-decision framing ("Decision (2026-06-15):", "as of <date>", "(decided <date>)")
- finished-task imperatives ("update file X", "remove Y now", "... accordingly")
- one-off absolute paths and over-scoped project / repo / file names, UNLESS the lesson is
  genuinely scoped to that project and useless elsewhere
Keep the transferable rule, phrased as guidance. Emit `REPLACE` with the cleaned text.

### 2. Demote past-event records (be bold)

An entry that is fundamentally a record of a PAST EVENT — a dated decision, a completed
checklist/migration/cutover, a "we did/decided/shipped X" status — is DIARY, even when it
embeds a reusable fragment. Do NOT keep the event in order to save the fragment. Instead:
- If a genuine general rule can be lifted out, `REPLACE` the entry's text with that rule
  alone (strip ALL event scaffolding: dates, specific repo/project names, the checklist
  itself, what was done). Example: "Decision: repos A/B/C go public; pre-public: scrub gho_
  token, remove scalestack skill, secret scan" → "Before making any repo public: scrub
  secrets from git history AND rotate them, and strip employer-specific content."
- Otherwise `DROP` it with a one-line reason.
When unsure whether something is a durable rule or a one-time event, treat it as an EVENT:
extract any rule, drop the rest. Memory is guidance that changes future action — not a log
of what happened.

DO keep durable reference facts — how a system is configured, stable identifiers (regions,
instance/secret names, ports), standing preferences. Those are not events; they inform future
work. The target is records of things that HAPPENED or were DECIDED at a point in time.

### 3. Drop mis-filed toolchain change-requests

If a memory '### N.' entry is really a change-request to the TOOLCHAIN's own code / config /
file-structure ("split these files", "delete this orphan", "this script should also check X"),
it is not memory — emit `DROP <file>#<n> toolchain change-request`. There is no destination
file for these; a change worth making gets made. Do not drop general knowledge that merely
mentions the system.

### 4. Fix catch-all mis-routing

The user message includes each memory file's PURPOSE / OWNS DOMAINS / NOT HERE block. If an
entry is filed under a file whose NOT-HERE line names its subject, or under a broadly-named
file when another file's PURPOSE clearly owns the subject, `MOVE` it to the owning file.
Broadly-named files are never fallbacks, and a tool/agent having performed the work does not
make the learning about that tool/agent — route by what the lesson is ABOUT. Only move on a
clear subject mismatch; do not reshuffle borderline entries.

### 5. Merge cross-slice duplicates (new — read this one carefully)

This proposal was assembled from several independently drafted slices of the backlog, so the
SAME lesson learned on two different days can appear as two entries — often under the same
target file, sometimes worded differently. Nothing upstream can catch that. You are the first
and only place it is visible.

When two entries state the same rule, `MERGE` them: keep the better-worded one and absorb
the other. The surviving entry keeps BOTH `**Source:**` lines, so no provenance is lost.
Merge only genuine restatements of one rule — two related rules about the same subject are
two entries, not one.

## Directive grammar — emit ONLY these, one per line

REPLACE <file>#<n> <new text for the entry>
MOVE <file>#<n> -> <other.md>
DROP <file>#<n> <reason>
MERGE <file>#<n> <- <file>#<m>
NOOP

`<file>` is the target file named in the `## Updates for \\`file\\`` heading; `<n>` is the
entry's `### N.` number. Numbers run continuously across the whole proposal, so `<n>` is
unique on its own — but still name the file it appears under.

## Hard rules

- Emit directives and NOTHING else. No preamble, no explanation, no markdown fences, no
  restatement of the proposal. A line that is not a directive is discarded.
- If the proposal needs no changes, emit exactly: NOOP
- NEVER touch a `**Source:**` line. It cites `filename:line` for traceability and must stay
  exact; a directive that would edit or remove one is refused. `MERGE`'s append is the one
  sanctioned operation — the surviving entry ends up with both Source lines verbatim.
- One directive per line, one edit per directive. Do not batch several entries into one.
- Do not invent entries, do not reorder, do not renumber, and do not touch entries that are
  already clean.
- Preserve **[RULE-PROPOSAL]** / **[warning low confidence]** markers in any REPLACE text.
"""


def _batch_proposal_for_critic(proposal: str) -> list[str]:
    """Split *proposal* into critic-sized batches, never splitting a ``## `` section.

    The critic is input-bound: it reads a whole proposal and emits only a handful
    of directive lines. That asymmetry is what made the unbatched pass impossible
    — a 283 KB fixture run handed it a 228 KB prompt, which burned both 900 s
    attempts without finishing and silently degraded to "keep the merged draft",
    so the second-pass quality gate never ran at all on exactly the backlogs that
    need it most.

    Sections are kept whole because ``MERGE <file>#<n> <- <file>#<m>`` compares
    two entries of the same file; splitting a file across batches would hide half
    its duplicates from the comparison. A single section larger than the budget
    gets a batch of its own rather than being split.
    """
    from lib import dream_chunking

    budget = dream_chunking.chunk_budget_bytes(
        CHUNK_TIMEOUT_S, _calibrated_throughput()
    )
    # Keep each "## " heading with its body; the split loses the delimiter, so
    # re-attach it. Anything before the first heading is the proposal preamble
    # and rides along with the first batch.
    parts = re.split(r"^(?=## )", proposal, flags=re.M)
    sections = [p for p in parts if p.strip()]

    batches: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for section in sections:
        n = len(section.encode("utf-8"))
        if current and current_bytes + n > budget:
            batches.append("".join(current))
            current, current_bytes = [], 0
        current.append(section)
        current_bytes += n
    if current:
        batches.append("".join(current))
    return batches or [proposal]


async def _critique_batch(client, batch: str, memory_context: str, sem, index: int,
                          total: int) -> str:
    """One critic call over one batch. Returns raw directive text ("" on failure).

    Fails open per batch: a batch that times out loses only its own directives,
    and the other batches' edits still land.

    A **rate limit** is the exception, retried once after a backoff. It is not a
    property of the batch — the batch was fine, the account was busy — and the
    critic's own fan-out is usually what made it busy, so dropping the batch
    discards good work over a condition that clears by waiting. On 2026-08-03 all
    12 batches hit 429 and the whole critic pass silently contributed nothing.
    """
    from lib import dream_chunking

    content = f"## Drafted proposal:\n\n{batch}"
    if memory_context:
        content = (
            f"## Memory file domains (for the mis-routing check):\n\n{memory_context}\n\n"
            + content
        )
    n_bytes = len(batch.encode("utf-8"))
    # An oversized section cannot be split, so give it the same escalation
    # plan_chunks gives an oversized block rather than letting it fail outright.
    budget = dream_chunking.chunk_budget_bytes(CHUNK_TIMEOUT_S, _calibrated_throughput())
    timeout_s = (
        min(2.0 * CHUNK_TIMEOUT_S, dream_chunking.MAX_ESCALATED_TIMEOUT_S)
        if n_bytes > budget
        else CHUNK_TIMEOUT_S
    )

    async with sem:
        started = time.monotonic()
        response = None
        # The backoff deliberately happens while holding the semaphore. The slot
        # is the only throttle available here, so pausing inside it stalls the
        # whole critic fan-out — which is the correct response to being rate
        # limited, and lets the retry land in a quieter window rather than back
        # in the storm that caused the 429.
        for attempt in range(1, _RATE_LIMIT_ATTEMPTS + 1):
            try:
                response = await _query(
                    client, _CRITIC_SYSTEM,
                    [{"role": "user", "content": content}], timeout_s,
                )
                break
            except Exception as exc:
                if attempt < _RATE_LIMIT_ATTEMPTS and _is_rate_limit(exc):
                    logger.warning(
                        "Critic batch %d/%d rate limited (%s) — retrying once in "
                        "%.0fs rather than dropping its directives",
                        index, total, exc, _RATE_LIMIT_BACKOFF_S,
                    )
                    await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
                    continue
                logger.exception(
                    "Critic batch %d/%d failed (%d bytes) — its directives are lost, "
                    "the other batches still apply", index, total, n_bytes,
                )
                return ""
        elapsed = max(time.monotonic() - started, 1e-6)

    # Unreachable: the loop either breaks with a response or returns "" (the last
    # attempt fails the `attempt < _RATE_LIMIT_ATTEMPTS` guard and falls through
    # to the failure branch). Kept so a future change to the retry ladder cannot
    # turn this into an AttributeError on None mid-run.
    if response is None:  # pragma: no cover - defensive
        return ""

    raw = (response.content or "").strip()
    logger.info(
        "Critic batch %d/%d: %d bytes in, %d directive bytes out, %.0fs",
        index, total, n_bytes, len(raw.encode("utf-8")), elapsed,
    )
    return raw


async def _critique_proposal(client, proposal: str, memory_context: str = "",
                             stats: dict | None = None) -> str:
    """Run the directive critic over a drafted proposal; return the edited version.

    ``memory_context`` carries the same PURPOSE / OWNS DOMAINS / NOT HERE file
    blocks the drafting pass saw, so the critic's mis-routing check works from
    the live catalog instead of hardcoded file knowledge.

    The pass runs in batches (see ``_batch_proposal_for_critic``) and applies
    every batch's directives to the **whole** proposal in one go. That is safe
    because a directive addresses ``<file>#<n>`` and ``apply_directives`` sorts
    back-to-front, so an index can never be shifted by another edit.

    Fails **open**, exactly as the rewrite critic did: an unparseable, empty or
    all-NOOP response keeps the merged draft and logs a warning. A
    residue-bearing proposal is still useful; a lost one is not.
    """
    try:
        from lib import proposal_edits

        batches = _batch_proposal_for_critic(proposal)
        if len(batches) > 1:
            logger.info(
                "Critic: %d bytes over %d batches", len(proposal.encode("utf-8")),
                len(batches),
            )
        sem = asyncio.Semaphore(_concurrency())
        raws = await asyncio.gather(*(
            _critique_batch(client, b, memory_context, sem, i, len(batches))
            for i, b in enumerate(batches, 1)
        ))
        if stats is not None:
            stats["batches"] = len(batches)
            stats["failed"] = sum(1 for r in raws if not r)

        directives, rejected = [], []
        for raw in raws:
            if not raw:
                continue
            parsed, bad = proposal_edits.parse_directives(raw)
            directives.extend(parsed)
            rejected.extend(bad)

        if not any(raws):
            logger.warning("Critic returned nothing — keeping the merged draft")
            return proposal
        if rejected:
            logger.info("Critic: %d unparseable line(s) ignored", len(rejected))
        if not directives:
            logger.warning(
                "Critic emitted no usable directives (%d line(s) rejected) — keeping the "
                "merged draft", len(rejected),
            )
            return proposal

        edited, applied, refused = proposal_edits.apply_directives(proposal, directives)
        for description in applied:
            logger.info("Critic applied: %s", description)
        for reason in refused:
            logger.info("Critic refused: %s", reason)
        logger.info(
            "Critic pass: %d applied, %d refused, %d rejected (%d -> %d bytes)",
            len(applied), len(refused), len(rejected),
            len(proposal.encode("utf-8")), len(edited.encode("utf-8")),
        )
        # Same degenerate-output guard as the rewrite critic: never let the pass
        # hand back something that no longer looks like a proposal.
        if "## Updates for" not in edited and "## Filtered Out" not in edited:
            logger.warning("Critic output lost the proposal structure — keeping the draft")
            return proposal
        return edited
    except Exception:
        logger.exception("Critic pass failed — keeping the merged draft")
        return proposal


def _plan_run(
    learnings_dir: Path, *, persist_migration: bool = False
) -> tuple[list, list, list[Path], float]:
    """Shared planning for ``--check`` and a real run — no LLM, no spend.

    Returns ``(pending_blocks, chunks, files, throughput)``. Keeping one
    implementation is the point: ``--check``'s prediction is worthless if it is
    computed differently from what the run actually does.

    Ledger keys are migrated to the version-2 projection scheme here, since
    this is the one place that holds both the ledger and the records it was
    computed from. ``persist_migration`` is the caller's assertion that it holds
    the run lock: ``--check`` deliberately takes no lock, so it migrates in
    memory (its count stays right) and writes nothing, rather than racing a
    live run's ``record`` and dropping a key that was just written.
    """
    from lib import dream_chunking

    blocks, files = _collect_blocks(learnings_dir)
    ledger_path = learnings_ledger.default_ledger_path()
    ledger = learnings_ledger.load(ledger_path)
    stale_version = ledger.get("version") != learnings_ledger.LEDGER_VERSION
    moved = learnings_ledger.migrate(ledger, blocks)
    if moved:
        logger.info(
            "Migrated %d ledger key(s) to the format-invariant scheme%s",
            moved, "" if persist_migration else " (in memory — no lock held)",
        )
    # Persist on a version bump even when nothing moved. A v1 ledger holding no
    # legacy records is already in the v2 scheme, but it kept `"version": 1` on
    # disk forever — so the file could not answer "has this been migrated?",
    # which is the first thing the next migration will need to ask.
    if persist_migration and (moved or stale_version):
        learnings_ledger.save(ledger_path, ledger)
    pending = learnings_ledger.unprocessed(blocks, ledger)
    throughput = dream_chunking.resolve_throughput(_calibrated_throughput())
    chunks = dream_chunking.plan_chunks(pending, timeout_s=CHUNK_TIMEOUT_S,
                                        throughput=throughput)
    return pending, chunks, files, throughput


async def dream_report() -> None:
    """Generate ONE change proposal and write it to .multiplai/dreams/ for review.

    Order matters and is load-bearing: lock, plan, fold, draft — everything
    before the first token is spent. Folding ahead of drafting is what keeps the
    "no decided items" check honest: the hub writes decisions into a proposal
    item by item, so checking at write time would be a claim that had decayed
    over a multi-minute run.
    """
    if not acquire_run_lock():
        return

    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dreams_dir = paths.dreams_dir()
    ledger_path = learnings_ledger.default_ledger_path()

    pending, chunks, source_files, throughput = _plan_run(
        learnings_dir, persist_migration=True
    )

    # Resume BEFORE pruning: a staged draft is kept only while its blocks are
    # ledgered, and pruning drops keys for learnings files that have since been
    # deleted. Pruning first would discard a perfectly good draft whose source
    # file is gone — the one case where the draft is the only surviving copy.
    run_dir = _runs_dir() / _new_run_id()
    resumed = _resume_staged_drafts(run_dir, learnings_ledger.load(ledger_path))

    # Keys for learnings files that no longer exist are dead weight (--auto and
    # /dream-remember delete files after applying).
    pruned = learnings_ledger.prune(ledger_path, {f.name for f in source_files})
    if pruned:
        logger.info("Ledger: pruned %d key(s) for deleted learnings files", pruned)

    if not pending and not resumed:
        logger.info("No new learnings since the last consolidation — nothing to propose")
        print("No new learnings — nothing to propose.")
        return

    total_bytes = 0
    for f in source_files:
        try:
            total_bytes += len(f.read_text().encode("utf-8"))
        except OSError:
            pass
    new_bytes = sum(len(b.text.encode("utf-8")) for b in pending)
    plan = _plan_line(new_bytes=new_bytes, total_bytes=total_bytes,
                      chunks=chunks, throughput=throughput)
    logger.info("%s", plan)
    print(plan)

    # Fold now, under the lock, before a single token is spent. Stage what was
    # folded straight away: the files have already left the dreams root, so
    # crashing before the write would otherwise lose them entirely.
    folded = _fold_pending_proposals(dreams_dir)
    for i, text in enumerate(folded, start=1):
        try:
            _stage_draft(run_dir, i, text, [], kind="folded")
        except OSError:
            logger.warning("Could not stage folded proposal %d", i, exc_info=True)

    dreams_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = _proposal_output_path(dreams_dir, today)

    # Mirror dream_auto(): a crash inside the SDK call — including an
    # SDK-unavailable RuntimeError from create_client itself — must leave a
    # traceback in dream.log / hook-errors.log, not just the ephemeral task
    # stdout. Re-raise so exit status stays non-zero.
    try:
        client = await create_client(component="dream")
        logger.info("Dream using %s", type(client).__name__)

        memory_contents = _read_memory_files(memory_dir)
        logger.info(
            "Loaded %d memory files for context: %s",
            len(memory_contents), ", ".join(sorted(memory_contents)),
        )
        memory_context = _memory_context(memory_contents)

        drafts = await _draft_chunks(
            client, chunks,
            memory_context=memory_context,
            source_names=", ".join(f.name for f in source_files),
            run_dir=run_dir,
            ledger_path=ledger_path,
            proposal_name=output_file.name,
        )

        merge_input = resumed + drafts + folded
        _enforce_ledger_coverage(ledger_path, run_dir, resumed + drafts)

        from lib import proposal_merge

        merged = proposal_merge.merge_drafts(merge_input)
        logger.info(
            "Merged %d draft(s) (%d fresh, %d resumed, %d folded) into %d bytes",
            len(merge_input), len(drafts), len(resumed), len(folded),
            len(merged.encode("utf-8")),
        )

        critic_stats: dict = {}
        cleaned = await _critique_proposal(client, merged, memory_context, critic_stats)
        # Repair citations before the deterministic sections are attached, so the
        # regex only ever sees model-written provenance and cannot rewrite a
        # citation that this code put there itself.
        sourced = _with_repaired_citations(cleaned, learnings_dir)
        validated = _with_relearns(_with_routing_warnings(sourced, memory_contents))
        pending_text = "\n\n".join(b.text for b in pending)
        proposal = _with_conflict_resolutions(validated, pending_text, memory_contents)
    except Exception:
        logger.exception("Dream report generation failed")
        raise

    # Quick structural digest so the log answers "what did the model decide?"
    # without having to open the proposal file.
    proposal_lines = proposal.splitlines()
    target_files = [
        l.split("`")[1] for l in proposal_lines
        if l.startswith("## Updates for `") and "`" in l[16:]
    ]
    has_filtered = any(l.startswith("## Filtered Out") for l in proposal_lines)
    logger.info(
        "Proposal generated: %d bytes, %d target files (%s), filtered-out section=%s",
        len(proposal.encode("utf-8")),
        len(target_files),
        ", ".join(target_files) if target_files else "none",
        has_filtered,
    )
    if not has_filtered or not target_files:
        logger.warning(
            "Proposal looks incomplete — missing target updates or Filtered Out section"
        )

    output_file.write_text(_stamp_generated(proposal))
    logger.info("Proposal written to %s", output_file)

    # Everything staged is now inside a written proposal; keeping it would fold
    # the same drafts in again on the next run.
    _clear_staging()

    # Report what was actually consolidated, not what was planned. A run that
    # loses chunks — to a rate limit, a timeout, an outage — still writes a
    # useful proposal and still exits 0, because the lost blocks stay pending and
    # come back next time. But printing "231 new learning block(s)" when 109 of
    # them were deferred tells the user their backlog is done when it is not.
    # The ledger is the authority on what landed, so ask it rather than the plan.
    deferred = learnings_ledger.unprocessed(
        list(pending), learnings_ledger.load(ledger_path)
    )
    consolidated = len(pending) - len(deferred)
    failed_chunks = len(chunks) - len(drafts)

    print(f"Proposal written to {output_file}")
    if deferred:
        print(
            f"Sources: {len(source_files)} files, {consolidated} of {len(pending)} new "
            f"learning block(s) consolidated"
        )
        print(
            f"  ⚠ {failed_chunks} of {len(chunks)} chunk(s) did not complete — "
            f"{len(deferred)} block(s) stay pending and are picked up by the next run "
            f"(see dream.log)"
        )
    else:
        print(f"Sources: {len(source_files)} files, {len(pending)} new learning block(s)")
    if critic_stats.get("failed"):
        print(
            f"  ⚠ second-pass review incomplete: {critic_stats['failed']} of "
            f"{critic_stats['batches']} batch(es) failed — duplicates and mis-routed "
            f"items may remain"
        )
    if folded:
        print(f"Folded in {len(folded)} undecided pending proposal(s) → dreams/superseded/")
    print(f"Targets: {len(target_files)} files ({', '.join(target_files) or 'none'})")
    print("Review with: /multiplai-context:dream-remember")


# ---------------------------------------------------------------------------
# Auto mode (--auto)
# ---------------------------------------------------------------------------

def _memory_dir_is_git_repo(memory_dir: Path) -> bool:
    if not memory_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _commit_memory_changes(
    memory_dir: Path,
    *,
    pathspec: Sequence[str] = ("*.md",),
    message: str | None = None,
) -> bool:
    """Stage and commit memory changes. Returns True on commit, False otherwise.

    ``pathspec`` narrows what is staged and recorded. The default sweeps every
    memory file, which is right for a full consolidation but wrong for triage:
    there, only the files the applier actually rewrote should be in the commit,
    so an unrelated hand-edit sitting in the working tree is not attributed to
    the dream run.
    """
    if not _memory_dir_is_git_repo(memory_dir):
        logger.warning(
            "Memory auto-commit skipped — %s is not a git repository.", memory_dir
        )
        return False

    try:
        # Stage only memory markdown files. Staging the whole tree would
        # sweep in unrelated dirty work when memory_dir lives inside a
        # larger repo (dotfiles/workspace) and record it in the snapshot.
        subprocess.run(
            ["git", "-C", str(memory_dir), "add", "--", *pathspec],
            check=True, timeout=15, capture_output=True,
        )
        # Check for staged changes scoped to the *.md pathspec only — otherwise
        # anything the user had pre-staged elsewhere would make this look
        # "dirty" and fire a snapshot that sweeps those unrelated files in.
        diff = subprocess.run(
            ["git", "-C", str(memory_dir), "diff", "--cached", "--quiet", "--", *pathspec],
            timeout=10, capture_output=True,
        )
        if diff.returncode == 0:
            logger.info("Memory auto-commit skipped — no changes to record")
            return False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Restrict the snapshot to the *.md pathspec: passing the pathspec
        # records just those paths and leaves any other staged files untouched.
        subprocess.run(
            ["git", "-C", str(memory_dir), "commit",
             "-m", message or f"dream: consolidate {today}", "--", *pathspec],
            check=True, timeout=30, capture_output=True,
        )
        logger.info("Memory auto-committed in %s", memory_dir)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Memory auto-commit failed: git %s exited %d (stderr: %s)",
            e.cmd, e.returncode, e.stderr.decode("utf-8", "replace") if e.stderr else "",
        )
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        logger.warning("Memory auto-commit failed: %s", e)
        return False


# Mechanical applier — executes an already-generalized proposal. It does NOT decide
# what is or isn't a memory; all that judgment lives in _PROPOSAL_SYSTEM. This keeps
# --auto using the exact same generalization brain as report mode (just no human gate).
_APPLIER_SYSTEM = (
    "You apply an approved set of memory updates to a memory file. Make ONLY the "
    "changes the proposal specifies (add / update / replace at the named sections). "
    "Match the file's existing style and formatting exactly. Do not generalize, "
    "re-judge, invent, or add anything not in the proposal. Every line the proposal "
    "does not name is reproduced verbatim — in particular the 'Last Updated' line, "
    "which you never touch: its date is refreshed in code after your result is "
    "checked, not by you. Return the full updated file content and nothing else."
)
# The narrow constraint is the point. "Reproduce every existing line verbatim"
# contradicts "update / replace at the named sections" two clauses earlier, and
# the applier has to resolve the conflict itself: the safe reading is to append,
# which leaves the stale line beside the new one — the "three phrasings of one
# fact" accumulation `lib.conflict_edits` exists to prevent. `--triage` is
# unaffected either way (`_is_additive_result` refuses a changed line in code),
# but `--auto` has no additive check, so there the prompt *is* the constraint.


def _split_proposal_by_file(proposal: str) -> dict[str, str]:
    """Split a proposal into {filename: section_text} by '## Updates for `file`' headers.

    'Filtered Out' and any preamble are not target sections and are dropped — only the
    per-file update blocks become applier instructions.
    """
    sections: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []

    def _flush():
        if current_file is not None:
            sections[current_file] = "\n".join(buf).strip()

    for line in proposal.splitlines():
        if line.startswith("## Updates for `") and "`" in line[16:]:
            _flush()
            current_file = line.split("`")[1]
            buf = [line]
        elif line.startswith("## "):
            # any other H2 (e.g. "## Filtered Out") ends the current file section
            _flush()
            current_file = None
            buf = []
        elif current_file is not None:
            buf.append(line)
    _flush()
    return sections


def _is_safe_memory_update(current: str, new: str) -> bool:
    """Guard against an applier response that would destroy a memory file.

    A consolidation rewrites the file in full, so a truncated response or a
    refusal preamble ("I'm sorry, I can't…") would silently overwrite good
    content with garbage. Consolidation only adds or lightly edits, so the
    result should never collapse to a fraction of the original. Reject an
    empty/whitespace result or one that lost more than 40% of the original
    length — the caller then keeps the existing file and the learnings so the
    run can be retried.
    """
    stripped = new.strip()
    if not stripped:
        return False
    # A memory file is prose+markdown; a bare apology/refusal is not a valid
    # rewrite. Cheap heuristic on the opening.
    head = stripped[:80].lower()
    if head.startswith(("i'm sorry", "i am sorry", "i cannot", "i can't", "sorry,")):
        return False
    if len(current.strip()) >= 200 and len(stripped) < 0.6 * len(current.strip()):
        return False
    return True


def _atomic_write(path: Path, content: str) -> None:
    """Replace *path*'s contents atomically (temp file + ``os.replace``).

    ``write_text`` is truncate-then-write: a kill or an ENOSPC part-way through
    leaves the file half-written, and the file in question is the user's
    long-term memory. ``save_cache`` and ``learnings_ledger`` both already write
    this way; the one file whose loss actually matters did not.
    """
    path = Path(path)
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


def _is_additive_result(current: str, new: str, proposed_texts: Sequence[str]) -> str | None:
    """Check an applier result really only *added* to *current*.

    Returns ``None`` when the result is additive, else a one-line reason.

    ``_is_safe_memory_update`` is a floor, not a check: it accepts a result that
    threw away 40% of the file, which for triage — where every item is an
    ``add`` into an existing section — is not a near-miss, it is exactly the
    outcome the triage path promises cannot happen. The human reviewed nothing,
    so the only thing standing between a hallucinating applier and a silently
    rewritten memory file is this function.

    Two deterministic conditions, both necessary:

    - **Every original line survives**, as a multiset. Order is not checked (an
      insert legitimately moves lines) but count is: dropping or altering a
      single line fails. This is what makes the operation reviewable — the diff
      can only be additions.
    - **Growth is bounded by what was proposed.** The applier may reword an
      item, so the added text is not compared literally; it is compared in
      *size* against the proposed texts plus a 3x allowance for formatting and
      section headers. An applier that pads the file with invented content
      trips this even though every original line is intact.
    """
    original = [ln for ln in current.splitlines() if ln.strip()]
    updated = [ln for ln in new.splitlines() if ln.strip()]

    from collections import Counter

    missing = Counter(original) - Counter(updated)
    if missing:
        lost = sum(missing.values())
        sample = next(iter(missing)).strip()[:60]
        return f"{lost} original line(s) lost or altered, e.g. {sample!r}"

    added_chars = len(new.strip()) - len(current.strip())
    budget = 3 * sum(len(t) for t in proposed_texts) + 200
    if added_chars > budget:
        return (
            f"grew by {added_chars} chars, more than 3x the {len(proposed_texts)} "
            f"proposed item(s) can account for (budget {budget})"
        )
    return None


_LAST_UPDATED_RE = re.compile(
    r"^(?P<label>\*\*Last Updated:\*\*[ \t]*)\d{4}-\d{2}-\d{2}", re.MULTILINE | re.IGNORECASE
)


def _refresh_last_updated(content: str, today: str) -> str:
    """Restamp the file's ``**Last Updated:** YYYY-MM-DD`` header to *today*.

    The applier used to be told to do this, and it obeyed — which made
    :func:`_is_additive_result` reject the whole file, because refreshing the
    date deletes the old line and the multiset check counts that as a lost
    line. 18 of 29 memory files in the reporting workspace carry the stamp, so
    triage could not write to any of them (issue #189).

    Doing it in code after the check is what resolves the contradiction: the
    model reproduces the file verbatim (checkable), and the one edit that is
    not additive is deterministic and made by the caller. Only the date is
    replaced, so anything else on the line survives; the substitution is
    line-count preserving, which is why it is safe to run after the check
    rather than before.

    Files with no stamp are returned unchanged — this never adds one, because
    which files carry a stamp is the catalog's business, not the applier's.

    **The first match only.** A memory file has one header stamp, at the top;
    ``context_manager._memory_freshness_date`` reads exactly that, taking the
    first match in the first 2048 characters. Any later occurrence is prose or a
    sample — ``multiplai.md`` documents this very marker twice — and the regex
    is ``MULTILINE`` with no fenced-block tracking, so a documented example
    written at column 0 inside a ``` block would otherwise be silently restamped
    to today.
    """
    return _LAST_UPDATED_RE.sub(lambda m: m.group("label") + today, content, count=1)


async def _apply_proposal_to_file(client, memory_file: Path, proposal_section: str) -> str | None:
    """Apply one file's slice of the proposal. Returns validated new content,
    or None if the call failed or the result looks unsafe to write."""
    if not memory_file.exists():
        return None

    current_content = memory_file.read_text()
    messages = [
        {
            "role": "user",
            "content": (
                f"## Approved updates for {memory_file.name}:\n{proposal_section}\n\n"
                f"## Current file content:\n{current_content}"
            ),
        }
    ]

    # The applier rewrites the whole file, so its cost scales with section +
    # current file, not with the section alone. A single call cannot be left on
    # the default deadline: a 283 KB fixture run produced sections of 41 KB
    # (`claude-code-tools.md`), 38 KB (`multiplai.md`), 33 KB (`dolcebot.md`) and
    # 27 KB (`writing-workflow.md`), and at the slow tail that run measured
    # (25 B/s) every one of those needs more than 900 s. Size the deadline to the
    # work and escalate, exactly as `plan_chunks` does for an oversized block.
    from lib import dream_chunking

    n_bytes = len(proposal_section.encode("utf-8")) + len(current_content.encode("utf-8"))
    timeout_s = min(
        dream_chunking.MAX_ESCALATED_TIMEOUT_S,
        max(CHUNK_TIMEOUT_S, dream_chunking.estimate_seconds(n_bytes, _calibrated_throughput())),
    )

    try:
        response = await _query(client, _APPLIER_SYSTEM, messages, timeout_s)
    except Exception:
        logger.exception(
            "Failed to apply updates to %s (%d bytes, %.0fs deadline)",
            memory_file.name, n_bytes, timeout_s,
        )
        return None

    if not _is_safe_memory_update(current_content, response.content):
        logger.error(
            "Rejected unsafe applier output for %s (%d chars -> %d); keeping original",
            memory_file.name, len(current_content), len(response.content.strip()),
        )
        return None
    return response.content


async def dream_auto() -> None:
    """Apply learnings directly to memory files without review (autonomous mode).

    Takes the same exclusive run lock as report mode — two concurrent runs
    writing the same memory files is strictly worse than two writing proposals.
    The applier stage itself is unchanged; it shares ``_generate_proposal()``.
    """
    if not acquire_run_lock():
        return

    paths = get_paths()
    learnings_dir = paths.learnings_dir
    memory_dir = paths.memory_dir()
    dream_state_file = paths.dream_state_file()

    all_learnings, source_files = _read_all_learnings(learnings_dir)
    if not all_learnings:
        logger.info("No pending learnings to consolidate")
    else:
        try:
            client = await create_client(component="dream")
            logger.info("Dream (auto) using %s", type(client).__name__)

            # Stage 1 — generalize. IDENTICAL to report mode: same _PROPOSAL_SYSTEM,
            # same call. All the diary-vs-memory judgment happens here. The only
            # difference from report mode is that we apply the result instead of
            # waiting for /dream-remember approval.
            memory_contents = _read_memory_files(memory_dir)
            proposal = await _generate_proposal(
                client, all_learnings, memory_contents, source_files
            )

            # Audit trail: write the same proposal artifact report mode would,
            # without clobbering a prior same-day artifact.
            dreams_dir = paths.dreams_dir()
            dreams_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            proposal_file = _proposal_output_path(dreams_dir, today)
            proposal_file.write_text(proposal)

            # Stage 2 — mechanically apply each file's slice of the proposal.
            # Files are independent, so apply them concurrently.
            per_file = _split_proposal_by_file(proposal)
            logger.info("Dream (auto) proposal targets %d files: %s",
                        len(per_file), ", ".join(sorted(per_file)) or "none")

            targets = []
            for filename, section in per_file.items():
                memory_file = memory_dir / filename
                if not memory_file.exists():
                    logger.warning("Proposal targets unknown file %s — skipped", filename)
                    continue
                targets.append((filename, memory_file, section))

            results = await asyncio.gather(*(
                _apply_proposal_to_file(client, mf, section)
                for _, mf, section in targets
            ))

            updated_count = 0
            failed_count = 0
            for (filename, memory_file, _), updated_content in zip(targets, results):
                if updated_content:
                    try:
                        # Atomic, like the triage path: the learnings this came from
                        # are unlinked below, so a half-written file here is the one
                        # loss with no source left to retry from.
                        _atomic_write(memory_file, _refresh_last_updated(updated_content, today))
                    except OSError:
                        # Same reasoning as the triage path: an unhandled OSError on
                        # the second file aborted the run with the first already
                        # rewritten and `state` never saved. Counting it as a failed
                        # apply keeps every source learnings file on disk (the
                        # `failed_count == 0` guard below), so the next run retries
                        # from intact material instead of losing it.
                        failed_count += 1
                        logger.exception("Could not write %s — not applied", filename)
                        continue
                    updated_count += 1
                    logger.info("Applied updates to %s", filename)
                else:
                    failed_count += 1

            # Only delete the raw learnings once every target that was supposed
            # to change actually did. If any apply failed (API outage, unsafe
            # output), keep the backlog so the next run can retry — deleting it
            # here would lose the source insights with nothing persisted.
            #
            # The residual, stated rather than fixed: this is all-or-nothing per
            # run, so a partial failure leaves the memory files that *did* write
            # already rewritten while every learnings file is retained. The next
            # run therefore re-consolidates material that is now in memory. That
            # is the deliberate trade — re-proposing a line is recoverable and
            # the cross-file dedup gate should catch it before it reaches the
            # reviewer, where deleting a source whose memory write failed is
            # not. Per-file retention would need the ledger to record which
            # blocks fed which target, which it does not.
            if failed_count == 0:
                for f in source_files:
                    try:
                        dest = _archive_learning(f)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        logger.exception("Could not archive %s — left in place", f.name)
                        continue
                    logger.info("Archived processed learnings: %s -> %s", f.name, dest)
                # Fully applied → the audit artifact is no longer pending;
                # archive it so the dreams root holds only pending proposals
                # (dream-remember Step 1 must never re-present it). On any
                # failure it stays put alongside the kept learnings, as a
                # recovery path for a human review.
                try:
                    archived = _archive_proposal(proposal_file, dreams_dir)
                    logger.info("Archived auto-applied proposal to %s", archived)
                except OSError:
                    logger.exception("Could not archive %s — left in dreams root", proposal_file)
            else:
                logger.warning(
                    "Kept %d learnings file(s): %d/%d targets failed to apply — "
                    "will retry next run",
                    len(source_files), failed_count, len(targets),
                )

            state = load_yaml(dream_state_file)
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            state["learnings_processed"] = sum(1 for _ in all_learnings.splitlines())
            state["files_updated"] = updated_count
            save_yaml(dream_state_file, state)

            logger.info("Dream (auto) complete: %d files updated", updated_count)
        except Exception:
            logger.exception("Dream (auto) consolidation failed")
            raise

    try:
        config = load_catalog_config()
        catalog_results = await generate_catalogs(config=config)
        logger.info("Catalog regeneration complete: %d generators ran", len(catalog_results))
    except Exception:
        logger.exception("Catalog generation failed (dream still complete)")

    _commit_memory_changes(memory_dir)


# ---------------------------------------------------------------------------
# Learnings garbage collection (--gc-learnings)
# ---------------------------------------------------------------------------


# ``_latest_pending_proposal`` is imported from ``lib.dream_processed`` at the
# top of this module. The rule — newest by **mtime**, root only — lives there so
# every tool that picks "the" proposal (triage here, the reviewer's pre-screen)
# resolves the same file; a same-day ``-2`` re-run sorts *before* the base name,
# so lexical order picks the oldest.


# ---------------------------------------------------------------------------
# The memory judge (--triage) — one batched model call over pending items
# ---------------------------------------------------------------------------

# Items per judge call. Small enough that one failure loses little (contract C4
# makes a failed batch contribute *zero* verdicts, so batch size is the blast
# radius of a timeout), large enough that a 194-item proposal is 8 calls rather
# than 194. Each item carries a bounded section excerpt, so the prompt size is
# roughly linear in this number.
_JUDGE_BATCH_ITEMS = 12


async def _judge_batch(client, items, target_texts, sem, index: int, total: int):
    """One judge call over one batch. Returns ``(verdicts, ok)``.

    **Fails closed**, which is the one way this differs from ``_critique_batch``
    above — and the difference is deliberate, not an oversight to be tidied up.
    A lost critique loses a cosmetic edit; a lost verdict, if it defaulted the
    other way, would write something into memory that no model ever approved. So
    a batch that times out, 429s past its retry, or returns garbage contributes
    **nothing**, and every one of its items keeps the conservative default.

    A rate limit still gets the single retry ``_critique_batch`` gets, for the
    same reason: the batch was fine, the account was busy, and the pass's own
    fan-out is usually what made it busy.
    """
    from lib import memory_judge

    content = memory_judge.render_batch(items, target_texts)
    n_bytes = len(content.encode("utf-8"))

    async with sem:
        started = time.monotonic()
        response = None
        for attempt in range(1, _RATE_LIMIT_ATTEMPTS + 1):
            try:
                response = await _query(
                    client, memory_judge.SYSTEM,
                    [{"role": "user", "content": content}], CHUNK_TIMEOUT_S,
                )
                break
            except Exception as exc:
                if attempt < _RATE_LIMIT_ATTEMPTS and _is_rate_limit(exc):
                    logger.warning(
                        "Judge batch %d/%d rate limited (%s) — retrying once in "
                        "%.0fs", index, total, exc, _RATE_LIMIT_BACKOFF_S,
                    )
                    await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
                    continue
                logger.exception(
                    "Judge batch %d/%d failed (%d items, %d bytes) — its %d item(s) "
                    "keep the conservative default and stay pending",
                    index, total, len(items), n_bytes, len(items),
                )
                return {}, False
        elapsed = max(time.monotonic() - started, 1e-6)

    if response is None:  # pragma: no cover - defensive
        return {}, False

    # The batch's own labels are passed in, so a well-formed verdict line for
    # anything else — including one echoed out of a learning's own text — is
    # unaddressable rather than merely implausible.
    verdicts = memory_judge.parse_verdicts(
        response.content or "", memory_judge.batch_labels(items),
    )
    logger.info(
        "Judge batch %d/%d: %d item(s) in, %d verdict(s) out, %.0fs",
        index, total, len(items), len(verdicts), elapsed,
    )
    if not verdicts:
        # An empty parse is indistinguishable from a failed call as far as the
        # partition goes, but not as far as diagnosis goes: this one produced a
        # reply the parser refused. Either it matched nothing (a prompt or format
        # problem) or it was discarded wholesale — a duplicate label, which
        # `parse_verdicts` logs with the label. Both mean the batch's items keep
        # their conservative default, which is why one branch covers both.
        logger.warning(
            "Judge batch %d/%d yielded no usable verdict lines — its %d "
            "item(s) keep the conservative default", index, total, len(items),
        )
        return {}, False
    return verdicts, True


async def _judge_items(
    client, items, memory_dir: Path, cache_path: Path, *, persist_cache: bool = True
):
    """Judge every item in *items*. Returns ``(verdicts, unjudged_count)``.

    Cached verdicts short-circuit the call entirely: the same item must not
    classify differently across runs, or a receipt is impossible to reason
    about, and a killed run should resume rather than pay to re-judge what it
    already decided.

    ``persist_cache=False`` reads the cache but does not write it, so a
    ``--dry-run`` leaves no state behind for a later real run to apply from. A
    dry run is a preview, and a preview that changes what the next real run
    would do is not one.

    A ``client`` of ``None`` (no SDK — contract C3) is not an error path here;
    it simply produces zero verdicts, and zero verdicts is the same partition
    ``review`` mode produces.
    """
    from lib import memory_judge

    cache = memory_judge.load_cache(cache_path)
    known, pending = memory_judge.cached_verdicts(items, cache)
    if known:
        logger.info("Judge: %d verdict(s) served from cache", len(known))
    if not pending:
        return known, 0
    if client is None:
        return known, len(pending)

    target_texts: dict[str, str] = {}
    for item in pending:
        if item.target in target_texts:
            continue
        path = memory_dir / item.target
        try:
            target_texts[item.target] = path.read_text()
        except OSError:
            target_texts[item.target] = ""

    batches = [
        pending[i:i + _JUDGE_BATCH_ITEMS]
        for i in range(0, len(pending), _JUDGE_BATCH_ITEMS)
    ]
    sem = asyncio.Semaphore(_concurrency())
    results = await asyncio.gather(*(
        _judge_batch(client, batch, target_texts, sem, i, len(batches))
        for i, batch in enumerate(batches, 1)
    ))

    fresh: dict[tuple[str, int], object] = {}
    unjudged = 0
    for batch, (verdicts, ok) in zip(batches, results):
        if not ok:
            unjudged += len(batch)
            continue
        for item in batch:
            hit = verdicts.get((item.target, item.number))
            if hit is None:
                # The batch succeeded but skipped this item. It is not a batch
                # failure, and it is still an item with no verdict.
                unjudged += 1
                continue
            fresh[(item.target, item.number)] = hit
            cache[memory_judge.item_key(item)] = hit

    if fresh and persist_cache:
        try:
            memory_judge.save_cache(cache_path, cache)
        except OSError:
            logger.exception("Judge: could not write the verdict cache %s", cache_path)
    if unjudged:
        logger.warning(
            "Judge: %d item(s) kept a conservative default because their batch "
            "failed or skipped them", unjudged,
        )
    known.update(fresh)
    return known, unjudged


async def dream_triage(proposal_arg: str | None, *, dry_run: bool) -> int:
    """Classify a proposal with a model, apply what clears, leave the rest.

    The reviewing bottleneck was never judgement, it was volume: a ~190-item
    proposal costs a whole context window to walk item by item, so reviews got
    abandoned partway and the backlog grew instead of shrinking.

    Three layers decide each item, and only one of them is a model.
    ``lib.dream_triage.classify`` computes what the provenance/kind rubric
    *permits* — nothing is applied on that alone. ``_judge_items`` asks a
    separate model call, which never learns it is grading a sibling pass's
    output, to re-derive the pair, check the citation against the claim, check
    the target file for redundancy, and return apply/review/drop; it may only
    ever make an item more conservative. ``lib.memory_write_floor`` then refuses
    anything whose target, verb or parse is wrong, after the verdict, where no
    prompt can reach it.

    What that buys, concretely: a model failure of any kind — a timeout, a rate
    limit, an unparseable reply, no SDK at all — produces zero verdicts, and
    zero verdicts is the same partition ``review`` mode produces. Degradation
    goes toward more human review, never less.

    Takes the same exclusive run lock as report and ``--auto`` modes: this
    writes memory files, which is the case the lock exists for.

    Returns a process exit code.
    """
    from lib import dream_triage as triage_lib
    from lib import memory_judge, memory_write_floor, rejections

    if not acquire_run_lock():
        return 0

    paths = get_paths()
    dreams_dir = paths.dreams_dir()
    memory_dir = paths.memory_dir()

    proposal_path = Path(proposal_arg) if proposal_arg else _latest_pending_proposal(dreams_dir)
    if proposal_path is None:
        print("triage: no pending proposal in the dreams root — nothing to do")
        return 0
    if not proposal_path.is_file():
        print(f"ERROR: proposal not found: {proposal_path}")
        return 1
    print(f"triage: reading {proposal_path}")

    proposal = proposal_path.read_text()
    if not triage_lib.has_routing_section(proposal):
        # `flagged_by_routing` returns an empty set both when nothing was
        # flagged and when the section is absent, so a proposal written before
        # routing validation existed — or by a drafter that failed to emit it —
        # would reach the judge with the gate's evidence silently missing, on
        # exactly the proposals where that evidence matters most. Refuse.
        print(
            f"ERROR: {proposal_path.name} has no '## Routing Warnings' section, so "
            "routing-flagged items cannot be identified and the judge would be "
            "given incomplete evidence. Refusing to apply anything — review this "
            "proposal by hand, or regenerate it."
        )
        return 1

    dupes = triage_lib.duplicate_labels(proposal)
    if dupes:
        # `(target, number)` is the item identity the judge labels by and the
        # verdict lookup keys on. Two items sharing one label both resolve to
        # the same verdict, so one is written on a judgement rendered about the
        # other's text. Renumbering would silently move existing receipts and
        # cached verdicts onto different items, so refuse instead.
        listed = ", ".join(f"{t}#{n}" for t, n in dupes)
        print(
            f"ERROR: {proposal_path.name} numbers these items more than once: "
            f"{listed}. Two items with one label cannot be judged separately — "
            "each would be written on the other's verdict. Refusing to apply "
            "anything — renumber them by hand, or regenerate the proposal."
        )
        return 1

    triage = triage_lib.classify(proposal)
    if triage.total == 0:
        print(f"triage: {proposal_path.name} has no pending update items")
        return 0

    mode = triage_lib.write_mode()
    if mode == "review":
        # Nothing is judged and nothing is written: this is today's flow, kept
        # as a mode so turning the feature off is a config change rather than
        # an uninstall. The partition is still printed, because knowing what
        # *would* have applied is the point of leaving the mode reachable.
        print(f"triage: {proposal_path.name} — {triage.total} pending item(s)")
        print("memory_write_mode=review — nothing judged, nothing applied.")
        print(triage_lib.render_summary(
            triage, applied_count=0, receipt_path="(none — review mode)",
            dry_run=True, mode=mode,
        ))
        return 0

    # Contract C3: no SDK must never be a hard failure, and must never widen
    # what gets written. A client we cannot create yields zero verdicts, and
    # zero verdicts is the `review`-mode partition.
    client = None
    try:
        client = await create_client(component="dream")
    except Exception:
        logger.exception(
            "triage: no model client — every item keeps its conservative default "
            "and nothing is applied"
        )
        print("triage: no model client available — falling back to review for "
              "every item (nothing applied).")

    verdicts, unjudged = await _judge_items(
        client, triage.review, memory_dir,
        memory_judge.default_cache_path(paths.data_dir()),
        persist_cache=not dry_run,
    )
    # Per-item first, then the breakdown. The bare count this used to log could
    # not distinguish label noise from genuine caution, and resolution is
    # one-way — see `triage_lib.reconciliation_detail` (issue #203).
    disagreements = 0
    prov_disagreed = kind_disagreed = 0
    won: dict[str, int] = {}
    resolved_kinds: dict[str, dict[str, int]] = {}
    for item in triage.review:
        detail = triage_lib.reconciliation_detail(
            item, verdicts.get((item.target, item.number))
        )
        resolved_kind = str(detail["resolved_pair"]).split("/")[-1]
        bucket = resolved_kinds.setdefault(resolved_kind, {})
        bucket[str(detail["kind_won"])] = bucket.get(str(detail["kind_won"]), 0) + 1
        if not detail["disagreed"]:
            continue
        disagreements += 1
        prov_disagreed += bool(detail["provenance_disagreed"])
        kind_disagreed += bool(detail["kind_disagreed"])
        for half in ("provenance_won", "kind_won"):
            if detail[half] in ("extractor", "judge"):
                key = f"{half.split('_')[0]}:{detail[half]}"
                won[key] = won.get(key, 0) + 1
        logger.debug(
            "triage: %s#%s provenance/kind contested — extractor=%s judge=%s "
            "resolved=%s (provenance won by %s, kind won by %s)",
            item.target, item.number, detail["extractor_pair"],
            detail["judge_pair"], detail["resolved_pair"],
            detail["provenance_won"], detail["kind_won"],
        )
    if disagreements:
        logger.info(
            "triage: judge and extractor disagreed on the provenance/kind pair for "
            "%d of %d item(s); the more conservative half of each won "
            "(provenance contested %d, kind contested %d; winners %s)",
            disagreements, len(triage.review), prov_disagreed, kind_disagreed,
            ", ".join(f"{k}={v}" for k, v in sorted(won.items())) or "none",
        )
    for resolved_kind, breakdown in sorted(resolved_kinds.items()):
        # The number that decides whether the rubric or the labelling needs
        # changing: how many items landed on this kind because *both* passes
        # said so, versus because one said so and the other lost.
        logger.info(
            "triage: kind %s — %d item(s) [%s]",
            resolved_kind or "-", sum(breakdown.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(breakdown.items())),
        )
    triage = triage_lib.apply_verdicts(triage, verdicts, mode=mode)
    if unjudged:
        # `apply_verdicts` recounts this from the verdict map; the batch-level
        # number is logged above. Both should agree — if they ever do not, the
        # cache handed back a verdict for an item no batch covered.
        logger.info("triage: %d item(s) unjudged after folding verdicts",
                    triage.unjudged)

    by_file = triage.auto_by_file()
    rejections_path = rejections.default_path(paths.data_dir())

    if dry_run:
        print(f"triage (dry run): {proposal_path.name} — {triage.total} pending item(s)")
        print(triage_lib.render_summary(
            triage, applied_count=len(triage.auto),
            receipt_path="(not written)", dry_run=True, mode=mode,
        ))
        return 0

    # Rejections are logged before anything is applied. A drop writes nothing to
    # memory, so recording it first cannot leave an inconsistent state — and a
    # crash mid-apply must not lose the record of what was refused.
    if triage.dropped:
        try:
            written = rejections.append(
                rejections_path,
                triage_lib.rejection_records(
                    triage, proposal_name=proposal_path.name,
                    key_of=memory_judge.item_key,
                ),
            )
            logger.info("triage: logged %d rejection(s) to %s", written, rejections_path)
            rejections.rotate(rejections_path)
        except Exception:
            # Broader than OSError on purpose. `append` serialises arbitrary item
            # text, so a TypeError or UnicodeEncodeError is reachable, and this
            # runs BEFORE any memory write — so swallowing it is fail-closed for
            # the memory but fail-open for the audit trail, which is the right way
            # round only if the failure is reported. Losing the log must never
            # undo a decision, and must never abort the run either.
            logger.exception("triage: could not write the rejection log %s",
                             rejections_path)

    if not by_file and not triage.dropped:
        print(f"triage: {proposal_path.name} — {triage.total} pending item(s)")
        print(triage_lib.render_summary(
            triage, applied_count=0, receipt_path="(none — nothing appliable)",
            mode=mode,
        ))
        return 0

    # Apply per file, concurrently — files are independent. Each call gets a
    # *rebuilt* section holding only that file's auto items, never the
    # proposal's own text, which still contains the review items.
    targets = []
    skipped: dict[str, str] = {}
    for filename, items in sorted(by_file.items()):
        memory_file = memory_dir / filename
        # The floor has already passed on the target *string*. This is the same
        # question asked of the resolved path, because every call below follows
        # symlinks — and a symlink in the memory dir defeats both containment and
        # the reserved-filename list with a target string that looks perfect.
        path_problem = memory_write_floor.path_refusal(memory_dir, filename)
        if path_problem:
            skipped[filename] = f"refused by the write floor: {path_problem}"
            logger.error(
                "triage: refusing to write %s — %s", filename, path_problem,
            )
            continue
        if client is None:
            # Reachable only from the verdict cache: a previous run judged these
            # and this one has no SDK to run the applier. Applying needs a model
            # too, so they stay pending.
            skipped[filename] = "no model client — the applier could not run"
            continue
        if not memory_file.exists():
            skipped[filename] = "target memory file does not exist"
            logger.warning("triage: proposal targets unknown file %s — skipped", filename)
            continue
        targets.append((filename, memory_file, items, memory_file.read_text()))

    results = await asyncio.gather(*(
        _apply_proposal_to_file(client, memory_file, triage_lib.auto_slice(items))
        for _, memory_file, items, _before in targets
    ))

    applied: dict[str, list] = {}
    failed: dict[str, str] = dict(skipped)
    decisions: list[Decision] = []
    for (filename, memory_file, items, before), updated_content in zip(targets, results):
        if not updated_content:
            # The applier failed or returned something `_is_safe_memory_update`
            # rejected. Write nothing and leave every one of that file's items
            # pending — a partial apply we cannot describe is worse than none.
            failed[filename] = "applier returned no safe content — items left pending"
            logger.warning("triage: %s not applied (unsafe/empty applier result)", filename)
            continue
        not_additive = _is_additive_result(before, updated_content, [i.text for i in items])
        if not_additive:
            # Every triage item is an `add`, so the result must be a superset.
            # It is not — and nobody reviewed this, so refuse the write.
            failed[filename] = f"applier result was not purely additive: {not_additive}"
            logger.error("triage: %s not applied — %s", filename, not_additive)
            continue
        try:
            # Restamp in code, after the additive check has passed on the
            # applier's verbatim result — see `_refresh_last_updated`.
            _atomic_write(
                memory_file,
                _refresh_last_updated(
                    updated_content, datetime.now(timezone.utc).strftime("%Y-%m-%d")
                ),
            )
        except OSError as exc:
            # The receipt is written after this loop, and its guarantee is that a
            # crash between the two leaves items pending WITH a receipt rather
            # than processed with no record. An unhandled OSError on the second
            # file broke exactly that: the first file was already rewritten, and
            # nothing recorded it. Treat a failed write like a failed applier —
            # this file's items stay pending, every other file still gets its
            # receipt line, and the run completes.
            failed[filename] = f"could not write the memory file: {exc}"
            logger.exception("triage: %s not applied — write failed", filename)
            continue
        applied[filename] = items
        decisions += [
            Decision(kind="update", file=filename, index=item.number,
                     status="applied", target=filename)
            for item in items
        ]
        logger.info("triage: auto-applied %d item(s) to %s", len(items), filename)

    # A dropped item is decided: it is in the rejection log in full, and leaving
    # it pending in the proposal would put it back in front of the human, which
    # is exactly the queue `drop` exists to shorten.
    decisions += [
        Decision(kind="update", file=item.target, index=item.number,
                 status="rejected")
        for item in triage.dropped
    ]

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    receipt = triage_lib.render_receipt(
        triage, proposal_name=proposal_path.name, applied=applied,
        failed=failed, generated=generated, mode=mode,
        rejections_log=str(rejections_path),
    )
    receipts_dir = dreams_dir / "applied"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    receipt_path = receipts_dir / f"{today}-auto-apply-receipt.md"
    n = 2
    while receipt_path.exists():
        receipt_path = receipts_dir / f"{today}-auto-apply-receipt-{n}.md"
        n += 1
    receipt_path.write_text(receipt)

    # Only now mark them processed. Receipt first, so a crash between the two
    # leaves items pending with a receipt (a visible, re-runnable state) rather
    # than processed with no record of what was written.
    if decisions:
        try:
            marked, _ = mark_many_processed(proposal_path, decisions)
            logger.info("triage: marked %d item(s) processed in %s",
                        marked, proposal_path.name)
        except OSError:
            logger.exception("triage: could not mark items processed in %s — they "
                             "were applied and are in the receipt", proposal_path)

    applied_count = sum(len(v) for v in applied.values())
    print(f"triage: {proposal_path.name} — {triage.total} pending item(s)")
    print(triage_lib.render_summary(
        triage, applied_count=applied_count, receipt_path=str(receipt_path),
        mode=mode,
    ))
    if failed:
        print("")
        print(f"NOT APPLIED ({len(failed)} file(s)) — items left pending:")
        for name, reason in sorted(failed.items()):
            print(f"    {name} — {reason}")

    if applied:
        # Pathspec is the files this run actually rewrote, not `*.md`: a
        # hand-edit sitting unstaged in memory must not be swept into a commit
        # captioned as an automatic triage apply.
        _commit_memory_changes(
            memory_dir,
            pathspec=sorted(applied),
            message=f"dream: triage apply {applied_count} item(s) "
                    f"from {proposal_path.name}",
        )
        # The receipt is written before the commit on purpose (a crash between
        # the two must leave a visible, re-runnable state), so the sha it tells
        # you to revert only exists afterwards. Appending it is best-effort: the
        # receipt already names the log command that finds it.
        _append_revert_line(receipt_path, memory_dir)
    return 0


def _append_revert_line(receipt_path: Path, memory_dir: Path) -> None:
    """Append the exact `git revert` command for the commit just written."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if not sha:
            return
        with open(receipt_path, "a") as f:
            f.write(
                f"\n**Revert this batch:** `git -C {memory_dir} revert {sha}`\n"
            )
    except (OSError, subprocess.SubprocessError):
        logger.warning("triage: could not record the revert sha in the receipt",
                       exc_info=True)


def _reconcile_is_finished(text: str) -> bool:
    """The archive-authorizing predicate for ``--reconcile``: **nothing** left
    undecided, conflict resolutions included.

    Deliberately not :func:`has_pending_items` alone, which is blind to
    ``## Conflict Resolutions``. That blindness is correct where it is used (see
    :func:`lib.dream_processed.has_pending_conflicts`) and was harmless while
    only a human ran ``--stamp --archive`` at the end of a review they had just
    read. It is not harmless here: ``--reconcile`` is Step 0 of
    ``/dream-remember``, running *before* the reviewer opens anything, and what
    it authorizes is irreversible — archive, stamp, and an unconditional
    ``_gc_learnings`` that deletes the source learnings files. A proposal whose
    updates are all decided but whose conflicts are not would have been filed
    and its sources collected with those conflicts never seen, and they cannot
    be re-derived afterwards.
    """
    return not has_pending_items(text) and not has_pending_conflicts(text)


def _reconcile(*, dry_run: bool = False) -> int:
    """Finish proposals that are fully decided but still sitting in the dreams root.

    The invariant, and it needs no model call: a proposal with **zero pending
    items** that is not in ``applied/``/``rejected/``/``superseded/`` is
    finished-but-unfiled. Three things should have happened to it and did not —
    archive, stamp, collect the spent learnings.

    Why nothing else catches this (issue #202). ``/dream-remember`` Step 1 takes
    the *newest* proposal by mtime and never looks at older ones, so a stale
    fully-processed file is outside what the skill inspects and re-running it
    cannot help. Nothing stamps on the apply path, so a review that ends before
    Step 6 leaves memory changed with no state recording it and the gate reading
    "due" forever. And ``_gc_learnings`` requires the proposal to be archived
    before collecting its sources, so a missed archive silently pins the
    learnings files too — which is the compounding part: the next ``/dream``
    re-scans them and drafts items from material already consolidated.

    Returns a process exit code: 0 when the root is clean or everything was
    finished, 1 when at least one proposal could not be archived **or could not
    be read**.
    """
    paths = get_paths()
    dreams_dir = paths.dreams_dir()
    if not dreams_dir.exists():
        print("No dreams directory — nothing to reconcile.")
        return 0

    candidates = sorted(
        p for p in dreams_dir.glob("processed-learnings-*.md") if p.is_file()
    )
    if not candidates:
        print("Dreams root is empty — nothing to reconcile.")
        return 0

    finished: list[tuple[Path, str]] = []
    still_pending: list[Path] = []
    unreadable = 0
    for proposal in candidates:
        try:
            text = proposal.read_text()
        except OSError:
            # Fail closed and *visibly*, the same way `_gc_learnings` treats an
            # unreadable pending proposal. A bare `continue` put the file in
            # neither list, so the closing count under-reported the root and the
            # operator was never told which file could not be read — the one
            # thing they need in order to fix it.
            logger.exception("reconcile: could not read %s", proposal)
            print(f"ERROR: could not read {proposal.name} — left in the dreams root")
            still_pending.append(proposal)
            unreadable += 1
            continue
        if _reconcile_is_finished(text):
            finished.append((proposal, text))
        else:
            still_pending.append(proposal)

    for proposal in still_pending:
        print(f"pending:  {proposal.name} — has undecided items, left alone")

    if not finished:
        print(f"Nothing to reconcile ({len(still_pending)} proposal(s) still pending).")
        return 1 if unreadable else 0

    if dry_run:
        for proposal, _ in finished:
            print(f"WOULD FINISH: {proposal.name} — 0 pending items, not archived")
        print(
            f"reconcile (dry run): {len(finished)} finished-but-unfiled proposal(s). "
            "Re-run without --dry-run to archive, stamp and collect."
        )
        return 1 if unreadable else 0

    failures = 0
    for proposal, text in finished:
        try:
            # File it by what the review actually decided. Every item's outcome
            # is already on its `**Processed:**` line; nothing read them, so a
            # proposal rejected in full landed in `applied/` — the disposition
            # `/dream-remember` Step 6 requires `--archive-as rejected` for, and
            # the one `_gc_learnings` reads as evidence the memory was written.
            archived = _archive_proposal(proposal, dreams_dir, archive_disposition(text))
        except OSError:
            # Leave it in the root rather than half-finishing it: an un-archived
            # proposal is the state this function exists to report, so failing
            # loudly here is strictly better than stamping as if it worked.
            logger.exception("reconcile: could not archive %s", proposal)
            print(f"ERROR: could not archive {proposal.name} — left in the dreams root")
            failures += 1
            continue
        print(f"archived: {proposal.name} -> {archived.parent.name}/{archived.name}")

    # Stamp once, after the moves. `last_run` answers "has a dream been applied
    # since?", and the answer is the same whether one proposal was filed or
    # three. Only stamp if something actually moved — otherwise a run that
    # failed every archive would silence the gate it should be tripping.
    if failures < len(finished):
        dream_state_file = paths.dream_state_file()
        state = load_yaml(dream_state_file) or {}
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_yaml(dream_state_file, state)
        print(f"stamped:  dream_state last_run={state['last_run']}")

        # Now that the proposals are archived, `_gc_learnings` can see their
        # sources as spent. It applies its own per-file conditions and explains
        # everything it keeps, so it is safe to call unconditionally here.
        _gc_learnings()

    return 1 if (failures or unreadable) else 0


def _archive_learning(f: Path) -> Path:
    """Move a spent learnings file into ``archived/`` beside it. Never deletes.

    ``.multiplai/learnings/`` is not a git repo, so an unlink here is
    unrecoverable. Raises ``OSError`` when the move fails; the caller keeps the
    file where it is. A name already in ``archived/`` is never overwritten: the
    new file gets a ``-2``, ``-3`` suffix.
    """
    dest_dir = f.parent / "archived"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # os.rename replaces an existing destination without a word, and another
    # process can take the name between a check and the rename. os.link fails
    # if the name exists, so claiming it is atomic.
    n = 1
    while True:
        dest = dest_dir / (f.name if n == 1 else f"{f.stem}-{n}{f.suffix}")
        try:
            os.link(f, dest)
            break
        except FileExistsError:
            n += 1
    os.unlink(f)
    return dest


def _gc_learnings() -> None:
    """Archive learnings files that are fully consolidated **and** fully decided.

    "Archive" means a move into ``learnings/archived/``; nothing is deleted.

    This replaces a judgement call the reviewing skill used to make in prose
    ("delete the sources, but only if the proposal is now fully decided, else
    skip this step"). That conditional was easy to get wrong and unrecoverable
    when it was, so the decision moves into code, per file, with a stated reason
    for everything kept.

    A file is archived only when **all** of these hold:

    (a) every ``## Session Learnings`` record in it hashes to a key the ledger
        has recorded — i.e. dream has already consolidated all of it. A file
        appended to since the run (today's, typically) fails this and is kept,
        which is why the old "today's file exception" no longer needs writing
        down;
    (b) no proposal those keys were recorded into is still sitting in the
        dreams root awaiting review;
    (c) every one of those proposals is *present* in ``applied/``,
        ``rejected/`` or ``superseded/``. Note this is positive evidence, not
        (b)'s absence restated. A proposal's name enters the ledger chunk by
        chunk as a run proceeds, but the file itself is only written when the
        run completes — so during a long run, and permanently after a crashed
        one, a fully-ledgered name exists in no directory at all. Absence used
        to read as "decided", which meant gc deleted the inputs of the very run
        that was still consuming them;
    (d) no proposal still in the dreams root cites the file. The ledger names
        the proposal a block was *first* consolidated into, and a fold-forward
        (``_fold_pending_proposals``) moves items into a successor and archives
        the original to ``superseded/`` without re-pointing those entries — so
        by the ledger alone the sources behind a live, unreviewed proposal look
        decided. The citations travel with the items, so reading them is what
        stays true.

    (b)-(d) all serve one property: a pending review must remain readable. Its
    ``**Source:** <file>:<line>`` citations have to resolve while the reviewer
    is still deciding.

    Anything else is kept, with the reason printed per file. Deliberately
    conservative in every direction — an unreadable pending proposal or an
    unreadable learnings file stops deletion rather than licensing it, because
    the failure being guarded against is unrecoverable and the cost of keeping
    a file too long is a few kilobytes.

    Takes no lock, and does not need one: after (c) and (d) it can no longer
    delete anything a consolidation run is mid-way through. (The previous
    version of this docstring claimed the same immunity while (c) was missing,
    which was simply untrue.)
    """
    paths = get_paths()
    learnings_dir = paths.learnings_dir
    files = sorted(learnings_dir.glob("*.md")) if learnings_dir.exists() else []
    if not files:
        print("GC learnings: no learnings files")
        return

    ledger_path = learnings_ledger.default_ledger_path()
    recorded = learnings_ledger.load(ledger_path).get("processed", {})
    dreams_dir = paths.dreams_dir()
    # Non-recursive on purpose: applied/, rejected/ and superseded/ are decided,
    # and the hub's own listing globs the root the same way.
    pending_paths = sorted(dreams_dir.glob("*.md")) if dreams_dir.exists() else []
    pending = {p.name for p in pending_paths}

    # Positive evidence of a decision, rather than inferring it from absence.
    # A proposal name is ledgered per chunk as the run goes, but the file is
    # only written when the run finishes — so between those two moments, and
    # forever after a crash, a fully-ledgered name exists in no directory at
    # all. Reading that gap as "decided" is what let gc delete a live run's
    # own inputs.
    decided: set[str] = set()
    for disposition in ("applied", "rejected", "superseded"):
        sub = dreams_dir / disposition
        if sub.exists():
            decided |= {p.name for p in sub.glob("*.md")}

    # What the pending proposals actually cite, which is the only thing that
    # stays true across a fold-forward: `_fold_pending_proposals` moves items
    # (citations and all) into a successor and archives the original to
    # superseded/, but never re-points the ledger at the successor. Going by
    # the ledger alone, those sources look decided while a reviewer is still
    # reading them.
    still_cited: set[str] = set()
    for p in pending_paths:
        try:
            still_cited |= citation_repair.cited_files(p.read_text())
        except OSError as exc:
            # Fail closed. An unreadable pending proposal is the one case where
            # we cannot know what it cites, so nothing may be deleted on the
            # strength of not having seen a citation in it.
            logger.warning("Pending proposal %s unreadable (%s) — keeping every "
                           "learnings file this pass", p.name, exc.__class__.__name__)
            print(f"GC learnings: {p.name} unreadable — nothing deleted this pass")
            return

    archived: list[str] = []
    kept: list[tuple[str, str]] = []

    for f in files:
        try:
            text = f.read_text()
        except OSError as exc:
            kept.append((f.name, f"unreadable ({exc.__class__.__name__})"))
            continue
        blocks = learnings_ledger.parse_blocks(f.name, text)
        if not blocks:
            kept.append((f.name, "no `## Session Learnings` records to consolidate"))
            continue
        entries = [learnings_ledger.lookup(recorded, b) for b in blocks]
        missing = sum(1 for e in entries if e is None)
        if missing:
            kept.append((f.name, f"{missing}/{len(blocks)} record(s) not yet consolidated"))
            continue
        proposals = {e.get("proposal") or "" for e in entries if e is not None}
        if "" in proposals:
            kept.append((f.name, "ledger entry names no proposal"))
            continue
        undecided = sorted(n for n in proposals if Path(n).name in pending)
        if undecided:
            kept.append((f.name, "proposal still pending review: " + ", ".join(undecided)))
            continue
        unwritten = sorted(n for n in proposals if Path(n).name not in decided)
        if unwritten:
            kept.append((f.name, "proposal not yet written — a run may be in "
                                 "flight or have crashed: " + ", ".join(unwritten)))
            continue
        if f.name in still_cited:
            kept.append((f.name, "still cited by a pending proposal"))
            continue
        try:
            _archive_learning(f)
        except OSError as exc:
            kept.append((f.name, f"could not archive ({exc.__class__.__name__})"))
            continue
        archived.append(f.name)

    if archived:
        remaining = {p.name for p in learnings_dir.glob("*.md")}
        learnings_ledger.prune(ledger_path, remaining)

    print(f"GC learnings: archived {len(archived)}, kept {len(kept)}")
    for name in archived:
        print(f"  archived {name}")
    for name, reason in kept:
        print(f"  kept     {name} — {reason}")
    logger.info("gc-learnings: archived=%d kept=%d", len(archived), len(kept))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _daily_dirs() -> "daily_review.Dirs":
    paths = get_paths()
    learnings = Path(paths.learnings_dir)
    return daily_review.Dirs(
        workspace=learnings.parent.parent,
        memory=Path(paths.memory_dir()),
        learnings=learnings,
        dreams=Path(paths.dreams_dir()),
    )


PRINCIPLES_TIMEOUT_S = 300.0


async def _ask_for_principles(prompt: str) -> str:
    client = await create_client(component="dream")
    response = await _query(client, principles.SYSTEM,
                            [{"role": "user", "content": prompt}], PRINCIPLES_TIMEOUT_S)
    return response.content


def _propose_principles(store, dirs, pending: list, today: str) -> list:
    """New candidate principles from the queued learnings. One model call.

    Returns [] when there is nothing to ask about or enough already waiting.
    Raises on a model or parse failure; the caller decides what that costs.
    """
    room = principles.MAX_NEW - sum(1 for c in pending if not c.existing)
    entries = principles.model_input(store, pending)
    if room <= 0 or len(entries) < principles.MIN_ENTRIES:
        return []
    book = principles.read_book(dirs.memory)
    prompt, idmap = principles.build_prompt(entries, book, principles.rejected_sentences(store),
                                            principles.feedback_notes(store, pending))
    answer = asyncio.run(_ask_for_principles(prompt))
    # The client returns text only, so size is the only cost measure here.
    print(f"Principles call: {len(entries)} learning(s), prompt {len(principles.SYSTEM) + len(prompt)} "
          f"chars, answer {len(answer)} chars.")
    logger.info("principles prompt=%d chars answer=%d chars entries=%d",
                len(principles.SYSTEM) + len(prompt), len(answer), len(entries))
    return principles.parse_candidates(
        answer, idmap, book_numbers={n for n, _ in book},
        declined=principles.load_declined(store), max_new=room, today=today,
    )


def _daily_review(today: str | None = None) -> int:
    """Sort learnings into one queue per target file and write today's review file.

    One model call groups queued learnings into candidate principles. If that
    call fails, the review is written with its facts part only. Nothing is
    deleted: entries move between files.
    """
    if not acquire_run_lock():
        return 1
    today = today or datetime.now().strftime("%Y-%m-%d")
    dirs = _daily_dirs()
    dirs.learnings.mkdir(parents=True, exist_ok=True)
    store = daily_review.Store(dirs.learnings)
    ing = daily_review.ingest(store)
    bal = daily_review.rebalance(store, today=today)
    section, exclude = "", frozenset()
    if not daily_review.review_path(dirs, today).exists():
        pending = principles.prune_pending(store, principles.load_pending(store))
        try:
            pending += _propose_principles(store, dirs, pending, today)
        except Exception as exc:
            logger.exception("principles step failed; the review has facts only")
            print(f"Principles step skipped ({exc.__class__.__name__}: {str(exc)[:120]}). "
                  "The review has facts only.")
        principles.save_pending(store, pending)
        section = principles.render_section(pending, store, dirs.memory)
        exclude = frozenset(k for c in pending for k in c.keys)
    path, n = daily_review.build_review(store, dirs, today=today,
                                        principles_section=section, exclude=exclude)
    print(f"Ingested {ing.entries} entr(ies) from {len(ing.files_archived)} file(s).")
    for name, why in ing.files_held:
        print(f"  held     {name}: {why}")
    print(f"Reserve: {bal.to_reserve} moved in, {bal.refilled} refilled, {bal.expired} expired to rejected.")
    if path is None:
        print("Nothing to review today.")
    else:
        print(f"Review file: {path} ({section.count('<!-- principle ')} principle item(s), {n} fact(s))")
    return 0


def _daily_apply(review_arg: str | None = None, today: str | None = None) -> int:
    """Apply the ticked entries of a review file. Only `yes` writes anything."""
    if not acquire_run_lock():
        return 1
    today = today or datetime.now().strftime("%Y-%m-%d")
    dirs = _daily_dirs()
    path = Path(review_arg) if review_arg else daily_review.review_path(dirs, today)
    if not path.is_file():
        print(f"No review file at {path}")
        return 1
    store = daily_review.Store(dirs.learnings)
    # Principles first: a yes moves learnings out of their queues, and the
    # facts below were already chosen to exclude them.
    prep = principles.apply_principles(path, store, dirs, today=today)
    print(f"Principles: added {len(prep.accepted)}, rejected {len(prep.rejected)}, left {len(prep.left)}.")
    for label in prep.accepted:
        print(f"  added    {label}")
    for label in prep.rejected:
        print(f"  rejected {label}")
    for label, why in prep.left:
        print(f"  left     {label}: {why}")
    rep = daily_review.apply_review(path, store, dirs, today=today, refresh=_refresh_last_updated)
    rep.written_files += prep.written_files
    print(f"Facts: applied {len(rep.applied)}, rejected {len(rep.rejected)}, left {len(rep.left)}.")
    for label in rep.applied:
        print(f"  applied  {label}")
    for label in rep.rejected:
        print(f"  rejected {label}")
    for label, why in rep.left:
        print(f"  left     {label}: {why}")
    in_memory = [p for p in rep.written_files if dirs.memory.resolve() in p.resolve().parents]
    if in_memory:
        _commit_memory_changes(
            dirs.memory,
            pathspec=[str(p.resolve().relative_to(dirs.memory.resolve())) for p in in_memory],
            message="memory: daily learnings review",
        )
    return 0


def _load_decisions(source: str) -> list[Decision]:
    """Parse a JSON array of decisions from stdin (``-``) or a file.

    stdin is the shape the reviewing skill uses: a 70-item review would not fit
    argv, and a heredoc needs no shell quoting rules written into a SKILL.md.
    """
    import json

    raw = sys.stdin.read() if source == "-" else Path(source).read_text()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"--decisions must be a JSON array, got {type(data).__name__}")
    return [Decision.from_dict(item) for item in data]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dream — learnings consolidation")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report the pending-learnings count, the chunk plan and the predicted "
             "wall clock, then exit. Takes no run lock, so it answers while a "
             "consolidation is in progress.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Apply changes directly to memory files without review (autonomous mode)",
    )
    parser.add_argument(
        "--triage",
        action="store_true",
        help="Apply the uncontroversial half of a pending proposal and leave the "
             "rest for review, then record what was applied in a receipt under "
             "dreams/applied/. A MODEL decides: each pending item is judged by a "
             "separate reviewing pass that can only ever be more cautious than "
             "the code rubric, and an item with no verdict — failed call, no SDK, "
             "unparseable reply — always stays pending. Anything the rubric does "
             "not already permit, anything that rewrites rather than appends, and "
             "any item targeting CLAUDE.md stays pending regardless of what the "
             "judge says; kind: RULE items are never applied in any mode. "
             "Governed by the memory_write_mode option (review/triage/auto): "
             "'review' judges nothing and applies nothing. Uses the newest "
             "pending proposal unless --proposal names one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --triage: print the partition and exit without writing to "
             "memory, without a receipt, and without a commit. It DOES call the "
             "model — the partition is the judge's answer, so a preview that "
             "skipped judging could only ever report 'nothing would apply'. The "
             "verdict cache is not written either, so a dry run leaves nothing "
             "behind for a later real run to apply from.",
    )
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="Record that a consolidation was applied (updates dream_state). "
             "Used by /dream-remember after the human-in-the-loop apply so the "
             "dream gate stops nudging.",
    )
    parser.add_argument("--files-updated", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--learnings-processed", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--archive",
        metavar="PROPOSAL_PATH",
        help="With --stamp: move the reviewed proposal file out of the dreams "
             "root into applied/ (or rejected/, per --archive-as), so the root "
             "holds only pending proposals. Collision-safe.",
    )
    parser.add_argument(
        "--archive-as",
        choices=("applied", "rejected"),
        default="applied",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Find proposals in the dreams root with zero pending items and "
             "finish them: archive, stamp dream_state, then collect spent "
             "learnings. A fully-decided proposal left in the root is "
             "indistinguishable from a pending one — it keeps the dream gate "
             "nudging and pins the learnings files its sources came from. "
             "Combine with --dry-run to report without changing anything.",
    )
    parser.add_argument(
        "--mark-processed",
        action="store_true",
        help="Move decided proposal items into the proposal's `## Processed` "
             "section — the in-file decision record the multiplai-gui hub writes "
             "too (natively; it does not call this script for it). Batch form: "
             "--proposal PATH --decisions - with a JSON array on stdin, which is "
             "what /dream-remember uses, one call per target file. Single-item "
             "form: --proposal, --kind, --index, --status (and --file for updates).",
    )
    parser.add_argument("--proposal", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument(
        "--decisions",
        metavar="PATH_OR_DASH",
        help="With --mark-processed: read a JSON array of decisions from this "
             "file, or from stdin when given `-`. Each element is "
             '{"kind","file","index","status","target"} — kind update|action, '
             "status applied|edited|rejected, file names the `## Updates for` "
             "group, target the memory file actually written.",
    )
    parser.add_argument(
        "--kind",
        choices=("update", "action", "conflict"),
        default="update",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--file", metavar="TARGET.md", help=argparse.SUPPRESS)
    parser.add_argument(
        "--status", choices=("applied", "edited", "rejected"), help=argparse.SUPPRESS
    )
    parser.add_argument("--target", metavar="TARGET.md", help=argparse.SUPPRESS)
    parser.add_argument(
        "--gc-learnings",
        action="store_true",
        help="Delete learnings files that are fully consolidated (every record "
             "in the ledger) and fully decided (no proposal citing them is still "
             "pending). Pure code, no model call, no lock. Prints what it removed "
             "and why it kept the rest.",
    )
    parser.add_argument(
        "--daily-review",
        action="store_true",
        help="Sort learnings into one queue per target file (at most 10 each, the "
             "rest to archived/reserve.md), ask the model for up to 3 candidate "
             "principles, and write dreams/review-YYYY-MM-DD.md with those and the "
             "top fact of each queue. Deletes nothing.",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Apply the ticked entries of today's review file (or --review FILE). "
             "Only a ticked `yes` writes to a memory or project file.",
    )
    parser.add_argument("--review", metavar="FILE", help="With --daily: the review file to apply.")
    args = parser.parse_args()

    # `--dry-run` is read only by the paths that name it below. Without this,
    # `dream.py --dry-run` silently runs a full consolidation and writes a
    # proposal — the opposite of what the flag's name promises. Widen this
    # tuple when a new path honours the flag; never drop the guard.
    if args.dry_run and not (args.triage or args.reconcile):
        parser.error("--dry-run requires --triage or --reconcile")

    if args.mark_processed and args.decisions:
        if not args.proposal:
            print("ERROR: --mark-processed --decisions needs --proposal")
            sys.exit(2)
        proposal_path = Path(args.proposal)
        if not proposal_path.is_file():
            print(f"ERROR: --proposal path not found: {proposal_path}")
            sys.exit(1)
        try:
            decisions = _load_decisions(args.decisions)
        except (OSError, ValueError) as exc:
            print(f"ERROR: --decisions could not be read: {exc}")
            sys.exit(2)
        if not decisions:
            print("marked 0 processed, 0 unchanged")
            return
        try:
            marked, unchanged = mark_many_processed(proposal_path, decisions)
        except OSError as exc:
            # The new document is built in memory and swapped in atomically, so
            # a write failure here leaves the proposal exactly as it was.
            print(f"ERROR: could not write {proposal_path}: {exc} — proposal unchanged")
            sys.exit(1)
        print(f"marked {marked} processed, {unchanged} unchanged")
        return

    if args.mark_processed:
        if not args.proposal or args.index is None or not args.status:
            print("ERROR: --mark-processed needs --proposal, --index and --status")
            sys.exit(2)
        proposal_path = Path(args.proposal)
        if not proposal_path.is_file():
            print(f"ERROR: --proposal path not found: {proposal_path}")
            sys.exit(1)
        if args.kind == "update":
            if not args.file:
                print("ERROR: --mark-processed --kind update needs --file")
                sys.exit(2)
            ref: tuple = ("update", args.file, args.index)
            label = f"update {args.file}#{args.index}"
        elif args.kind == "conflict":
            # A conflict has no `### N.` number — it is keyed by the memory line
            # it proposes to supersede, so --file is the memory file and --index
            # is the line number in it.
            if not args.file:
                print(
                    "ERROR: --mark-processed --kind conflict needs --file "
                    "(the memory file named in the heading); --index is its line number"
                )
                sys.exit(2)
            ref = ("conflict", args.file, args.index)
            label = f"conflict {args.file}:{args.index}"
        else:
            ref = ("action", args.index)
            label = f"action A{args.index}"
        changed = mark_processed(proposal_path, ref, args.status, target=args.target)
        if changed:
            print(f"Marked {label} as {args.status} in {proposal_path.name}")
        else:
            print(f"No change: {label} not pending (already processed or not found)")
        return

    if args.stamp:
        paths = get_paths()
        dream_state_file = paths.dream_state_file()
        state = load_yaml(dream_state_file) or {}
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["files_updated"] = args.files_updated
        state["learnings_processed"] = args.learnings_processed
        save_yaml(dream_state_file, state)
        print(f"Stamped dream_state: last_run={state['last_run']}")
        if args.archive:
            proposal_path = Path(args.archive)
            if not proposal_path.is_file():
                print(f"ERROR: --archive path not found: {proposal_path}")
                sys.exit(1)
            # Backstop: a proposal is archivable only once every item is decided
            # (moved under ## Processed). Undecided items outside that section
            # would be silently discarded by the move — leave it pending instead.
            if has_pending_items(proposal_path.read_text()):
                print(
                    f"ERROR: {proposal_path.name} still has pending items (not under "
                    "## Processed) — left pending, not archived. Decide or move them first."
                )
                sys.exit(3)
            archived = _archive_proposal(
                proposal_path, paths.dreams_dir(), args.archive_as
            )
            print(f"Archived proposal to {archived}")
        return

    if args.daily_review:
        sys.exit(_daily_review())

    if args.daily:
        sys.exit(_daily_apply(args.review))

    if args.reconcile:
        sys.exit(_reconcile(dry_run=args.dry_run))

    if args.gc_learnings:
        _gc_learnings()
        return

    if args.check:
        # Deliberately takes NO lock: the plan is read-only, and its whole point
        # is to be answerable while a run is in progress.
        from lib import dream_chunking

        paths = get_paths()
        pending, chunks, files, throughput = _plan_run(paths.learnings_dir)
        if not files:
            print("No pending learnings")
            return
        if not pending:
            print(f"No new learnings ({len(files)} file(s), all blocks already consolidated)")
            return
        new_bytes = sum(len(b.text.encode("utf-8")) for b in pending)
        total_bytes = 0
        for f in files:
            try:
                total_bytes += len(f.read_text().encode("utf-8"))
            except OSError:
                pass
        # Wall clock, not serial time: chunks run `concurrency` at a time, so a
        # wave costs its slowest chunk rather than the sum of its chunks.
        eta = dream_chunking.estimate_wall_clock(chunks, _concurrency(), throughput)
        print(
            f"Pending learnings: {len(files)} file(s), {len(pending)} new block(s), "
            f"{new_bytes} new bytes of {total_bytes}"
        )
        print(_plan_line(new_bytes=new_bytes, total_bytes=total_bytes,
                         chunks=chunks, throughput=throughput))
        # A floor, not an estimate — see estimate_wall_clock. Saying "at least"
        # is the difference between under-promising and being wrong.
        print(f"Estimated wall clock: at least {eta / 60:.0f} min")
        return

    if args.triage:
        sys.exit(asyncio.run(dream_triage(args.proposal, dry_run=args.dry_run)))

    if args.auto:
        asyncio.run(dream_auto())
    else:
        asyncio.run(dream_report())


if __name__ == "__main__":
    main()
