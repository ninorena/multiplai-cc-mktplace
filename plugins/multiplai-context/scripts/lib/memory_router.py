"""Multi-corpus router strategies for context assembly.

Context routing picks which catalog entries (across memory, skills,
and resources) to inject into each user prompt. Two strategies are
supported, selected via the ``memory_router`` plugin option:

    token_overlap  (default)   Cheap, offline. Tokenizes the prompt
                               (plus the last assistant response if
                               available) and scores each catalog
                               entry by word overlap against
                               intent_domains. Zero LLM calls,
                               instant, but misses synonym matches.

    llm                        Semantic. Sends ALL catalogs in a
                               SINGLE LLM call along with the prompt
                               and last response, asking for a
                               three-key JSON object selecting from
                               each corpus. Higher precision; one
                               LLM hop per prompt.

A third strategy, ``embeddings``, is reserved for a future port —
zero-cost per prompt after an initial embed pass, but requires model
setup out of scope here.

Both routers expose one method:

    select_multi(prompt, last_response, corpora, *, max_files_per_corpus=10)
        -> dict[str, list[str]]
        Multi-corpus selection. The canonical entry point for the
        context_manager flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Protocol, runtime_checkable

from multiplai_core.plugin_options import option, option_var

from lib.router_prompt import SYSTEM_PROMPT, FEW_SHOT_EXAMPLES, build_user_message
from lib.thinking import (
    core_supports_thinking,
    resolve_thinking_option,
)

logger = logging.getLogger(__name__)

# Bare option key; ``option_var`` derives the variable the harness exports
# (``CLAUDE_PLUGIN_OPTION_MEMORY_ROUTER``) for messages that name it.
ROUTER_OPTION = "memory_router"
ROUTER_ENV_VAR = option_var(ROUTER_OPTION)

# Model used by the llm router. Haiku is the right default for a
# per-prompt blocking hook: routing is a cheap classification, so the
# smallest/fastest model keeps latency tolerable. Overridable via
# the ``router_model`` option.
ROUTER_MODEL_OPTION = "router_model"
ROUTER_MODEL_ENV_VAR = option_var(ROUTER_MODEL_OPTION)
DEFAULT_ROUTER_MODEL = "claude-haiku-4-5"

# How long the llm router waits before giving up. A timeout injects **zero**
# memory for that turn, so this ceiling is the single most consequential number
# for anyone running `memory_router: llm` — and section anchors make each call
# do more work (a longer catalog to read, a finer choice to make), which pushes
# some prompts over it. It is configurable because the honest mitigation for
# "some of my prompts time out" is "raise the ceiling", and that advice is
# useless if it means editing this file.
ROUTER_TIMEOUT_OPTION = "router_timeout_seconds"
ROUTER_TIMEOUT_ENV_VAR = option_var(ROUTER_TIMEOUT_OPTION)
DEFAULT_ROUTER_TIMEOUT_SECONDS = 25.0

# Extended thinking is OFF for routing, and this is the single change that makes
# `memory_router: llm` viable inside a blocking hook.
#
# Measured 2026-08-09 (see INBOX/memory-work-2026/04-routing-latency/): a cold
# no-tools SDK call takes 18.4s with thinking on and 2.9s with it disabled. The
# cold-start story this file used to tell was wrong — spawning the CLI
# subprocess is worth 4-6s, not the ~12s claimed above, and `effort` is inert on
# this path. Thinking was the cost. Against a 30s hook kill, that was the
# difference between a median 27.4s (10 of 22 prompts killed on 2026-08-09) and
# fitting comfortably.
#
# Routing is a classification over a catalog the model can see in full. It is
# exactly the shape of task that does not need deliberation, so this is not a
# quality trade in the way it would be for extraction or synthesis. Set the
# option to "1"/"true" to restore thinking if a future catalog makes routing a
# harder judgement than it is today.
ROUTER_THINKING_OPTION = "router_thinking"
ROUTER_THINKING_ENV_VAR = option_var(ROUTER_THINKING_OPTION)


# Re-exported, not re-implemented. What this name has always meant here — "can
# the resolved dependencies carry a thinking= kwarg, cached because this runs
# per prompt on a blocking hook path" — is exactly ``lib/thinking.py``'s
# function, down to the default target (``ModelClient.query``, the router's
# call path). The wrapper this replaces added a second module-global cache over
# the same probe, which is one more thing to keep in sync and nothing else.
#
# What it guards is worth restating, because the failure mode is quiet: an old
# core rejects the *name* ``thinking``, and the resulting TypeError inside
# ``_select_async_multi`` is caught by its ``except Exception`` degrade path —
# so every prompt would answer from ``token_overlap`` while the log says the
# llm router is configured.


def resolve_router_thinking() -> dict | None:
    """Return the ``thinking`` config for router calls, or ``None`` to leave it
    to the model's default.

    Disabled unless the option explicitly asks for thinking back. ``None`` is
    the "send nothing" signal that `multiplai_core` needs for old-dependency
    tolerance, so this returns the dict for the *default* case and ``None`` for
    the opt-out — the inverse of how the other options here read. The shared
    implementation is ``lib/thinking.py``; this wrapper pins the router's
    option name and public name.
    """
    return resolve_thinking_option(ROUTER_THINKING_OPTION)


def resolve_router_timeout(raw: str | float | None = None) -> float:
    """The llm router's timeout in seconds, from config or the default.

    Fail-soft like ``resolve_strategy``: a non-numeric or non-positive value
    logs and falls back rather than raising, because a typo in a plugin option
    must not break a ``UserPromptSubmit`` hook.
    """
    value = raw if raw is not None else option(ROUTER_TIMEOUT_OPTION)
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_ROUTER_TIMEOUT_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring %s=%r — not a number; using %.1fs",
            ROUTER_TIMEOUT_ENV_VAR, value, DEFAULT_ROUTER_TIMEOUT_SECONDS,
        )
        return DEFAULT_ROUTER_TIMEOUT_SECONDS
    if seconds <= 0:
        logger.warning(
            "Ignoring %s=%r — must be positive; using %.1fs",
            ROUTER_TIMEOUT_ENV_VAR, value, DEFAULT_ROUTER_TIMEOUT_SECONDS,
        )
        return DEFAULT_ROUTER_TIMEOUT_SECONDS
    return seconds

STRATEGY_TOKEN_OVERLAP = "token_overlap"
STRATEGY_LLM = "llm"
STRATEGY_LLM_HYBRID = "llm_hybrid"
STRATEGY_EMBEDDINGS = "embeddings"

# token_overlap is the shipped default: instant, runs synchronously in
# the UserPromptSubmit hook every prompt. It is the *conservative* default,
# not the better router — see the quality numbers below.
#
# QUALITY (backtested 2026-08-10 on 300 real prompts from 21 days of
# transcripts, both routers scored against a hindsight oracle; full method in
# INBOX/memory-work-2026/04-routing-latency/):
#
#   arm            F1     NONE acc   files   bytes
#   token_overlap  20.0   32.7       3.20    162,935
#   llm            48.6   53.8       1.77    122,150
#
# The llm router wins 2.4x on F1 and injects fewer bytes. An earlier 17-case
# golden eval said the opposite — token_overlap scored 100% — but that set is
# phrased in the catalog's own vocabulary, which is precisely what token_overlap
# matches on, so it could not discriminate. Do not re-derive a preference from
# that eval; it is saturated.
#
# What token_overlap is actually good at is *stopping*: on session-opening
# prompts it gets NONE right 76.9% of the time against the llm router's 51.3%,
# and injects 93,001 bytes against 170,940. STRATEGY_LLM_HYBRID exists to take
# both — see HybridRouter.
#
# LATENCY (measured 2026-08-09, corrected): the ~12s "SDK cold start" this
# comment used to claim does not reproduce. Spawning the CLI subprocess is worth
# 4-6s; the real cost was extended thinking, and `effort` is inert here.
# thinking={"type":"disabled"} takes a cold call 18.4s -> 2.9s, which is why
# ROUTER_THINKING_OPTION defaults to off. Before that fix the llm router ran at a
# 27.4s median against a 30s hook kill and lost 10 of 22 prompts on 2026-08-09;
# that — not routing quality — is why the shipped default was reverted to
# token_overlap in multiplai-kit e2d12f2. An external routing daemon was
# explored and rejected: the SDK's session_id does not isolate conversations, so
# a warm long-lived process cannot serve concurrent sessions.
#
# The hook timeout (30s) and router timeout (25s) remain coupled: keep the
# router's below the hook's, leaving headroom for parse/log/inject.
# create_router() degrades an explicit llm choice to token_overlap when no model
# client is available.
DEFAULT_STRATEGY = STRATEGY_TOKEN_OVERLAP
KNOWN_STRATEGIES = frozenset({
    STRATEGY_TOKEN_OVERLAP,
    STRATEGY_LLM,
    STRATEGY_LLM_HYBRID,
    STRATEGY_EMBEDDINGS,
})

CORPUS_TYPES = ("memory", "skills", "resources")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CorpusRouter(Protocol):
    """Selects relevant entries across one or more catalog corpora."""

    name: str

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus pick — returns ``{corpus_name: [name, ...]}``."""
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


