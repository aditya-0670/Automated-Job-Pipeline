"""Part 3: Node 1 — scraping and keyword extraction.

The scraper is stubbed throughout: this covers the node's orchestration
(input paths, caching, failure translation), not the tiers themselves, which
have their own coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import scraper_keyword as node_mod
from app.agents.scraper_keyword import scraper_keyword_agent
from app.clients.cache import MemoryCache, NullCache, cache_key
from app.clients.scraper import ScrapeError, ScrapeResult
from app.graph.state import initial_state
from app.graph.steps import Step

JD = (Path(__file__).parent / "fixtures" / "sample_jd.txt").read_text(encoding="utf-8")


def state(**overrides):
    s = initial_state(
        session_id="s-1",
        user_id="u-1",
        user_latex=r"\documentclass{article}\begin{document}x\end{document}",
        user_profile={},
    )
    s.update(overrides)
    return s


@pytest.fixture
def stub_scraper(monkeypatch):
    """Replace the tier ladder with a counting stub."""
    calls: list[str] = []

    async def fake(url: str) -> ScrapeResult:
        calls.append(url)
        return ScrapeResult(text=JD, tier="http", url=url, metadata={"title": "Senior MTS"})

    monkeypatch.setattr(node_mod, "scrape_job_posting", fake)
    return calls


# ── Input paths ──────────────────────────────────────────────────────────
async def test_scrapes_url_and_extracts(stub_scraper):
    result = await scraper_keyword_agent(state(job_url="https://example.com/job"))
    assert stub_scraper == ["https://example.com/job"]
    assert result["scrape_tier"] == "http"
    assert result["current_step"] == Step.EXTRACTING
    assert result["error"] is None
    terms = {kw["term"] for kw in result["keywords"]}
    assert {"Docker", "Kubernetes", "Java"} <= terms


async def test_pasted_text_skips_scraping_entirely(stub_scraper):
    """Tier 3 fallback: the user pasted the description, so no network call."""
    result = await scraper_keyword_agent(state(job_text=JD))
    assert stub_scraper == []
    assert result["scrape_tier"] == "manual"
    assert result["keywords"]


async def test_no_input_fails_with_actionable_message(stub_scraper):
    result = await scraper_keyword_agent(state())
    assert result["current_step"] == Step.FAILED
    assert "paste" in result["error"].lower()
    assert stub_scraper == []


async def test_scrape_failure_is_returned_not_raised(monkeypatch):
    """The graph must be able to surface a message, not a stack trace."""

    async def boom(url: str):
        raise ScrapeError("Could not read this job posting automatically.")

    monkeypatch.setattr(node_mod, "scrape_job_posting", boom)
    result = await scraper_keyword_agent(state(job_url="https://blocked.example/job"))
    assert result["current_step"] == Step.FAILED
    assert "Could not read" in result["error"]


async def test_too_short_text_is_rejected(stub_scraper):
    result = await scraper_keyword_agent(state(job_text="Java developer wanted."))
    assert result["current_step"] == Step.FAILED
    assert "too short" in result["error"].lower()


# ── Caching ──────────────────────────────────────────────────────────────
async def test_second_run_uses_cache_and_makes_no_network_call(stub_scraper):
    cache = MemoryCache()
    url = "https://example.com/job"

    first = await scraper_keyword_agent(state(job_url=url), cache=cache)
    second = await scraper_keyword_agent(state(job_url=url), cache=cache)

    assert len(stub_scraper) == 1, "the posting should only be fetched once"
    assert second["scrape_tier"] == "http (cached)"
    assert {k["term"] for k in first["keywords"]} == {k["term"] for k in second["keywords"]}


async def test_cache_outage_looks_like_a_miss(stub_scraper):
    """A broken cache must not break the pipeline -- it is an optimisation.

    Both reads and writes raise here, so the node has to survive each.
    """

    class BrokenCache(NullCache):
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, key, value, ttl=0):
            raise RuntimeError("redis down")

    result = await scraper_keyword_agent(
        state(job_url="https://example.com/job"), cache=BrokenCache()
    )
    assert result["error"] is None
    assert result["keywords"], "extraction should still have run"
    assert len(stub_scraper) == 1, "a failed cache read must fall through to scraping"


def test_cache_key_hashes_the_url():
    """URLs carry tracking params and can exceed key length limits."""
    key = cache_key("scrape", "https://example.com/job?utm_source=x")
    assert key.startswith("resumeforge:scrape:")
    assert "example.com" not in key
    assert cache_key("scrape", "a") != cache_key("scrape", "b")


# ── Contract with the rest of the graph ──────────────────────────────────
async def test_spends_zero_llm_tokens(stub_scraper):
    result = await scraper_keyword_agent(state(job_url="https://example.com/job"))
    assert result["extraction_stats"]["llm_tokens_used"] == 0


async def test_reopens_the_confirmation_gate(stub_scraper):
    """Re-running via modify_keywords must not inherit the old approval."""
    result = await scraper_keyword_agent(
        state(job_url="https://example.com/job", keywords_confirmed=True)
    )
    assert result["keywords_confirmed"] is False


async def test_emits_events_for_the_ui(stub_scraper):
    result = await scraper_keyword_agent(state(job_url="https://example.com/job"))
    steps = [e["step"] for e in result["events"]]
    assert "SCRAPING" in steps and "EXTRACTING" in steps
    sequences = [e["sequence"] for e in result["events"]]
    assert sequences == sorted(sequences), "event sequence must increase"


async def test_returns_only_partial_state(stub_scraper):
    """Nodes return partials; returning a whole state would clobber merges."""
    result = await scraper_keyword_agent(state(job_url="https://example.com/job"))
    assert "user_profile" not in result
    assert "session_id" not in result


async def test_categorises_keywords(stub_scraper):
    result = await scraper_keyword_agent(state(job_url="https://example.com/job"))
    by_category = result["keywords_by_category"]
    assert "devops" in by_category
    assert "Kubernetes" in by_category["devops"]
