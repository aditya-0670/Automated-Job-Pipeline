"""LLM access behind a narrow interface, with a deterministic mock fallback.

Two reasons for the seam:
  1. The whole pipeline -- graph, checkpointing, self-correction routing, SSE --
     must be runnable and testable without an API key or network access. The
     mock provider lets CI exercise the real graph.
  2. Provider choice is a config value, not a code dependency.

Why the official `google-genai` SDK rather than `langchain-google-genai`:
the LangChain wrapper still depends on the deprecated `google.generativeai`
package, and it reports `output_tokens=0` for thinking models -- which would
silently break the token accounting this project makes cost claims about.
LangGraph does not require its LLM calls to go through LangChain.

Thinking tokens matter here. Gemini 3.x models emit reasoning tokens that are
billed as output but are NOT included in `candidates_token_count`. Measured: a
one-token answer costs 50-120 thinking tokens by default. They are tracked
separately in `LLMResponse` so the budget is honest, and `thinking_budget` is
configurable per call so mechanical tasks can opt out.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    # Billed as output but reported separately by the API. Excluded from
    # output_tokens so the two can be reasoned about independently.
    thinking_tokens: int = 0
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    @property
    def billed_output_tokens(self) -> int:
        return self.output_tokens + self.thinking_tokens


@dataclass
class TokenLedger:
    """Accumulates spend across a pipeline run, per logical step.

    The project claims a per-run token budget, so that number has to come from
    somewhere auditable rather than an estimate.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, step: str, response: LLMResponse) -> None:
        self.entries.append(
            {
                "step": step,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "thinking_tokens": response.thinking_tokens,
                "total_tokens": response.total_tokens,
            }
        )

    @property
    def total_input(self) -> int:
        return sum(e["input_tokens"] for e in self.entries)

    @property
    def total_output(self) -> int:
        return sum(e["output_tokens"] + e["thinking_tokens"] for e in self.entries)

    @property
    def total(self) -> int:
        return self.total_input + self.total_output

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": len(self.entries),
            "input_tokens": self.total_input,
            "output_tokens": self.total_output,
            "total_tokens": self.total,
            "by_step": self.entries,
        }


class LLMError(RuntimeError):
    """Provider failure. Retryable variants are raised as LLMTransientError."""


