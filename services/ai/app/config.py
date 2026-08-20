"""Environment-driven configuration. No secret ever has a real default."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Service ──
    service_name: str = "resumeforge-ai"
    log_level: str = "INFO"
    ai_port: int = 8000

    # ── Trust boundary ──
    # The AI service is not internet-facing. Only the API gateway may call it,
    # authenticated by this shared key.
    internal_api_key: str = Field(default="dev-internal-key-change-me")

    # ── Data layer ──
    database_url: str = "postgresql://resumeforge:resumeforge@postgres:5432/resumeforge"
    redis_url: str = "redis://redis:6379"

    # ── LLM ──
    llm_provider: Literal["gemini", "mock"] = "gemini"
    gemini_api_key: str = ""
    # gemini-2.0-flash and gemini-2.5-flash are retired for new API keys.
    # Verified live against the models list; see docs/07-decision-log.md ADR-009.
    gemini_model: str = "gemini-3.7-flash"
    # Tried in order when the primary returns 503 (overload) or 429 (quota).
    # Measured reality on the free tier: gemini-3.7-flash allows 5 requests per
    # minute and 20 per day, and returns 503 "high demand" often enough to see
    # it several times in one session. A single pipeline run needs at least two
    # calls, so without a fallback the product is unusable for long stretches.
    # Lite models carry higher free-tier limits, so they are the safety net.
    gemini_fallback_models: str = "gemini-3.1-flash-lite,gemini-3.5-flash"
    llm_temperature: float = 0.2
    # Generous, because on thinking models reasoning consumes this budget before
    # any text is emitted -- too low yields zero-length output, not short output.
    llm_max_output_tokens: int = 16384
    # Gemini 3.x reasoning tokens are billed as output. A small explicit budget
    # measured far cheaper than the default: 11 total tokens vs 65 on a trivial
    # prompt. -1 leaves the model's default in place.
    llm_thinking_budget: int = 512

    # ── Pipeline behaviour ──
    max_self_correction_iterations: int = 3
    scrape_timeout_seconds: float = 15.0
    latex_compile_timeout_seconds: int = 60
    max_job_text_chars: int = 60_000

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]

    @property
    def llm_configured(self) -> bool:
        """Falls back to the deterministic mock when no key is present, so the
        full pipeline stays runnable end-to-end without credentials."""
        return self.llm_provider == "gemini" and bool(self.gemini_api_key)

    @property
    def psycopg_dsn(self) -> str:
        """LangGraph's PostgresSaver wants a plain libpq DSN."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
