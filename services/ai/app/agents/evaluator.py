"""Node 4 — Evaluator. The quality gate, and the reason "fault-tolerant" is true.

Order matters and is the design:

  1. **Structural rules** (0 tokens) — did the template survive?
  2. **Factual rules** (0 tokens) — does every claim trace to evidence?
  3. **LLM pass** (tokens) — only if the rules passed, and only for judgement
     that rules cannot make.

Running the rules first is not merely an optimisation. If the output already
contains a hallucinated skill, the correct next action is a targeted retry, and
asking a model about tone first would spend tokens on output that is going to be
regenerated anyway. It also means the anti-hallucination guarantee never depends
on a model call succeeding.
"""

from __future__ import annotations

import logging
from typing import Any

from app import metrics
from app.clients.llm import LLMError, LLMProvider, TokenLedger, get_llm
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step
from app.guardrails.factual import check_facts
from app.guardrails.structural import check_structure
from app.prompts.evaluate import (
    EVALUATOR_SYSTEM_PROMPT,
    build_evaluation_prompt,
)

logger = logging.getLogger(__name__)

#: Below this, the rewrite is not serving the posting well enough to be worth
#: showing without comment -- but it is a warning, never a retry trigger.
#: Looping on a taste judgement would burn tokens without a defined endpoint.
LOW_COVERAGE_THRESHOLD = 0.45

#: The quality pass is cheap reasoning; a large thinking budget here buys little.
EVALUATOR_THINKING_BUDGET = 256


async def evaluator_agent(
    state: ResumeForgeState,
    *,
    llm: LLMProvider | None = None,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """Evaluate the generated resume. Rules first, model second."""
    generated = state.get("refactored_latex") or state.get("edited_latex") or ""
    original = state.get("user_latex") or ""
    profile = state.get("user_profile") or {}
    keywords = list(state.get("keywords") or [])
    evidence = list(state.get("matched_evidence") or [])

    if not generated.strip():
        return {
            "error": "There is no generated resume to evaluate.",
            "current_step": Step.FAILED.value,
            "events": [step_event(state, Step.FAILED, detail="Nothing to evaluate")],
        }

    # ── 1 & 2: deterministic checks ──
    structural = check_structure(original, generated)
    factual = check_facts(
        generated_latex=generated,
        profile=profile,
        original_latex=original,
        matched_evidence=evidence,
    )

    factual_errors = factual.errors
    structural_errors = list(structural.errors)
    blocking = bool(factual_errors or structural_errors)

    # Counted by kind, because they mean different things: a rise in factual
    # failures says the model started claiming things the evidence does not
    # support, while structural failures say it damaged the template. Neither
    # shows up in an error rate, because the pipeline *handles* both -- which is
    # exactly why they need their own signal.
    if factual_errors:
        metrics.guardrail_failures.labels("factual").inc(len(factual_errors))
    if structural_errors:
        metrics.guardrail_failures.labels("structural").inc(len(structural_errors))
    metrics.self_corrections.observe(max(1, state.get("iteration_count", 1)))

    logger.info(
        "Evaluator rules: %d structural, %d factual errors (%d supported skills, 0 LLM tokens)",
        len(structural_errors),
        len(factual_errors),
        factual.supported_count,
    )

    quality_issues: list[dict[str, Any]] = []
    coverage: float | None = None
    strengths: list[str] = []
    feedback_parts: list[str] = []
    ledger = TokenLedger()
    ledger.entries = list((state.get("token_ledger") or {}).get("by_step") or [])

    # ── 3: the LLM pass, only when there is nothing blocking to fix ──
    if blocking:
        feedback_parts.append(
            f"Blocking issues found: {len(factual_errors)} factual, "
            f"{len(structural_errors)} structural. Skipped the quality review."
        )
        logger.info("Skipping LLM quality pass -- blocking errors will trigger a retry")
    elif skip_llm:
        feedback_parts.append("Quality review skipped by request.")
    else:
        llm = llm or get_llm()
        try:
            payload, response = await llm.complete_json(
                system=EVALUATOR_SYSTEM_PROMPT,
                user=build_evaluation_prompt(
                    generated_latex=generated,
                    keywords=keywords,
                    job_metadata=state.get("job_metadata"),
                ),
                thinking_budget=EVALUATOR_THINKING_BUDGET,
            )
            ledger.record("evaluate", response)
            raw_issues = payload.get("quality_issues")
            quality_issues = (
                [i for i in raw_issues if isinstance(i, dict)]
                if isinstance(raw_issues, list)
                else []
            )
            coverage = _coerce_coverage(payload.get("keyword_coverage"))
            strengths = [s for s in (payload.get("strengths") or []) if isinstance(s, str)]
            if payload.get("feedback"):
                feedback_parts.append(str(payload["feedback"]))
        except LLMError as exc:
            # A failed quality review must not fail the pipeline: the
            # deterministic checks already passed, so the resume is factually
            # sound and structurally intact. Quality is advisory.
            logger.warning("Quality review unavailable: %s", exc)
            feedback_parts.append("The quality review could not be completed.")

    warnings = list(state.get("warnings") or [])
    warnings.extend(structural.warnings)
    if coverage is not None and coverage < LOW_COVERAGE_THRESHOLD:
        warnings.append(
            f"This resume addresses about {coverage:.0%} of the posting's priorities. "
            f"The gap is mostly experience you do not have yet."
        )

    evaluation = {
        "passed": not blocking,
        # Named so `has_blocking_errors()` in routing reads these two keys only.
        "factual_errors": factual_errors,
        "structural_errors": structural_errors,
        "quality_issues": quality_issues,
        "keyword_coverage": coverage,
        "strengths": strengths,
        "feedback": " ".join(feedback_parts).strip(),
        "details": {
            "structural": structural.to_dict(),
            "factual": factual.to_dict(),
        },
    }

    return {
        "evaluation": evaluation,
        "evaluator_feedback": evaluation["feedback"],
        "current_step": Step.EVALUATING.value,
        "token_ledger": ledger.to_dict(),
        "warnings": warnings,
        "error": None,
        "events": [
            step_event(
                state,
                Step.EVALUATING,
                detail=(
                    "Passed all checks"
                    if not blocking
                    else f"Found {len(factual_errors) + len(structural_errors)} blocking issue(s)"
                ),
                data={
                    "passed": not blocking,
                    "factual_errors": len(factual_errors),
                    "structural_errors": len(structural_errors),
                    "quality_issues": len(quality_issues),
                    "keyword_coverage": coverage,
                    "iteration": state.get("iteration_count", 0),
                },
            )
        ],
    }


def _coerce_coverage(value: Any) -> float | None:
    """Models return 0.82, "0.82", or 82. Normalise, or discard."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))