class LLMTransientError(LLMError):
    """Overload, rate limit, or timeout -- worth retrying.

    `retry_after` carries the delay the API itself asked for, when it supplies
    one. Guessing a backoff when the server has told you the answer is how a
    client ends up hammering a quota it could have waited out.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMQuotaError(LLMTransientError):
    """Free-tier quota exhausted (429). Retryable, but only after a real wait."""


def parse_retry_delay(message: str) -> float | None:
    """Pull the server-specified retry delay out of a Gemini error payload.

    Gemini reports it twice -- as `'retryDelay': '58s'` and as
    "Please retry in 58.44s" -- so both shapes are accepted.
    """
    for pattern in (r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", r"retry in (\d+(?:\.\d+)?)s"):
        found = re.search(pattern, message)
        if found:
            return float(found.group(1))
    return None


class LLMTruncatedError(LLMError):
    """The model hit its output ceiling before finishing.

    Distinct from a transient failure: retrying the same request unchanged will
    truncate again. With thinking models this is a real hazard, because reasoning
    consumes the output budget before any text is produced.
    """


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        thinking_budget: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse: ...

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        thinking_budget: int | None = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        response = await self.complete(
            system=system, user=user, thinking_budget=thinking_budget, json_mode=True
        )
        return parse_json(response.text), response


def parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response.

    Native JSON mode makes this nearly always a plain `json.loads`, but the
    fence and brace fallbacks cost nothing and cover providers or models that
    ignore the mime type.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise LLMError("Model returned empty text where JSON was expected")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Model did not return parseable JSON: {cleaned[:300]!r}")


def _server_directed_wait(fallback):
    """Wait for the delay the API asked for, if it supplied one."""

    def wait(retry_state):
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        requested = getattr(exc, "retry_after", None)
        if requested:
            # Capped: a 60s+ instruction is honest but a request should not hang
            # a user's session on it -- the fallback model is the better answer.
            return min(float(requested) + 1.0, 30.0)
        return fallback(retry_state)

    return wait


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, fallbacks: list[str] | None = None) -> None:
        from google import genai

        self.model = model
        self.fallbacks = list(fallbacks or [])
        self._client = genai.Client(api_key=api_key)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        thinking_budget: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Try the primary model, then each fallback, before giving up.

        Fallback triggers only on transient failures (overload, quota). A 400 is
        our bug and would fail identically on every model, so it propagates
        immediately rather than burning quota on three models.
        """
        last_error: LLMTransientError | None = None
        for candidate in [self.model, *self.fallbacks]:
            try:
                return await self._complete_with(
                    candidate,
                    system=system,
                    user=user,
                    thinking_budget=thinking_budget,
                    json_mode=json_mode,
                )
            except LLMTransientError as exc:
                last_error = exc
                logger.warning(
                    "Model %s unavailable (%s); trying next candidate",
                    candidate,
                    type(exc).__name__,
                )
        raise last_error or LLMError("No Gemini model was available")

    @retry(
        retry=retry_if_exception_type(LLMTransientError),
        stop=stop_after_attempt(3),
        wait=_server_directed_wait(wait_exponential(multiplier=1, min=2, max=30)),
        reraise=True,
    )
    async def _complete_with(
        self,
        model: str,
        *,
        system: str,
        user: str,
        thinking_budget: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        from google.genai import errors, types

        settings = get_settings()
        if thinking_budget is None:
            thinking_budget = settings.llm_thinking_budget

        config: dict[str, Any] = {
            "system_instruction": system,
            "temperature": settings.llm_temperature,
            "max_output_tokens": settings.llm_max_output_tokens,
        }
        if json_mode:
            config["response_mime_type"] = "application/json"
        if thinking_budget >= 0:
            config["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(**config),
            )
        except errors.ServerError as exc:  # 5xx -- overload, worth retrying
            raise LLMTransientError(
                f"Gemini unavailable: {exc}", retry_after=parse_retry_delay(str(exc))
            ) from exc
        except errors.ClientError as exc:
            # 429 is quota exhaustion: retryable, but only after the delay the
            # API specifies. Other 4xx are our own fault and must not retry.
            if getattr(exc, "code", None) == 429:
                raise LLMQuotaError(
                    f"Gemini quota exceeded: {exc}", retry_after=parse_retry_delay(str(exc))
                ) from exc
            raise LLMError(f"Gemini rejected the request: {exc}") from exc
        except Exception as exc:
            raise LLMTransientError(f"Gemini call failed: {exc}") from exc

        usage = response.usage_metadata
        finish_reason = None
        if response.candidates:
            finish_reason = getattr(response.candidates[0].finish_reason, "name", None)

        result = LLMResponse(
            text=response.text or "",
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            thinking_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
            model=model,
            finish_reason=finish_reason,
        )

        if not result.text.strip():
            # With thinking models an exhausted output budget yields reasoning
            # tokens and no text at all. Say so plainly rather than letting an
            # empty string flow downstream.
            if finish_reason == "MAX_TOKENS":
                raise LLMTruncatedError(
                    f"Model hit max_output_tokens ({settings.llm_max_output_tokens}) "
                    f"after {result.thinking_tokens} thinking tokens without "
                    f"producing text. Raise LLM_MAX_OUTPUT_TOKENS or lower "
                    f"LLM_THINKING_BUDGET."
                )
            raise LLMTransientError(f"Model returned no text (finish_reason={finish_reason})")

        return result


class MockProvider(LLMProvider):
    """Deterministic stand-in. Never touches the network.

    Not a constant stub: it inspects the prompt and returns evaluator-shaped
    JSON or echoes back the LaTeX block, so downstream nodes receive plausibly
    shaped input and stay genuinely testable.
    """

    name = "mock"
    model = "mock-deterministic"

    async def complete(
        self,
        *,
        system: str,
        user: str,
        thinking_budget: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        logger.warning("MockProvider in use -- no LLM configured. Output is synthetic.")
        payload = self._respond(system, user, json_mode=json_mode)
        return LLMResponse(
            text=payload,
            # ~4 chars per token is a rough proxy, enough to exercise accounting.
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(payload) // 4,
            model=self.model,
            finish_reason="STOP",
        )

    @staticmethod
    def _respond(system: str, user: str, *, json_mode: bool = False) -> str:
        """Shape the response to what the caller actually asked for.

        The refactorer requests JSON containing a `latex` key. Returning bare
        LaTeX there fails parsing, which would make the whole offline path
        untestable -- the mock exists precisely so that cannot happen.

        Role is detected from the SYSTEM prompt only. Matching against the user
        message too looked harmless and was not: a real resume contains words
        like "review" and "evaluate", so the refactor request was being answered
        with an evaluator payload. Dispatch must key on text we author, never on
        user content.
        """
        role = system.lower()
        lowered = f"{system}\n{user}".lower()

        if "evaluat" in role or "reviewer" in role:
            return json.dumps(
                {
                    "passed": True,
                    "factual_errors": [],
                    "grounding_errors": [],
                    "structural_errors": [],
                    "quality_issues": [],
                    "keyword_coverage": 0.82,
                    "feedback": "Mock evaluator: no issues detected.",
                }
            )

        latex = _extract_latex_block(user)
        if latex:
            annotated = f"% ResumeForge (mock provider -- no LLM configured)\n{latex}"
            if json_mode or "resume editor" in role:
                return json.dumps(
                    {
                        "latex": annotated,
                        "changelog": [
                            {
                                "section": "Summary",
                                "change_type": "reworded",
                                "before": "(mock)",
                                "after": "(mock)",
                                "reason": "MockProvider: no LLM configured",
                            }
                        ],
                    }
                )
            return annotated

        if json_mode or "json" in lowered:
            return json.dumps({"mock": True, "note": "MockProvider response"})
        return "Mock provider response."


def _extract_latex_block(text: str) -> str | None:
    match = re.search(r"\\documentclass.*?\\end\{document\}", text, re.S)
    return match.group(0) if match else None


def get_llm() -> LLMProvider:
    """Resolve the configured provider, degrading to the mock without a key."""
    settings = get_settings()
    if settings.llm_configured:
        return GeminiProvider(
            settings.gemini_api_key, settings.gemini_model, settings.fallback_models
        )
    logger.warning(
        "No LLM credentials found (LLM_PROVIDER=%s). Using MockProvider; "
        "set GEMINI_API_KEY for real generation.",
        settings.llm_provider,
    )
    return MockProvider()
