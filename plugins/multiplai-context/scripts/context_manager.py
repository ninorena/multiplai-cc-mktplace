"""Context manager hook for multiplai plugin.

Manages context assembly for user prompts. Memory and skills are read
catalog-first and routed by intent domain; resources bypass both and
come from a qmd index.

Pipeline:
    1. Read stdin (cwd, prompt, transcript_path)
    2. Extract last assistant response from transcript (disambiguation)
    3. Load each enabled corpus catalog (gated on plugin options)
    4. Run multi-corpus router → per-corpus filename picks
    5. Expand picks via bundle + co_retrieve_for metadata
    6. Read picked file contents (with section_loader for memory)
    7. Emit a single JSON object with the assembled context

Catalog-first reads (Design Decision 8): when a valid catalog exists,
entries are read from the catalog. Missing or corrupt catalogs fall
back to live scanning (fail-open). Warnings emit once per session.

Memory remains the only corpus with a metadata-fallback path
(_read_top_memory_files) so prompts still get useful context even
without a current catalog. Skills are catalog-only.

Resources do not use the catalog or the router at all. When
``enable_resources`` is on and ``resources_dir`` is set, they are
retrieved per-prompt from a qmd index (scripts/qmd_retrieval.py) and
render as path+excerpt entries in the RESOURCES section.

A fourth section, DEV REFERENCES, does NOT go through the router at
all: engineering standards apply because of what the project is, not
because of how the prompt was phrased, so they are detected from the
project's manifests and injected as pointers (see lib/reference_docs.py).
"""

import json
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.config import read_session_state, write_session_state
from multiplai_core.log_utils import HookRun, hook_run, setup_logging, log_event
from lib.banks import (
    SHARED_BANK_NOTICE,
    bank_ref,
    configured_banks,
    render_shared_block,
)
from lib.banks import resolve_ref as resolve_bank_ref
from lib.banks import split_ref as split_bank_pick
from lib.hook_input import read_hook_input
from lib.hook_watchdog import make_on_watchdog
from lib.memory_router import create_router
from lib.plugin_skills import plugin_skill_owners, qualify
from lib import reference_docs
from lib.routing_logic import expand_picks
from lib.section_loader import (
    h2_names,
    load_picked_content,
    parse_section_ref,
    preamble as section_preamble,
)
from lib.transcript_helper import read_last_assistant_response
from generators.base import CATALOG_SCHEMA_VERSION
from generators.config import load_catalog_config

# Forward the router's and multiplai_core's loggers into context_manager.log.
# Without this, every line they wrote (the router's timeout/degrade warning,
# run_agent's START/DONE/FAIL and CLI debug-log path) reached only the hook's
# stderr, and context_manager.log never showed why a router call was slow.
logger = setup_logging(
    "context_manager", propagate_loggers=("multiplai_core", "lib"),
)

# Seconds into the run at which the watchdog logs the stage still running and
# probes the network. Must stay under this hook's "timeout" in
# hooks/hooks.json (30s; a test enforces the gap) with room for the probes,
# which take at most ~2.4s.
_WATCHDOG_S = 26.0

# Catalog types supported by _read_catalog_or_scan()
_KNOWN_CATALOG_TYPES = frozenset({"memory", "banks", "diary", "skills"})

# Cap on the last-assistant-response text handed to the router — see the
# truncation site in _assemble_and_emit for why (P7).
_LAST_RESPONSE_MAX_CHARS = 2000

# Once-per-session warning deduplication (Decision 8).
# Keyed by full catalog file path to correctly dedup across
# different data directories.
_catalog_warnings_emitted: set[str] = set()


@dataclass
class RankedFile:
    """A memory file ranked by metadata."""
    path: Path
    score: float


def _iter_markdown_files(directory: Path):
    """Yield ``.md`` files in *directory*, skipping non-files and subdirs."""
    if not directory.exists():
        return
    for f in directory.iterdir():
        if f.is_file() and f.suffix == ".md":
            yield f


# Scoring weights for metadata-first ranking (R2 mitigation)
_RECENCY_WEIGHT = 0.7
_SIZE_WEIGHT = 0.3
_RECENCY_DECAY_DAYS = 60
_SIZE_NORM_BYTES = 10_000


def _rank_memory_files(memory_dir: Path) -> list[RankedFile]:
    """Rank memory files by metadata (mtime, size) without reading content.

    More recently modified files and larger files score higher, ensuring
    the most relevant context is prioritised under the 5-second timeout (R2).
    """
    ranked: list[RankedFile] = []
    now = time.time()

    for f in _iter_markdown_files(memory_dir):
        # memory/CLAUDE.md is the memory system's own index, excluded from
        # the catalog (generators/memory.py discover_sources); keep the
        # recency fallback consistent — it ranks by mtime/size, and the
        # index is both recently touched and large, so it would otherwise
        # top this ranking.
        if f.name.lower() == "claude.md":
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        recency_score = max(0.0, 1.0 - age / (86400 * _RECENCY_DECAY_DAYS))
        size_score = min(1.0, st.st_size / _SIZE_NORM_BYTES)
        score = recency_score * _RECENCY_WEIGHT + size_score * _SIZE_WEIGHT
        ranked.append(RankedFile(path=f, score=score))

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def _read_top_memory_files(
    memory_dir: Path,
    *,
    max_files: int = 5,
    max_bytes: int = 40_000,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """Rank memory files by metadata, then read only the top candidates.

    This is the key R2 mitigation: avoid reading all files to stay under
    the 5-second timeout on large memory sets. Two additional guards keep the
    recency fallback (which fires on every prompt when no catalog exists yet)
    from dumping the whole memory corpus each turn:
      - ``exclude`` skips files still within the re-recommendation cooldown,
        so the fallback doesn't re-inject what a recent turn already sent.
      - ``max_bytes`` caps the total injected payload.
    """
    exclude = exclude or set()
    ranked = _rank_memory_files(memory_dir)
    result: dict[str, str] = {}
    total = 0
    for item in ranked:
        if len(result) >= max_files:
            break
        if item.path.name in exclude:
            continue
        try:
            content = item.path.read_text()
        except Exception:
            logger.warning("Failed to read ranked memory file: %s", item.path)
            continue
        if total + len(content.encode("utf-8")) > max_bytes and result:
            break
        result[item.path.name] = content
        total += len(content.encode("utf-8"))
    return result


# ---------------------------------------------------------------------------
# Catalog-first read path with fail-open fallback (Decision 8)
# ---------------------------------------------------------------------------

def _read_catalog_or_scan(catalog_type: str, ttl_hours: float | None = None) -> list[dict]:
    """Try catalog first. On any failure, fall back to an empty list.

    Implements the fail-open read path (Design Decision 8): attempt to
    read and validate a catalog JSON file for *catalog_type*.  On any
    failure — missing file, parse error, schema mismatch — log a
    once-per-session warning and return an empty list so the caller can
    fall back to live file scanning.

    Args:
        catalog_type: One of ``"memory"``, ``"banks"``, ``"diary"``
            or ``"skills"``. Anything else returns an empty list —
            notably ``"resources"``, which has not been a catalog
            since 0.52.0.
        ttl_hours: When set (> 0), a catalog older than this is flagged
            as stale via a once-per-session warning (the entries are
            still returned — staleness is advisory, not fatal).

    Returns:
        Catalog entries on success, or an empty list on any failure.
    """
    if catalog_type not in _KNOWN_CATALOG_TYPES:
        logger.debug("Unknown catalog type '%s', returning empty", catalog_type)
        return []

    catalog_file = _resolve_catalog_path(catalog_type)
    warn_key = str(catalog_file)

    try:
        if not catalog_file.exists():
            logger.debug("Catalog file not found: %s", catalog_file)
            return []

        data = json.loads(catalog_file.read_text(encoding="utf-8"))
        return _validate_catalog(data, catalog_type, warn_key, ttl_hours)

    except json.JSONDecodeError as e:
        _warn_once(warn_key, f"Catalog {catalog_type} contains invalid JSON: {e}")
        return []
    except OSError as e:
        _warn_once(warn_key, f"Error reading catalog {catalog_type}: {e}")
        return []
    except Exception as e:
        _warn_once(warn_key, f"Unexpected error reading catalog {catalog_type}: {e}")
        return []


def _resolve_catalog_path(catalog_type: str) -> Path:
    """Return the expected filesystem path for a catalog type.

    Filename convention matches what each generator writes via
    ``catalog_filename`` (e.g., ``memory.json`` for MemoryGenerator).
    """
    paths = get_paths()
    return paths.catalogs_dir() / f"{catalog_type}.json"


def _validate_catalog(
    data: object,
    catalog_type: str,
    warn_key: str,
    ttl_hours: float | None = None,
) -> list[dict]:
    """Validate parsed catalog JSON and return entries, or ``[]`` on failure.

    Checks that *data* is a dict with a matching ``schema_version`` and
    a list-typed ``entries`` field. Logs a once-per-session warning for
    each distinct validation failure.

    When *ttl_hours* is provided (> 0), a stale catalog — one whose
    ``generated_at`` timestamp is older than the TTL — is surfaced as a
    once-per-session warning so ``/health`` and the logs can flag drift.
    This is a cheap timestamp compare only: a stale catalog is still
    returned and used (fail-open); regeneration is a separate, explicit
    action (``/refresh-catalogs``), never inline on the hook path.
    """
    if not isinstance(data, dict):
        _warn_once(warn_key, f"Catalog {catalog_type} is not a JSON object, falling back")
        return []

    schema_version = data.get("schema_version")
    if schema_version is None:
        _warn_once(warn_key, f"Catalog {catalog_type} missing schema_version field, falling back")
        return []

    if schema_version != CATALOG_SCHEMA_VERSION:
        _warn_once(
            warn_key,
            f"Catalog {catalog_type} schema version mismatch: "
            f"expected {CATALOG_SCHEMA_VERSION}, got {schema_version}",
        )
        return []

    if ttl_hours:
        _warn_if_stale(data, catalog_type, warn_key, ttl_hours)

    entries = data.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        _warn_once(warn_key, f"Catalog {catalog_type} entries is not a list, falling back")
        return []

    return entries


def _warn_if_stale(
    data: dict, catalog_type: str, warn_key: str, ttl_hours: float
) -> None:
    """Warn once when a catalog's ``generated_at`` exceeds *ttl_hours*.

    Fail-open: an unparseable/missing timestamp or a negative TTL is
    silently ignored — staleness reporting must never break the read
    path. The compare is a single subtraction, safe on the per-prompt
    hook budget.
    """
    if ttl_hours <= 0:
        return
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        return
    try:
        gen = datetime.fromisoformat(generated_at)
    except ValueError:
        return
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - gen).total_seconds() / 3600.0
    if age_hours > ttl_hours:
        _warn_once(
            f"{warn_key}::stale",
            f"Catalog {catalog_type} is stale: generated {age_hours:.1f}h ago "
            f"(TTL {ttl_hours:.0f}h). Run /refresh-catalogs to regenerate.",
        )


