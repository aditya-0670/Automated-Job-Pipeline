"""Node 5 — Human review. The interrupt where the pipeline hands back control.

The graph is compiled with `interrupt_before=["human_review"]`, so the pause
happens *before* this function ever runs. That ordering is the whole design:

  * While the graph is paused, there is no coroutine suspended anywhere. The
    pause is a checkpoint row, so it survives a redeploy, a crash, or a user who
    closes the tab and comes back tomorrow.
  * The review payload the UI renders (`review_payload` below) is therefore
    computed from the paused state on demand, not written by this node.
  * When the user answers, `app/graph/resume.py` writes the decision into state
    and re-invokes. *Then* this node runs, with a decision already present, and
    its job is to make that decision safe to route on.

So this node is not "ask the user" -- it is "apply what the user said". It
spends no tokens.
"""

from __future__ import annotations

import logging
from typing import Any

from app.diff import diff_sections, diff_summary
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)

VALID_DECISIONS = frozenset({"accept", "request_changes", "edit", "modify_keywords"})


def review_payload(state: dict[str, Any], *, include_unified: bool = True) -> dict[str, Any]:
    """Everything the UI needs to render the review, derived from paused state.

    Pure and side-effect free, so the API can call it on every page load without
    touching the graph. Deriving rather than storing also means a fix to the diff
    algorithm applies to sessions that were already paused when it shipped.
    """
    original = state.get("user_latex") or ""
    revised = state.get("refactored_latex") or ""
    sections = diff_sections(original, revised, include_unified=include_unified)
    evaluation = state.get("evaluation") or {}

    return {
        "session_id": state.get("session_id", ""),
        "sections": sections,
        "summary": diff_summary(sections),
        "changelog": list(state.get("changelog") or []),
        "warnings": list(state.get("warnings") or []),
        # Surfaced even when the graph decided they were not worth another retry
        # (Part 7's graceful degradation): the user is the one approving this
        # resume, so unresolved problems must be visible at the moment they sign
        # off, not buried in a log.
        "unresolved": {
            "factual_errors": list(evaluation.get("factual_errors") or []),
            "structural_errors": list(evaluation.get("structural_errors") or []),
        },
        "quality": {
            key: evaluation.get(key)
            for key in ("passed", "keyword_coverage", "feedback", "score")
            if key in evaluation
        },
        "suggestions": list(state.get("suggestions") or []),
        "unsupported_keywords": list(state.get("unsupported_keywords") or []),
        "iteration_count": state.get("iteration_count", 0),
        "review_iteration": state.get("review_iteration", 0),
        "latex": revised,
    }


async def human_review_agent(state: ResumeForgeState) -> dict[str, Any]:
    """Apply the user's decision and prepare the state the next node needs."""
    decision = state.get("user_decision")
    review_iteration = state.get("review_iteration", 0) + 1
    warnings = list(state.get("warnings") or [])

    if decision not in VALID_DECISIONS:
        # Reached only if the graph is resumed without a decision -- a client
        # bug. Accepting is the safe reading: the user has already seen the
        # resume, and refusing would strand a session that has been fully paid
        # for in LLM spend.
        logger.warning("Resumed with no valid decision (%r); treating as accept", decision)
        decision = "accept"

    if decision == "request_changes" and not (state.get("user_change_request") or "").strip():
        warnings.append("No change instruction was given, so the resume was accepted as-is.")
        decision = "accept"

    if decision == "edit" and not (state.get("edited_latex") or "").strip():
        warnings.append("No edited LaTeX was supplied, so the resume was accepted as-is.")
        decision = "accept"

    handler = {
        "accept": _accept,
        "request_changes": _request_changes,
        "edit": _apply_edit,
        "modify_keywords": _reopen_keywords,
    }[decision]
    update = handler(state)

    logger.info(
        "Human review #%d: decision=%s -> %s",
        review_iteration,
        decision,
        update.get("current_step"),
    )

    return {
        **update,
        # Written back so routing dispatches on the *validated* decision rather
        # than the raw one, and so the audit trail records what was actually done.
        "user_decision": decision,
        "review_iteration": review_iteration,
        "warnings": warnings,
        "events": [
            step_event(
                state,
                Step(update["current_step"]),
                detail=_DETAIL[decision],
                data={"decision": decision, "review_iteration": review_iteration},
            )
        ],
    }


_DETAIL = {
    "accept": "Approved by the user",
    "request_changes": "User requested changes",
    "edit": "User supplied their own edits",
    "modify_keywords": "User reopened keyword selection",
}


def _accept(state: ResumeForgeState) -> dict[str, Any]:
    """Freeze the approved LaTeX as `final_latex`.

    A separate field, not a reuse of `refactored_latex`: it records what the user
    actually approved. If anything later writes to `refactored_latex`, the PDF
    still traces to the version that was signed off on.
    """
    return {
        "final_latex": state.get("refactored_latex") or "",
        "current_step": Step.COMPILING.value,
    }


def _revision_budget() -> dict[str, Any]:
    """A user-initiated revision starts a fresh self-correction budget.

    `iteration_count` bounds *automatic* retries against a model that cannot
    satisfy the evaluator. Letting it carry over would mean the user's third
    revision silently gets no self-correction at all -- the cheapest safety net
    in the pipeline -- because earlier rounds spent the budget. Each new round is
    a deliberate human decision to spend, and is bounded by the human, not by the
    counter.
    """
    return {"iteration_count": 0, "evaluation": {}, "evaluator_feedback": ""}


def _request_changes(state: ResumeForgeState) -> dict[str, Any]:
    # `user_change_request` is deliberately *not* cleared here; the refactorer
    # consumes it and clears it once it has actually been applied.
    return {**_revision_budget(), "current_step": Step.REFINING.value}


def _apply_edit(state: ResumeForgeState) -> dict[str, Any]:
    """Promote the user's hand-edit to the working copy, then re-evaluate it.

    A user can break their own LaTeX -- an unbalanced brace, a deleted
    `\\end{document}` -- and the guardrails cost nothing to run, so finding out
    here is strictly better than finding out from a pdflatex log.
    """
    return {
        **_revision_budget(),
        "refactored_latex": state.get("edited_latex") or "",
        "edited_latex": "",
        "current_step": Step.EVALUATING.value,
    }


def _reopen_keywords(state: ResumeForgeState) -> dict[str, Any]:
    """Send the session back to the top with the keyword gate reopened.

    `refactored_latex` is left in place as the fallback if the second pass fails,
    but `evaluation` is cleared so the refactorer runs in generate mode rather
    than trying to "correct" an output that was built from different keywords.
    """
    return {
        **_revision_budget(),
        "keywords_confirmed": False,
        "current_step": Step.SCRAPING.value,
    }
