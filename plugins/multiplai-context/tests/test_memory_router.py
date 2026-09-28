"""Tests for scripts/lib/memory_router.py."""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Strategy resolution
# ---------------------------------------------------------------------------


class TestResolveStrategy:
    def test_default_is_token_overlap(self, monkeypatch):
        # token_overlap is the default: instant, runs synchronously
        # every prompt. llm is opt-in/deferred (~17s via the SDK).
        from lib.memory_router import (
            DEFAULT_STRATEGY,
            ROUTER_ENV_VAR,
            STRATEGY_TOKEN_OVERLAP,
            resolve_strategy,
        )
        monkeypatch.delenv(ROUTER_ENV_VAR, raising=False)
        assert DEFAULT_STRATEGY == STRATEGY_TOKEN_OVERLAP
        assert resolve_strategy() == STRATEGY_TOKEN_OVERLAP

    def test_env_override_to_llm(self, monkeypatch):
        from lib.memory_router import (
            ROUTER_ENV_VAR,
            STRATEGY_LLM,
            resolve_strategy,
        )
        monkeypatch.setenv(ROUTER_ENV_VAR, "llm")
        assert resolve_strategy() == STRATEGY_LLM

    def test_unknown_strategy_falls_back(self, monkeypatch):
        from lib.memory_router import (
            DEFAULT_STRATEGY,
            ROUTER_ENV_VAR,
            resolve_strategy,
        )
        monkeypatch.setenv(ROUTER_ENV_VAR, "nonsense-strategy")
        assert resolve_strategy() == DEFAULT_STRATEGY

    def test_explicit_arg_wins(self, monkeypatch):
        from lib.memory_router import resolve_strategy
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_ROUTER", "token_overlap")
        assert resolve_strategy("llm") == "llm"


class TestCreateRouter:
    def test_default_returns_token_overlap(self, monkeypatch):
        # Default strategy is token_overlap — returned directly,
        # without consulting the model client.
        import lib.memory_router as mr
        monkeypatch.delenv(mr.ROUTER_ENV_VAR, raising=False)
        assert isinstance(mr.create_router(), mr.TokenOverlapRouter)

    def test_llm_returns_llm_router_when_client_available(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "multiplai_core.model_client.detect_client_type", lambda: "AgentSDKClient"
        )
        assert isinstance(mr.create_router("llm"), mr.LLMRouter)

    def test_llm_degrades_to_token_overlap_without_client(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "multiplai_core.model_client.detect_client_type",
            lambda: "none (no SDK or API key)",
        )
        assert isinstance(mr.create_router("llm"), mr.TokenOverlapRouter)

    def test_embeddings_not_implemented(self):
        from lib.memory_router import create_router
        with pytest.raises(NotImplementedError):
            create_router("embeddings")


# ---------------------------------------------------------------------------
# TokenOverlapRouter
# ---------------------------------------------------------------------------


class TestTokenOverlapRouter:
    """Single-corpus semantics, exercised through the canonical
    ``select_multi`` entry point (the legacy ``select`` is gone)."""

    def _pick(self, prompt, catalog, **kw):
        from lib.memory_router import TokenOverlapRouter
        return TokenOverlapRouter().select_multi(
            prompt, None, {"memory": catalog, "skills": [], "resources": []}, **kw
        )["memory"]

    def _catalog(self) -> list[dict]:
        return [
            {
                "source": "writing.md",
                "summary": "voice guide for blog posts",
                "intent_domains": ["writing a blog post", "long-form content"],
                "anti_domains": ["debugging python"],
            },
            {
                "source": "python.md",
                "summary": "python patterns",
                "intent_domains": ["debugging python code", "async patterns"],
                "anti_domains": [],
            },
            {
                "source": "unrelated.md",
                "summary": "cooking notes",
                "intent_domains": ["cooking dinner"],
                "anti_domains": [],
            },
        ]

    def test_empty_prompt_returns_empty(self):
        assert self._pick("", self._catalog()) == []

    def test_empty_catalog_returns_empty(self):
        assert self._pick("debug async code", []) == []

    def test_matches_by_intent_domain(self):
        picks = self._pick("I need help debugging python async code", self._catalog())
        assert "python.md" in picks
        assert "unrelated.md" not in picks

    def test_anti_domain_drops_match(self):
        """File with matching intent_domain is dropped if anti_domain also matches."""
        # Prompt matches writing.md's intent ("blog") AND its anti ("debugging")
        picks = self._pick(
            "writing a blog post about debugging python", self._catalog()
        )
        assert "writing.md" not in picks

    def test_anti_domain_sharing_domain_vocab_does_not_self_exclude(self):
        """An anti_domain that reuses the entry's own positive words must
        not hard-exclude it on those shared tokens.

        Regression: anti phrases routinely restate the topic to negate a
        sub-case (e.g. "...inspection unrelated to memory routing"). A
        naive bag-of-words OR match excluded the entry on "memory" /
        "routing" — the very words that make it relevant.
        """
        catalog = [
            {
                "source": "router-audit.md",
                "intent_domains": [
                    "auditing retrieval routing quality",
                    "identifying false negatives in context routing",
                ],
                "anti_domains": [
                    "general log inspection unrelated to memory routing",
                ],
            },
        ]
        picks = self._pick(
            "audit the retrieval routing quality and false negatives", catalog
        )
        assert "router-audit.md" in picks

    def test_sorts_by_overlap_count(self):
        catalog = [
            {"source": "a.md", "intent_domains": ["debugging python code"]},
            {"source": "b.md", "intent_domains": ["debugging python async code patterns"]},
        ]
        picks = self._pick("debugging python async code patterns", catalog)
        # b.md matches more tokens, so it ranks first when both survive
        assert picks[0] == "b.md"

    def test_respects_max_files(self):
        catalog = [
            {"source": f"f{i}.md", "intent_domains": ["python code patterns"]}
            for i in range(20)
        ]
        picks = self._pick("python code patterns", catalog, max_files_per_corpus=3)
        assert len(picks) == 3