# Genuine function words only. Domain-meaningful gerunds (writing,
# managing, configuring…) are deliberately NOT here — IDF down-weights
# them corpus-adaptively, which is more robust than a hand list.
STOPWORDS = frozenset({
    "the", "and", "for", "with", "how", "you", "are", "not", "but",
    "that", "this", "what", "when", "where", "your", "from", "have",
    "has", "had", "was", "were", "will", "would", "can", "could",
    "should", "did", "does", "doing", "done", "into", "out", "about",
    "any", "all", "some", "its", "our", "his", "her", "their", "them",
    "they", "who", "why", "which", "while", "than", "then", "there",
    "here", "such", "via", "per", "etc", "get", "got", "let", "may",
    "might", "must", "need", "want", "like", "also", "just", "only",
})

# Tunable routing policy (select_multi only — select() stays pure
# rank+cap so the mechanism contract / unit tests are untouched).
# token_overlap is the DEFAULT router. Splitting the score into a
# trusted IDF-weighted intent_domains signal + a capped keyword boost
# (keywords alone can't clear MIN_SIGNAL) gives a large precision /
# false-positive / NONE-accuracy gain — the right trade given the
# dominant failure was a generic-keyword bloated entry (e.g.
# career-history.md) flooding unrelated prompts.
#
# KEEP_RATIO history: the original 0.20 was cited as "calibrated on a
# 50-case golden eval", but that eval set no longer exists in the repo
# (or the user's evals/ dir), so the figure was unverifiable. Meanwhile
# *production* told a clearer story: replaying 439 real routing calls
# (2026-07-10..16) showed 45% of memory routes hitting the 10-file cap —
# the domain_score is an UNNORMALIZED SUM of matched IDF weights, so a
# long/rich prompt inflates the whole ranking and a shallow 0.20×top
# floor admits a fat filler tail (median 16 candidates cleared it; the
# cap chopped to 10). The cap, not relevance, was doing the filtering.
#
# Note a relative floor can only be moved by the *ratio* itself, not by
# per-prompt score normalization: dividing every file in a prompt by the
# same constant (e.g. prompt length) leaves floor/top — and thus the
# picked set — identical. So the lever is the ratio. Production-replay
# (exact for a tighter policy, which can only trim the visible set):
#   ratio  %cap-hit  avg files
#   0.20     50.9%     7.96   (was default; the observed pathology)
#   0.30     28.4%     6.88   (new default: ~halves saturation)
#   0.35     17.1%     6.25
#   0.40      8.7%     5.55
# 0.30 is the conservative default (clear win, low recall risk); it is
# now a live-tunable plugin option (`keep_ratio`) so it can be dialed in
# production without a release while a real golden set is rebuilt.
DEFAULT_KEEP_RATIO = 0.30  # drop entries scoring < ratio × top
MIN_SIGNAL = 2.0           # top must clear this or the corpus → []

