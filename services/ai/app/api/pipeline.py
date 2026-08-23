"""The pipeline surface: start a session, watch it, answer it, download it.

Every endpoint here is a thin translation between HTTP and the graph. The rules
that decide anything live in `app/graph/` -- routing in `routing.py`, resume
validation in `resume.py`, background execution in `runner.py` -- so the same
behaviour is reachable from a test, a script, or a future gRPC surface without
going through FastAPI.

Long work never happens inside a request. `run` and `resume` return 202 and a
session id; progress arrives on the SSE stream. That is not only about latency:
the run is checkpointed per node, so a client that disconnects loses its view of
the pipeline and nothing else.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from app.agents.human_review import review_payload
from app.api.deps import get_graph, get_registry, require_internal_key
from app.graph.builder import HUMAN_REVIEW, KEYWORD_REVIEW
from app.graph.resume import ResumeError, paused_at, resume_keywords, resume_review
from app.graph.runner import SessionBusy, session_status, start_resume, start_run, stream_events
from app.graph.state import initial_state
from app.graph.steps import HUMAN_READABLE, Step, progress_fraction

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/pipeline",
    tags=["pipeline"],
    dependencies=[Depends(require_internal_key)],
)


# ── Schemas ──────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    """Everything the pipeline needs. The AI service fetches no user data itself.

    The profile and the LaTeX template arrive from the gateway, which owns the
    database. Keeping the AI service ignorant of user storage means it holds no
    credentials for it and cannot leak one user's data into another's session.
    """

    user_id: str = Field(min_length=1)
    user_latex: str = Field(min_length=1, description="The LaTeX resume template to preserve")
    user_profile: dict[str, Any]
    session_id: str = Field(
        "", description="Supply to make the call idempotent; generated if empty"
    )
    job_url: str = ""
    job_text: str = ""
    max_iterations: int = Field(3, ge=1, le=5)

    @model_validator(mode="after")
    def require_a_posting(self) -> RunRequest:
        if not self.job_url.strip() and not self.job_text.strip():
            raise ValueError("Provide either job_url or job_text")
        return self


class ResumeRequest(BaseModel):
    """The answer to whichever interrupt the session is waiting at.

    One endpoint rather than two, because the client already knows the session id
    and the server already knows where that session is paused. Asking the client
    to route correctly would let a review decision be posted to a keyword pause.
    """

    decision: str = Field(
        "", description="human_review: accept | request_changes | edit | modify_keywords"
    )
    change_request: str = ""
    edited_latex: str = ""
    keywords: list[dict[str, Any]] | None = Field(
        None, description="keyword_review: replaces the extracted set; omit to confirm as-is"
    )


class RunAccepted(BaseModel):
    session_id: str
    status: str
    events_url: str


# ── Start ────────────────────────────────────────────────────────────────
@router.post("/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunAccepted)
async def run_pipeline(
    payload: RunRequest,
    graph: Annotated[Any, Depends(get_graph)],
    registry: Annotated[Any, Depends(get_registry)],
) -> RunAccepted:
    """Start a session and return immediately.

    202, not 200: the work has been accepted, not completed. The client follows
    `events_url` to watch it.
    """
    session_id = payload.session_id.strip() or uuid.uuid4().hex

    # A session id is a checkpoint thread id, so reusing one would resume someone
    # else's pipeline rather than start a new one. Refused rather than silently
    # branched, because the two are indistinguishable to the caller afterwards.
    if await session_status(graph, session_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Session {session_id!r} already exists. Use the resume endpoint to continue it.",
        )

    state = initial_state(
        session_id=session_id,
        user_id=payload.user_id,
        user_latex=payload.user_latex,
        user_profile=payload.user_profile,
        job_url=payload.job_url,
        job_text=payload.job_text,
        max_iterations=payload.max_iterations,
    )
    try:
        start_run(graph, registry, state)
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return RunAccepted(
        session_id=session_id,
        status="running",
        events_url=f"/internal/pipeline/{session_id}/events",
    )


# ── Answer an interrupt ──────────────────────────────────────────────────
@router.post(
    "/{session_id}/resume", status_code=status.HTTP_202_ACCEPTED, response_model=RunAccepted
)
async def resume_pipeline(
    session_id: str,
    payload: ResumeRequest,
    graph: Annotated[Any, Depends(get_graph)],
    registry: Annotated[Any, Depends(get_registry)],
) -> RunAccepted:
    """Answer the interrupt the session is paused at, then continue it.

    Validation happens **before** the background task is launched, so bad input
    is a 400 the caller can act on rather than a failure they would have to
    discover by watching the stream.
    """
    where = await paused_at(graph, session_id)
    if where is None:
        detail = (
            "Session not found"
            if await session_status(graph, session_id) is None
            else "This session is not waiting for input."
        )
        code = status.HTTP_404_NOT_FOUND if "not found" in detail else status.HTTP_409_CONFLICT
        raise HTTPException(code, detail)

    if where == KEYWORD_REVIEW:

        async def work() -> Any:
            return await resume_keywords(graph, session_id, keywords=payload.keywords)

        # Validated here, not in the task, for the same reason as above.
        if payload.keywords is not None and not payload.keywords:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "At least one keyword is needed to match against your profile.",
            )
    elif where == HUMAN_REVIEW:
        try:
            _validate_review(payload)
        except ResumeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        async def work() -> Any:
            return await resume_review(
                graph,
                session_id,
                payload.decision,  # type: ignore[arg-type]
                change_request=payload.change_request,
                edited_latex=payload.edited_latex,
            )
    else:  # pragma: no cover -- only reachable if a new interrupt is added
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot resume from {where!r}")

    try:
        start_resume(graph, registry, session_id, work)
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return RunAccepted(
        session_id=session_id,
        status="running",
        events_url=f"/internal/pipeline/{session_id}/events",
    )


def _validate_review(payload: ResumeRequest) -> None:
    """Reuse the resume module's rules rather than restating them in HTTP terms."""
    valid = {"accept", "request_changes", "edit", "modify_keywords"}
    if payload.decision not in valid:
        raise ResumeError(f"decision must be one of {sorted(valid)}")
    if payload.decision == "request_changes" and not payload.change_request.strip():
        raise ResumeError("request_changes needs an instruction describing the change.")
    if payload.decision == "edit" and not payload.edited_latex.strip():
        raise ResumeError("edit needs the edited LaTeX.")


