"""LLM access behind a narrow interface, with a deterministic mock fallback.

Two reasons for the seam:
  1. The whole pipeline -- graph, checkpointing, self-correction routing, SSE --
     must be runnable and testable without an API key or network access. The
     mock provider makes CI able to exercise the real graph.
  2. Provider choice is a config value, not a code dependency. Gemini today
     because of its free tier; swapping providers touches this file only.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMError(RuntimeError):
    """Retryable provider failure."""


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def complete(self, *, system: str, user: str) -> LLMResponse: ...

    async def complete_json(self, *, system: str, user: str) -> tuple[dict[str, Any], LLMResponse]:
        """Ask for JSON and parse it, tolerating markdown fences."""
        response = await self.complete(
            system=f"{system}\n\nRespond with valid JSON only. No prose, no markdown fences.",
            user=user,
        )
        return _parse_json(response.text), response


def _parse_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: the outermost brace pair.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"Model did not return parseable JSON: {text[:300]!r}")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        settings = get_settings()
        self.model = model
        self._client = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    @retry(
        retry=retry_if_exception_type(LLMError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def complete(self, *, system: str, user: str) -> LLMResponse:
        try:
            message = await self._client.ainvoke(
                [("system", system), ("human", user)]
            )
        except Exception as exc:
            raise LLMError(f"Gemini call failed: {exc}") from exc

        usage = getattr(message, "usage_metadata", None) or {}
        return LLMResponse(
            text=str(message.content),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=self.model,
        )


class MockProvider(LLMProvider):
    """Deterministic stand-in. Never calls the network.

    It is not a stub that returns a constant: it performs a crude but real
    transformation so that downstream nodes (evaluator, diff, LaTeX compile)
    receive plausibly shaped input and can be genuinely tested.
    """

    name = "mock"
    model = "mock-deterministic"

    async def complete(self, *, system: str, user: str) -> LLMResponse:
        logger.warning("MockProvider in use -- no LLM configured. Output is synthetic.")
        payload = self._respond(system, user)
        return LLMResponse(
            text=payload,
            # Rough proxy so token accounting is exercised in tests: ~4 chars/token.
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(payload) // 4,
            model=self.model,
        )

    @staticmethod
    def _respond(system: str, user: str) -> str:
        lowered = system.lower()
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
        if "json" in lowered:
            return json.dumps({"mock": True, "note": "MockProvider response"})
        # Refactor-shaped request: echo the LaTeX back with a marker comment so
        # the compile step has something valid to work with.
        latex = _extract_latex_block(user)
        if latex:
            return f"% ResumeForge (mock provider -- no LLM configured)\n{latex}"
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