# ---------------------------------------------------------------------------
# LLMRouter
# ---------------------------------------------------------------------------


class TestLLMRouter:
    """Single-corpus LLM semantics via ``select_multi``."""

    def _pick(self, prompt, catalog):
        from lib.memory_router import LLMRouter
        return LLMRouter().select_multi(
            prompt, None, {"memory": catalog, "skills": [], "resources": []}
        )["memory"]

    def _catalog(self) -> list[dict]:
        return [
            {"source": "writing.md", "summary": "voice guide",
             "intent_domains": ["writing"]},
            {"source": "python.md", "summary": "py patterns",
             "intent_domains": ["python code"]},
        ]

    def _client(self, content):
        mock_response = MagicMock()
        mock_response.content = content
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client(**kwargs):
            return mock_client

        return _fake_create_client

    def test_empty_prompt_returns_empty(self):
        assert self._pick("", self._catalog()) == []

    def test_empty_catalog_returns_empty(self):
        assert self._pick("prompt", []) == []

    def test_select_uses_model_client(self):
        """Successful LLM response returns the parsed filename list (filtered to known)."""
        client = self._client('{"memory": ["python.md"], "skills": [], "resources": []}')
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("help me debug python", self._catalog())
        assert picks == ["python.md"]

    def test_filters_unknown_filenames(self):
        """LLM-hallucinated filenames not in the catalog are dropped."""
        client = self._client(
            '{"memory": ["python.md", "hallucinated.md"], "skills": [], "resources": []}'
        )
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("prompt", self._catalog())
        assert picks == ["python.md"]

    def test_handles_fenced_json(self):
        client = self._client(
            '```json\n{"memory": ["python.md"], "skills": [], "resources": []}\n```'
        )
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("prompt", self._catalog())
        assert picks == ["python.md"]

    def test_malformed_response_degrades_to_token_overlap(self):
        """A reply that isn't JSON is a failure, not an abstention.

        This previously asserted ``== []`` and passed for the wrong reason: the
        prompt it used ("prompt") matches nothing offline either, so a silent
        empty and a working fallback were indistinguishable. The prompt here is
        one token_overlap can answer, which is what makes the assertion mean
        something.
        """
        client = self._client("not even close to JSON")
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("help me debug python code", self._catalog())
        assert picks == ["python.md"]

    def test_non_object_json_degrades_to_token_overlap(self):
        """A well-formed JSON *list* is still not an answer to the question."""
        client = self._client('["python.md"]')
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("help me debug python code", self._catalog())
        assert picks == ["python.md"]

    def test_well_formed_empty_reply_is_a_real_abstention(self):
        """The other half of the contract: empty-but-valid must stay empty.

        Without this, "malformed degrades" could be implemented by degrading on
        any empty result, which would destroy the abstention the LLM router is
        actually better at than token_overlap.
        """
        client = self._client('{"memory": [], "skills": [], "resources": []}')
        with patch("multiplai_core.model_client.create_client", client):
            picks = self._pick("help me debug python code", self._catalog())
        assert picks == []

    def test_client_exception_degrades_to_token_overlap(self):
        """A raising client is a failure, so the offline router answers instead."""
        async def _failing_client():
            raise RuntimeError("no backend configured")

        # A prompt token_overlap *can* match, so "empty" cannot be mistaken for
        # "the fallback ran and found nothing".
        with patch("multiplai_core.model_client.create_client", _failing_client):
            picks = self._pick("help me debug python code", self._catalog())
        assert picks == ["python.md"]


# ---------------------------------------------------------------------------
# Failure is not abstention
# ---------------------------------------------------------------------------