# ── Read ─────────────────────────────────────────────────────────────────
@router.get("/{session_id}")
async def get_session(
    session_id: str,
    graph: Annotated[Any, Depends(get_graph)],
    include_diff: bool = Query(True, description="Include the section diff when awaiting review"),
) -> dict[str, Any]:
    """Where is my session? Answered without advancing the graph.

    This is what a browser calls after a reload. It must be safe to call at any
    time and any number of times, which is why it reads the checkpoint rather
    than invoking anything.
    """
    status_ = await session_status(graph, session_id)
    if status_ is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown session {session_id!r}")

    values = status_["values"]
    step = Step(status_["step"])
    body: dict[str, Any] = {
        "session_id": session_id,
        "step": status_["step"],
        "label": HUMAN_READABLE.get(step, status_["step"]),
        "progress": round(progress_fraction(step), 3),
        "is_paused": status_["is_paused"],
        "paused_at": status_["paused_at"],
        "is_complete": status_["is_complete"],
        "error": status_["error"],
        "warnings": status_["warnings"],
        "iteration_count": values.get("iteration_count", 0),
        "token_ledger": values.get("token_ledger") or {},
        "pdf_ready": bool(values.get("pdf_path")),
        # A trimmed evidence list, not the whole thing: the UI shows what was
        # matched and why, and the full items carry the entire text of every
        # bullet and README that was indexed -- megabytes on a status poll.
        "evidence": [
            {
                "item_id": item.get("item_id", ""),
                "kind": item.get("kind", ""),
                "title": item.get("title", ""),
                "matched_keywords": list(item.get("matched_keywords") or []),
                "relevance": item.get("relevance", 0.0),
                "already_on_resume": bool(item.get("already_on_resume")),
            }
            for item in (values.get("matched_evidence") or [])
        ],
    }

    # The payload for whichever gate the user is standing at, and nothing else:
    # sending the full diff on every poll of a running session would be pure
    # bandwidth, and the resume is large.
    if status_["paused_at"] == KEYWORD_REVIEW:
        body["keyword_review"] = {
            "keywords": values.get("keywords") or [],
            "by_category": values.get("keywords_by_category") or {},
            "job_metadata": values.get("job_metadata") or {},
            "scrape_tier": values.get("scrape_tier", ""),
            "stats": values.get("extraction_stats") or {},
        }
    elif status_["paused_at"] == HUMAN_REVIEW:
        body["human_review"] = review_payload(values, include_unified=include_diff)

    return body


@router.get("/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    graph: Annotated[Any, Depends(get_graph)],
    last_event_id: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> EventSourceResponse:
    """Server-sent events for one session, resumable and replica-independent.

    The resume point comes from the standard `Last-Event-ID` header (set
    automatically by `EventSource` on reconnect) or an explicit query parameter
    for clients that cannot set headers. Event ids are the sequence numbers the
    nodes already assign, so a reconnect replays exactly the gap.
    """
    after = _resume_sequence(request.headers.get("last-event-id") or last_event_id)

    async def publisher():
        async for event in stream_events(graph, session_id, after_sequence=after):
            payload = event["data"]
            frame = {"event": event["type"], "data": json.dumps(payload)}
            if event["type"] == "progress":
                # Only progress frames are resumable positions, so only they
                # carry an id. Numbering a terminal frame would make a reconnect
                # ask to resume from a point that is not in the event list.
                frame["id"] = str(payload["sequence"])
            yield frame

    # `ping` keeps intermediaries from closing an idle connection during a slow
    # LLM node; a comment frame, so it never looks like an event to the client.
    return EventSourceResponse(publisher(), ping=15)


def _resume_sequence(raw: str | None) -> int:
    """A malformed Last-Event-ID replays the session rather than failing it."""
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        logger.warning("Ignoring unparseable Last-Event-ID %r", raw)
        return 0


@router.get("/{session_id}/pdf")
async def session_pdf(
    session_id: str,
    graph: Annotated[Any, Depends(get_graph)],
) -> Response:
    """The compiled PDF, if the session produced one."""
    from pathlib import Path

    status_ = await session_status(graph, session_id)
    if status_ is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown session {session_id!r}")

    pdf_path = status_["values"].get("pdf_path") or ""
    if not pdf_path:
        # 409, not 404: the session exists and the PDF may yet appear. A 404 would
        # tell a polling client to give up.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No PDF yet; the session is at {status_['step']}.",
        )
    if not Path(pdf_path).is_file():
        # The path is checkpointed but the file is not there -- a restarted
        # container with a non-persistent volume. Say so rather than 500.
        raise HTTPException(
            status.HTTP_410_GONE,
            "The compiled PDF is no longer on disk. Re-run the compilation step.",
        )

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"resume-{session_id}.pdf",
    )