def _warn_once(warn_key: str, message: str) -> None:
    """Log *message* at WARNING level, but only once per session per *warn_key*.

    Subsequent calls with the same key are demoted to DEBUG to avoid
    log spam (Design Decision 8).
    """
    if warn_key in _catalog_warnings_emitted:
        logger.debug("Suppressed repeated warning for %s: %s", warn_key, message)
        return
    _catalog_warnings_emitted.add(warn_key)
    logger.warning(message)


# ---------------------------------------------------------------------------
# Multi-corpus catalog loading
# ---------------------------------------------------------------------------

def _load_corpora(cfg) -> dict[str, list[dict]]:
    """Read all enabled catalogs.

    Memory is always loaded. Skills are gated on ``enable_skills`` —
    when disabled, the corpus is treated as empty (no routing, no
    content loading). Resources never enter the router at all; the
    key stays in the dict so downstream shapes are uniform, and
    _qmd_resources_entries() fills it.

    Shared memory banks (``banks.json``) are folded into the **memory**
    corpus rather than routed as a corpus of their own: a bank's file
    answers the same kind of question a personal file does, and the router
    must be free to pick it over a personal one on relevance alone. What
    distinguishes them is trust, not routing, and trust is applied at
    render time (``_render_memory_section``) where it belongs.
    """
    ttl_hours = getattr(cfg, "ttl_hours", None)
    corpora: dict[str, list[dict]] = {
        "memory": _read_catalog_or_scan("memory", ttl_hours)
        + _read_catalog_or_scan("banks", ttl_hours),
        "skills": [],
        "resources": [],
    }
    if cfg.enable_skills:
        corpora["skills"] = _read_catalog_or_scan("skills", ttl_hours)
    return corpora


# ---------------------------------------------------------------------------
# Per-corpus content loading
# ---------------------------------------------------------------------------


def _memory_path_for(pick: str, memory_dir: Path, banks) -> Path | None:
    """The file a router pick names, across every subscribed bank.

    A bare ``file.md`` (or ``file.md#Section``) is a personal-bank pick and
    resolves exactly as it always has. A ``bank/file.md`` pick resolves
    through the named bank, and resolves to ``None`` when no such bank is
    subscribed — never to a personal file of that name, which is how a pick
    left over in a stale catalog would otherwise read someone else's note
    from the user's own corpus.
    """
    if banks is None:
        base, _ = parse_section_ref(pick)
        return memory_dir / base
    resolved = resolve_bank_ref(pick, banks)
    if resolved is None:
        logger.debug("Picked memory ref resolves to no subscribed bank: %s", pick)
        return None
    return resolved[2]


def _load_memory_content(
    memory_dir: Path, picks: list[str], banks=None
) -> dict[str, str]:
    """Read picked memory files; honor ``filename#Section`` for partial loads.

    Returns ``{display_name: content}``. ``display_name`` is the raw pick
    (``"file.md#Section"``, ``"team/file.md"``) so the assembled context shows
    which slice, from which bank, was loaded — **except** when the whole file
    was loaded, where the key is the canonical bare ref from
    :func:`_whole_file_ref`. Several picks can collapse onto that one key, so
    the keys are not the picks; :func:`_cooldown_keys` maps back.

    *banks* defaults to ``None``, which means "personal only" and is the
    pre-banks behaviour verbatim — the parameter is additive so existing
    callers and tests need no edit.

    Three things happen per **file** rather than per pick, because each is a
    property of the file and none can be decided one pick at a time. They
    apply to bank files exactly as they do to personal ones:

    * **A whole-file pick absorbs that file's section picks.** The router may
      legitimately emit ``["team/dev.md", "team/dev.md#Testing"]``; loading both
      injected the section twice and made the byte attribution report more bytes
      than the file has.
    * **An unresolvable anchor absorbs the file's other picks too.** A stale
      anchor still falls back to the whole file — that is the safety claim
      ``TestWrongAnchorFallsBackToFullFile`` pins, and it does not change. What
      changes is that the fallback now happens **once**. Per pick, two stale
      anchors on one file injected that file twice: measured 2026-08-30 on the
      real corpus, where two ``ai-agent-patterns.md`` section picks logged
      64,817 B each against a ~65 KB file. Nothing upstream validates the
      fragment against ``section_anchors``, so catalog↔file drift reaches here
      unfiltered and used to be amplified by it rather than caught.
    * **The file's pre-first-H2 preamble is prepended once.** No section slice
      contains it, so without this a section pick silently returns less than the
      whole file would have — including cross-file load instructions. Once per
      file, not once per section, since several picks from one file are normal
      for an anchored file.
    """
    # Group by the ref the picks address, preserving first-appearance order.
    # A repeated pick is one pick: `memory_router._parse_llm_multi_selection`
    # de-duplicates, but this function is called with picks assembled by
    # `expand_bundles` and by tests too, and a duplicate used to overwrite its
    # own entry with the copy that skips the preamble.
    by_ref: dict[str, list[str]] = {}
    seen_picks: set[str] = set()
    for pick in picks:
        if pick in seen_picks:
            continue
        seen_picks.add(pick)
        bank_name, filename, _section = split_bank_pick(pick)
        by_ref.setdefault(f"{bank_name}/{filename}", []).append(pick)

    result: dict[str, str] = {}
    for ref_picks in by_ref.values():
        path = _memory_path_for(ref_picks[0], memory_dir, banks)
        if path is None:
            continue
        if not path.exists():
            logger.debug("Picked memory file missing: %s", path)
            continue
        try:
            text = path.read_text()
        except OSError:
            logger.warning("Failed to read router-picked memory file: %s", path)
            continue

        whole_ref = _whole_file_ref(ref_picks[0])
        whole = next(
            (p for p in ref_picks if split_bank_pick(p)[2] is None), None
        )

        # Which anchors this file can answer is a property of the file, so it
        # is settled once, here, before either branch decides what to emit —
        # including the whole-file branch, where a stale anchor riding along
        # with a bare pick used to be absorbed without ever being named.
        #
        # `h2_names` is the single place both the catalog generator and
        # `extract_section` learn a file's section names, so "the fragment is
        # not in this list" is exactly what catalog↔file drift means. The
        # earlier signal — `extract_section` returning the object it was handed
        # — also fired for an empty file and for a file with no H2 at all,
        # neither of which is drift and neither of which regenerating a catalog
        # can fix.
        names = {n.strip().lower() for n in h2_names(text)}
        unresolved = [
            p for p in ref_picks
            if (frag := split_bank_pick(p)[2]) is not None
            and frag.strip().lower() not in names
        ]
        if unresolved and names:
            # Once per session per file+anchor set: the drift persists until
            # someone regenerates the catalog, and this hook runs on every
            # prompt, so an un-deduped warning is one WARNING per prompt for
            # as long as the rename stands.
            joined = ", ".join(_loggable(p) for p in unresolved)
            _warn_once(
                f"anchor::{whole_ref}::{joined}",
                f"Memory anchor(s) {joined} did not resolve in {whole_ref} — "
                f"loading the whole file once. Run "
                f"/multiplai-context:refresh-catalogs --only memory if a "
                f"section was renamed.",
            )
        elif unresolved:
            logger.debug(
                "Memory file %s has no H2 sections; %d section pick(s) load "
                "the whole file", whole_ref, len(unresolved),
            )

        if whole is not None or unresolved:
            if whole is not None and len(ref_picks) > 1:
                logger.debug(
                    "Memory pick %s covers %d section pick(s) of the same file — "
                    "loading the file once", _loggable(whole), len(ref_picks) - 1,
                )
            # The whole file answers every pick of it, so emit it once — under
            # the canonical **bare** ref, bank prefix intact. Keying it on a
            # fragment pick would make `_section_attribution` log a section
            # list for a whole-file load, and an empty list there is precisely
            # how a whole-file load is recorded. That mis-attribution is what
            # let the duplicate hide: the log read as "two sections", not "the
            # file, twice".
            result[whole_ref] = text
            continue

        # Only now is extraction worth doing: every anchor resolved, so no
        # slice built here is about to be discarded for the whole file.
        head = section_preamble(text)
        for index, pick in enumerate(ref_picks):
            # load_picked_content returns (filename, content_or_section)
            _, content = load_picked_content(_strip_bank(pick), text)
            if head and index == 0 and not content.startswith(head):
                content = f"{head}\n{content}"
            result[pick] = content
    return result


