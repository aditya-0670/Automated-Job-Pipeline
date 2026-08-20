"""Provider layer: JSON parsing, mock behaviour, and token accounting.

Live Gemini calls are marked `integration` and skipped without a key, so the
default test run stays offline and deterministic.
"""

from __future__ import annotations

import os

import pytest

from app.clients.llm import (
    GeminiProvider,
    LLMError,
    LLMResponse,
    MockProvider,
    TokenLedger,
    get_llm,
    parse_json,
)
from app.config import Settings, get_settings

HAS_KEY = bool(os.getenv("GEMINI_API_KEY"))
integration = pytest.mark.skipif(not HAS_KEY, reason="GEMINI_API_KEY not set")


# ── JSON parsing ─────────────────────────────────────────────────────────
def test_parses_plain_json():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parses_fenced_json():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parses_json_with_surrounding_prose():
    assert parse_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


@pytest.mark.parametrize("bad", ["", "   ", "not json at all", "{unclosed"])
def test_rejects_unparseable(bad):
    with pytest.raises(LLMError):
        parse_json(bad)


# ── Token accounting ─────────────────────────────────────────────────────
def test_thinking_tokens_count_toward_total_but_not_output():
    """Reasoning tokens are billed as output but reported separately.

    Conflating them would understate cost; ignoring them would understate it
    more. They are tracked as their own field.
    """
    r = LLMResponse(text="x", input_tokens=100, output_tokens=50, thinking_tokens=200, model="m")
    assert r.output_tokens == 50
    assert r.billed_output_tokens == 250
    assert r.total_tokens == 350


def test_ledger_accumulates_across_steps():
    ledger = TokenLedger()
    ledger.record("refactor", LLMResponse("a", 1000, 500, "m", thinking_tokens=300))
    ledger.record("evaluate", LLMResponse("b", 800, 200, "m", thinking_tokens=100))
    assert ledger.total_input == 1800
    assert ledger.total_output == 1100  # 500+300 + 200+100
    assert ledger.total == 2900
    payload = ledger.to_dict()
    assert payload["calls"] == 2
    assert [e["step"] for e in payload["by_step"]] == ["refactor", "evaluate"]


# ── Mock provider ────────────────────────────────────────────────────────
async def test_mock_returns_evaluator_shaped_json():
    provider = MockProvider()
    payload, response = await provider.complete_json(
        system="You are the Evaluator agent.", user="check this"
    )
    assert payload["passed"] is True
    assert "factual_errors" in payload
    assert response.model == "mock-deterministic"


async def test_mock_echoes_latex_so_compile_step_has_valid_input():
    latex = r"\documentclass{article}\begin{document}Hi\end{document}"
    provider = MockProvider()
    response = await provider.complete(system="Refactor the resume.", user=f"LaTeX:\n{latex}")
    assert r"\documentclass" in response.text
    assert r"\end{document}" in response.text


async def test_mock_is_deterministic():
    provider = MockProvider()
    a = await provider.complete(system="s", user="u")
    b = await provider.complete(system="s", user="u")
    assert a.text == b.text


async def test_mock_reports_nonzero_token_estimate():
    """Accounting must be exercised even offline, or the ledger is untested."""
    response = await MockProvider().complete(system="s" * 400, user="u" * 400)
    assert response.input_tokens > 0
    assert response.total_tokens > 0


# ── Provider resolution ──────────────────────────────────────────────────
def test_falls_back_to_mock_without_credentials(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert isinstance(get_llm(), MockProvider)
    finally:
        get_settings.cache_clear()


def test_llm_configured_requires_a_key():
    assert not Settings(llm_provider="gemini", gemini_api_key="").llm_configured
    assert Settings(llm_provider="gemini", gemini_api_key="k").llm_configured


# ── Live provider (skipped without a key) ────────────────────────────────
@integration
async def test_gemini_completes_and_reports_usage():
    settings = get_settings()
    provider = GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    response = await provider.complete(
        system="Answer in one word.", user="Capital of France?", thinking_budget=128
    )
    assert "paris" in response.text.lower()
    assert response.input_tokens > 0
    assert response.total_tokens >= response.input_tokens
    assert response.finish_reason == "STOP"


@integration
async def test_gemini_json_mode_returns_structured_payload():
    settings = get_settings()
    provider = GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    payload, response = await provider.complete_json(
        system="You return JSON.",
        user='Return {"ok": true} exactly.',
        thinking_budget=128,
    )
    assert payload.get("ok") is True
    assert response.input_tokens > 0
