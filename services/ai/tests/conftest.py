"""Shared test configuration.

**Quota discipline.** The Gemini free tier allows 20 requests per day and 5 per
minute. Merely having a key in `.env` is not consent to spend it, so live-API
tests require an explicit opt-in:

    RUN_LIVE_LLM_TESTS=1 pytest -m live

Without it they skip, even when GEMINI_API_KEY is set. Every other test in the
suite uses MockProvider and makes no network call.
"""

from __future__ import annotations

import os

import pytest

#: Set to "1" to permit tests that spend real API quota.
LIVE_ENABLED = os.getenv("RUN_LIVE_LLM_TESTS") == "1"
HAS_KEY = bool(os.getenv("GEMINI_API_KEY"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: spends real Gemini quota; requires RUN_LIVE_LLM_TESTS=1"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `live` test unless quota spending was explicitly authorised."""
    if LIVE_ENABLED and HAS_KEY:
        return
    reason = (
        "live LLM tests are opt-in: set RUN_LIVE_LLM_TESTS=1 (and GEMINI_API_KEY) "
        "to spend daily quota"
        if not LIVE_ENABLED
        else "GEMINI_API_KEY is not set"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