# intent_domains are the trusted, well-scoped signal (curated task
# phrases) and carry the full IDF-weighted score — they alone can
# clear MIN_SIGNAL. keywords are noisy: LLM catalog generation tends
# to dump every technology/proper-noun a file mentions (e.g. a career
# bio keyworded with Python/Docker/AWS), and IDF over a tiny catalog
# *rewards* those because they're locally rare. So keyword hits are a
# small, capped *boost* that can rank or tie-break a domain-matched
# entry but, by construction (cap < MIN_SIGNAL), can never pull a file
# in on keywords alone.
KEYWORD_UNIT = 0.5         # per distinct keyword TOKEN matched
KEYWORD_PHRASE_UNIT = 1.0  # per verbatim multi-word keyword matched
KEYWORD_CAP = 1.5          # max total keyword contribution per entry
                           # (< MIN_SIGNAL: keywords can't clear floor;
                           # eval is flat across [1.5,1.9] — the
                           # domains-primary split, not this value, is
                           # what moves precision/recall)

# A domain match must be BROAD, not merely strong, to clear the NONE
# floor. Smoothed IDF over a small catalog makes any df=1 term worth
# log((N+1)/2)+1 — ≈3.74 on a 30-entry catalog — so ONE incidental
# generic token ("search", "browser", "skill") out-scored MIN_SIGNAL
# and injected an unrelated file (a travel prompt pulled in the career
# file via "search"). Raising MIN_SIGNAL can't fix this without also
# killing legitimate single strong matches, so floor eligibility gates
# on match breadth instead: an entry may clear MIN_SIGNAL only if it
# matched ≥ MIN_DOMAIN_MATCHES distinct intent_domains tokens, OR a
# multi-word intent_domains phrase appears verbatim in the prompt
# (phrases whose other words are stopwords — "out of office" — match
# one scoring token yet are unmistakably deliberate). Ineligible
# entries still appear in the scored ranking/diagnostics but can never
# be picked via select_multi — mirroring how KEYWORD_CAP keeps
# keywords from pulling a file in alone. Hardcoded rather than a
# plugin option: 2 is the smallest breadth that kills one-token noise,
# and no catalog geometry favors a different value.
MIN_DOMAIN_MATCHES = 2

# Per-term ceiling on smoothed IDF, bounding the small-catalog
# inflation above: no single locally-rare term can dominate a score
# (the historical audiovideo.md=23.5 spike was several generic tokens
# × df=1 IDF ≈ 3.74 each). 2.5 sits above the IDF of a term shared by
# a few entries (real signal) but below the df=1 ceiling for catalogs
# of ≳12 entries, so it only trims the inflated top.
MAX_TERM_IDF = 2.5

# Short go-aheads: the conversation already has the context (mirrors
# the LLM router's rule #1).
_CONTINUATION = frozenset({
    "yes", "y", "ok", "okay", "go", "sure", "yep", "yeah", "do it",
    "go ahead", "continue", "next", "proceed", "sounds good", "lgtm",
    "ship it", "perfect", "thanks", "thank you", "great", "nice",
})


def _tokenize(text: str) -> set[str]:
    """Lowercase content-word set for overlap scoring (stopwords removed)."""
    out: set[str] = set()
    for w in text.split():
        t = w.strip(".,;:!?\"'()[]{}").lower()
        if len(t) >= 3 and t not in STOPWORDS:
            out.add(t)
    return out


def _idf_map(per_entry_terms: list[set[str]]) -> dict[str, float]:
    """Smoothed IDF over the corpus itself — zero cost, corpus-adaptive.

    A term in one entry's curated fields is highly discriminating; a
    term in many is near-worthless. ``log((N+1)/(df+1)) + 1`` floors
    at 1.0 so a universal term still contributes a little (keeps the
    uniform-catalog unit tests well-defined), and is capped at
    ``MAX_TERM_IDF`` so a df=1 term on a small catalog can't inflate
    past the point where one token dominates a score.
    """
    n = len(per_entry_terms)
    df: dict[str, int] = {}
    for terms in per_entry_terms:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    return {
        t: min(math.log((n + 1) / (c + 1)) + 1.0, MAX_TERM_IDF)
        for t, c in df.items()
    }


def _apply_policy(
    scored: list[tuple[float, str]],
    max_files: int,
    keep_ratio: float = DEFAULT_KEEP_RATIO,
    eligible: set[str] | None = None,
) -> list[str]:
    """Turn a full ranking into a relevance-gated, variable-length pick.

    Three gates replace the old "always take top-N":
    - Eligibility gate: when ``eligible`` is given, only those entries
      can be picked and the floor/threshold anchor on the best
      *eligible* score — a single-token domain match (see
      MIN_DOMAIN_MATCHES) may rank in the diagnostics but never
      injects. ``None`` means all entries are eligible (select()'s
      pure rank+cap contract and direct callers are unchanged).
    - NONE floor: if even the best eligible entry is below
      ``MIN_SIGNAL`` the prompt has no real memory match → return
      nothing.
    - Relative cutoff: keep only entries within ``keep_ratio`` of the
      top score (and above the floor), so the output length tracks
      how many files are actually relevant rather than the cap. The
      ratio is the sole lever on the filler tail (see DEFAULT_KEEP_RATIO
      notes) and is plumbed from the ``keep_ratio`` plugin option so it
      can be tuned without a code release.
    """
    if not scored:
        return []
    pool = scored if eligible is None else [p for p in scored if p[1] in eligible]
    if not pool:
        return []
    top = pool[0][0]
    if top < MIN_SIGNAL:
        return []
    threshold = max(MIN_SIGNAL, keep_ratio * top)
    kept = [fn for s, fn in pool if s >= threshold]
    return kept[:max_files]


