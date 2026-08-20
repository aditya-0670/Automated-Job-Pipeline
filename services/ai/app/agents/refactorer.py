"""Node 3 — Resume Refactorer. The first real LLM call.

Two modes, one node:
  * **generate** — first attempt, from evidence and keywords.
  * **correct**  — a retry driven by the Evaluator's structured feedback.

The correction mode is the point of the self-correction loop. It sends the
previous output plus the specific problems, so the model makes a targeted edit
instead of regenerating from scratch. That is both cheaper and less likely to
introduce a *new* error while fixing an old one.

Token discipline is enforced here rather than hoped for: the assembled prompt is
measured and evidence is dropped from the tail until it fits, because sending an
over-budget prompt and paying for the failure is worse than sending slightly less
context.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.clients.llm import LLMError, LLMProvider, TokenLedger, get_llm
from app.config import get_settings
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step
from app.prompts.refactor import (
    CORRECTION_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_correction_prompt,
    build_refactor_prompt,
)

logger = logging.getLogger(__name__)

#: Rough characters-per-token for English plus LaTeX. Used only to decide how
#: much evidence fits; the authoritative count comes back from the API.
CHARS_PER_TOKEN = 3.6

#: NFR-02.4. Deliberately a budget for the *assembled* prompt, not a hope.
MAX_INPUT_TOKENS = 4000

#: Never drop below this much evidence -- a resume rewritten against two facts is
#: worse than one that slightly overspends.
MIN_EVIDENCE_ITEMS = 3


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def fit_evidence_to_budget(
    build_prompt,
    evidence: list[dict[str, Any]],
    *,
    max_tokens: int = MAX_INPUT_TOKENS,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Drop the least-relevant evidence until the prompt fits the budget.

    Evidence arrives ranked, so truncating from the tail removes the least
    relevant material first. Returns the prompt, the evidence actually used, and
    whether truncation happened -- the caller records that as a warning, since a
    silently shortened prompt is a silently worse resume.
    """
    used = list(evidence)
    while True:
        prompt = build_prompt(used)
        if estimate_tokens(prompt) <= max_tokens or len(used) <= MIN_EVIDENCE_ITEMS:
            return prompt, used, len(used) < len(evidence)
        used.pop()