class TestLLMRouterDegradesRatherThanSilencing:
    """A failed model call must answer with token_overlap, never with nothing.

    The context manager treats an empty memory pick from a router that *ran*
    as a deliberate abstention and suppresses its own recency net. So any
    failure that returned empty produced a prompt with no memory at all —
    measured on 5 real prompts, every one of which returned 0 files, 0 bytes
    under load. These tests pin the distinction between the two cases.
    """

    def _catalog(self) -> list[dict]:
        return [
            {"source": "writing.md", "summary": "voice guide",
             "intent_domains": ["writing"]},
            {"source": "python.md", "summary": "py patterns",
             "intent_domains": ["python code"]},
        ]

    def _router(self, timeout_seconds=0.01):
        from lib.memory_router import LLMRouter
        return LLMRouter(timeout_seconds=timeout_seconds)

    def _select(self, router, prompt):
        return router.select_multi(
            prompt, None,
            {"memory": self._catalog(), "skills": [], "resources": []},
        )["memory"]

    def test_timeout_returns_token_overlap_picks_not_empty(self):
        """The measured failure: a slow model must not cost the turn's memory."""
        async def _slow_client(**kwargs):
            mock_client = MagicMock()

            async def _never(**_):
                await asyncio.sleep(5)

            mock_client.query = _never
            return mock_client

        with patch("multiplai_core.model_client.create_client", _slow_client):
            picks = self._select(self._router(), "help me debug python code")
        assert picks == ["python.md"], (
            "a timed-out router must answer with the offline ranking; "
            "returning [] here is read downstream as abstention and injects nothing"
        )

    def test_timeout_raises_rather_than_returning_empty(self):
        """The mechanism, pinned directly: the async path signals failure."""
        from lib.memory_router import LLMRouter, RouterCallFailed

        async def _slow_client(**kwargs):
            mock_client = MagicMock()

            async def _never(**_):
                await asyncio.sleep(5)

            mock_client.query = _never
            return mock_client

        router = LLMRouter(timeout_seconds=0.01)
        with patch("multiplai_core.model_client.create_client", _slow_client):
            with pytest.raises(RouterCallFailed):
                asyncio.run(router._bounded_select_multi(
                    "prompt", None,
                    {"memory": self._catalog(), "skills": [], "resources": []},
                    {"memory": {"python.md", "writing.md"},
                     "skills": set(), "resources": set()},
                ))

    def test_stalled_client_creation_is_also_bounded(self):
        """M5: the budget covers create_client, not only the query.

        Client creation spawns the CLI subprocess; when THAT stalled, the old
        inner wait_for never started counting, so the hook ran to the 30s
        harness kill — no RouterCallFailed, no degrade, no fallback."""
        from lib.memory_router import LLMRouter, RouterCallFailed

        async def _stalled_create(**kwargs):
            await asyncio.sleep(5)

        router = LLMRouter(timeout_seconds=0.01)
        with patch("multiplai_core.model_client.create_client", _stalled_create), \
                pytest.raises(RouterCallFailed):
            asyncio.run(router._bounded_select_multi(
                "prompt", None,
                {"memory": self._catalog(), "skills": [], "resources": []},
                {"memory": {"python.md", "writing.md"},
                 "skills": set(), "resources": set()},
            ))

    def test_model_that_ran_and_chose_nothing_still_abstains(self):
        """Abstention is preserved — the fallback must not fire on a real answer."""
        mock_response = MagicMock()
        mock_response.content = '{"memory": [], "skills": [], "resources": []}'
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _create(**kwargs):
            return mock_client

        with patch("multiplai_core.model_client.create_client", _create):
            picks = self._select(self._router(timeout_seconds=30),
                                 "help me debug python code")
        assert picks == [], (
            "a model that ran and picked nothing is a deliberate NONE; "
            "degrading here would re-inject on every abstention"
        )

    def test_empty_prompt_is_not_a_failure(self):
        assert self._select(self._router(), "") == []

    def test_fallback_inherits_configured_keep_ratio(self):
        """create_router must hand the fallback the user's keep_ratio."""
        from lib.memory_router import create_router
        with patch("multiplai_core.model_client.detect_client_type",
                   return_value="agent_sdk"):
            router = create_router("llm", keep_ratio=0.42)
        assert router._fallback.keep_ratio == 0.42


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_token_overlap_router_is_corpus_router(self):
        from lib.memory_router import CorpusRouter, TokenOverlapRouter
        assert isinstance(TokenOverlapRouter(), CorpusRouter)

    def test_llm_router_is_corpus_router(self):
        from lib.memory_router import CorpusRouter, LLMRouter
        assert isinstance(LLMRouter(), CorpusRouter)


# ---------------------------------------------------------------------------
# TokenOverlapRouter.select_multi (multi-corpus)
# ---------------------------------------------------------------------------


