"""Prompt for the Evaluator's quality pass.

Scope is deliberately narrow. Factual grounding and structural preservation are
already decided by rules before this prompt runs, so the model is asked only
about things rules cannot judge: whether a bullet reads well, whether the
rewrite actually speaks to the posting, and whether phrasing has drifted into
vagueness.

The model is told explicitly *not* to comment on facts. Inviting it to would
produce contradictory verdicts against the deterministic checker -- and when a
rule and a model disagree about a fact, the rule is right.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "1.0.0"

EVALUATOR_SYSTEM_PROMPT = """\
You are a resume reviewer. You judge writing quality and alignment with a job \
posting. You do NOT judge factual accuracy -- that has already been verified by \
a separate deterministic check, and second-guessing it would produce \
contradictory results.

Judge only:
1. CLARITY. Is each bullet specific and readable? Flag vague filler such as \
"leveraged synergies", "worked on various tasks", or "responsible for".
2. ALIGNMENT. Does the resume speak to the posting's priorities using the \
posting's own vocabulary, where the underlying facts genuinely match?
3. IMPACT. Do bullets lead with what was achieved rather than what was assigned?
4. CONSISTENCY. Is tense, person and formatting uniform across bullets?

Do NOT flag:
- Anything about whether a claim is true or supported.
- The absence of a skill the candidate does not have.
- LaTeX syntax, packages, or formatting commands.

Scoring: `keyword_coverage` is your estimate, from 0.0 to 1.0, of how well the \
resume addresses the posting's most important requirements *given what the \
candidate actually has*. A candidate genuinely missing half the requirements \
should still score well if the resume presents what they do have effectively.

Return exactly one JSON object, no prose, no markdown fences:
{
  "quality_issues": [
    {"section": "<section name>", "detail": "<what is wrong>", "suggestion": "<a concrete fix>"}
  ],
  "keyword_coverage": 0.0,
  "strengths": ["<what the rewrite did well>"],
  "feedback": "<one or two sentences summarising the verdict>"
}

Report at most 5 quality_issues, most important first. An empty list is a valid \
and expected result for a good rewrite.\
"""


def build_evaluation_prompt(
    *,
    generated_latex: str,
    keywords: list[dict[str, Any]],
    job_metadata: dict[str, str] | None = None,
    max_keywords: int = 15,
) -> str:
    from app.guardrails.factual import strip_latex

    title = (job_metadata or {}).get("title") or "the role"
    confirmed = [k for k in keywords if "taxonomy" in (k.get("sources") or [])][:max_keywords]
    keyword_line = ", ".join(k["term"] for k in confirmed) or "(none identified)"

    # The readable text, not the LaTeX. Sending markup would spend a third of the
    # prompt on macros the model has been told to ignore, and invites comments
    # about formatting that were explicitly ruled out of scope.
    readable = strip_latex(generated_latex)

    return f"""\
TARGET ROLE: {title}

THE POSTING'S KEY REQUIREMENTS:
{keyword_line}

THE REWRITTEN RESUME (text only; formatting has been stripped deliberately):
{readable}

Judge clarity, alignment, impact and consistency. Return the JSON object \
described in your instructions."""
