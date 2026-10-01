"""Memory catalog generator.

Implements MemoryGenerator, a GeneratorBase subclass that catalogs
memory files (*.md) from the configured memory directory.

Design Decision 5: Preserves hand-authored fields (sections, bundle,
co_retrieve_for) across regeneration via merge_entry() override.

``section_anchors`` used to be in that preserved set and is now
generated — see the note on ``_HAND_AUTHORED_FIELDS`` below.
"""

import logging
from pathlib import Path
from typing import Any

from multiplai_core.paths import Paths
from generators.base import GeneratorBase
from lib.section_loader import h2_names

logger = logging.getLogger(__name__)

# Hand-authored fields preserved during merge. intent_domains and
# anti_domains are emitted by the LLM on first generation but may be
# hand-tuned later — preserving them across regeneration prevents the
# LLM from silently overwriting curated routing hints.
#
# ``section_anchors`` is deliberately NOT in this list. It is derived
# from the file's own H2 headers, so preserving it would freeze the
# first value ever written: a file could gain, rename, or drop a
# section and the anchors would never follow. A stale anchor is not a
# loud failure either — ``section_loader`` falls back to the whole
# file — so the rot would be invisible forever. Regeneration is already
# content-hash-gated (``generators/base.py``), so anchors are only
# re-derived when the file actually changed.
# Memory files the router never offers. CLAUDE.md is the memory system's own
# index; the two principles files are loaded by another route (see
# lib/principles.py, NOT_ROUTED). context_manager.py keeps the same list.
_NOT_ROUTED = frozenset({"claude.md", "principles.md", "principles-examples.md"})

_HAND_AUTHORED_FIELDS = (
    "sections",
    "bundle",
    "co_retrieve_for",
    "intent_domains",
    "anti_domains",
)

# Anchoring thresholds. Below either bar a per-section index costs more
# in the router prompt than a slice could save, so the whole file is the
# honest answer.
#
# MIN_FILE_BYTES was 8 KB and that bar was set too high. Measured over
# ``.multiplai/data/utilisation.jsonl`` (2026-08-22): 76.6% of all
# injected memory bytes were whole-file loads, and seven of 28 memory
# files carried no anchors at all. Every one of the seven cleared
# MIN_H2_SECTIONS and failed *only* the size bar — ``personal-projects.md``
# by 162 bytes, while being injected whole 57 times for 399 KB. Its 23
# section-shaped picks averaged 6866 bytes against a 7003-byte whole-file
# average, i.e. they were ``section_loader`` falling back to the full file.
#
# The two sides of the trade are not the same currency, which is what the
# old bar got wrong. Anchor lines are ~75 bytes each in the *router*
# prompt: a small, stable, cheap-model input. A whole-file injection lands
# in the main session's own context window. Paying router bytes to save
# session bytes is a good trade well below 8 KB.
#
# What the floor is still for: a 300-byte file with three one-line
# sections, where the anchor block would be larger than the file it
# indexes. 2 KB (~500 tokens) is where a whole-file load really is one
# router pick's worth of context. The smallest current memory file clears
# it by ~200 bytes, so this is a floor, not a fit to today's corpus.
MIN_H2_SECTIONS = 3
MIN_FILE_BYTES = 2 * 1024


def _anchor_prompt_block(sections: list[str]) -> str:
    """Instruction asking the model to gloss a *closed set* of headers.

    The names are extracted in code and echoed back verbatim; the model
    only supplies the one-line gloss. Letting it write the ``name`` is
    how you get a paraphrased header that matches no H2 and silently
    degrades to a full-file load for the life of the catalog entry.
    """
    listing = "\n".join(f"  {i}. {name}" for i, name in enumerate(sections, 1))
    return (
        '- "section_anchors": an array of objects, one per section listed '
        "below, in the same order, each "
        '{"name": "<the section name COPIED EXACTLY from the list>", '
        '"gloss": "<one short line, max ~12 words, saying what is in that '
        'section>"}. Copy each name character-for-character — do NOT '
        "reword, retitle, translate, merge, split, add, or drop any. The "
        "gloss is what a router reads to decide whether to load that "
        "section instead of the whole file, so make it concrete "
        "(name the things inside), not generic.\n"
        f"\nSECTIONS TO GLOSS (closed set — copy these names verbatim):\n{listing}\n"
    )


