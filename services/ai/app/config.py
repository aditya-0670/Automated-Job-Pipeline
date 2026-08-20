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
    gemini_model: str = "gemini-2.0-flash"
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = 8192

    # ── Pipeline behaviour ──
    max_self_correction_iterations: int = 3
    scrape_timeout_seconds: float = 15.0
    latex_compile_timeout_seconds: int = 60
    max_job_text_chars: int = 60_000

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