def _whole_file_ref(pick: str) -> str:
    """The canonical bare-file ref for *pick*: ``"dev.md"``, ``"team/dev.md"``.

    Built through ``split_bank_pick``/``bank_ref`` rather than by cutting the
    string at its ``#``, so one file cannot reach the cooldown map and the
    byte attribution under two spellings. ``split_bank_ref`` lower-cases and
    strips the bank segment, so a hand-cut ``"Team/dev.md#Gone"`` groups under
    ``"team/dev.md"`` and would then be emitted under ``"Team/dev.md"``.
    """
    bank_name, filename, _ = split_bank_pick(pick)
    return bank_ref(bank_name, filename)


# Control characters are stripped, not escaped, and the ref is capped: a
# section fragment is free text the routing model emitted (only the part
# before the ``#`` is checked against the known-name set), and a newline in it
# would write a second, attacker-shaped line into context_manager.log — the
# file the log-doctor skill parses.
_LOGGABLE_REF_MAX = 120


def _loggable(ref: str) -> str:
    """*ref* rendered safe to interpolate into a one-line log record."""
    flat = "".join(ch for ch in str(ref) if ch.isprintable())
    return flat if len(flat) <= _LOGGABLE_REF_MAX else flat[:_LOGGABLE_REF_MAX] + "…"


def _cooldown_keys(picks: list[str], content: dict[str, str]) -> list[str]:
    """The cooldown keys for what *content* actually injected.

    ``_filter_cooldown`` looks a **raw pick** up in the cooldown map, so the
    map has to be stamped with raw picks. ``_load_memory_content`` does not
    return them: a whole-file pick and an unresolvable anchor each collapse
    several picks onto one bare-filename key. Stamping only the returned keys
    left every absorbed pick unstamped, so the router re-picked it and the
    file was re-injected on the very next turn — the fallback that should fire
    once per cooldown window fired every turn instead, which costs more than
    the duplicate load it replaced.

    Content keys are stamped as well as picks, because the recency-net
    fallback (``_read_top_memory_files``) produces content for no pick at all.

    A pick that resolved to nothing — missing file, unsubscribed bank — has no
    key and is deliberately left unstamped: it was never in the conversation,
    so it must stay eligible next turn.
    """
    keys = set(content)
    return sorted(
        keys | {p for p in picks if p in keys or _whole_file_ref(p) in keys}
    )


def _strip_bank(pick: str) -> str:
    """``"team/dev.md#Testing"`` → ``"dev.md#Testing"``.

    ``load_picked_content`` reasons about a file and a section; the bank
    prefix is addressing, not content, and would make its filename check
    fail.
    """
    _, filename, section = split_bank_pick(pick)
    return f"{filename}#{section}" if section else filename


def _build_skills_recommendations(
    cfg, picks: list[str], entries: list[dict]
) -> dict[str, str]:
    """Build lightweight skill recommendations from the catalog.

    Skills are surfaced as *pointers*, not content. Claude Code already
    exposes every skill body via the Skill tool and lists available
    skills, so injecting the full SKILL.md per prompt is redundant and
    heavy. Instead we emit the catalog summary plus the ``/<name>``
    invocation hint so the model knows a relevant skill exists and how
    to call it. No file reads — the catalog is the source of truth.
    """
    if not picks or not cfg.enable_skills:
        return {}
    by_name: dict[str, dict] = {}
    for entry in entries or []:
        key = entry.get("name") or entry.get("source")
        if key:
            by_name[key] = entry
    # A plugin-shipped skill is invoked as /plugin:skill; the bare directory
    # name is not a valid identifier for it. Suggesting the bare name is what
    # produced a measured 23.2% Skill-tool failure rate, every failure an
    # "Unknown skill" on a name this hook had just taught. See lib/plugin_skills.
    owners = plugin_skill_owners(getattr(cfg, "plugins_dir", None))
    skills_dir = getattr(cfg, "skills_dir", None)
    result: dict[str, str] = {}
    for pick in picks:
        # Skills entries don't use section refs.
        base, _ = parse_section_ref(pick)
        summary = (by_name.get(base) or {}).get("summary", "").strip()
        hint = f"Invoke with /{qualify(base, owners, skills_dir)} when relevant."
        result[base] = f"{summary}\n{hint}" if summary else hint
    return result


def _qmd_resources_entries(cfg, prompt: str, cwd: str) -> list[dict]:
    """Retrieve resources from the qmd index over ``resources_dir``.

    Returns ``[{"path", "title", "score", "snippet"}, ...]`` best first.
    The workspace root (where the project-local ``.qmd/`` index lives) is
    the session's cwd from the hook payload; when absent, fall back to the
    parent of ``resources_dir``, which sits at the workspace root in the
    standard layout. Fail-open: any error means "no resources this turn".
    """
    if not prompt or not cfg.enable_resources or not cfg.resources_dir.strip():
        return []
    try:
        import qmd_retrieval

        workspace = cwd or str(Path(cfg.resources_dir).expanduser().parent)
        target = qmd_retrieval.target_from_config(cfg, workspace)
        entries = qmd_retrieval.search(prompt, target)
        if entries:
            logger.info(
                "QMD_RETRIEVAL %s",
                json.dumps({
                    "mode": target.mode,
                    "strategy": target.strategy,
                    "picked": [[e["path"], round(e["score"], 3)] for e in entries],
                }),
            )
        return entries
    except Exception:
        logger.exception("qmd resources retrieval failed; skipping resources")
        return []