class TestTokenOverlapMultiCorpus:
    def _corpora(self) -> dict[str, list[dict]]:
        return {
            "memory": [
                {"source": "writing.md", "intent_domains": ["writing a blog post"]},
                {"source": "python.md", "intent_domains": ["debugging python code"]},
            ],
            "skills": [
                {"name": "writing", "intent_domains": ["writing a blog post"]},
                {"name": "code-review", "intent_domains": ["reviewing pull requests"]},
            ],
            "resources": [
                {"source": "voice-ai.md", "intent_domains": ["voice AI frameworks"]},
            ],
        }

    def test_returns_dict_with_three_corpora(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "writing a blog post", None, self._corpora()
        )
        assert set(result.keys()) == {"memory", "skills", "resources"}

    def test_empty_prompt_returns_all_empty(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi("", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_routes_to_each_corpus_independently(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "writing a blog post", None, self._corpora()
        )
        assert "writing.md" in result["memory"]
        assert "writing" in result["skills"]
        assert result["resources"] == []  # no match for "writing"

    def test_resources_corpus_matched_by_intent(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "researching voice AI frameworks", None, self._corpora()
        )
        assert "voice-ai.md" in result["resources"]

    def test_last_response_supplements_tokens(self):
        """Last-response tokens combine with prompt tokens for matching."""
        from lib.memory_router import TokenOverlapRouter
        # Prompt alone has zero overlap with python intent
        result = TokenOverlapRouter().select_multi(
            "what next?",
            "We were just debugging python async code patterns",
            self._corpora(),
        )
        assert "python.md" in result["memory"]

    def test_missing_corpus_treated_as_empty(self):
        from lib.memory_router import TokenOverlapRouter
        # Only memory corpus provided
        # intent_domains rich enough to clear the relevance floor —
        # the mechanism under test is "absent corpora → []", not the
        # (removed) "any single-token overlap → returned" behavior.
        result = TokenOverlapRouter().select_multi(
            "writing a blog post",
            None,
            {"memory": [{"source": "x.md",
                         "intent_domains": ["writing a blog post"]}]},
        )
        assert result["skills"] == []
        assert result["resources"] == []
        assert "x.md" in result["memory"]

    def test_max_files_per_corpus_caps_each_independently(self):
        from lib.memory_router import TokenOverlapRouter
        many = [
            {"source": f"m{i}.md", "intent_domains": ["python code"]}
            for i in range(20)
        ]
        corpora = {"memory": many, "skills": [], "resources": []}
        result = TokenOverlapRouter().select_multi(
            "python code", None, corpora, max_files_per_corpus=4
        )
        assert len(result["memory"]) == 4


# ---------------------------------------------------------------------------
# Routing policy (NONE floor, continuation guard, relative cutoff) —
# select_multi only; select() stays pure rank+cap.
# ---------------------------------------------------------------------------


class TestRoutingPolicy:
    def _rich(self) -> list[dict]:
        # One strongly-relevant entry + several weak ones.
        return [
            {"source": "finances.md",
             "intent_domains": ["italian taxes and FBAR filing"],
             "keywords": ["FBAR", "backdoor Roth", "Form 8606"]},
            {"source": "python.md", "intent_domains": ["debugging python"]},
            {"source": "life.md", "intent_domains": ["personal life logistics"]},
        ]

    def test_none_floor_returns_empty_when_no_real_match(self):
        from lib.memory_router import TokenOverlapRouter
        # Prompt shares at most a faint token with any entry → below
        # MIN_SIGNAL → nothing injected (the abstention the old
        # always-top-N behavior could never do).
        result = TokenOverlapRouter().select_multi(
            "fix the CSS bug on line 42", None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        assert result["memory"] == []

    @pytest.mark.parametrize("phrase", ["yes", "go ahead", "do it", "thanks", "continue"])
    def test_continuation_guard_returns_all_empty(self, phrase):
        from lib.memory_router import TokenOverlapRouter
        r = TokenOverlapRouter()
        result = r.select_multi(
            phrase,
            "We were deep in italian taxes and FBAR filing details",
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        assert result == {"memory": [], "skills": [], "resources": []}
        assert r.last_scores["memory"].get("continuation") is True

    def test_relative_cutoff_isolates_the_strong_match(self):
        from lib.memory_router import TokenOverlapRouter
        result = TokenOverlapRouter().select_multi(
            "help with my FBAR and backdoor Roth for italian taxes",
            None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        # The strong entry is picked; weak unrelated ones are cut off,
        # so the result does not saturate to the whole catalog.
        assert "finances.md" in result["memory"]
        assert len(result["memory"]) < 3

    def _graded_catalog(self) -> list[dict]:
        # Distinct domain-token overlap per file → a graded ranking with a
        # weak tail (the production pathology: one strong match + weaker
        # same-topic files). Scores ~ a.md 7.0 > b.md 3.6 > c.md 1.0.
        return [
            {"source": "a.md", "intent_domains": ["alpha beta gamma delta epsilon"]},
            {"source": "b.md", "intent_domains": ["alpha beta gamma"]},
            {"source": "c.md", "intent_domains": ["alpha"]},
        ]

    def test_apply_policy_ratio_trims_the_tail(self):
        # Direct unit test of the lever: a higher ratio raises the floor.
        from lib.memory_router import _apply_policy
        scored = [(10.0, "a"), (6.0, "b"), (3.0, "c"), (2.5, "d"), (2.1, "e")]
        # 0.05×10 = 0.5 < MIN_SIGNAL, so floor = 2.0 → everything ≥ 2.0.
        assert _apply_policy(scored, 10, 0.05) == ["a", "b", "c", "d", "e"]
        # 0.60×10 = 6.0 → only a, b clear it.
        assert _apply_policy(scored, 10, 0.60) == ["a", "b"]

    def test_keep_ratio_tightens_the_pick(self):
        from lib.memory_router import TokenOverlapRouter
        prompt = "alpha beta gamma delta epsilon"
        cat = self._graded_catalog()
        loose = TokenOverlapRouter(keep_ratio=0.05).select_multi(
            prompt, None, {"memory": cat, "skills": [], "resources": []})
        strict = TokenOverlapRouter(keep_ratio=0.60).select_multi(
            prompt, None, {"memory": cat, "skills": [], "resources": []})
        # A higher ratio can only trim the tail, never add files, and the
        # top match survives both.
        assert len(strict["memory"]) < len(loose["memory"])
        assert set(strict["memory"]) <= set(loose["memory"])
        assert strict["memory"][0] == loose["memory"][0]

    def test_create_router_threads_keep_ratio(self):
        from lib.memory_router import create_router, DEFAULT_KEEP_RATIO
        assert create_router("token_overlap").keep_ratio == DEFAULT_KEEP_RATIO
        assert create_router("token_overlap", keep_ratio=0.5).keep_ratio == 0.5

    def test_diagnostics_exposed_for_logging(self):
        from lib.memory_router import TokenOverlapRouter
        r = TokenOverlapRouter()
        r.select_multi(
            "FBAR and backdoor Roth", None,
            {"memory": self._rich(), "skills": [], "resources": []},
        )
        mem = r.last_scores["memory"]
        assert set(mem) >= {"scored", "cap", "n_candidates", "n_picked", "capped"}
        assert mem["scored"] and mem["scored"][0][0] > 0

    # --- glossary-keyword regression (the career-history.md bug) ---
    # A bio file whose catalog keywords are a generic tech glossary
    # (Python/Docker/AWS…) must NOT be pulled into unrelated technical
    # prompts on keywords alone — keywords are a capped boost, only
    # intent_domains can clear MIN_SIGNAL.
    def _glossary_catalog(self) -> list[dict]:
        return [
            {"source": "career.md",
             "intent_domains": ["writing or tailoring a resume or CV"],
             "keywords": ["Python", "Docker", "AWS", "Kubernetes",
                          "FastAPI", "DevOps", "agentic AI"]},
            {"source": "python.md",
             "intent_domains": ["debugging python code"]},
        ]

    def test_glossary_keywords_alone_do_not_pick_the_file(self):
        from lib.memory_router import TokenOverlapRouter
        # Pure coding prompt: overlaps career.md's generic keywords
        # (python, docker) but none of its resume intent_domain.
        result = TokenOverlapRouter().select_multi(
            "debug my python docker container", None,
            {"memory": self._glossary_catalog(), "skills": [], "resources": []},
        )
        assert "career.md" not in result["memory"]

    def test_intent_domain_still_picks_the_file(self):
        from lib.memory_router import TokenOverlapRouter
        # The legitimate route: a prompt that hits the resume domain.
        result = TokenOverlapRouter().select_multi(
            "help writing and tailoring my resume", None,
            {"memory": self._glossary_catalog(), "skills": [], "resources": []},
        )
        assert "career.md" in result["memory"]


# ---------------------------------------------------------------------------
# Match-breadth eligibility gate (single-token NONE-floor fix) + IDF cap
# ---------------------------------------------------------------------------


class TestSingleTokenMatchFloor:
    """A single incidental domain-token match must not clear the NONE floor.

    Root cause: smoothed IDF over a small catalog gives a df=1 term
    ``log((N+1)/2)+1`` (≈3.74 at N=30), so ONE generic token ("search",
    "browser", "skill") out-scored MIN_SIGNAL alone and injected an
    unrelated file. Eligibility now requires ≥ MIN_DOMAIN_MATCHES
    distinct domain tokens, or a verbatim multi-word domain phrase;
    single-token entries still rank in diagnostics but never inject.
    """

    def _catalog(self) -> list[dict]:
        # ≥5 entries so a df=1 term's smoothed IDF (log(6/2)+1 ≈ 2.10)
        # clears MIN_SIGNAL on its own — the failure being reproduced
        # needs a lone token's score to be sufficient.
        return [
            {"source": "jobs.md", "intent_domains": ["job search strategies"]},
            {"source": "ooo.md", "intent_domains": ["out of office"]},
            {"source": "py.md", "intent_domains": ["debugging python segfaults"]},
            {"source": "cook.md", "intent_domains": ["cooking dinner recipes"]},
            {"source": "infra.md", "intent_domains": ["deploying cloud infrastructure"]},
        ]

    def _route(self, prompt: str, catalog: list[dict] | None = None) -> list[str]:
        from lib.memory_router import TokenOverlapRouter
        return TokenOverlapRouter().select_multi(
            prompt, None,
            {"memory": catalog or self._catalog(), "skills": [], "resources": []},
        )["memory"]

    def test_single_domain_token_match_does_not_pick(self):
        # "search" is jobs.md's only overlapping token — an incidental
        # hit on a travel prompt. Its lone-token score clears MIN_SIGNAL
        # but the entry must not inject.
        assert "jobs.md" not in self._route("search for train tickets to florence")

    def test_single_token_plus_keyword_boost_does_not_pick(self):
        # Keyword boosts must not rescue an ineligible single-token
        # domain match — breadth, not score, gates the floor.
        catalog = self._catalog()
        catalog[0] = {**catalog[0], "keywords": ["linkedin"]}
        picks = self._route("search linkedin for train tickets", catalog)
        assert "jobs.md" not in picks

    def test_two_domain_tokens_still_pick(self):
        # {debugging, python} — two distinct domain tokens → eligible.
        assert "py.md" in self._route("debugging a python segfault")

    def test_verbatim_multiword_phrase_still_picks(self):
        # "out of office" matches only ONE scoring token ("office" —
        # "out"/"of" are stopword/short) but appears verbatim in the
        # prompt: unmistakably deliberate → eligible.
        assert "ooo.md" in self._route("configure my out of office autoreply")

    def test_all_single_token_ties_abstain(self):
        # The live none-travel failure in miniature: three entries each
        # matching one distinct generic token → full abstention.
        catalog = [
            {"source": "career.md", "intent_domains": ["job search strategies"]},
            {"source": "tools.md", "intent_domains": ["browser automation workflows"]},
            {"source": "sales.md", "intent_domains": ["skill assessment pipelines"]},
            {"source": "py.md", "intent_domains": ["debugging python segfaults"]},
            {"source": "cook.md", "intent_domains": ["cooking dinner recipes"]},
        ]
        picks = self._route(
            "use the host browser skill to search for train tickets", catalog
        )
        assert picks == []

    def test_apply_policy_eligibility_gate(self):
        from lib.memory_router import _apply_policy
        scored = [(5.0, "inelig"), (4.0, "elig")]
        # Gate: only eligible entries can be picked; floor/threshold
        # anchor on the best ELIGIBLE score.
        assert _apply_policy(scored, 10, 0.30, eligible={"elig"}) == ["elig"]
        assert _apply_policy(scored, 10, 0.30, eligible=set()) == []
        # None (default) = all eligible — select()'s pure rank+cap
        # contract and direct callers are unchanged.
        assert _apply_policy(scored, 10, 0.30) == ["inelig", "elig"]


class TestIdfCap:
    def test_lone_df1_term_capped_at_max_term_idf(self):
        from lib.memory_router import MAX_TERM_IDF, _idf_map
        # 30-entry catalog, term in exactly one entry: uncapped smoothed
        # IDF is log(31/2)+1 ≈ 3.74 — the inflation that let one token
        # beat MIN_SIGNAL. The cap bounds it.
        sets = [{"common", "rare"} if i == 0 else {"common"} for i in range(30)]
        idf = _idf_map(sets)
        assert idf["rare"] == pytest.approx(MAX_TERM_IDF)
        # A universal term still floors at ~1.0 (unchanged smoothing).
        assert idf["common"] == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# LLMRouter.select_multi (multi-corpus, single LLM call)
# ---------------------------------------------------------------------------


class TestLLMRouterMultiCorpus:
    def _corpora(self) -> dict[str, list[dict]]:
        return {
            "memory": [
                {"source": "voice.md", "intent_domains": ["writing"]},
                {"source": "py.md", "intent_domains": ["python"]},
            ],
            "skills": [
                {"name": "writing", "intent_domains": ["writing"]},
                {"name": "code-review", "intent_domains": ["review"]},
            ],
            "resources": [
                {"source": "ai/voice-ai.md", "intent_domains": ["voice AI"]},
            ],
        }

    def _make_mock_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.content = response_text
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create(**kwargs):
            return mock_client

        return _fake_create, mock_client

    def test_single_call_returns_three_corpus_dict(self):
        """A successful LLM response with all three keys is parsed correctly."""
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client(
            '{"memory": ["voice.md"], "skills": ["writing"], "resources": ["ai/voice-ai.md"]}'
        )
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "help me write a blog post", None, self._corpora()
            )
        assert result == {
            "memory": ["voice.md"],
            "skills": ["writing"],
            "resources": ["ai/voice-ai.md"],
        }
        # Single LLM call regardless of corpus count
        mock_client.query.assert_called_once()

    def test_empty_prompt_returns_all_empty_no_call(self):
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client('{}')
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}
        mock_client.query.assert_not_called()

    def test_filters_hallucinated_per_corpus(self):
        """LLM-hallucinated names not in their corpus are dropped."""
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md", "fake.md"], "skills": ["writing", "no-such"], "resources": []}'
        )
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result["memory"] == ["voice.md"]
        assert result["skills"] == ["writing"]

    def test_section_refs_pass_validation(self):
        """Entries like 'voice.md#Section' validate by stripping the fragment."""
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md#Voice Tone"], "skills": [], "resources": []}'
        )
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result["memory"] == ["voice.md#Voice Tone"]

    def test_malformed_response_returns_all_empty(self):
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client("not json at all")
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_query_exception_returns_all_empty(self):
        from lib.memory_router import LLMRouter

        async def _failing_client():
            raise RuntimeError("no backend")

        with patch("multiplai_core.model_client.create_client", _failing_client):
            result = LLMRouter().select_multi("prompt", None, self._corpora())
        assert result == {"memory": [], "skills": [], "resources": []}

    def test_all_empty_corpora_no_call(self):
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client('{}')
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "prompt",
                None,
                {"memory": [], "skills": [], "resources": []},
            )
        assert result == {"memory": [], "skills": [], "resources": []}
        mock_client.query.assert_not_called()

    def test_last_response_included_in_user_message(self):
        """When last_response is provided, it appears in the LLM input."""
        from lib.memory_router import LLMRouter

        fake_create, mock_client = self._make_mock_client(
            '{"memory": [], "skills": [], "resources": []}'
        )
        with patch("multiplai_core.model_client.create_client", fake_create):
            LLMRouter().select_multi(
                "are these costs ok?",
                "I just showed you the API pricing breakdown.",
                self._corpora(),
            )
        call_args = mock_client.query.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "LAST ASSISTANT RESPONSE" in user_msg
        assert "API pricing" in user_msg

    def test_max_files_per_corpus_caps_picks(self):
        from lib.memory_router import LLMRouter

        fake_create, _ = self._make_mock_client(
            '{"memory": ["voice.md", "py.md"], "skills": [], "resources": []}'
        )
        with patch("multiplai_core.model_client.create_client", fake_create):
            result = LLMRouter().select_multi(
                "prompt", None, self._corpora(), max_files_per_corpus=1
            )
        assert len(result["memory"]) == 1


