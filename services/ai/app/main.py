"""FastAPI surface for the AI service.

Scope note: this is the subset of Part 11 needed to make the service a real
container -- health, readiness, and a synchronous extraction endpoint that
demonstrates the zero-token pipeline. The pipeline run/resume/SSE endpoints
arrive with Part 11, once the remaining nodes exist.

The service is not internet-facing. Every functional route requires the shared
internal key; only the probes are open, because a load balancer cannot present
a credential.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.agents.scraper_keyword import scraper_keyword_agent
from app.clients.cache import get_cache
from app.config import Settings, get_settings
from app.extraction.aho import get_matcher
from app.extraction.pipeline import extract_keywords
from app.graph.builder import SCRAPER_KEYWORD, build_graph
from app.graph.checkpointer import async_checkpointer
from app.logging_config import configure_logging, request_id_var, session_id_var

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, service=settings.service_name)

    # Build the automaton once, at boot, rather than on the first request. The
    # cost is small (~1ms) but it belongs in startup, not in a user's latency --
    # and doing it here means readiness reflects it.
    started = time.perf_counter()
    matcher = get_matcher()
    app.state.matcher = matcher
    app.state.cache = get_cache()

    # The checkpointer's connection pool must live as long as the app, so it is
    # entered on an exit stack rather than a `with` block.
    async with AsyncExitStack() as stack:
        checkpointer = None
        try:
            checkpointer = await stack.enter_async_context(async_checkpointer())
        except Exception:
            # A missing database must not stop the service from starting: the
            # extraction endpoint needs no persistence, and readiness reports the
            # degradation honestly rather than the container crash-looping.
            logger.exception("Checkpointer unavailable; pipeline endpoints disabled")

        app.state.checkpointer = checkpointer
        app.state.graph = (
            build_graph({SCRAPER_KEYWORD: scraper_keyword_agent}, checkpointer=checkpointer)
            if checkpointer is not None
            else None
        )
        app.state.ready = True
        logger.info(
            "AI service ready",
            extra={
                "taxonomy_skills": len(matcher.taxonomy),
                "taxonomy_patterns": matcher.pattern_count,
                "build_ms": round((time.perf_counter() - started) * 1000, 2),
                "llm_provider": settings.llm_provider if settings.llm_configured else "mock",
                "model": settings.gemini_model if settings.llm_configured else "mock",
                "checkpointer": "postgres" if checkpointer else "unavailable",
            },
        )
        yield
        app.state.ready = False
        logger.info("AI service shutting down")


app = FastAPI(
    title="ResumeForge AI Service",
    description=(
        "Internal service: LangGraph agent pipeline and deterministic keyword "
        "extraction. Not internet-facing -- reached only via the API gateway."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


# ── Correlation ──────────────────────────────────────────────────────────
@app.middleware("http")
async def correlate(request: Request, call_next):
    """Adopt the gateway's request id, or mint one, and echo it back."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request_id_var.set(request_id)
    session_id_var.set(request.headers.get("x-session-id", "-"))

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000

    response.headers["x-request-id"] = request_id
    if request.url.path not in ("/health", "/ready"):
        logger.info(
            "request complete",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
    return response


# ── Trust boundary ───────────────────────────────────────────────────────
async def require_internal_key(
    x_internal_key: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """The AI service trusts the gateway, and nothing else.

    Compared with `compare_digest` rather than `==` so a wrong key cannot be
    recovered by timing the response.
    """
    import secrets

    expected = settings.internal_api_key
    if not expected:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_API_KEY is not configured"
        )
    if not x_internal_key or not secrets.compare_digest(x_internal_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing internal API key")


# ── Schemas ──────────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    job_url: str = Field("", description="Job posting URL to scrape")
    job_text: str = Field("", description="Job description text, if already available")
    max_keywords: int = Field(35, ge=1, le=200)

    @model_validator(mode="after")
    def require_one_input(self) -> ExtractRequest:
        if not self.job_url.strip() and not self.job_text.strip():
            raise ValueError("Provide either job_url or job_text")
        return self


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


# ── Probes (unauthenticated: a load balancer has no credential) ──────────
@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness: the process is up. Deliberately does no dependency checks --
    a failing probe here means "restart me", and Redis being down is not that."""
    return HealthResponse(status="ok", service="resumeforge-ai", version=app.version)


@app.get("/ready", tags=["ops"])
async def ready(request: Request) -> dict[str, Any]:
    """Readiness: startup finished and the automaton is built.

    Separate from liveness because this service has real boot work; routing
    traffic to it before the automaton exists would serve errors.
    """
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is still starting")
    settings = get_settings()
    matcher = request.app.state.matcher
    return {
        "status": "ready",
        "taxonomy_skills": len(matcher.taxonomy),
        "taxonomy_patterns": matcher.pattern_count,
        "llm": settings.gemini_model if settings.llm_configured else "mock (no credentials)",
        # Reported rather than hidden: without a checkpointer the pipeline
        # endpoints cannot offer durable sessions, and that is worth surfacing.
        "checkpointer": "postgres"
        if getattr(request.app.state, "checkpointer", None)
        else "unavailable",
        "pipeline_available": getattr(request.app.state, "graph", None) is not None,
    }


# ── Extraction ───────────────────────────────────────────────────────────
@app.post("/internal/extract", tags=["pipeline"], dependencies=[Depends(require_internal_key)])
async def extract(payload: ExtractRequest, request: Request) -> dict[str, Any]:
    """Run the deterministic extraction pipeline. No LLM call, no graph state.

    Exists on its own (rather than only inside the graph) because it is the
    cheapest possible smoke test of the whole determinism story, and because the
    gateway can offer a keyword preview without starting a session.
    """
    from app.clients.scraper import ScrapeError, scrape_job_posting

    job_text = payload.job_text.strip()
    tier = "manual"
    metadata: dict[str, str] = {}

    if not job_text:
        try:
            scraped = await scrape_job_posting(payload.job_url)
        except ScrapeError as exc:
            # 422, not 500: the request was well-formed, the page was not usable.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        job_text, tier, metadata = scraped.text, scraped.tier, scraped.metadata

    result = extract_keywords(
        job_text, matcher=request.app.state.matcher, max_keywords=payload.max_keywords
    )
    return {"scrape_tier": tier, "job_metadata": metadata, **result.to_dict()}
