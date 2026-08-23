"""Shared FastAPI dependencies.

Extracted from `main` so the pipeline router can depend on the trust boundary
without importing the app object, which would be circular.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings


async def require_internal_key(
    x_internal_key: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """The AI service trusts the gateway, and nothing else.

    Compared with `compare_digest` rather than `==` so a wrong key cannot be
    recovered by timing the response.
    """
    expected = settings.internal_api_key
    if not expected:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "INTERNAL_API_KEY is not configured"
        )
    if not x_internal_key or not secrets.compare_digest(x_internal_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing internal API key")


def get_graph(request: Request) -> Any:
    """The compiled graph, or a 503 explaining why there isn't one.

    Without a checkpointer there is no durable session, and a pipeline that
    cannot survive a restart is worse than an honest refusal -- the user would
    spend real tokens on a run that a deploy could erase.
    """
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The pipeline is unavailable: no checkpointer, so sessions cannot be made durable.",
        )
    return graph


def get_registry(request: Request) -> Any:
    return request.app.state.runs