class MemoryGenerator(GeneratorBase):
    """Catalog generator for memory files.

    Scans the memory directory for .md files, summarizes each via LLM,
    generates ``section_anchors`` for files big enough to be worth
    loading a slice of, and preserves hand-authored catalog fields
    across regeneration.
    """

    name = "memory"
    catalog_filename = "memory.json"
    # Guarded by GeneratorBase.hand_field_losses: these are the fields
    # merge_entry promises to carry forward, so losing one is a defect.
    preserved_fields = _HAND_AUTHORED_FIELDS

    def __init__(self, *, config, model_client):
        super().__init__(config=config, model_client=model_client)
        # filename -> the H2 names the file actually has, recorded when
        # its prompt is built. merge_entry() validates the model's
        # anchors against this. Single-threaded asyncio, and populated
        # by the same task that later merges, so no locking is needed.
        self._h2_by_key: dict[str, list[str]] = {}

    def discover_sources(self) -> dict[str, Any]:
        """Find all .md files in the configured memory directory.

        ``CLAUDE.md`` and the two principles files are excluded (``_NOT_ROUTED``).
        The principles book loads every session by another route, and its
        examples are read only on request. ``CLAUDE.md`` is the memory system's own index —
        meta-documentation about how routing works, not personal context —
        and cataloging it makes the router recommend it under a name three
        other always-loaded files share. Memory-management skills read it
        by explicit path instead. Mirrors the banks generator's
        ``_NOT_CONTENT`` exclusion of ``bank.md``/``readme.md``; the base
        class prunes any previously cataloged entry once the source stops
        being discovered.
        """
        memory_dir = Paths.resolve().memory_dir()
        if not memory_dir.exists() or not memory_dir.is_dir():
            return {}

        sources = {}
        for path in sorted(memory_dir.glob("*.md")):
            if path.is_file() and path.name.lower() not in _NOT_ROUTED:
                sources[path.name] = path
        return sources

    # ---- Section anchors ----

    @staticmethod
    def anchorable_sections(text: str) -> list[str]:
        """H2 names worth anchoring, or ``[]`` when the file is too small.

        Returning ``[]`` is a normal outcome, not an error: most memory
        files are short enough that a whole-file load is already cheap.
        """
        if len(text.encode("utf-8")) < MIN_FILE_BYTES:
            return []
        names = h2_names(text)
        if len(names) < MIN_H2_SECTIONS:
            return []
        return names

    @staticmethod
    def validate_anchors(raw: Any, sections: list[str]) -> list[dict]:
        """Keep only anchors naming a real H2; canonicalise to the file's spelling.

        Accepts both the object form (``{"name", "gloss"}``) and a bare
        string, so a model that ignores half the instruction still yields
        usable anchors rather than none. Anything naming a header the
        file does not have is dropped — the alternative is an entry that
        invites the router to pick a section that will never resolve.
        """
        if not sections or not isinstance(raw, list):
            return []
        by_lower = {name.lower(): name for name in sections}
        out: list[dict] = []
        used: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                name, gloss = item, ""
            elif isinstance(item, dict):
                name = item.get("name") or item.get("section") or ""
                gloss = item.get("gloss") or item.get("summary") or ""
            else:
                continue
            if not isinstance(name, str) or not isinstance(gloss, str):
                continue
            canonical = by_lower.get(name.strip().lower())
            if canonical is None or canonical in used:
                continue
            used.add(canonical)
            anchor = {"name": canonical}
            gloss = " ".join(gloss.split())
            if gloss:
                anchor["gloss"] = gloss
            out.append(anchor)
        return out

    def build_prompt(self, source: Path) -> str:
        """Build an LLM prompt for summarizing a memory file.

        Emits intent_domains / anti_domains so context routing can
        select files by task intent (e.g., "blog-style-guide.md"
        matches intent_domain "writing long-form content") rather
        than by mtime+size alone. These fields are hand-authorable
        and preserved across regeneration.

        For files large enough to be worth partial loading, also asks
        for a one-line gloss per H2 section. The section *names* are
        extracted here, in code, and handed to the model as a closed
        set — see ``_anchor_prompt_block``.
        """
        content = source.read_text(encoding="utf-8")
        sections = self.anchorable_sections(content)
        self._h2_by_key[source.name] = sections
        return (
            "Analyze the following memory file and produce a JSON object with:\n"
            '- "summary": a concise summary of the file\'s content\n'
            '- "topics": an array of topic strings relevant for routing\n'
            '- "keywords": 5-15 *discriminative* terms that, if they appear '
            'in a user prompt, specifically point to THIS file — proper '
            'nouns, project/product/company names, people, and distinctive '
            'multi-word phrases unique to this file. EXCLUDE generic '
            'technology, tool, framework, or skill names (e.g. "Python", '
            '"Docker", "AWS", "API", "DevOps", "product management") that '
            'recur across many unrelated prompts: e.g. a career bio must '
            'NOT be keyworded with every technology it mentions. If a term '
            'would also fit dozens of unrelated files, leave it out — a '
            'precise short list beats an exhaustive one.\n'
            '- "intent_domains": an array of short phrases describing task intents '
            'for which this file is relevant (e.g., "writing a blog post", '
            '"debugging python async code"). 3-8 phrases.\n'
            '- "anti_domains": an array of short phrases describing task intents '
            'for which this file is NOT relevant (use sparingly — most files have '
            'none). 0-3 phrases.\n'
            + (_anchor_prompt_block(sections) if sections else "")
            + "\nRespond with ONLY valid JSON, no explanation.\n\n"
            f"---\n{content}\n---"
        )

    def merge_entry(self, existing: dict | None, new: dict) -> dict:
        """Merge new LLM entry with existing, preserving hand-authored fields.

        Preserves all entries in ``_HAND_AUTHORED_FIELDS`` (sections,
        bundle, co_retrieve_for, intent_domains, anti_domains) from the
        existing entry. Updates everything else (summary, topics,
        keywords) from the new LLM output.

        ``section_anchors`` is regenerated, never preserved, and is
        validated here against the H2 names read off the file when its
        prompt was built. A file below the anchoring thresholds ends up
        with no ``section_anchors`` key at all — and any stale one from a
        previous catalog is dropped, which is the point of taking the
        field out of the preserved set.
        """
        merged = dict(new)
        if existing is not None:
            for field in _HAND_AUTHORED_FIELDS:
                if field in existing:
                    merged[field] = existing[field]

        key = merged.get("source") or ""
        sections = self._h2_by_key.get(key)
        if sections is None:
            # No prompt was built for this key in this process (a direct
            # merge_entry call, or a resumed run). Validate against the
            # file if it is still readable; refusing to guess is what
            # keeps an unvalidated anchor out of the catalog.
            sections = self._sections_for_key(key)
        proposed = merged.get("section_anchors")
        proposed_count = len(proposed) if isinstance(proposed, list) else 0
        anchors = self.validate_anchors(proposed, sections)
        if anchors:
            merged["section_anchors"] = anchors
            logger.info(
                "SECTION_ANCHORS %s valid=%d/%d anchorable=%d",
                key, len(anchors), proposed_count, len(sections),
            )
        else:
            merged.pop("section_anchors", None)
            if sections:
                # This feature's failure mode *is* silence: no anchors means
                # whole-file loads forever for this entry, with no error, no
                # counter, and nothing the token_overlap router eval can see.
                # A model swap, a truncated JSON reply, or an all-invented name
                # list turns it off for one file and looks identical to working.
                logger.warning(
                    "SECTION_ANCHORS %s valid=0/%d — %d anchorable section(s) "
                    "exist but no proposed anchor named one; this entry falls "
                    "back to whole-file loads",
                    key, proposed_count, len(sections),
                )
        return merged

    def entry_needs_regeneration(
        self, key: str, source: Any, existing: dict | None
    ) -> bool:
        """True when an anchorable file's entry has no ``section_anchors`` yet.

        Without this, the whole feature ships inert. ``section_anchors`` is new
        in this version, the skip gate is content-hash-only, and every memory
        file's hash is unchanged on upgrade — so a release headlined "injected
        memory fell 68%" produces exactly zero anchors until each file is
        individually edited. One regeneration pass per anchorable file, once,
        is the cost; after that the hash gate governs as before.
        """
        if not isinstance(existing, dict):
            return False
        if existing.get("section_anchors"):
            return False
        try:
            text = Path(source).read_text(encoding="utf-8")
        except (OSError, TypeError):
            return False
        if not self.anchorable_sections(text):
            return False
        logger.info(
            "SECTION_ANCHORS %s regenerating — anchorable but has no anchors "
            "(this version derives them; the content hash cannot see that)",
            key,
        )
        return True

    def _sections_for_key(self, key: str) -> list[str]:
        """Anchorable H2 names for ``key``, read from disk. ``[]`` on any failure."""
        if not key:
            return []
        try:
            path = Paths.resolve().memory_dir() / key
            return self.anchorable_sections(path.read_text(encoding="utf-8"))
        except OSError:
            return []
