"""Conditional edge functions -- where the graph's control flow lives.

Kept separate from the nodes so the routing rules can be tested as pure
functions on plain dicts, with no agent, no LLM, and no database.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)

AfterEvaluation = Literal["refactor_again", "human_review", "failed"]
AfterHumanReview = Literal["compile", "refactor_again", "evaluate_again", "extract_again"]


def has_blocking_errors(evaluation: dict) -> bool:
    """Only factual and structural failures are worth another LLM round trip.

    A low keyword-coverage score is a quality signal for the user to judge, not a
    correctness failure -- looping on it would burn tokens on a matter of taste.
    Unsupported factual claims and broken LaTeX are objectively wrong.
    """
    return bool(evaluation.get("factual_errors")) or bool(evaluation.get("structural_errors"))


def route_after_evaluation(state: ResumeForgeState) -> AfterEvaluation:
    """Self-correction loop, with a hard bound.

    **`max_iterations` is the cap on total refactor attempts, not extra retries.**
    The refactorer increments `iteration_count`, so `max_iterations=3` means at
    most 3 calls to the refactorer: one initial attempt plus up to 2 corrections.
    Stated explicitly because "3 retries" and "3 attempts" differ by a full LLM
    round trip, and the looser reading would let cost exceed the budget.

    Three outcomes:
      * clean, or out of retries  -> hand to the human
      * blocking errors, retries left -> targeted retry with feedback
      * no LaTeX to review at all -> fail rather than show an empty diff
    """
    if state.get("error") and not state.get("refactored_latex"):
        return "failed"

    evaluation = state.get("evaluation") or {}
    iteration = state.get("iteration_count", 0)
    limit = state.get("max_iterations", 3)

    if not has_blocking_errors(evaluation):
        return "human_review"

    if iteration >= limit:
        # Graceful degradation, not silent failure: the user still gets a resume,
        # with the outstanding problems attached as warnings. Looping forever on
        # a model that cannot satisfy the evaluator is the worse outcome.
        logger.warning(
            "Self-correction exhausted after %d iterations; degrading to human review",
            iteration,
        )
        return "human_review"

    return "refactor_again"


def route_after_human_review(state: ResumeForgeState) -> AfterHumanReview:
    """Dispatch on the user's decision at the review interrupt."""
    decision = state.get("user_decision")

    if decision == "accept":
        return "compile"
    if decision == "request_changes":
        return "refactor_again"
    if decision == "edit":
        # A hand-edited template still goes through the guardrails. The user can
        # break their own LaTeX, and finding out at compile time is worse.
        return "evaluate_again"
    if decision == "modify_keywords":
        return "extract_again"

    logger.warning("No user_decision at human review; treating as accept")
    return "compile"


def should_skip_scraping(state: ResumeForgeState) -> bool:
    """True when the user pasted the description, so Tier 1-3 are unnecessary."""
    return bool(state.get("job_text")) and not state.get("job_url")


def is_terminal(state: ResumeForgeState) -> bool:
    # Compares as strings: current_step is stored as a str, and Step is a StrEnum.
    return state.get("current_step") in {Step.COMPLETE.value, Step.FAILED.value}