# Rendered once above qmd-retrieved resource entries: the injected text
# is excerpts, not full documents, so the model must Read before citing.
_QMD_RESOURCES_PREAMBLE = (
    "Files from the resources knowledge base matching this prompt, best "
    "first (qmd retrieval). Each entry is a chunk-level excerpt — 'line N' "
    "is where the matching chunk starts in the file. If one looks "
    "relevant, Read the full file at its path (start near that line) "
    "before answering; ignore them if the prompt is unrelated."
)


# Rendered once above injected memory. Memory is long-term state and can
# lag reality; only the model sees the full session context (documents,
# pasted files, earlier turns), so the model is the only place a
# memory-vs-session contradiction can be detected. The directive makes
# surfacing that disagreement mandatory rather than best-effort.
_MEMORY_CONFLICT_PREAMBLE = (
    "Long-term memory retrieved for this prompt. Memory can be stale. "
    "Cross-check it against everything else in this session's context — "
    "documents, files, other injected context, and user statements. If "
    "anything below disagrees with another source in the session, you "
    "MUST surface the disagreement to the user in your reply: name the "
    "memory file, quote or paraphrase both versions, and say which one "
    "you are following (prefer the newer or in-session source unless the "
    "user says otherwise). Never silently pick one side. Files below are "
    "stamped with their last-updated date, where available, to help "
    "judge staleness."
)


# In-content freshness header maintained by the dream/dream-remember
# tooling (each memory apply refreshes it). Preferred over filesystem
# mtime, which lies whenever files are re-materialized without a
# content change (git clone/checkout, rsync without -t): every file
# would then stamp as "today" and teach the model to trust stale facts.
_LAST_UPDATED_RE = re.compile(
    r"^\*\*Last Updated:\*\*\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE | re.IGNORECASE
)


def _dev_reference_block(
    prompt: str,
    cwd: str,
    state: dict,
    turn_index: int,
) -> tuple[str, Path | None]:
    """The DEV REFERENCES block for this turn, and the project it describes.

    Returns ``("", None)`` whenever there is nothing to say: no reference
    directory at all (a vanilla install — the docs ship with multiplai-kit), no
    manifest anywhere in reach of cwd or the prompt, a detected stack with no
    docs mapped to it, or a project already announced this session. At most one
    project is announced per turn, so a prompt naming three repos does not
    inject three blocks.

    Records the announcement by mutating ``state``; the caller owns
    persisting it. Never raises — the hook runs on every prompt, and a
    standards pointer is not worth a broken turn.
    """
    try:
        # Any reference subdirectory will do: `dev` is the usual one, but a kit
        # that ships only `review/` is still a kit with references (issue #204).
        dirs = reference_docs.reference_dirs()
        if not dirs:
            return "", None
        candidates: list[Path] = []
        primary = reference_docs.find_project_dir(cwd or ".")
        if primary is not None:
            candidates.append(primary)
        for project in reference_docs.projects_from_prompt(prompt, cwd or "."):
            if project not in candidates:
                candidates.append(project)

        searched = ",".join(str(d) for d in dirs)
        for project in candidates:
            # One bad project must not cost the others. The candidate list is
            # cwd's project plus the ones the prompt named, and they are
            # independent: an unreadable manifest in the first is no reason to
            # stop before the one the user actually pointed at.
            try:
                # Announce gate first: it is a dict lookup, where stack
                # detection and doc resolution read manifests and whole
                # reference docs (~60k chars) — waste on the ~29 of 30
                # turns whose announcement is already recorded.
                #
                # It does cost the resolved=none diagnostic below on the turns
                # it skips. That is acceptable BECAUSE the line still fires on
                # every announcement-due turn, so a doc renamed out from under
                # the map is reported within one cooldown rather than never —
                # but it is a real trade, not a free efficiency win, and the
                # log is the only signal that a project is building with no
                # standards loaded.
                if not reference_docs.should_announce(state, project, turn_index):
                    continue
                keys = reference_docs.detect_stack_keys(project)
                names = reference_docs.doc_names_for(keys)
                if not names:
                    continue
                docs = reference_docs.resolve_docs(names)
                if not docs:
                    # The map named docs that are not on disk. Worth a line: it
                    # means this project builds with no standards loaded, and
                    # the usual cause is a doc renamed without updating the
                    # map. Naming the directories actually searched is what
                    # makes that diagnosable rather than merely reported.
                    logger.info(
                        "DEV_REFERENCES project=%s stack=%s wanted=%s resolved=none searched=%s",
                        project, ",".join(keys), ",".join(names), searched,
                    )
                    continue
                block = reference_docs.build_block(project, docs)
                if not block:
                    continue
                reference_docs.record_announced(state, project, turn_index)
                logger.info(
                    "DEV_REFERENCES project=%s stack=%s docs=%s",
                    project, ",".join(keys), ",".join(p.name for p, _ in docs),
                )
                return block, project
            except Exception:
                logger.debug(
                    "DEV_REFERENCES project=%s failed; trying the next candidate",
                    project, exc_info=True,
                )
                continue
        return "", None
    except Exception:
        logger.debug("Dev reference detection failed; injecting nothing", exc_info=True)
        return "", None


def _memory_freshness_date(path: Path) -> str | None:
    """Best-available last-updated date for a memory file, or None.

    Prefers the ``**Last Updated:**`` header near the top of the file;
    falls back to filesystem mtime. Fail-open: any read/stat/conversion
    error yields None (the caller skips the stamp).
    """
    try:
        with path.open(errors="replace") as f:
            head = f.read(2048)
        m = _LAST_UPDATED_RE.search(head)
        if m:
            return m.group(1)
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        # OSError: unreadable/vanished file. ValueError/OverflowError:
        # mtime outside datetime range — stat() succeeds but
        # fromtimestamp() raises, and it is NOT an OSError subclass.
        return None


def _memory_dates(
    memory_dir: Path, memory_content: dict[str, str], banks=None
) -> dict[str, str | None]:
    """Freshness date per pick, resolved through the pick's own bank."""
    dates: dict[str, str | None] = {}
    for name in memory_content:
        path = _memory_path_for(name, memory_dir, banks)
        dates[name] = _memory_freshness_date(path) if path is not None else None
    return dates


def _stamp_memory_dates(
    memory_dir: Path, memory_content: dict[str, str], banks=None
) -> dict[str, str]:
    """Prefix each memory file's content with its last-updated date.

    Gives the model a concrete recency signal to weigh memory against
    in-session sources when the conflict preamble fires. Applied at
    render time only — ``memory_content`` keys (which the attribution and,
    through ``_cooldown_keys``, the cooldown bookkeeping read) are
    untouched. Section slices (``file.md#Section``)
    carry the base file's date. Files with no obtainable date are
    passed through unstamped.
    """
    dates = _memory_dates(memory_dir, memory_content, banks)
    stamped: dict[str, str] = {}
    for name, content in memory_content.items():
        date = dates.get(name)
        stamped[name] = f"(last updated: {date})\n{content}" if date else content
    return stamped


