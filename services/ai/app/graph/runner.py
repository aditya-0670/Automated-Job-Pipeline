"""Driving the graph from HTTP, and tailing its progress.

Two decisions shape this module.

**A pipeline run outlives its HTTP request.** A full run makes at least two LLM
calls and can take a minute. If the run were driven *by* the request, a user
closing the tab would cancel it mid-node and waste the spend already incurred.
So `start_run` launches the graph on a background task and returns immediately;
the request only reports that the session started.

**Progress is tailed from the checkpoint, not from the running task.** The
obvious implementation streams `graph.astream(...)` straight to the client, but
that only works while the stream and the run share a process -- the moment there
is more than one replica, a reconnecting browser can land on the one that is not
running the graph and see nothing. Every node already appends to the checkpointed
`events` list, so `stream_events` polls that instead. Any replica can serve any
session's stream, reconnection is a matter of resuming from the last sequence
number, and the stream is a read: it can never advance the graph.

The cost is a poll against Postgres per active stream, which is the right trade
for a pipeline whose nodes take seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app import metrics
from app.graph.builder import HUMAN_REVIEW, KEYWORD_REVIEW
from app.graph.checkpointer import latest_state, thread_config
from app.graph.events import make_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)

#: How often to re-read the checkpoint while tailing a session.
POLL_INTERVAL_SECONDS = 0.4

#: A stream is closed after this long so a forgotten browser tab cannot hold a
#: connection and a database poll open forever. Clients reconnect with
#: `Last-Event-ID` and lose nothing.
MAX_STREAM_SECONDS = 600.0


class SessionBusy(RuntimeError):
    """A run is already in flight for this session."""


class RunRegistry:
    """The in-flight background tasks for this process.

    Deliberately *not* the source of truth for whether a session is running --
    that is the checkpoint, which every replica can see. This only prevents one
    process from driving the same thread twice concurrently, and gives shutdown
    something to cancel.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def __contains__(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def launch(self, session_id: str, coro: Awaitable[Any]) -> asyncio.Task:
        if session_id in self:
            # Closing the coroutine avoids a "never awaited" warning masking the
            # real error the caller is about to raise.
            coro.close()  # type: ignore[attr-defined]
            raise SessionBusy(f"Session {session_id!r} is already running")
        task = asyncio.create_task(coro, name=f"pipeline:{session_id}")
        self._tasks[session_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(session_id, None))
        return task

    async def shutdown(self) -> None:
        """Cancel in-flight runs on shutdown.

        Safe because the work is checkpointed per node: a cancelled run resumes
        from its last completed node when the session is next invoked, so at
        worst one node is repeated.
        """
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def _drive(graph: Any, session_id: str, work: Callable[[], Awaitable[Any]]) -> None:
    """Run one graph invocation, converting any crash into durable state.

    An unhandled exception in a background task is otherwise invisible: the HTTP
    request has already returned 202, and the client would wait on a stream that
    never ends. Recording the failure in the checkpoint means every replica sees
    it, the stream terminates, and the user gets a reason.
    """
    started = time.perf_counter()
    try:
        await work()
        # Duration is observed per *invocation*, not per session: a session that
        # pauses for a human and resumes an hour later would otherwise report an
        # hour of pipeline time, which is a measure of the user's coffee break.
        metrics.pipeline_duration.observe(time.perf_counter() - started)
        metrics.pipeline_runs.labels("completed").inc()
    except asyncio.CancelledError:
        logger.info("Run cancelled for session %s; state is checkpointed", session_id)
        raise
    except Exception as exc:
        logger.exception("Pipeline run failed for session %s", session_id)
        message = f"The pipeline stopped unexpectedly: {exc}"
        try:
            snapshot = await graph.aget_state(thread_config(session_id))
            events = list((snapshot.values or {}).get("events") or [])
            await graph.aupdate_state(
                thread_config(session_id),
                {
                    "error": message,
                    "current_step": Step.FAILED.value,
                    "events": [
                        make_event(
                            Step.FAILED,
                            session_id=session_id,
                            detail=message,
                            sequence=len(events) + 1,
                        )
                    ],
                },
            )
        except Exception:
            # The database is the thing that failed, most likely. Nothing further
            # can be recorded; the stream's terminal timeout is the backstop.
            logger.exception("Could not record the failure for session %s", session_id)


def start_run(
    graph: Any,
    registry: RunRegistry,
    state: ResumeForgeState,
) -> asyncio.Task:
    """Begin a new session. Runs until the first interrupt or the end."""
    session_id = state["session_id"]
    config = thread_config(session_id)

    async def work() -> Any:
        return await graph.ainvoke(state, config)

    metrics.pipeline_runs.labels("started").inc()
    logger.info("Starting pipeline run for session %s", session_id)
    return registry.launch(session_id, _drive(graph, session_id, work))


def start_resume(
    graph: Any,
    registry: RunRegistry,
    session_id: str,
    resume: Callable[[], Awaitable[Any]],
) -> asyncio.Task:
    """Continue a paused session in the background.

    The caller has already validated the input against the pause point, so a
    rejected resume is a synchronous 4xx rather than a failure the user has to
    discover by watching the stream.
    """
    logger.info("Resuming pipeline for session %s", session_id)
    return registry.launch(session_id, _drive(graph, session_id, resume))


async def session_status(graph: Any, session_id: str) -> dict[str, Any] | None:
    """A read-only view of where a session is. Never advances the graph."""
    snapshot = await latest_state(graph, session_id)
    if snapshot is None:
        return None
    values = snapshot["values"]
    step = values.get("current_step", Step.INIT.value)

    # Two things that look like a pause from the outside and are not.
    #
    # A node that raised leaves its task pending, so LangGraph still reports a
    # `next` node. Nobody is being waited on there, and reporting it as a pause
    # would leave a client watching a stream for a person who will never answer,
    # so a recorded failure outranks the pending task.
    #
    # A *running* graph also always has a `next` node -- it is the node about to
    # execute. Only the two interrupt points mean "waiting for a person"; every
    # other pending node means "working". Without this, a status read taken mid
    # run reports "paused at refactorer" and the UI renders a gate that does not
    # exist.
    failed = step == Step.FAILED.value
    next_node = snapshot["next"][0] if snapshot["next"] else None
    paused = next_node in {KEYWORD_REVIEW, HUMAN_REVIEW} and not failed
    return {
        "session_id": session_id,
        "step": step,
        "is_paused": paused,
        "paused_at": next_node if paused else None,
        "is_failed": failed,
        "is_complete": failed or (step == Step.COMPLETE.value and not snapshot["next"]),
        "error": values.get("error"),
        "warnings": list(values.get("warnings") or []),
        "values": values,
    }


def _is_finished(status: dict[str, Any]) -> bool:
    """Nothing more will happen without another request from the user."""
    return status["is_complete"] or status["is_paused"]


async def stream_events(
    graph: Any,
    session_id: str,
    *,
    after_sequence: int = 0,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Yield pipeline events as they are checkpointed, oldest first.

    Args:
        after_sequence: resume point. Events are numbered from 1 within a
            session, so a reconnecting client passes the last id it saw and
            receives exactly what it missed -- no duplicates, no gap.

    Terminates when the session finishes or pauses for input, emitting a final
    envelope saying which. A pause is a normal ending: the graph is waiting on a
    person, and holding the connection open would be waiting on them too.

    **Except a pause the client has already seen.** A client that answers a gate
    reopens its stream immediately, and the background task has usually not
    written its first checkpoint yet -- so a naive reader observes the *old*
    pause, reports it as the end, and the page sits at a gate it already
    answered. A caller that is caught up (`after_sequence` at or past the last
    event) is asking "what happens next?", not "where am I?", so that first
    already-known stop is waited through rather than reported.
    """
    seen = after_sequence
    deadline = asyncio.get_running_loop().time() + max_seconds
    first_poll = True
    # True when this connection opened onto a stop its caller already knew about.
    resumed_into_known_stop = False
    # Any new event means the graph has moved, so the next stop is a real one.
    saw_progress = False

    while True:
        status = await session_status(graph, session_id)
        if status is None:
            yield {"type": "error", "data": {"message": f"Unknown session {session_id!r}"}}
            return

        for event in status["values"].get("events") or []:
            if event.get("sequence", 0) > seen:
                seen = event["sequence"]
                saw_progress = True
                yield {"type": "progress", "data": event}

        if first_poll:
            first_poll = False
            # `after_sequence > 0` distinguishes a resuming client from a fresh
            # reader: a page load starts at 0 and must be told where the session
            # is, even if that is a stop.
            resumed_into_known_stop = (
                after_sequence > 0 and not saw_progress and _is_finished(status)
            )

        if _is_finished(status) and not (resumed_into_known_stop and not saw_progress):
            yield {
                "type": "paused" if status["is_paused"] else "done",
                "data": {
                    "session_id": session_id,
                    "step": status["step"],
                    "paused_at": status["paused_at"],
                    "error": status["error"],
                    "warnings": status["warnings"],
                },
            }
            return

        if asyncio.get_running_loop().time() >= deadline:
            # Not an error: the run continues on its background task. The client
            # reconnects with Last-Event-ID and picks up exactly where it left off.
            yield {"type": "timeout", "data": {"session_id": session_id, "last_sequence": seen}}
            return

        await asyncio.sleep(poll_interval)
