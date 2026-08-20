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
    """Overload, rate limit, or timeout -- worth retrying."""


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


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self.model = model
        self._client = genai.Client(api_key=api_key)

    @retry(
        retry=retry_if_exception_type(LLMTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    async def complete(
        self,
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
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(**config),
            )
        except errors.ServerError as exc:  # 5xx -- overload, worth retrying
            raise LLMTransientError(f"Gemini unavailable: {exc}") from exc
        except errors.ClientError as exc:
            # 429 is a rate limit and retryable; other 4xx are our fault.
            if getattr(exc, "code", None) == 429:
                raise LLMTransientError(f"Gemini rate limited: {exc}") from exc
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
            model=self.model,
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
        payload = self._respond(system, user)
        return LLMResponse(
            text=payload,
            # ~4 chars per token is a rough proxy, enough to exercise accounting.
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(payload) // 4,
            model=self.model,
            finish_reason="STOP",
        )

    @staticmethod
    def _respond(system: str, user: str) -> str:
        lowered = f"{system}\n{user}".lower()
        if "evaluat" in lowered:
            return json.dumps(
                {
                    "passed": True,
                    "factual_errors": [],
                    "grounding_errors": [],
                    "structural_errors": [],
                    "keyword_coverage": 0.82,
                    "feedback": "Mock evaluator: no issues detected.",
                }
            )
        latex = _extract_latex_block(user)
        if latex:
            return f"% ResumeForge (mock provider -- no LLM configured)\n{latex}"
        if "json" in lowered:
            return json.dumps({"mock": True, "note": "MockProvider response"})
        return "Mock provider response."


def _extract_latex_block(text: str) -> str | None:
    match = re.search(r"\\documentclass.*?\\end\{document\}", text, re.S)
    return match.group(0) if match else None


def get_llm() -> LLMProvider:
    """Resolve the configured provider, degrading to the mock without a key."""
    settings = get_settings()
    if settings.llm_configured:
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    logger.warning(
        "No LLM credentials found (LLM_PROVIDER=%s). Using MockProvider; "
        "set GEMINI_API_KEY for real generation.",
        settings.llm_provider,
    )
    return MockProvider()