def _section_attribution(
    memory_content: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Attribute injected memory to ``(sections_by_file, bytes_by_file)``.

    ``memory_content`` is keyed by the *raw pick*, so ``"dev.md"`` and
    ``"dev.md#Testing"`` are different keys for the same file. Both
    returned maps are keyed by the bare filename instead:

    * ``sections_by_file`` — the H2 names loaded from that file, in pick
      order. **An empty list means the whole file was injected**, which
      is what a bare filename pick produces; it never means "nothing was
      loaded", because a file with nothing loaded has no key at all.
    * ``bytes_by_file`` — characters of content injected for that file,
      summed across its picks (before the date stamp and section
      headers the renderer adds).

    Together these are what makes a section pick measurable: ``files``
    alone cannot say whether ``multiplai.md`` cost 181 KB or 6 KB.
    """
    sections: dict[str, list[str]] = {}
    sizes: dict[str, int] = {}
    for pick, content in memory_content.items():
        base, section = parse_section_ref(pick)
        bucket = sections.setdefault(base, [])
        if section and section not in bucket:
            bucket.append(section)
        sizes[base] = sizes.get(base, 0) + len(content)
    return sections, sizes


def _split_by_trust(
    memory_content: dict[str, str], banks
) -> tuple[dict[str, str], list[tuple[str, object, str]]]:
    """Partition picks into ``(personal, [(ref, bank, content), …])``.

    The split is by **authorship**, read off the bank the pick resolves to —
    never off anything in the text. A file that arrived in a shared bank is
    other people's writing whatever it happens to say, and a personal file
    is the user's own however much it looks like a team note.
    """
    if not banks:
        return dict(memory_content), []
    personal: dict[str, str] = {}
    shared: list[tuple[str, object, str]] = []
    for ref, content in memory_content.items():
        resolved = resolve_bank_ref(ref, banks)
        if resolved is not None and resolved[0].is_shared:
            shared.append((ref, resolved[0], content))
        else:
            personal[ref] = content
    return personal, shared


def _render_memory_section(
    memory_dir: Path,
    memory_content: dict[str, str],
    conflict_preamble: bool,
    banks=None,
) -> str:
    """Render the MEMORY block, pairing the conflict preamble with stamps.

    The preamble promises date stamps, so the two must never render
    separately — any injection path goes through here. The config
    toggle (``memory_conflict_preamble``) disables both together.

    Content from a **shared bank** is rendered differently, and the
    difference is not cosmetic. Its heading names the bank so the user can
    tell "my note" from "the team's note" at a glance, and its body goes
    inside an ``<untrusted-content>`` fence per ``docs/untrusted-content.md``,
    because a subscribed bank is memory somebody else writes and syncs in
    over the network. Without the fence a teammate's commit becomes a
    standing instruction to this user's agent — a prompt-injection channel
    with a git remote in front of it. The fence is applied on the *authorship*
    test (``bank.is_shared``), so a new file in a subscribed bank is fenced
    the moment it syncs, with nothing to configure and no content heuristic
    to evade. The shared notice is only rendered when shared content is
    actually present, so a user with no banks sees byte-identical output to
    before.
    """
    personal, shared = _split_by_trust(memory_content, banks)
    if not conflict_preamble:
        block = _render_corpus_section("MEMORY", personal)
        return _append_shared_blocks(block, memory_dir, shared, banks, stamp=False)
    block = _render_corpus_section(
        "MEMORY",
        _stamp_memory_dates(memory_dir, personal, banks),
        preamble=_MEMORY_CONFLICT_PREAMBLE,
    )
    return _append_shared_blocks(block, memory_dir, shared, banks, stamp=True)


def _append_shared_blocks(
    block: str, memory_dir: Path, shared, banks, *, stamp: bool
) -> str:
    """Append the fenced shared-bank blocks (and their notice) to *block*."""
    if not shared:
        return block
    dates = (
        _memory_dates(memory_dir, {ref: content for ref, _, content in shared}, banks)
        if stamp
        else {}
    )
    parts = [block, SHARED_BANK_NOTICE]
    for ref, bank, content in shared:
        parts.append(render_shared_block(ref, bank, content, date=dates.get(ref)))
    return "\n\n".join(parts)


def _render_qmd_resource(entry: dict) -> str:
    """Render one qmd entry as the content body under its path heading."""
    loc = f", line {entry['line']}" if entry.get("line") else ""
    body = f"(score {entry['score']:.2f}{loc}) {entry.get('title', '')}".rstrip()
    if entry.get("snippet"):
        body += f"\n{entry['snippet']}"
    return body


def _render_corpus_section(label: str, files: dict[str, str], preamble: str = "") -> str:
    """Render one corpus's loaded files as a labeled markdown block."""
    parts = [f"=== {label} ==="]
    if preamble:
        parts.append(preamble)
    for name, content in files.items():
        parts.append(f"## {name}\n{content}")
    return "\n\n".join(parts)


# Max chars of user prompt echoed on ROUTING_SCORES lines. The prompt
# is already session context (per the logging standard's PII rule);
# truncation just bounds log growth.
_PROMPT_LOG_MAX_CHARS = 80


def _prompt_for_log(prompt: str, max_chars: int = _PROMPT_LOG_MAX_CHARS) -> str:
    """Whitespace-collapsed, truncated prompt for log attribution.

    Collapsing keeps the log line single-line (newlines in a prompt
    would otherwise split it and break line-oriented parsers).
    """
    collapsed = " ".join(prompt.split())
    if len(collapsed) > max_chars:
        return collapsed[:max_chars] + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Re-recommendation cooldown (turn-based dedup)
# ---------------------------------------------------------------------------


def _filter_cooldown(
    picks: list[str], last_injected: dict, turn_index: int, cooldown: int
) -> tuple[list[str], list[str]]:
    """Split *picks* into (kept, suppressed) by the cooldown window.

    A pick is suppressed when it was injected within the last *cooldown*
    turns (it's already in the conversation). ``turn_index - last <=
    cooldown`` is the suppression test.
    """
    kept: list[str] = []
    suppressed: list[str] = []
    cmap = last_injected if isinstance(last_injected, dict) else {}
    for pick in picks:
        last = cmap.get(pick)
        if isinstance(last, int) and (turn_index - last) <= cooldown:
            suppressed.append(pick)
        else:
            kept.append(pick)
    return kept, suppressed


# Post-cooldown relevance re-check. _apply_policy admits everything
# within keep_ratio (default 0.30) of the corpus's top score, so weak
# entries ride in as companions of a strong top match. When cooldown
# then suppresses that top scorer, the survivors lose the anchor that
# justified them — they were only "relevant" relative to a file already
# in the conversation. Survivors must re-clear a bar derived from the
# suppressed top score, or nothing is injected: noise is worse than
# silence. 0.5 = "at least half as relevant as what's already in
# context". The alternative design (running cooldown *before* the
# floor/cap pick) was rejected: the surviving weak tail then forms a
# fresh ranking whose top can still clear the absolute MIN_SIGNAL
# floor — exactly the observed failure (life.md at 3.335 injected
# after the 10.8/9.9 top scorers were suppressed).
POST_COOLDOWN_KEEP_RATIO = 0.5


def _picked_ranking(diag: dict) -> list:
    """``(score, filename)`` pairs the router actually injected.

    The match-breadth eligibility gate (memory_router.MIN_DOMAIN_MATCHES)
    means picks are no longer a contiguous prefix of the raw ranking —
    an ineligible entry can outrank every pick — so consumers must not
    slice ``scored``. Use the router's own ``picked_scored`` (sorted
    desc, like ``scored``), falling back to the top-``n_picked`` prefix
    for diags without it.
    """
    picked = diag.get("picked_scored")
    if picked is None:
        picked = (diag.get("scored") or [])[: diag.get("n_picked", 0)]
    return picked


def _routing_score_hint(diag: dict) -> str:
    """Compact routing-quality fragment for the human activity line.

    Scores are read from the *picked* set (``_picked_ranking``), not the
    raw candidate pool — under the eligibility gate the raw top/floor
    can describe entries that never injected. Abstention is reported
    honestly instead of a fake range, and distinguishes the two floors:
    zero *eligible* candidates means the best raw score may well clear
    MIN_SIGNAL (the single-token trap) — claiming "< floor" there would
    be false — while a best-eligible score below MIN_SIGNAL is a genuine
    below-floor abstention. Returns "" when there is nothing to say.
    """
    scored = diag.get("scored") or []
    n_picked = diag.get("n_picked", 0)
    picked = _picked_ranking(diag)
    if diag.get("continuation"):
        return " · continuation — nothing injected"
    if n_picked <= 0:
        if not scored:
            return " · no candidates — nothing injected"
        top_eligible = diag.get("top_eligible")
        if top_eligible is None:
            return (
                f" · no match (best {scored[0][0]:g} lacks match breadth)"
                " — nothing injected"
            )
        return (
            f" · no match (best eligible {top_eligible:g} < floor)"
            " — nothing injected"
        )
    if not picked:
        return ""
    n_cand = diag.get("n_candidates", len(scored))
    cap_tag = " CAP-HIT" if diag.get("capped") else ""
    return (
        f" · scores {picked[0][0]:g}→{picked[-1][0]:g}{cap_tag} "
        f"({n_picked}/{n_cand} kept)"
    )


def _refloor_after_cooldown(
    kept: list[str],
    suppressed: list[str],
    scored: list,
) -> tuple[list[str], list[str]]:
    """Re-check cooldown survivors against a post-suppression bar.

    Fires only when the corpus's top *pick* was itself suppressed (the
    ranking anchor is gone); if the top pick is still going in, the
    original relevance contract holds and nothing changes. Survivors
    scoring below ``POST_COOLDOWN_KEEP_RATIO × top score`` are dropped.
    Picks without a score (bundle / co_retrieve expansion) are kept —
    they were pulled in by metadata, not ranking. ``scored`` is the
    router's ranking of the *injected picks* (``_picked_ranking``) —
    NOT the raw candidate ranking, whose top can be a breadth-ineligible
    entry that never injected and so can never appear in ``suppressed``
    (which would silently disable this check). Without it (LLM router)
    behavior is unchanged. Returns ``(kept, dropped)``.
    """
    if not kept or not suppressed or not scored:
        return kept, []
    top_score, top_name = scored[0]
    if top_name not in set(suppressed):
        return kept, []
    score_by_name = {name: s for s, name in scored}
    bar = POST_COOLDOWN_KEEP_RATIO * top_score
    survivors: list[str] = []
    dropped: list[str] = []
    for name in kept:
        score = score_by_name.get(name)
        if score is None or score >= bar:
            survivors.append(name)
        else:
            dropped.append(name)
    return survivors, dropped


def _persist_turn_state(
    session_state: dict,
    turn_index: int,
    recent: dict,
    injected_by_corpus: dict[str, Iterable[str]],
    cooldown: int,
) -> None:
    """Stamp this turn's injections into *recent* and write session state.

    Each corpus value is iterated for its **keys**: a mapping of injected
    content works, and so does the explicit key list ``_cooldown_keys`` builds
    where the content keys are not the picks.

    Records ``turn_index`` for every injected key, prunes entries that
    have aged past the cooldown window (bounds file growth), bumps the
    turn counter, and atomically rewrites ``session_state.json``.
    Fail-open: a write error is logged at debug, never raised — the hook
    must not break a prompt over bookkeeping.
    """
    for corpus_type, content in injected_by_corpus.items():
        cmap = recent.get(corpus_type)
        if not isinstance(cmap, dict):
            cmap = recent[corpus_type] = {}
        for key in content:
            cmap[key] = turn_index
        stale = [
            k for k, t in cmap.items()
            if not isinstance(t, int) or (turn_index - t) > cooldown
        ]
        for k in stale:
            del cmap[k]
    session_state["turn_index"] = turn_index
    session_state["recently_injected"] = recent
    # NOTE: turn_index / recently_injected live in a single shared
    # session_state.json. Two sessions running concurrently against the same
    # plugin-data dir can race this read-modify-write and clobber each other's
    # cooldown map — a mild degradation (a file may be re-injected a turn early
    # or suppressed a turn late), not data loss. All three writers now
    # read-merge-write and go through the atomic write_session_state helper
    # (this function, session_start.main, session_stop.main), so a session
    # start/stop no longer *drops* a concurrent session's turn_index /
    # recently_injected — but interleaved read-modify-write cycles can still
    # lose an update to the cooldown map under true concurrency. The severe
    # cross-session bug (deferred diary/learnings markers filed under the wrong
    # session id) is fixed separately in session_end.py / pre_compact.py by
    # trusting the hook-input session id. A full fix here needs per-session
    # state files (deliberately out of scope).
    try:
        write_session_state(get_paths().data_dir(), session_state)
    except Exception:
        logger.debug("Could not persist turn state", exc_info=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _emit_result(context: str) -> None:
    """Write the hook result to stdout in the shape Claude Code actually reads.

    UserPromptSubmit only injects context from ``hookSpecificOutput.
    additionalContext`` (or plain stdout text) — a bare ``{"context": ...}``
    key is silently ignored, which once made routed memory a no-op. The
    legacy top-level ``context`` / ``*_files`` keys that used to ride along
    for old consumers are gone (P3): grep found tests as their only readers,
    and at 40KB injections the duplicate context doubled hook stdout on
    every prompt.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


def _watchdog_kwargs() -> dict:
    """``hook_run`` watchdog arguments, or ``{}`` on a core without them.

    ``watchdog_s``/``on_watchdog`` arrived in multiplai-core after the version
    this plugin first locked. Passing them to an older ``hook_run`` would
    raise TypeError on every prompt, so the watchdog is armed only when the
    resolved core accepts it.
    """
    import inspect

    try:
        params = inspect.signature(hook_run).parameters
    except (TypeError, ValueError):
        return {}
    if "watchdog_s" not in params or "on_watchdog" not in params:
        return {}
    return {
        "watchdog_s": _WATCHDOG_S,
        "on_watchdog": make_on_watchdog(logger, lambda: get_paths().logs_dir()),
    }


def main() -> None:
    """Context manager main: read stdin, route context, write JSON to stdout."""
    # Read stdin and name the session *before* opening hook_run, so the
    # HOOK_ENTRY line identifies whose prompt is at stake. This hook has a 30s
    # harness ceiling and a kill leaves nothing behind but that line.
    input_data = read_hook_input()

    # A subagent / nested hook session gets no routed memory: a per-prompt
    # router call inside every SDK child would multiply cost and latency for
    # context the child was deliberately spawned without (M7). Still emit the
    # safe empty payload — this hook's contract is JSON on stdout, always.
    from lib.checkpoint import is_child_session

    if is_child_session(input_data.get("transcript_path") or ""):
        _emit_result("")
        return

    session_id = input_data.get("session_id")
    setup_logging("context_manager", session_id=session_id or "")
    with hook_run(
        "context_manager", logger, session_id=session_id or "",
        **_watchdog_kwargs(),
    ) as run:
        _assemble_and_emit(input_data, run)


def _assemble_and_emit(input_data: dict, run: HookRun) -> None:
    """The hook body: route every corpus, assemble the block, emit the payload."""
    paths = get_paths()
    memory_dir = paths.memory_dir()

    prompt = input_data.get("prompt", "")
    transcript_path = input_data.get("transcript_path")
    session_id = input_data.get("session_id")

    # Capture the working directory for the diary/now pipeline. UserPromptSubmit
    # reliably carries cwd, so this is the dependable place to record it;
    # SessionEnd reads it back from session_state to tag the diary entry (and
    # thus the project). Best-effort and only written when it changes, so the
    # common steady-state path adds no extra write.
    cwd = input_data.get("cwd", "")
    if cwd:
        try:
            _ss = read_session_state(paths.data_dir()) or {}
            if _ss.get("cwd") != cwd:
                _ss["cwd"] = cwd
                write_session_state(paths.data_dir(), _ss)
        except Exception:
            logger.debug("Could not persist cwd to session_state", exc_info=True)

    # Task-notification guard. When a background task finishes, the
    # harness re-invokes the model with a synthetic
    # "<task-notification> <task-id>…" prompt, and UserPromptSubmit
    # fires on it like any user turn. The conversation already holds
    # that task's context, so routing the notification text only
    # injects noise (2026-08-10: two notifications each pulled 4-5
    # memory files). Same class as the routers' continuation guard,
    # but placed here so it covers every strategy — and an early
    # return rather than a blanked prompt, because the recency
    # fallback below is not prompt-gated and would fire on the empty
    # pick set.
    if prompt.lstrip().startswith("<task-notification>"):
        logger.info("Task notification — routing skipped, nothing injected")
        log_event(
            "context", "skip",
            "task notification — context already in conversation, "
            "nothing injected",
            session_id=session_id,
        )
        run.note(outcome="task_notification", files=0, bytes=0)
        _emit_result("")
        return

    # The ordered banks for this workspace. With no `memory-banks.yaml` this is
    # exactly one bank at `memory_dir`, so every path below behaves as before.
    # Resolved after the guards above so notification turns pay nothing.
    banks = configured_banks()

    # Last-assistant-response disambiguation. Failure modes already
    # encoded in read_last_assistant_response (returns None on any error).
    # Staged separately from the catalogs: this one reads the session
    # transcript, which grows without bound, so it is the stage that would
    # show up first if transcript size ever became the cost.
    with run.stage("transcript"):
        last_response = (
            read_last_assistant_response(transcript_path) if prompt else None
        )
    # Bound the disambiguation text (P7). token_overlap's domain score is an
    # UNNORMALIZED sum of matched IDF weights, so a 64KB previous turn can
    # out-vote a ten-word prompt and route the turn on what was just said
    # rather than what was just asked; the same text also rides verbatim
    # into the LLM router's prompt. 2000 chars keeps the disambiguation
    # signal (the head states what the turn was about) without the weight.
    if last_response and len(last_response) > _LAST_RESPONSE_MAX_CHARS:
        last_response = last_response[:_LAST_RESPONSE_MAX_CHARS]

    with run.stage("catalogs"):
        # Plugin-options-driven config (which corpora are enabled, paths, etc.)
        cfg = load_catalog_config()

        # Load all enabled corpora
        corpora = _load_corpora(cfg) if prompt else {"memory": [], "skills": [], "resources": []}

    # Multi-corpus router pass
    picks_by_corpus: dict[str, list[str]] = {"memory": [], "skills": [], "resources": []}
    router_diag: dict | None = None
    used_fallback = False
    router_ran = False
    if prompt and any(corpora.values()):
        try:
            router = create_router(keep_ratio=cfg.keep_ratio)
            # The likeliest single consumer of a 30s budget — an LLM call over
            # the network. It is not the only one: the qmd retrieval below is a
            # subprocess that has been measured at ~25s for 40 docs, and it is
            # staged for exactly that reason.
            with run.stage("router"):
                picks_by_corpus = router.select_multi(prompt, last_response, corpora)
            router_ran = True
            run.note(router=router.name)
            logger.info(
                "Router %s picked: memory=%d skills=%d",
                router.name,
                len(picks_by_corpus.get("memory", [])),
                len(picks_by_corpus.get("skills", [])),
            )
            # Routing-quality diagnostics (token_overlap only; the LLM
            # router exposes no scores). Machine-readable line consumed
            # by the eval harness and the /health Routing Quality check.
            router_diag = getattr(router, "last_scores", None) or None
            if router_diag:
                # Score→prompt attribution lives in the line itself so
                # forensics don't require digging through transcripts.
                # It's a JSON key (not a trailing key=value) so parsers
                # anchored on `memory=({...})$` keep working.
                _plog = _prompt_for_log(prompt)
                for _ct in ("memory", "skills"):
                    # memory always logs (the /health contract); skills
                    # logs when its corpus is enabled so those injections
                    # leave a score trail too. Resources never reach the
                    # router, so they have no scores to log here — their
                    # trail is the QMD_RETRIEVAL line.
                    if _ct != "memory" and not corpora.get(_ct):
                        continue
                    _d = router_diag.get(_ct) or {}
                    _scored = _d.get("scored") or []
                    _cap = _d.get("cap", 10)
                    # Emit the set actually injected — NOT scored[:cap],
                    # the raw candidate pool — so /health's live
                    # top/floor describe what was injected, not an
                    # excluded candidate (see _picked_ranking).
                    _np = _d.get("n_picked", 0)
                    _picked = _picked_ranking(_d)
                    _picked_fns = {fn for _s, fn in _picked}
                    # Best score that did NOT inject (may outrank picks
                    # now that ineligible entries can top the ranking).
                    _excluded = [s for s, fn in _scored if fn not in _picked_fns]
                    logger.info(
                        "ROUTING_SCORES %s=%s",
                        _ct,
                        json.dumps({
                            "picked": [[fn, round(s, 3)] for s, fn in _picked],
                            "cap": _cap,
                            "n_candidates": _d.get("n_candidates", len(_scored)),
                            "n_picked": _np,
                            "capped": _d.get("capped", False),
                            "floor_excluded": (
                                round(max(_excluded), 3) if _excluded else None
                            ),
                            "prompt": _plog,
                        }),
                    )
        except NotImplementedError as e:
            logger.warning("Router misconfigured: %s", e)
        except Exception:
            logger.exception("Router call failed; per-corpus picks will be empty")

    # Bundle + co_retrieve_for expansion (per-corpus, against that corpus's catalog)
    for corpus_type in ("memory", "skills"):
        picks = picks_by_corpus.get(corpus_type) or []
        if picks:
            picks_by_corpus[corpus_type] = expand_picks(picks, corpora.get(corpus_type) or [])

    # Resources retrieval: qmd against its index, never the router. Entry
    # paths become the corpus picks so the cooldown machinery below dedups
    # qmd injections exactly like router picks.
    qmd_active = cfg.enable_resources and bool(cfg.resources_dir.strip())
    qmd_entries: dict[str, dict] = {}
    if qmd_active and prompt:
        # Staged: this shells out to the qmd CLI, and qmd_retrieval.py:472
        # records the measurement — 10 docs 5.9s, 40 docs 24.9s, most of this
        # hook's 30s ceiling. Unstaged, a run killed here would show an ENTRY,
        # no EXIT, and no attribution at all.
        with run.stage("qmd"):
            for entry in _qmd_resources_entries(cfg, prompt, cwd):
                qmd_entries.setdefault(entry["path"], entry)
        picks_by_corpus["resources"] = list(qmd_entries)

    # Log per-file routing decisions (post-expansion) for health audit analytics.
    logger.info(
        "ROUTING memory=%s skills=%s resources=%s",
        json.dumps(sorted(picks_by_corpus.get("memory") or [])),
        json.dumps(sorted(picks_by_corpus.get("skills") or [])),
        json.dumps(sorted(picks_by_corpus.get("resources") or [])),
    )

    # --- Re-recommendation cooldown (turn-based dedup) -------------------
    # Drop picks injected within the last `cooldown` turns: they're
    # already in the conversation, so re-injecting them just wastes
    # context. State (a turn counter + per-file last-injected turn) lives
    # in session_state.json and is reset on PreCompact, so once context
    # is summarized away every file becomes eligible again. Fail-open: a
    # state read/parse error degrades to "no cooldown this turn".
    cooldown = max(0, cfg.recommend_cooldown_turns)
    cooldown_active = bool(prompt) and cooldown > 0
    session_state: dict = {}
    turn_index = 0
    recent: dict = {}
    cooldown_suppressed: dict[str, list[str]] = {}
    refloor_dropped: dict[str, list[str]] = {}
    # Capture what the router actually picked, before cooldown trims it,
    # so the fallback can tell genuine abstention from cooldown removal.
    router_picked_memory = bool(picks_by_corpus.get("memory"))
    if cooldown_active:
        try:
            _ss = read_session_state(paths.data_dir())
        except Exception:
            _ss = None
        session_state = _ss if isinstance(_ss, dict) else {}
        turn_index = int(session_state.get("turn_index", 0) or 0) + 1
        _recent = session_state.get("recently_injected")
        recent = _recent if isinstance(_recent, dict) else {}
        for corpus_type in ("memory", "skills", "resources"):
            kept, suppressed = _filter_cooldown(
                picks_by_corpus.get(corpus_type) or [],
                recent.get(corpus_type) or {},
                turn_index, cooldown,
            )
            if suppressed and kept:
                # Anchor on the injected picks, not the raw ranking:
                # under the eligibility gate an ineligible entry can top
                # `scored` without ever injecting, and since suppressed
                # ⊆ picks the refloor would then never fire.
                _diag = (router_diag or {}).get(corpus_type) or {}
                kept, dropped = _refloor_after_cooldown(
                    kept, suppressed, _picked_ranking(_diag)
                )
                if dropped:
                    refloor_dropped[corpus_type] = dropped
            picks_by_corpus[corpus_type] = kept
            if suppressed:
                cooldown_suppressed[corpus_type] = suppressed
        if any(cooldown_suppressed.values()):
            logger.info(
                "COOLDOWN turn=%d window=%d suppressed=%s",
                turn_index, cooldown,
                json.dumps({k: sorted(v) for k, v in cooldown_suppressed.items()}),
            )
        if any(refloor_dropped.values()):
            logger.info(
                "COOLDOWN_REFLOOR turn=%d ratio=%s dropped=%s",
                turn_index, POST_COOLDOWN_KEEP_RATIO,
                json.dumps({k: sorted(v) for k, v in refloor_dropped.items()}),
            )

    # --- Dev reference docs (stack-detected, not routed) ------------------
    # Deliberately outside the router and the cooldown: a standards doc
    # applies because of what the project is, so relevance scoring against
    # the prompt is the wrong instrument. Announcement state lives in
    # session_state alongside the cooldown's, and is persisted here when the
    # cooldown is off (nothing else would write it).
    dev_refs = ""
    if cfg.enable_dev_references and prompt:
        if not cooldown_active:
            try:
                _rs = read_session_state(paths.data_dir())
            except Exception:
                _rs = None
            session_state = _rs if isinstance(_rs, dict) else {}
        dev_refs, _ = _dev_reference_block(prompt, cwd, session_state, turn_index)
        if dev_refs and not cooldown_active:
            try:
                write_session_state(paths.data_dir(), session_state)
            except Exception:
                logger.debug("Could not persist dev-reference state", exc_info=True)

    # Load content per corpus. Timed as one stage: this is where the picked
    # files are actually read off disk, so it is the stage that grows with the
    # size of what was picked rather than with the size of the catalog.
    with run.stage("content"):
        memory_content = _load_memory_content(
            memory_dir, picks_by_corpus.get("memory") or [], banks
        )
        skills_content = _build_skills_recommendations(
            cfg, picks_by_corpus.get("skills") or [], corpora.get("skills") or []
        )
        # Cooldown may have trimmed the picks; render only the survivors.
        resources_content = {
            path: _render_qmd_resource(qmd_entries[path])
            for path in picks_by_corpus.get("resources") or []
            if path in qmd_entries
        }

    # Memory fallback — a safety net for *failure*, not for abstention.
    # A successful router run that returns no memory picks is a
    # deliberate "nothing is relevant" (NONE floor or continuation
    # guard); honoring it means injecting nothing, which is correct.
    # The recency net only fires when the router never ran (exception /
    # misconfig) or it picked files that didn't resolve on disk
    # (catalog↔disk drift) — genuine failures with no ranking signal.
    # Use the *pre-cooldown* pick count for abstention: the router
    # genuinely abstained only if it picked no memory at all. If it
    # picked files that cooldown then trimmed, that's a deliberate
    # "already in context", not abstention — and must not trigger the
    # recency dump either.
    router_abstained = router_ran and not router_picked_memory
    mem_on_cooldown = bool(cooldown_suppressed.get("memory")) and not memory_content
    if not memory_content and not router_abstained and not mem_on_cooldown:
        # Don't re-inject files still within the cooldown window, and cap the
        # payload — the recency fallback fires every prompt when no catalog
        # exists yet, so an unbounded dump would flood each turn.
        _mem_recent = recent.get("memory") if isinstance(recent.get("memory"), dict) else {}
        on_cooldown = {
            name for name, t in _mem_recent.items()
            if isinstance(t, int) and (turn_index - t) <= cooldown
        }
        memory_content = _read_top_memory_files(memory_dir, exclude=on_cooldown)
        if memory_content:
            used_fallback = True
            logger.info("FALLBACK memory=%s", json.dumps(sorted(memory_content.keys())))
            log_event(
                "context", "fallback",
                "router matched nothing — fell back to recency-ranked memory → "
                + ", ".join(sorted(memory_content.keys())),
                session_id=session_id,
                files=sorted(memory_content.keys()),
            )

    if not memory_content and not skills_content and not resources_content and not dev_refs:
        logger.info("No context to inject")
        # Make abstention visible in the human log: a deliberate
        # "nothing relevant" is the floor/continuation guard working,
        # not a failure — say which, with the top score as evidence.
        _skip_msg = "no context matched this prompt"
        # Stable token for the EXIT line. Without it a skip's EXIT carries
        # neither files= nor outcome=, so a reader cannot tell "routed and
        # injected nothing" from "the note call was never reached" — the same
        # ambiguity outcome= was added to checkpoint_nudge to remove.
        _outcome = "no_match"
        if any(cooldown_suppressed.values()) and not router_abstained:
            _n = sum(len(v) for v in cooldown_suppressed.values())
            _nd = sum(len(v) for v in refloor_dropped.values())
            _outcome = "cooldown"
            if _nd:
                _skip_msg = (
                    f"top {_n} matched file(s) on cooldown; {_nd} weak "
                    f"co-pick(s) dropped below the post-cooldown relevance "
                    f"bar — nothing injected"
                )
            else:
                _skip_msg = (
                    f"all {_n} matched file(s) injected within the last "
                    f"{cooldown} turns — on cooldown, nothing injected"
                )
        elif router_abstained:
            _dm = (router_diag or {}).get("memory") or {}
            _ds = _dm.get("scored") or []
            _outcome = "abstained"
            if _dm.get("continuation"):
                _outcome = "continuation"
                _skip_msg = "continuation prompt — context already in conversation, nothing injected"
            elif _ds:
                _skip_msg = (
                    f"router abstained — best memory score {_ds[0][0]:g} "
                    f"below relevance floor ({len(_ds)} cand), nothing injected"
                )
            else:
                _skip_msg = "router abstained — no memory matched, nothing injected"
        log_event(
            "context", "skip", _skip_msg,
            session_id=session_id,
        )
        if cooldown_active:
            _persist_turn_state(session_state, turn_index, recent, {}, cooldown)
        run.note(outcome=_outcome, files=0, bytes=0)
        _emit_result("")
        return

    parts: list[str] = []
    if memory_content:
        parts.append(_render_memory_section(
            memory_dir, memory_content, cfg.memory_conflict_preamble, banks,
        ))
    if skills_content:
        parts.append(_render_corpus_section("SKILLS", skills_content))
    if resources_content:
        parts.append(_render_corpus_section(
            "RESOURCES", resources_content,
            preamble=_QMD_RESOURCES_PREAMBLE,
        ))
    if dev_refs:
        parts.append(dev_refs)

    session_context = "\n\n".join(parts)

    corpus_counts = {
        "memory": len(memory_content),
        "skills": len(skills_content),
        "resources": len(resources_content),
    }
    logger.info(
        "Context assembled: memory=%d skills=%d resources=%d",
        corpus_counts["memory"],
        corpus_counts["skills"],
        corpus_counts["resources"],
    )
    # On the EXIT line so one grep answers "what did this turn cost, and what
    # did the user get for it" without joining two log lines.
    run.note(
        outcome="injected",
        bytes=len(session_context),
        files=sum(corpus_counts.values()),
    )

    # Per-corpus file groups so the activity line says which files are
    # memory vs skills vs resources — a flat list loses that attribution.
    files_by_corpus = {
        "memory": sorted(memory_content),
        "skills": sorted(skills_content),
        "resources": sorted(resources_content),
    }
    injected = files_by_corpus["memory"] + files_by_corpus["skills"] + files_by_corpus["resources"]
    # Section-level attribution for the memory corpus — the only corpus
    # whose catalog carries section_anchors. Added alongside `files` and
    # `files_by_corpus` rather than replacing them: the fleet and
    # log-doctor tooling reads those two by name.
    sections_by_file, bytes_by_file = _section_attribution(memory_content)
    file_groups = [
        f"{corpus}: {', '.join(names)}"
        for corpus, names in files_by_corpus.items()
        if names
    ]
    # Compact routing-quality hint for the human activity log — see
    # _routing_score_hint for the picked-vs-raw and abstention rules.
    score_hint = ""
    if not used_fallback and router_diag:
        score_hint = _routing_score_hint(router_diag.get("memory") or {})
    summary = (
        f"injected {corpus_counts['memory']} memory · "
        f"{corpus_counts['skills']} skills · "
        f"{corpus_counts['resources']} resources"
        + (" · dev references" if dev_refs else "")
        + score_hint
        + (f" → {' · '.join(file_groups)}" if file_groups else "")
    )
    log_event(
        "context", "inject", summary,
        session_id=session_id,
        memory=corpus_counts["memory"],
        skills=corpus_counts["skills"],
        resources=corpus_counts["resources"],
        files=injected,
        files_by_corpus=files_by_corpus,
        sections_by_file=sections_by_file,
        bytes_by_file=bytes_by_file,
        # Which turn this injection belongs to, so utilisation telemetry can
        # tell one injection from a re-injection later in the same session.
        # 0 whenever the cooldown is off — that path never advances the
        # counter — which is why nothing downstream may treat it as a key.
        turn=turn_index,
        bytes=len(session_context),
    )

    if cooldown_active:
        _persist_turn_state(
            session_state, turn_index, recent,
            {"memory": _cooldown_keys(
                picks_by_corpus.get("memory") or [], memory_content),
             "skills": skills_content,
             "resources": resources_content},
            cooldown,
        )

    _emit_result(session_context)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hook runs on every prompt; never propagate a traceback or
        # non-zero exit, or we break the user's Claude Code session.
        try:
            logger.exception("context_manager hook failed; emitting empty context")
        except Exception:
            pass
        _emit_result("")
        sys.exit(0)