def _entry_filename(entry: dict) -> str:
    """Resolve the catalog-entry key.

    Skills entries use ``name``; memory and resources use ``source``
    (with ``path`` / ``file`` legacy fallbacks).
    """
    return (
        entry.get("source")
        or entry.get("path")
        or entry.get("name")
        or entry.get("file", "")
    )


# ---------------------------------------------------------------------------
# Token-overlap router (cheap, offline)
# ---------------------------------------------------------------------------


class TokenOverlapRouter:
    """Pure token-overlap router — no network, no LLM calls."""

    name = STRATEGY_TOKEN_OVERLAP

    def __init__(self, *, keep_ratio: float = DEFAULT_KEEP_RATIO) -> None:
        # Relative-cutoff ratio for select_multi's _apply_policy. Plumbed
        # from the `keep_ratio` plugin option (via create_router) so the
        # filler-tail lever is tunable without a release.
        self.keep_ratio = keep_ratio
        # Populated by select_multi each call: per-corpus full
        # pre-truncation ranking + cap diagnostics, for the context
        # manager to log. This is the routing-quality signal /health
        # reports. Empty until the first select_multi call.
        self.last_scores: dict[str, dict] = {}

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus selection.

        Token scoring combines prompt and last-response tokens — the
        last response disambiguates short prompts where the same
        token (e.g., "costs") could match different domains. Each
        corpus is scored independently using the same tokens.

        Per-corpus scoring diagnostics (full pre-truncation ranking,
        the cap, candidate count, whether the cap was binding) are
        stashed on ``self.last_scores`` for the context manager to
        log — this is the routing-quality signal /health reports.
        """
        self.last_scores = {}
        if not prompt:
            return {ct: [] for ct in CORPUS_TYPES}

        # Continuation guard: a short go-ahead means the conversation
        # already has the context — inject nothing (mirrors the LLM
        # router's rule #1, and the dominant golden-eval failure mode).
        norm = " ".join(prompt.lower().split()).strip(".,;:!?")
        if norm in _CONTINUATION:
            self.last_scores = {
                ct: {"scored": [], "cap": max_files_per_corpus,
                     "n_candidates": 0, "capped": False,
                     "continuation": True}
                for ct in CORPUS_TYPES
            }
            return {ct: [] for ct in CORPUS_TYPES}

        combined = prompt
        if last_response:
            combined = f"{prompt}\n{last_response}"
        result: dict[str, list[str]] = {}
        for corpus_type in CORPUS_TYPES:
            entries = corpora.get(corpus_type) or []
            scored, eligible = self._scored_pairs(combined, entries)
            picks = _apply_policy(
                scored, max_files_per_corpus, self.keep_ratio, eligible=eligible
            )
            result[corpus_type] = picks
            score_by_fn = {fn: s for s, fn in scored}
            self.last_scores[corpus_type] = {
                "scored": scored,
                # The eligibility gate means picks are no longer a
                # contiguous prefix of the ranking — expose the actual
                # injected (score, filename) pairs so the ROUTING_SCORES
                # log line reports what was really injected.
                "picked_scored": [(score_by_fn[fn], fn) for fn in picks],
                "n_eligible": len(eligible),
                # Best ELIGIBLE score (None when nothing passed the
                # breadth gate). On abstention the raw top may be an
                # ineligible entry scoring above MIN_SIGNAL, so the
                # activity-line hint needs this to say truthfully
                # whether the corpus abstained on breadth or on score.
                "top_eligible": next(
                    (s for s, fn in scored if fn in eligible), None
                ),
                "cap": max_files_per_corpus,
                "n_candidates": len(scored),
                "n_picked": len(picks),
                # "capped" now means the *policy output* hit the ceiling,
                # not merely that the raw pool exceeded it.
                "capped": len(picks) >= max_files_per_corpus,
            }
        return result

    def gate(
        self,
        text: str,
        catalog_entries: list[dict],
    ) -> tuple[list[tuple[float, str]], set[str]]:
        """Public seam over the offline gates: ``(ranking, eligible)``.

        ``eligible`` is "cleared the ``MIN_DOMAIN_MATCHES`` breadth gate and was
        not vetoed by ``anti_domains``" — a vetoed entry never enters the
        ranking, so the two mechanisms collapse into one set membership test.

        Exists for :class:`HybridRouter`, which needs the gates without the
        ranking policy: it is filtering somebody else's picks, not making its
        own. Keep the private ``_scored_pairs`` for this class's own use.
        """
        return self._scored_pairs(text, catalog_entries)

    def _scored_pairs(
        self,
        prompt: str,
        catalog_entries: list[dict],
    ) -> tuple[list[tuple[float, str]], set[str]]:
        """Score every entry; return ``(ranking, eligible)``.

        ``ranking`` is ``(score, filename)`` sorted desc — full,
        un-truncated; callers truncate / cut off. ``eligible`` is the
        set of filenames whose domain match is broad enough to clear
        the NONE floor (see MIN_DOMAIN_MATCHES): ≥ 2 distinct
        ``intent_domains`` tokens matched, or a multi-word domain
        phrase verbatim in the prompt. ``_apply_policy`` consumes it;
        ineligible entries rank/tie-break in diagnostics but are never
        picked.

        Scoring (two asymmetric signals):
        - ``intent_domains`` — the trusted, well-scoped signal. Tokens
          are IDF-weighted over the *domain* corpus (a term unique to
          one file dominates — capped at ``MAX_TERM_IDF``; a shared one
          is ~free) and carry the full score: only this can clear
          ``MIN_SIGNAL``.
        - ``keywords`` — noisy (LLM catalogs over-tag generic tech /
          proper nouns; IDF-over-a-tiny-corpus then rewards them). Each
          matched keyword token (``KEYWORD_UNIT``) and verbatim
          multi-word keyword (``KEYWORD_PHRASE_UNIT``) adds a flat,
          non-IDF amount, but the total keyword contribution per entry
          is capped at ``KEYWORD_CAP`` (< ``MIN_SIGNAL``) — keywords can
          boost / tie-break a domain match but never pull a file in
          alone.
        - ``summary``/``topics`` are not scored (prose floods the bag).
        - ``anti_domains`` still hard-excludes (unchanged contract).
        """
        if not catalog_entries or not prompt:
            return [], set()
        prompt_tokens = _tokenize(prompt)
        if not prompt_tokens:
            return [], set()
        prompt_lc = prompt.lower()

        # Pass 1: per-entry domain terms + verbatim-checkable domain
        # phrases (primary) kept separate from keyword terms / phrases
        # (demoted).
        rows: list[tuple[str, set[str], list[str], set[str], list[str], set[str]]] = []
        per_entry_domain_terms: list[set[str]] = []
        for entry in catalog_entries:
            filename = _entry_filename(entry)
            if not filename:
                continue
            domain_terms: set[str] = set()
            domain_phrases: list[str] = []
            for phrase in entry.get("intent_domains", []) or []:
                if isinstance(phrase, str):
                    domain_terms |= _tokenize(phrase)
                    normalized = " ".join(phrase.lower().split())
                    if " " in normalized:
                        domain_phrases.append(normalized)
            kw_terms: set[str] = set()
            phrases: list[str] = []
            for kw in entry.get("keywords", []) or []:
                if isinstance(kw, str) and kw.strip():
                    kw_terms |= _tokenize(kw)
                    if " " in kw.strip():
                        phrases.append(kw.strip().lower())
            anti: set[str] = set()
            for phrase in entry.get("anti_domains", []) or []:
                if isinstance(phrase, str):
                    anti |= _tokenize(phrase)
            # Drop anti tokens that are also the entry's own positive
            # vocabulary. Anti phrases routinely reuse domain words
            # (e.g. "...inspection UNRELATED to memory routing"), and a
            # bag-of-words OR match would otherwise hard-exclude an entry
            # on the very tokens that make it relevant. Only the
            # distinctive anti terms should gate exclusion.
            anti -= domain_terms
            rows.append(
                (filename, domain_terms, domain_phrases, anti, phrases, kw_terms)
            )
            per_entry_domain_terms.append(domain_terms)

        idf = _idf_map(per_entry_domain_terms)

        # Pass 2: IDF-weighted domain score + capped keyword boost,
        # plus the floor-eligibility predicate (match breadth).
        scored: list[tuple[float, str]] = []
        eligible: set[str] = set()
        for filename, domain_terms, domain_phrases, anti, phrases, kw_terms in rows:
            if anti & prompt_tokens:
                continue  # Respect anti_domains — skip this entry
            matched = domain_terms & prompt_tokens
            domain_score = sum(idf.get(t, 1.0) for t in matched)
            kw_boost = KEYWORD_UNIT * len(kw_terms & prompt_tokens)
            kw_boost += KEYWORD_PHRASE_UNIT * sum(
                1 for ph in phrases if ph in prompt_lc
            )
            score = domain_score + min(kw_boost, KEYWORD_CAP)
            if score <= 0.0:
                continue
            scored.append((score, filename))
            if len(matched) >= MIN_DOMAIN_MATCHES or any(
                ph in prompt_lc for ph in domain_phrases
            ):
                eligible.add(filename)

        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return scored, eligible


# ---------------------------------------------------------------------------
# LLM router (semantic, ONE call covering all corpora)
# ---------------------------------------------------------------------------


def _parse_llm_multi_selection(
    raw: str,
    known_per_corpus: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Extract a ``{corpus: [name, ...]}`` selection from the LLM response.

    Tolerates markdown-fenced JSON. Filters each corpus's selections
    to entries actually present in that corpus's known-name set.
    Section refs (``"file#Section"``) are validated by stripping the
    fragment before checking presence.

    Two things the fragment therefore needs, because nothing checks it:

    * **Non-printing characters are dropped from the whole ref.** Only the part
      before the ``#`` is matched against the known-name set; the fragment is
      free text this model wrote. A newline in it forges a second line in
      ``context_manager.log`` — the file the log-doctor skill parses and
      reasons over. Stripping is done here, at the boundary, rather than at
      each of the log sites that interpolate a ref.
    * **Repeats are dropped.** The same ref twice is one pick, and downstream
      a duplicate used to overwrite its own loaded content with the copy that
      omits the file's preamble.

    Raises:
        RouterCallFailed: the reply was not a JSON object. This used to return
            empty, which is the same bug :class:`RouterCallFailed` was created
            to fix one layer up: downstream, an empty pick from a router that
            *ran* means "nothing is relevant" and suppresses the context
            manager's recency net. A model that replied with prose has not
            decided that nothing is relevant — it has failed to answer, and the
            honest response is to degrade to the offline ranking.

            An empty but well-formed ``{"memory": [], ...}`` is still a genuine
            abstention and still returns empty. Only malformed replies raise.
    """
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise RouterCallFailed("returned non-JSON") from None

    if not isinstance(parsed, dict):
        raise RouterCallFailed(
            f"returned JSON {type(parsed).__name__}, not an object"
        )

    result: dict[str, list[str]] = {}
    for corpus_type in CORPUS_TYPES:
        raw_list = parsed.get(corpus_type, [])
        if not isinstance(raw_list, list):
            result[corpus_type] = []
            continue
        known = known_per_corpus.get(corpus_type, set())
        validated: list[str] = []
        for item in raw_list:
            if not isinstance(item, str):
                continue
            item = "".join(ch for ch in item if ch.isprintable())
            base = item.split("#", 1)[0]
            if base in known and item not in validated:
                validated.append(item)
        result[corpus_type] = validated
    return result