# ---------------------------------------------------------------------------
# Thinking: the latency lever
# ---------------------------------------------------------------------------


class TestRouterThinking:
    """`thinking={"type":"disabled"}` is what makes llm routing fit a hook.

    Measured 2026-08-09: 18.4s -> 2.9s on a cold call. These tests pin the
    default (off), the opt-out, and — the one that matters operationally — that
    the keyword is *omitted* rather than passed as None when the resolved core
    cannot accept it.
    """

    def _client(self, content='{"memory": [], "skills": [], "resources": []}'):
        mock_response = MagicMock()
        mock_response.content = content
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client(**kwargs):
            return mock_client

        return _fake_create_client, mock_client

    def _corpora(self):
        return {
            "memory": [{"source": "python.md", "summary": "py",
                        "intent_domains": ["python code"]}],
            "skills": [],
            "resources": [],
        }

    def test_resolve_defaults_to_disabled(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.delenv(mr.ROUTER_THINKING_ENV_VAR, raising=False)
        assert mr.resolve_router_thinking() == {"type": "disabled"}

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_opt_back_in_returns_none(self, monkeypatch, value):
        """None means "send no thinking config", i.e. the model's own default.

        Read through the environment rather than a `raw=` argument: the value
        is parsed by core's `option_bool`, the same reader every other boolean
        option here uses, and a bypass that re-implemented its truth table is
        the duplication this option no longer carries.
        """
        import lib.memory_router as mr
        monkeypatch.setenv(mr.ROUTER_THINKING_ENV_VAR, value)
        assert mr.resolve_router_thinking() is None

    def test_unrecognised_value_stays_disabled(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setenv(mr.ROUTER_THINKING_ENV_VAR, "maybe")
        assert mr.resolve_router_thinking() == {"type": "disabled"}

    def test_thinking_forwarded_to_client_when_core_supports_it(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(mr, "core_supports_thinking", lambda: True)
        fake_create, mock_client = self._client()
        with patch("multiplai_core.model_client.create_client", fake_create):
            mr.LLMRouter().select_multi("debug python code", None, self._corpora())
        assert mock_client.query.call_args.kwargs["thinking"] == {"type": "disabled"}

    def test_keyword_omitted_entirely_when_core_too_old(self, monkeypatch):
        """The failure this guards is subtle and was live for the whole window.

        An older core rejects the *name* `thinking`, whatever its value. The
        resulting TypeError is swallowed by _select_async_multi's `except
        Exception` degrade path, so every prompt would quietly answer with
        token_overlap while the log claimed the llm router was configured.
        Passing `thinking=None` would not help — the keyword must not be sent.
        """
        import lib.memory_router as mr
        monkeypatch.setattr(mr, "core_supports_thinking", lambda: False)
        fake_create, mock_client = self._client()
        with patch("multiplai_core.model_client.create_client", fake_create):
            mr.LLMRouter().select_multi("debug python code", None, self._corpora())
        assert "thinking" not in mock_client.query.call_args.kwargs

    def test_opt_out_also_omits_the_keyword(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(mr, "core_supports_thinking", lambda: True)
        monkeypatch.setenv(mr.ROUTER_THINKING_ENV_VAR, "true")
        fake_create, mock_client = self._client()
        with patch("multiplai_core.model_client.create_client", fake_create):
            mr.LLMRouter().select_multi("debug python code", None, self._corpora())
        assert "thinking" not in mock_client.query.call_args.kwargs

    def test_core_probe_agrees_with_the_resolved_signature(self, monkeypatch):
        """The probe reports the resolved dependencies, not a hardcoded guess.

        This tracks whatever the lockfile resolves — it does not assert those
        are new enough, and cannot: both sides read the same signature. What it
        catches is the probe drifting from reality (wrong module, wrong
        attribute, exception swallowed into a wrong default), which would make
        every other test in this class assert against a fiction.

        The router no longer keeps its own copy of this probe or its own cache;
        the name is re-exported from lib.thinking, which is also what the
        by-name monkeypatches above are patching.
        """
        import inspect
        import lib.memory_router as mr
        import lib.thinking as th
        from multiplai_core.model_client import ModelClient
        expected = "thinking" in inspect.signature(ModelClient.query).parameters
        monkeypatch.setattr(th, "_SUPPORT_CACHE", {})  # bypass the process cache
        assert mr.core_supports_thinking() is expected


# ---------------------------------------------------------------------------
# Hybrid: LLM picks, offline gates filter
# ---------------------------------------------------------------------------


class TestHybridRouter:
    """The gates can only remove, so every test here is about what survives."""

    def _client(self, content):
        mock_response = MagicMock()
        mock_response.content = content
        mock_client = MagicMock()
        mock_client.query = AsyncMock(return_value=mock_response)

        async def _fake_create_client(**kwargs):
            return mock_client

        return _fake_create_client

    def _corpora(self):
        return {
            # Two distinct domain tokens, so a prompt naming both clears
            # MIN_DOMAIN_MATCHES.
            "memory": [
                {"source": "python.md", "summary": "py patterns",
                 "intent_domains": ["python code debugging"]},
                {"source": "travel.md", "summary": "trips",
                 "intent_domains": ["flight booking itinerary"]},
            ],
            "skills": [],
            "resources": [],
        }

    def _pick(self, prompt, reply, last_response=None):
        from lib.memory_router import HybridRouter
        with patch("multiplai_core.model_client.create_client", self._client(reply)):
            return HybridRouter().select_multi(
                prompt, last_response, self._corpora()
            )["memory"]

    def test_pick_survives_when_it_clears_the_breadth_gate(self):
        picks = self._pick(
            "help me with python code debugging",
            '{"memory": ["python.md"], "skills": [], "resources": []}',
        )
        assert picks == ["python.md"]

    def test_pick_dropped_when_prompt_shares_no_domain_vocabulary(self):
        """The over-inclusion this exists to fix: 170,940 bytes vs 93,001 on
        turn-0 prompts. The LLM reaches for a file the prompt does not support."""
        picks = self._pick(
            "what time is dinner",
            '{"memory": ["travel.md"], "skills": [], "resources": []}',
        )
        assert picks == []

    def test_single_incidental_token_is_not_enough(self):
        """MIN_DOMAIN_MATCHES is a breadth gate: one word cannot pull a file in."""
        picks = self._pick(
            "is python installed",
            '{"memory": ["python.md"], "skills": [], "resources": []}',
        )
        assert picks == []

    def test_gate_scores_against_last_response_too(self):
        """Passing only the prompt would gate on less context than token_overlap
        itself uses, dropping files the LLM picked *because* of the last turn."""
        picks = self._pick(
            "is that ok?",
            '{"memory": ["python.md"], "skills": [], "resources": []}',
            last_response="here is the python code debugging trace",
        )
        assert picks == ["python.md"]

    def test_anti_domains_veto_is_honoured(self):
        from lib.memory_router import HybridRouter
        corpora = {
            "memory": [{
                "source": "python.md",
                "summary": "py",
                "intent_domains": ["python code debugging"],
                "anti_domains": ["kubernetes cluster"],
            }],
            "skills": [],
            "resources": [],
        }
        reply = '{"memory": ["python.md"], "skills": [], "resources": []}'
        with patch("multiplai_core.model_client.create_client", self._client(reply)):
            picks = HybridRouter().select_multi(
                "python code debugging on my kubernetes cluster", None, corpora
            )["memory"]
        assert picks == []

    def test_section_ref_is_gated_on_its_file(self):
        """P1 picks look like "file.md#Section"; the eligible set is by filename."""
        picks = self._pick(
            "help me with python code debugging",
            '{"memory": ["python.md#Debugging"], "skills": [], "resources": []}',
        )
        assert picks == ["python.md#Debugging"]

    def test_abstention_passes_through_untouched(self):
        picks = self._pick(
            "help me with python code debugging",
            '{"memory": [], "skills": [], "resources": []}',
        )
        assert picks == []

    def test_exposes_diagnostics_so_routing_scores_keeps_logging(self):
        """Switching strategy must not silently turn off the quality log that
        the eval harness and /health both read."""
        from lib.memory_router import HybridRouter
        reply = '{"memory": ["travel.md"], "skills": [], "resources": []}'
        with patch("multiplai_core.model_client.create_client", self._client(reply)):
            router = HybridRouter()
            router.select_multi("what time is dinner", None, self._corpora())
        assert router.last_scores["memory"]["hybrid_dropped"] == ["travel.md"]
        assert router.last_scores["memory"]["n_picked"] == 0

    def test_registered_in_the_factory(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "multiplai_core.model_client.detect_client_type", lambda: "agent-sdk"
        )
        router = mr.create_router("llm_hybrid")
        assert isinstance(router, mr.HybridRouter)
        assert router.name == "llm_hybrid"

    def test_degrades_to_token_overlap_without_a_client(self, monkeypatch):
        import lib.memory_router as mr
        monkeypatch.setattr(
            "multiplai_core.model_client.detect_client_type", lambda: "none"
        )
        router = mr.create_router("llm_hybrid")
        assert isinstance(router, mr.TokenOverlapRouter)

    def test_shares_one_gate_with_the_llm_fallback(self, monkeypatch):
        """A degraded prompt must be gated the same way a normal one is —
        otherwise keep_ratio means two different things in one session."""
        monkeypatch.setattr(
            "multiplai_core.model_client.detect_client_type", lambda: "agent-sdk"
        )
        import lib.memory_router as mr
        router = mr.create_router("llm_hybrid", keep_ratio=0.42)
        assert router._gate is router._llm._fallback
        assert router._gate.keep_ratio == 0.42


class TestLLMRouterReturnsWhenTeardownWedges:
    """The 2026-09-27 00:38 kill: a call that will not cancel.

    The SDK tears down the CLI subprocess when its task is cancelled, and that
    teardown can hang. ``asyncio.wait_for`` waits for the cancellation it
    triggers, and ``asyncio.run`` waits for every leftover task on exit, so a
    wedged teardown held the hook past the 25s router deadline until the
    harness killed it at 30s, with no fallback and no log line. The earlier
    timeout tests used a plain ``asyncio.sleep``, which cancels cleanly, so
    they could not see this.
    """

    WEDGE_S = 4.0

    def _catalog(self) -> list[dict]:
        return [
            {"source": "writing.md", "summary": "voice guide",
             "intent_domains": ["writing"]},
            {"source": "python.md", "summary": "py patterns",
             "intent_domains": ["python code"]},
        ]

    def _wedged_client(self):
        wedge_s = self.WEDGE_S

        async def _create(**kwargs):
            client = MagicMock()

            async def _query(**_):
                # Swallow every cancellation until wedge_s has passed, the way
                # a hung subprocess teardown holds a cancelled task open.
                loop = asyncio.get_running_loop()
                end = loop.time() + wedge_s
                while loop.time() < end:
                    try:
                        await asyncio.sleep(end - loop.time())
                    except asyncio.CancelledError:
                        continue

            client.query = _query
            return client

        return _create

    def test_degrades_within_deadline_plus_grace(self):
        import time

        from lib.memory_router import _LEFTOVER_GRACE_S, LLMRouter

        router = LLMRouter(timeout_seconds=0.2)
        started = time.monotonic()
        with patch("multiplai_core.model_client.create_client", self._wedged_client()):
            picks = router.select_multi(
                "help me debug python code", None,
                {"memory": self._catalog(), "skills": [], "resources": []},
            )["memory"]
        elapsed = time.monotonic() - started

        assert picks == ["python.md"], "a wedged call must still degrade to token_overlap"
        assert elapsed < 0.2 + _LEFTOVER_GRACE_S + 1.0, (
            f"select_multi took {elapsed:.1f}s; it waited on the wedged "
            f"teardown ({self.WEDGE_S}s) instead of returning at the deadline"
        )

    def test_degrade_reason_reaches_the_log(self, caplog):
        import logging

        from lib.memory_router import LLMRouter

        router = LLMRouter(timeout_seconds=0.1)
        with caplog.at_level(logging.WARNING, logger="lib.memory_router"), \
                patch("multiplai_core.model_client.create_client", self._wedged_client()):
            router.select_multi(
                "help me debug python code", None,
                {"memory": self._catalog(), "skills": [], "resources": []},
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any("timed out after 0.1s; degrading to token_overlap" in m for m in messages)
        assert any("still pending after the call" in m for m in messages)

    def test_refuses_to_run_inside_a_running_loop(self):
        from lib.memory_router import _run_without_awaiting_leftovers

        async def _inner():
            async def _noop():
                return 1

            with pytest.raises(RuntimeError, match="running event loop"):
                _run_without_awaiting_leftovers(_noop())
            # The outer loop is still this thread's current loop.
            assert asyncio.get_running_loop() is loop_holder["loop"]

        loop_holder: dict = {}

        async def _main():
            loop_holder["loop"] = asyncio.get_running_loop()
            await _inner()

        asyncio.run(_main())

    def test_hook_process_exits_with_a_wedged_real_subprocess(self, tmp_path):
        """End to end, in a child interpreter: a real subprocess whose wait
        swallows cancellation, and the interpreter must still exit promptly
        after the router degrades.

        The stub child gets its own stdio, as the SDK gives the CLI its own
        pipes. With inherited stdio it would hold this test's capture pipe
        open for its whole sleep, which times the child, not the hook."""
        import subprocess
        import time

        pidfile = tmp_path / "sleeper.pid"
        script = tmp_path / "hook.py"
        script.write_text(
            "import asyncio, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "from unittest.mock import MagicMock, patch\n"
            "from lib.memory_router import LLMRouter\n"
            "async def _create(**kw):\n"
            "    c = MagicMock()\n"
            "    async def _query(**_):\n"
            "        proc = await asyncio.create_subprocess_exec(\n"
            "            'sleep', '30', stdin=asyncio.subprocess.PIPE,\n"
            "            stdout=asyncio.subprocess.DEVNULL,\n"
            "            stderr=asyncio.subprocess.DEVNULL)\n"
            f"        open({str(pidfile)!r}, 'w').write(str(proc.pid))\n"
            "        while True:\n"
            "            try:\n"
            "                await proc.wait()\n"
            "                return\n"
            "            except asyncio.CancelledError:\n"
            "                continue\n"
            "    c.query = _query\n"
            "    return c\n"
            "cat = [{'source': 'python.md', 'summary': 'py', 'intent_domains': ['python code']}]\n"
            "with patch('multiplai_core.model_client.create_client', _create):\n"
            "    picks = LLMRouter(timeout_seconds=0.5).select_multi(\n"
            "        'help me debug python code', None,\n"
            "        {'memory': cat, 'skills': [], 'resources': []})\n"
            "print('PICKS', picks['memory'], flush=True)\n"
        )
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=20,
        )
        elapsed = time.monotonic() - started
        assert "PICKS ['python.md']" in proc.stdout, proc.stderr[-2000:]
        assert elapsed < 8, f"hook process took {elapsed:.1f}s to exit"

        # Not something the router guarantees (see
        # _run_without_awaiting_leftovers): just don't leave the stub's
        # sleeper running after the test.
        with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
            os.kill(int(pidfile.read_text()), 9)
