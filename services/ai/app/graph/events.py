"""Pipeline events -- the append-only audit trail and the SSE payload.

One event shape serves three consumers: the SSE stream driving the UI, the
`events` list checkpointed with the state, and the logs. Keeping them identical
means what the user saw and what the audit trail recorded cannot disagree.
"""

from __future__ import annotations

from typing import Any

from app.graph.steps import HUMAN_READABLE, Step, progress_fraction


def make_event(
    step: Step,
    *,
    session_id: str = "",
    detail: str = "",
    data: dict[str, Any] | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
    """Build one pipeline event.

    No timestamp is generated here on purpose: events live inside checkpointed
    state, and a wall-clock value would make otherwise identical replays differ,
    which breaks checkpoint comparison in tests. The transport layer stamps
    arrival time instead.
    """
    return {
        "sequence": sequence,
        "step": str(step),
        "label": HUMAN_READABLE.get(step, str(step)),
        "detail": detail,
        "progress": round(progress_fraction(step), 3),
        "session_id": session_id,
        "data": data or {},
    }


def next_sequence(state: dict[str, Any]) -> int:
    return len(state.get("events") or []) + 1


def step_event(
    state: dict[str, Any],
    step: Step,
    *,
    detail: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: build an event numbered within the current state."""
    return make_event(
        step,
        session_id=state.get("session_id", ""),
        detail=detail,
        data=data,
        sequence=next_sequence(state),
    )