# How long a timed-out router call's cancelled task gets to tear down (close
# the CLI subprocess) before the event loop is abandoned. Bounded, because the
# teardown is exactly the thing that can wedge.
_LEFTOVER_GRACE_S = 1.0


def _run_without_awaiting_leftovers(coro, grace_s: float = _LEFTOVER_GRACE_S):
    """``asyncio.run(coro)`` without waiting unboundedly on leftover tasks.

    ``asyncio.run`` cancels every task still pending when *coro* returns and
    then waits for each to finish. After ``hard_timeout`` gives up on a
    wedged SDK call, that task is still pending, so ``asyncio.run`` would
    block on the same teardown ``hard_timeout`` just stepped around. This
    gives leftovers *grace_s* seconds, then closes the loop regardless.

    A child process the leftover task started is not killed here; it outlives
    the loop (verified with a stub whose wait ignores cancellation). The SDK
    gives the CLI its own stdin/stdout/stderr pipes, so an orphaned CLI cannot
    hold the hook's output open, and its stdin reaches EOF when this process
    exits.

    Like ``asyncio.run``, refuses to run inside a running loop (raises
    ``RuntimeError``, which ``select_multi`` degrades on) rather than
    replacing that loop as the thread's current one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "_run_without_awaiting_leftovers() cannot be called from a "
            "running event loop"
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                logger.warning(
                    "LLMRouter: %d task(s) still pending after the call; "
                    "giving them %.1fs to tear down, then leaving them",
                    len(pending), grace_s,
                )
                loop.run_until_complete(asyncio.wait(pending, timeout=grace_s))
        except Exception:
            logger.debug("Leftover-task teardown raised", exc_info=True)
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class RouterCallFailed(Exception):
    """The model call did not produce an answer — timeout, transport, or loop.

    Exists so a *failure* cannot be mistaken for an *abstention*. Both used to
    leave ``select_multi`` as an empty dict, and the context manager reads an
    empty memory pick from a router that ran as a deliberate "nothing is
    relevant" (``router_abstained``) — which suppresses its own recency net.
    A timed-out prompt therefore got **no memory at all**, logged as one
    WARNING and otherwise indistinguishable from a correct abstention.
    """


class LLMRouter:
    """Semantic router — one LLM call per prompt covering all corpora.

    **A failed call degrades to the token_overlap router, never to nothing.**
    ``create_router`` already refuses to hand back an ``LLMRouter`` when no
    model client exists, for the stated reason that it "would silently return
    empty picks every prompt". A timeout produces exactly that outcome, so the
    same guard belongs on the call path: no client and no answer are the same
    failure, and both degrade to the offline ranking rather than to silence.

    Genuine abstention is preserved and is *not* a failure: an empty prompt, an
    empty corpus, or a model that ran and chose nothing all still return empty.
    Only :class:`RouterCallFailed` triggers the degrade.

    The fallback also changes what a good timeout is. When a timeout cost the
    whole turn's memory you wanted the ceiling as high as the hook allowed;
    once it merely costs you the offline ranking, a *shorter* ceiling is better
    — you stop paying blocking latency for an answer you are about to discard.
    """

    name = STRATEGY_LLM

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        model: str | None = None,
        fallback: "TokenOverlapRouter | None" = None,
        thinking: dict | None = None,
        thinking_set: bool = False,
    ) -> None:
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else resolve_router_timeout()
        )
        # `None` is a meaningful value here (send no thinking config at all), so
        # it cannot double as "caller did not specify". Hence the explicit flag
        # rather than the `is not None` idiom used for the other arguments.
        self._thinking = thinking if thinking_set else resolve_router_thinking()
        if self._thinking is not None and not core_supports_thinking():
            # core_supports_thinking() has already said *which* boundary failed
            # and how to fix it, once per process. This adds the part only the
            # router knows: what that costs against its own deadline.
            logger.warning(
                "Router calls therefore keep extended thinking on: expect ~18s "
                "per prompt against the %.0fs ceiling instead of ~3s, so "
                "prompts will start losing their injected memory.",
                self._timeout_seconds,
            )
            self._thinking = None
        self._model = (
            model
            or option(ROUTER_MODEL_OPTION)
            or DEFAULT_ROUTER_MODEL
        )
        # Built here, not on demand: constructing it inside the failure path
        # would put a second thing that can throw between the failure and the
        # recovery from it.
        self._fallback = fallback if fallback is not None else TokenOverlapRouter()

    def _degrade(
        self,
        reason: str,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        max_files_per_corpus: int,
    ) -> dict[str, list[str]]:
        """Answer with the offline router after a failed model call."""
        logger.warning(
            "LLMRouter %s; degrading to %s for this prompt",
            reason, self._fallback.name,
        )
        try:
            return self._fallback.select_multi(
                prompt, last_response, corpora,
                max_files_per_corpus=max_files_per_corpus,
            )
        except Exception:
            # The offline router is pure Python over an in-memory catalog, so
            # this should not happen — but the whole point of this path is that
            # it runs when something already went wrong.
            logger.exception("token_overlap fallback also failed")
            return {ct: [] for ct in CORPUS_TYPES}

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        """Multi-corpus selection via a single LLM call covering all 3 corpora."""
        empty = {ct: [] for ct in CORPUS_TYPES}
        if not prompt:
            return empty

        known_per_corpus: dict[str, set[str]] = {}
        any_entries = False
        for corpus_type in CORPUS_TYPES:
            entries = corpora.get(corpus_type) or []
            known_per_corpus[corpus_type] = {
                _entry_filename(e) for e in entries if _entry_filename(e)
            }
            if known_per_corpus[corpus_type]:
                any_entries = True
        if not any_entries:
            return empty

        try:
            picks = _run_without_awaiting_leftovers(
                self._bounded_select_multi(prompt, last_response, corpora, known_per_corpus)
            )
        except RouterCallFailed as e:
            return self._degrade(
                str(e), prompt, last_response, corpora, max_files_per_corpus)
        except RuntimeError as e:
            return self._degrade(
                f"could not run event loop: {e}",
                prompt, last_response, corpora, max_files_per_corpus)
        except Exception as e:
            logger.exception("LLMRouter call raised")
            return self._degrade(
                f"call raised {type(e).__name__}",
                prompt, last_response, corpora, max_files_per_corpus)

        capped = {
            ct: picks.get(ct, [])[:max_files_per_corpus] for ct in CORPUS_TYPES
        }
        if not any(capped.values()):
            # Make a genuine abstention legible. Three different states used to
            # print the same `picked: memory=0` line downstream — abstained,
            # replied unparseably, timed out — and telling them apart in the log
            # was guesswork. The other two now degrade instead, so this line
            # means the model ran, answered well-formed, and chose nothing.
            logger.info("LLMRouter abstained: model ran and picked nothing")
        return capped

    async def _bounded_select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        known_per_corpus: dict[str, set[str]],
    ) -> dict[str, list[str]]:
        """:meth:`_select_async_multi` under the timeout, timeout as failure.

        The budget covers the WHOLE call, ``create_client`` included: client
        creation spawns the CLI subprocess (measured 4-6s, unbounded when it
        stalls), and with the timeout only around ``client.query`` a stalled
        spawn ran until the 30s harness kill — no :class:`RouterCallFailed`,
        no ``_degrade()``, no token_overlap fallback, no log line, and a
        prompt with no memory at all (M5).

        A timeout raises rather than returning empty: an empty answer from a
        router that *ran* reads downstream as a deliberate abstention, and
        abstention suppresses the context manager's own recency net.

        ``hard_timeout``, not ``asyncio.wait_for``: on timeout ``wait_for``
        cancels the call and then *waits for the cancellation to finish*, and
        the SDK's cancellation tears down the CLI subprocess, which can wedge.
        On 2026-09-27 00:38 a Haiku call got no reply, the 25s deadline
        passed, and the hook was still inside ``wait_for`` when the harness
        killed it at 30s: no RouterCallFailed, no fallback, no log line.
        ``hard_timeout`` cancels without waiting and raises at the deadline.
        """
        from multiplai_core.aio import hard_timeout

        try:
            return await hard_timeout(
                self._select_async_multi(
                    prompt, last_response, corpora, known_per_corpus
                ),
                self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise RouterCallFailed(
                f"timed out after {self._timeout_seconds:.1f}s"
            ) from None

    async def _select_async_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        known_per_corpus: dict[str, set[str]],
    ) -> dict[str, list[str]]:
        from multiplai_core.model_client import create_client

        client = await create_client(component="memory-router")
        user_msg = build_user_message(prompt, last_response, corpora)
        query_kwargs: dict = dict(
            system=SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES,
            messages=[{"role": "user", "content": user_msg}],
            model=self._model,
        )
        # Omit the keyword entirely rather than passing None: an older core
        # rejects the *name*, whatever the value. __init__ has already forced
        # self._thinking to None when the resolved core cannot take it.
        if self._thinking is not None:
            query_kwargs["thinking"] = self._thinking
        # No inner wait_for: _bounded_select_multi owns the one deadline, so
        # client creation and the query share a single budget.
        response = await client.query(**query_kwargs)
        return _parse_llm_multi_selection(response.content, known_per_corpus)


# ---------------------------------------------------------------------------
# Hybrid router (LLM picks; the offline gates filter)
# ---------------------------------------------------------------------------


class HybridRouter:
    """LLM picks the files; ``token_overlap``'s gates decide which survive.

    **Why this exists.** Backtested on 300 real prompts (2026-08-10, see
    ``INBOX/memory-work-2026/04-routing-latency/``), the two routers fail in
    opposite directions. The LLM router is 2.4x better at *finding* the right
    file (F1 48.6 vs 20.0) and worse at *stopping*: on session-opening prompts
    it injects 170,940 bytes against token_overlap's 93,001, and gets NONE right
    51.3% of the time against 76.9%. token_overlap's edge there is not its
    scoring — it is two mechanisms the LLM never sees:

    - the **breadth gate** (``MIN_DOMAIN_MATCHES``): a file needs >= 2 distinct
      ``intent_domains`` tokens in the prompt, or one verbatim multi-word domain
      phrase. One incidental word cannot pull a file in.
    - the **``anti_domains`` veto**: an explicit hard-exclude, with the entry's
      own positive vocabulary subtracted first.

    Both are computed by ``TokenOverlapRouter._scored_pairs``, whose ``eligible``
    set is exactly "cleared the breadth gate and was not vetoed" — a vetoed
    entry never enters the ranking, so it can never be eligible. So the filter is
    a set-membership test, not a reimplementation.

    **What this trades.** The gates can only remove. Every drop is a recall risk:
    a file the LLM was right about, phrased in vocabulary the catalog does not
    carry, gets filtered. That is the exact case the LLM router was winning, so
    this is a genuine bet and not a free improvement — which is why it ships as
    an opt-in strategy rather than as a change to ``llm``, and why the drops are
    logged rather than silent. Score it on the discriminating case set before
    making it anybody's default.
    """

    name = "llm_hybrid"

    def __init__(
        self,
        *,
        llm: "LLMRouter | None" = None,
        gate: "TokenOverlapRouter | None" = None,
    ) -> None:
        self._gate = gate if gate is not None else TokenOverlapRouter()
        # The LLM router's own degrade path lands on the same gate instance, so a
        # failed call yields plain token_overlap output — already gated, and not
        # double-filtered by the code below (see select_multi).
        self._llm = llm if llm is not None else LLMRouter(fallback=self._gate)
        # Consumed by context_manager's ROUTING_SCORES line. Without it,
        # switching to this strategy would silently turn off the routing-quality
        # log the eval harness and /health both read.
        self.last_scores: dict[str, dict] = {}

    def select_multi(
        self,
        prompt: str,
        last_response: str | None,
        corpora: dict[str, list[dict]],
        *,
        max_files_per_corpus: int = 10,
    ) -> dict[str, list[str]]:
        picks = self._llm.select_multi(
            prompt, last_response, corpora,
            max_files_per_corpus=max_files_per_corpus,
        )
        # The gate scores against the same text the offline router would see.
        # Passing only `prompt` here would gate on less context than
        # token_overlap itself uses and drop files the LLM picked *because* of
        # the previous turn.
        gate_text = prompt if not last_response else f"{prompt}\n{last_response}"

        self.last_scores = {}
        result: dict[str, list[str]] = {}
        dropped: dict[str, list[str]] = {}
        for corpus_type in CORPUS_TYPES:
            selected = picks.get(corpus_type, [])
            entries = corpora.get(corpus_type) or []
            if not selected:
                # Still populate the diagnostics from the gate (P12): the
                # ROUTING_SCORES line — parsed by /health and the eval
                # harness — must distinguish "the LLM picked nothing here"
                # (candidates scored, none injected) from "corpus empty"
                # (no candidates at all). Skipping the entry made both look
                # identical downstream.
                scored, eligible = (
                    self._gate.gate(gate_text, entries) if entries else ([], set())
                )
                result[corpus_type] = []
                self.last_scores[corpus_type] = {
                    "scored": scored,
                    "picked_scored": [],
                    "n_eligible": len(eligible),
                    "top_eligible": next(
                        (s for s, fn in scored if fn in eligible), None
                    ),
                    "cap": max_files_per_corpus,
                    "n_candidates": len(scored),
                    "n_picked": 0,
                    "capped": False,
                }
                continue
            scored, eligible = self._gate.gate(gate_text, entries)
            kept, cut = [], []
            for pick in selected:
                # Section refs ("file.md#Section") are gated on their file: the
                # eligible set is keyed by filename, and the catalog's
                # intent_domains describe the whole file.
                base = pick.split("#", 1)[0]
                (kept if base in eligible else cut).append(pick)
            result[corpus_type] = kept
            if cut:
                dropped[corpus_type] = cut

            # Diagnostics in the shape context_manager already logs. The scores
            # are the *gate's*, not the LLM's — the LLM produces no scores at
            # all — so this line answers "why was this dropped", which is the
            # only question the gate can be wrong about.
            score_by_fn = {fn: s for s, fn in scored}
            self.last_scores[corpus_type] = {
                "scored": scored,
                "picked_scored": [
                    (score_by_fn.get(p.split("#", 1)[0], 0.0), p) for p in kept
                ],
                "n_eligible": len(eligible),
                "top_eligible": next(
                    (s for s, fn in scored if fn in eligible), None
                ),
                "cap": max_files_per_corpus,
                "n_candidates": len(scored),
                "n_picked": len(kept),
                "capped": len(kept) >= max_files_per_corpus,
                "hybrid_dropped": cut,
            }

        if dropped:
            # Logged, not silent: this is the mechanism's whole risk surface, and
            # a recall regression is invisible unless you can see what was cut.
            logger.info("HYBRID_GATE dropped=%s", json.dumps(dropped, sort_keys=True))
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_strategy(raw: str | None = None) -> str:
    """Return the effective strategy name, defaulting to ``token_overlap``.

    Unknown values are logged and fall back to the default rather than
    raising — a typo in a plugin option shouldn't break the hook.
    Note: this returns the *configured* strategy; create_router() may
    still degrade an explicit ``llm`` → ``token_overlap`` when no
    client exists.
    """
    value = (raw if raw is not None else option(ROUTER_OPTION)).strip().lower()
    if not value:
        return DEFAULT_STRATEGY
    if value not in KNOWN_STRATEGIES:
        logger.warning(
            "Unknown memory router strategy %r; falling back to %s",
            value, DEFAULT_STRATEGY,
        )
        return DEFAULT_STRATEGY
    return value


def create_router(
    strategy: str | None = None, *, keep_ratio: float | None = None
) -> CorpusRouter:
    """Build a router for *strategy* (or the env default).

    ``keep_ratio`` (when set) overrides the token_overlap relative-cutoff
    ratio — the context manager passes ``cfg.keep_ratio`` so the plugin
    option takes effect; ``None`` keeps the module default. It has no
    effect on the LLM router, which does its own selection.

    ``embeddings`` is accepted by name but not yet implemented — it
    raises :class:`NotImplementedError` so a misconfiguration is loud
    at session start rather than silently producing bad routing.
    """
    kr = DEFAULT_KEEP_RATIO if keep_ratio is None else keep_ratio
    effective = resolve_strategy(strategy)
    if effective == STRATEGY_TOKEN_OVERLAP:
        return TokenOverlapRouter(keep_ratio=kr)
    if effective in (STRATEGY_LLM, STRATEGY_LLM_HYBRID):
        # Degrade to the offline router when no model client exists
        # (no Agent SDK host, no API key) — otherwise LLMRouter would
        # silently return empty picks every prompt.
        try:
            from multiplai_core.model_client import detect_client_type
            client = detect_client_type()
        except Exception:
            client = "none"
        if client.startswith("none"):
            logger.warning(
                "memory_router=%s but no model client available (%s); "
                "degrading to token_overlap for this session",
                effective, client,
            )
            return TokenOverlapRouter(keep_ratio=kr)
        # The fallback carries the same keep_ratio as an explicitly-configured
        # token_overlap router, so degrading mid-session lands on the routing
        # the user actually configured rather than on module defaults.
        gate = TokenOverlapRouter(keep_ratio=kr)
        llm = LLMRouter(fallback=gate)
        if effective == STRATEGY_LLM_HYBRID:
            # One gate instance, shared: the hybrid's filter and the LLM
            # router's degrade path must agree on keep_ratio, or a degraded
            # prompt would be gated differently from a normal one.
            return HybridRouter(llm=llm, gate=gate)
        return llm
    if effective == STRATEGY_EMBEDDINGS:
        raise NotImplementedError(
            "Embeddings router is reserved for a future port — set "
            f"{ROUTER_ENV_VAR}={STRATEGY_TOKEN_OVERLAP} or "
            f"{STRATEGY_LLM} to pick an available strategy."
        )
    raise ValueError(f"Unhandled memory router strategy: {effective}")