async def refactorer_agent(
    state: ResumeForgeState,
    *,
    llm: LLMProvider | None = None,
) -> dict[str, Any]:
    """Rewrite the resume, or correct a previous attempt."""
    settings = get_settings()
    llm = llm or get_llm()

    user_latex = state.get("user_latex") or ""
    evidence = list(state.get("matched_evidence") or [])
    keywords = list(state.get("keywords") or [])
    unsupported = list(state.get("unsupported_keywords") or [])
    evaluation = state.get("evaluation") or {}
    iteration = state.get("iteration_count", 0)

    if not user_latex.strip():
        return _failure(state, "No resume template found. Upload your LaTeX resume first.")
    if not evidence:
        return _failure(
            state,
            "No relevant experience was found in your profile for this posting. "
            "Add more detail to your experiences and projects, then try again.",
        )

    # ── Choose mode ──
    previous_latex = state.get("refactored_latex") or ""
    user_request = (state.get("user_change_request") or "").strip()
    correcting = bool(previous_latex) and (bool(evaluation) or bool(user_request))

    if correcting:
        system = CORRECTION_SYSTEM_PROMPT
        step = Step.REFINING if user_request else Step.CORRECTING

        def build(used_evidence: list[dict[str, Any]]) -> str:
            prompt = build_correction_prompt(
                previous_latex=previous_latex,
                evaluation=evaluation,
                matched_evidence=used_evidence,
                unsupported_keywords=unsupported,
            )
            if user_request:
                # A user instruction is a requirement, not a suggestion, so it
                # goes last where it is least likely to be diluted by context.
                prompt += f"\n\nTHE USER ALSO ASKS: {user_request}"
            return prompt
    else:
        system = SYSTEM_PROMPT
        step = Step.REFACTORING

        def build(used_evidence: list[dict[str, Any]]) -> str:
            return build_refactor_prompt(
                user_latex=user_latex,
                keywords=keywords,
                matched_evidence=used_evidence,
                unsupported_keywords=unsupported,
                job_metadata=state.get("job_metadata"),
            )

    prompt, used_evidence, truncated = fit_evidence_to_budget(build, evidence)
    warnings = list(state.get("warnings") or [])
    if truncated:
        warnings.append(
            f"Only the {len(used_evidence)} most relevant profile items were used, "
            f"to stay within the token budget."
        )

    logger.info(
        "Refactorer %s: %d evidence items, ~%d input tokens, iteration %d",
        "correcting" if correcting else "generating",
        len(used_evidence),
        estimate_tokens(prompt) + estimate_tokens(system),
        iteration,
    )

    # ── Call the model ──
    try:
        payload, response = await llm.complete_json(system=system, user=prompt)
    except LLMError as exc:
        # Not fatal on a correction pass: the previous output still exists and is
        # better than nothing, so degrade to review with a warning.
        if correcting and previous_latex:
            warnings.append(f"Could not apply corrections ({exc}). Showing the previous version.")
            return {
                "warnings": warnings,
                "current_step": Step.EVALUATING.value,
                "events": [
                    step_event(state, Step.EVALUATING, detail="Correction failed; continuing")
                ],
            }
        return _failure(state, f"The resume could not be generated: {exc}")

    latex = (payload.get("latex") or "").strip()
    if not latex:
        return _failure(state, "The model returned no LaTeX document.")

    latex = _unescape_if_needed(latex)

    ledger = TokenLedger()
    ledger.entries = list((state.get("token_ledger") or {}).get("by_step") or [])
    ledger.record("refactor_correction" if correcting else "refactor", response)

    changelog = payload.get("changelog") or []
    if not isinstance(changelog, list):
        changelog = []

    logger.info(
        "Refactored: %d chars, %d changelog entries, %d in / %d out / %d thinking tokens",
        len(latex),
        len(changelog),
        response.input_tokens,
        response.output_tokens,
        response.thinking_tokens,
    )

    return {
        "refactored_latex": latex,
        "changelog": changelog,
        "iteration_count": iteration + 1,
        "current_step": step.value,
        "token_ledger": ledger.to_dict(),
        "warnings": warnings,
        "error": None,
        # Cleared so a stale verdict cannot route the next hop.
        "evaluation": {},
        "user_change_request": "",
        "events": [
            step_event(
                state,
                step,
                detail=(
                    f"Applied {len(changelog)} changes"
                    if not correcting
                    else f"Corrected {len(changelog)} items"
                ),
                data={
                    "prompt_version": PROMPT_VERSION,
                    "model": response.model,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.billed_output_tokens,
                    "evidence_used": len(used_evidence),
                    "iteration": iteration + 1,
                },
            )
        ],
    }


def _unescape_if_needed(latex: str) -> str:
    r"""Repair LaTeX that survived a JSON round trip badly.

    Models sometimes emit `\\documentclass` where `\documentclass` was meant --
    correct JSON escaping applied twice. Detected by the absence of any real
    control sequence alongside the presence of doubled backslashes.
    """
    # Order matters: "\documentclass" is a substring of "\\documentclass", so
    # checking for the single-backslash form first would always match and the
    # repair would never run. Check the doubled form first.
    if r"\\documentclass" in latex or r"\\begin{document}" in latex:
        logger.warning("Model double-escaped the LaTeX; unescaping")
        return latex.replace("\\\\", "\\")
    return latex


def _describe_sections(latex: str) -> list[str]:
    return re.findall(r"\\section\{([^}]*)\}", latex)


def _failure(state: ResumeForgeState, message: str) -> dict[str, Any]:
    logger.warning("Refactorer failed: %s", message)
    return {
        "error": message,
        "current_step": Step.FAILED.value,
        "events": [step_event(state, Step.FAILED, detail=message)],
    }
