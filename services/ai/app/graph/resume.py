"""Resuming a paused session -- the other half of the human-in-the-loop.

Starting a run and resuming one are different operations and this module exists
so they cannot be confused. A resume must:

  1. Read the checkpoint to confirm the session is paused, and paused *where*
     the caller thinks it is. Resuming a session that is actually waiting at
     `keyword_review` with a review decision would write a decision no node ever
     reads and then run the wrong node.
  2. Write the user's input into the checkpointed state.
  3. Re-invoke with `None` as the input -- LangGraph's signal for "continue from
     the checkpoint", as opposed to a dict, which would start a fresh run.

Step 3 is why the process holds nothing between the pause and the resume: any
replica with database access can serve the resume, which is what makes the
interrupt survive a deploy.
"""

from __future__ import annotations

import logging
from typing import Any

from app.graph.builder import HUMAN_REVIEW, KEYWORD_REVIEW
from app.graph.checkpointer import thread_config
from app.graph.state import UserDecision

logger = logging.getLogger(__name__)


class ResumeError(RuntimeError):
    """The session cannot accept this input in its current state."""


async def paused_at(graph: Any, session_id: str) -> str | None:
    """The node the session is paused before, or None if it is not paused."""
    snapshot = await graph.aget_state(thread_config(session_id))
    if snapshot is None or not snapshot.next:
        return None
    return snapshot.next[0]


async def _require_pause(graph: Any, session_id: str, expected: str) -> None:
    where = await paused_at(graph, session_id)
    if where is None:
        raise ResumeError(
            f"Session {session_id!r} is not waiting for input; it has finished or never ran."
        )
    if where != expected:
        raise ResumeError(f"Session {session_id!r} is waiting at {where!r}, not {expected!r}.")


async def resume_review(
    graph: Any,
    session_id: str,
    decision: UserDecision,
    *,
    change_request: str = "",
    edited_latex: str = "",
) -> dict[str, Any]:
    """Answer the `human_review` interrupt and run the graph to its next stop.

    Args:
        decision: `accept` | `request_changes` | `edit` | `modify_keywords`.
        change_request: required for `request_changes` -- the instruction is what
            makes the retry targeted rather than a blind regeneration.
        edited_latex: required for `edit`.

    Returns the state after the graph next stops (complete, failed, or paused
    again at another interrupt).
    """
    if decision not in {"accept", "request_changes", "edit", "modify_keywords"}:
        raise ResumeError(f"Unknown review decision: {decision!r}")
    if decision == "request_changes" and not change_request.strip():
        raise ResumeError("request_changes needs an instruction describing the change.")
    if decision == "edit" and not edited_latex.strip():
        raise ResumeError("edit needs the edited LaTeX.")

    await _require_pause(graph, session_id, HUMAN_REVIEW)
    config = thread_config(session_id)

    await graph.aupdate_state(
        config,
        {
            "user_decision": decision,
            "user_change_request": change_request,
            "edited_latex": edited_latex,
        },
    )
    logger.info("Resuming session %s from human review with decision=%s", session_id, decision)
    # None, not a dict: continue the existing thread rather than starting a run.
    return await graph.ainvoke(None, config)


async def resume_keywords(
    graph: Any,
    session_id: str,
    *,
    keywords: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Answer the `keyword_review` interrupt (extraction Layer 4).

    `keywords` replaces the extracted set when supplied, so the user can drop a
    keyword the posting mentions in passing or add one the taxonomy missed.
    Passing None confirms the extracted set unchanged.
    """
    await _require_pause(graph, session_id, KEYWORD_REVIEW)
    config = thread_config(session_id)

    update: dict[str, Any] = {"keywords_confirmed": True}
    if keywords is not None:
        if not keywords:
            raise ResumeError("At least one keyword is needed to match against your profile.")
        update["keywords"] = keywords

    await graph.aupdate_state(config, update)
    logger.info(
        "Resuming session %s from keyword review with %s keywords",
        session_id,
        len(keywords) if keywords is not None else "the extracted",
    )
    return await graph.ainvoke(None, config)
