"""3-tier job posting scraper with graceful degradation.

    Tier 1  HTTP + BeautifulSoup  -- works for most server-rendered postings.
    Tier 2  Playwright (Chromium) -- for JS-rendered pages (LinkedIn, Workday).
    Tier 3  Manual paste          -- surfaced to the user as an actionable error.

Playwright is instantiated lazily. A browser launch costs ~2-3 seconds and
~300MB of RSS; for the majority of postings a plain HTTP GET is sufficient, so
paying that cost on every request would be waste.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger(__name__)

# Enough text to plausibly be a job description. Below this the page is almost
# certainly a login wall, a JS shell, or a CAPTCHA -- escalate a tier.
MIN_USABLE_CHARS = 400

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Chrome/JS-heavy boards that essentially never work over plain HTTP.
_JS_REQUIRED_HOSTS = ("linkedin.com", "workday", "myworkdayjobs.com", "indeed.com", "glassdoor")

_NOISE_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "svg", "form", "iframe")


class ScrapeError(RuntimeError):
    """All tiers exhausted. The caller should fall back to manual paste."""


@dataclass
class ScrapeResult:
    text: str
    tier: Literal["http", "playwright"]
    url: str
    metadata: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "tier": self.tier,
            "url": self.url,
            "metadata": self.metadata,
            "chars": len(self.text),
        }


def _clean(html: str) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()

    metadata: dict[str, str] = {}
    if soup.title and soup.title.string:
        metadata["page_title"] = soup.title.string.strip()[:200]
    for prop, key in (("og:title", "title"), ("og:site_name", "company")):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            metadata[key] = tag["content"].strip()[:200]

    text = soup.get_text(separator="\n")
    # Collapse the ragged whitespace that get_text leaves behind, but keep
    # newlines -- section-heading detection downstream depends on line starts.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line), metadata


async def _scrape_http(url: str) -> ScrapeResult | None:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=settings.scrape_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("Tier 1 (HTTP) failed for %s: %s", url, exc)
        return None

    text, metadata = _clean(response.text)
    if len(text) < MIN_USABLE_CHARS:
        logger.info("Tier 1 returned only %d chars for %s; escalating", len(text), url)
        return None
    return ScrapeResult(text=text, tier="http", url=url, metadata=metadata)


async def _scrape_playwright(url: str) -> ScrapeResult | None:
    settings = get_settings()
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed; Tier 2 unavailable")
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(user_agent=_UA)
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(settings.scrape_timeout_seconds * 1000),
                )
                # Give client-side rendering a moment to populate the DOM.
                await page.wait_for_timeout(1500)
                html = await page.content()
            finally:
                await browser.close()
    except Exception as exc:  # any browser failure is a tier failure, not a crash
        logger.info("Tier 2 (Playwright) failed for %s: %s", url, exc)
        return None

    text, metadata = _clean(html)
    if len(text) < MIN_USABLE_CHARS:
        return None
    return ScrapeResult(text=text, tier="playwright", url=url, metadata=metadata)


async def scrape_job_posting(url: str) -> ScrapeResult:
    """Try each tier in order. Raises ScrapeError if none produce usable text."""
    settings = get_settings()
    needs_js = any(host in url.lower() for host in _JS_REQUIRED_HOSTS)

    tiers = [_scrape_playwright, _scrape_http] if needs_js else [_scrape_http, _scrape_playwright]

    for tier in tiers:
        result = await tier(url)
        if result is not None:
            result.text = result.text[: settings.max_job_text_chars]
            logger.info("Scraped %s via tier=%s (%d chars)", url, result.tier, len(result.text))
            return result

    raise ScrapeError(
        "Could not read this job posting automatically. The page may require a "
        "login or block automated access. Please paste the job description text "
        "directly instead."
    )
