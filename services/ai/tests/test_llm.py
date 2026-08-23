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

# Gated by tests/conftest.py: also requires RUN_LIVE_LLM_TESTS=1, so a key in
# .env is not on its own enough to spend quota.
HAS_KEY = bool(os.getenv("GEMINI_API_KEY"))
integration = pytest.mark.live


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


# ── Retry directives and model fallback ──────────────────────────────────
def test_parses_structured_retry_delay():
    from app.clients.llm import parse_retry_delay

    assert parse_retry_delay("{'retryDelay': '58s'}") == 58.0


def test_parses_prose_retry_delay():
    from app.clients.llm import parse_retry_delay

    assert parse_retry_delay("Please retry in 27.343938628s.") == pytest.approx(27.34, abs=0.01)


def test_absent_retry_delay_is_none():
    from app.clients.llm import parse_retry_delay

    assert parse_retry_delay("something else went wrong") is None


def test_server_directed_wait_prefers_the_api_instruction():
    """Guessing a backoff when the server has told you the answer is how a
    client hammers a quota it could have waited out."""
    from app.clients.llm import LLMQuotaError, _server_directed_wait

    wait = _server_directed_wait(lambda _state: 2.0)

    class Outcome:
        @staticmethod
        def exception():
            return LLMQuotaError("quota", retry_after=15.0)

    class State:
        outcome = Outcome()

    assert wait(State()) == pytest.approx(16.0)


def test_server_directed_wait_is_capped():
    """A 60s instruction is honest, but a user's request should not hang on it --
    the fallback model is the better answer at that point."""
    from app.clients.llm import LLMQuotaError, _server_directed_wait

    wait = _server_directed_wait(lambda _state: 2.0)

    class Outcome:
        @staticmethod
        def exception():
            return LLMQuotaError("quota", retry_after=600.0)

    class State:
        outcome = Outcome()

    assert wait(State()) == 30.0


def test_server_directed_wait_falls_back_without_an_instruction():
    from app.clients.llm import LLMTransientError, _server_directed_wait

    wait = _server_directed_wait(lambda _state: 7.0)

    class Outcome:
        @staticmethod
        def exception():
            return LLMTransientError("boom")

    class State:
        outcome = Outcome()

    assert wait(State()) == 7.0


def test_quota_error_is_transient_so_it_triggers_fallback():
    from app.clients.llm import LLMQuotaError, LLMTransientError

    assert issubclass(LLMQuotaError, LLMTransientError)


def test_fallback_chain_is_configurable():
    settings = Settings(gemini_fallback_models="a-model, b-model ,")
    assert settings.fallback_models == ["a-model", "b-model"]


def test_empty_fallback_chain_is_allowed():
    assert Settings(gemini_fallback_models="").fallback_models == []
