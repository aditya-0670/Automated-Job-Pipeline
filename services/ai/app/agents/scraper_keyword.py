"""Node 1 — Scraper & Keyword agent.

Thin by design: the scraping tiers (`clients/scraper.py`) and the four-layer
extraction pipeline (`extraction/`) already exist and are tested independently.
This node's whole job is orchestration, caching, and translating results into
checkpointable state.

Zero LLM tokens are spent here. That is the point of the node, and it is
asserted in the tests rather than left as a claim.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.cache import Cache, NullCache, cache_key
from app.clients.scraper import ScrapeError, scrape_job_posting
from app.extraction.pipeline import extract_keywords
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)

MIN_JOB_TEXT_CHARS = 200


async def scraper_keyword_agent(
    state: ResumeForgeState,
    *,
    cache: Cache | None = None,
) -> dict[str, Any]:
    """Obtain the job description, then extract and rank its keywords.

    Three input paths:
      1. `job_text` already present  -> the user pasted it (Tier 3), skip scraping.
      2. cache hit on `job_url`      -> reuse, no network call.
      3. otherwise                   -> scrape via the tier ladder.

    Returns a partial state update. A scrape failure is returned as an `error`
    rather than raised, so the graph can surface an actionable message instead of
    a stack trace.
    """
    cache = cache or NullCache()
    events: list[dict[str, Any]] = []
    job_text = (state.get("job_text") or "").strip()
    job_url = (state.get("job_url") or "").strip()
    job_metadata: dict[str, str] = dict(state.get("job_metadata") or {})
    scrape_tier = "manual" if job_text else ""

    # ── Acquire the text ──
    if not job_text:
        if not job_url:
            return _failure(
                state,
                "No job posting provided. Paste a job URL or the description text.",
            )

        key = cache_key("scrape", job_url)
        cached = await _safe_cache_get(cache, key)
        if cached:
            job_text = cached.get("text", "")
            job_metadata = cached.get("metadata", {})
            scrape_tier = f"{cached.get('tier', 'unknown')} (cached)"
            logger.info("Scrape cache hit for %s", job_url)
        else:
            events.append(step_event(state, Step.SCRAPING, detail=f"Fetching {job_url}"))
            try:
                result = await scrape_job_posting(job_url)
            except ScrapeError as exc:
                # Tier 3: not an exception the user can act on except by pasting.
                return _failure(state, str(exc), events=events)

            job_text = result.text
            job_metadata = result.metadata
            scrape_tier = result.tier
            await _safe_cache_set(
                cache, key, {"text": job_text, "metadata": job_metadata, "tier": result.tier}
            )

    if len(job_text) < MIN_JOB_TEXT_CHARS:
        return _failure(
            state,
            "The job description is too short to analyse. Paste the full posting text.",
            events=events,
        )

    # ── Extract (layers 1-3; layer 4 is the keyword_review interrupt) ──
    events.append(
        step_event(
            {**state, "events": [*(state.get("events") or []), *events]},
            Step.EXTRACTING,
            detail=f"Scanning {len(job_text):,} characters",
        )
    )
    result = extract_keywords(job_text)

    keywords = [kw.to_dict() for kw in result.keywords]
    logger.info(
        "Extracted %d keywords in %.1fms via tier=%s (0 LLM tokens)",
        len(keywords),
        result.duration_ms,
        scrape_tier,
    )

    return {
        "job_text": job_text,
        "job_metadata": job_metadata,
        "scrape_tier": scrape_tier,
        "keywords": keywords,
        "keywords_by_category": result.by_category(),
        "extraction_stats": result.stats,
        # Re-running this node (via the modify_keywords route) must reopen the
        # confirmation gate, otherwise the user's earlier approval would silently
        # apply to a different keyword set.
        "keywords_confirmed": False,
        "current_step": Step.EXTRACTING.value,
        "error": None,
        "events": events,
    }


async def _safe_cache_get(cache: Cache, key: str) -> dict[str, Any] | None:
    """A cache outage must be indistinguishable from a cache miss.

    `RedisCache` already swallows its own failures, but the node should not
    depend on every implementation being that disciplined -- an optional
    optimisation must never be able to fail the pipeline.
    """
    try:
        return await cache.get(key)
    except Exception:
        logger.warning("Cache read failed; treating as a miss", exc_info=True)
        return None


async def _safe_cache_set(cache: Cache, key: str, value: dict[str, Any]) -> None:
    try:
        await cache.set(key, value)
    except Exception:
        logger.warning("Cache write failed; continuing", exc_info=True)


def _failure(
    state: ResumeForgeState,
    message: str,
    *,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    events = list(events or [])
    events.append(
        step_event(
            {**state, "events": [*(state.get("events") or []), *events]},
            Step.FAILED,
            detail=message,
        )
    )
    logger.warning("Node 1 failed: %s", message)
    return {"error": message, "current_step": Step.FAILED.value, "events": events}
