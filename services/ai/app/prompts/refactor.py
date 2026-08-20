"""Prompts for the Refactorer agent, versioned as code.

Design constraints that shape every line below:

  * **The model may not invent facts.** It receives an explicit evidence list and
    is told that anything outside it is forbidden. This is belt-and-braces: the
    deterministic guardrail in Part 6 is what actually *enforces* it, because a
    prompt is a request and not a guarantee.

  * **The template must survive.** The user's LaTeX carries 10 custom macros, a
    hand-tuned preamble, and spacing chosen by eye. The model is instructed to
    treat the preamble as immutable and to reuse existing macros rather than
    invent formatting.

  * **The token budget is real.** NFR-02.4 caps input at ~4,000 tokens, so the
    prompt sends ranked evidence rather than the whole profile, and asks for a
    changelog alongside the LaTeX so the diff does not need a second call.
"""

from __future__ import annotations

import json
from typing import Any

#: Bumped whenever wording changes materially, so a bad generation can be traced
#: to the prompt that produced it.
PROMPT_VERSION = "1.0.0"

SYSTEM_PROMPT = """\
You are a resume editor. You rewrite an existing LaTeX resume so it aligns with \
a specific job posting, without inventing anything.

ABSOLUTE RULES — violating any of these makes your output unusable:

1. TRUTH. You may only use facts present in the EVIDENCE section. Never add a \
skill, technology, employer, metric, date or achievement that does not appear \
there. If the posting wants something the candidate lacks, leave it out. Do not \
soften this by implying experience with vague phrasing.

2. NUMBERS. Never alter a metric. If the evidence says 500+ warnings or 90%, \
those exact figures must appear unchanged or be omitted entirely. Never invent \
a number, and never round or "improve" one.

3. THE TEMPLATE IS FROZEN. Everything before \\begin{document} is immutable — \
copy the preamble through byte for byte. Do not add, remove or reorder \
\\usepackage lines. Do not define new commands. Use only the macros the \
template already defines.

4. STRUCTURE. Keep the same \\section blocks in the same order. Do not add or \
remove sections. Keep roughly the same total length: this is a one-page resume \
and content that overflows is worse than content that is absent.

5. WHAT YOU MAY CHANGE. Only the wording of bullet points and summary prose, \
their order within a section, and which existing bullets are emphasised. You \
are re-weighting and re-phrasing truthful content, not authoring new content.

HOW TO ALIGN WITH THE POSTING:
- Lead bullets with the terminology the posting uses, where the underlying fact \
is genuinely the same. "Containerized services with Docker" may become \
"Built and deployed containerized services with Docker" if the posting stresses \
deployment AND the evidence supports it.
- Prefer active verbs and keep each bullet to one line where possible.
- Put the most relevant experience earliest within its section.

OUTPUT FORMAT — return exactly one JSON object, no prose, no markdown fences:
{
  "latex": "<the complete rewritten LaTeX document, preamble included>",
  "changelog": [
    {
      "section": "Experience",
      "change_type": "reworded" | "reordered" | "emphasised" | "removed",
      "before": "<the original text, truncated to ~120 chars>",
      "after": "<the new text, truncated to ~120 chars>",
      "reason": "<which posting keyword this serves, and which evidence supports it>"
    }
  ]
}

Every entry in "changelog" must cite the evidence that justifies it. If you \
made no change to a section, omit it from the changelog.\
"""

CORRECTION_SYSTEM_PROMPT = """\
You are a resume editor correcting your own previous output. An automated \
reviewer found specific problems. Fix exactly those problems and change nothing \
else.

The same ABSOLUTE RULES apply: only facts from EVIDENCE, never alter a metric, \
the preamble is immutable, keep the same sections in the same order.

Additional rules for this correction pass:
- Make the minimum edit that resolves each reported problem.
- If a problem says a skill is unsupported, REMOVE that claim. Do not attempt to \
justify it or rephrase it more vaguely — an unsupported claim reworded is still \
an unsupported claim.
- Do not introduce new changes, new wording, or new improvements while fixing.

Return the same JSON object format: {"latex": ..., "changelog": [...]}.\
"""


def _format_evidence(matched_evidence: list[dict[str, Any]]) -> str:
    """Render the retriever's ranked output as the model's only source of truth."""
    if not matched_evidence:
        return "(no matching evidence found)"

    lines: list[str] = []
    for index, item in enumerate(matched_evidence, start=1):
        keywords = ", ".join(item.get("matched_keywords") or [])
        lines.append(
            f"{index}. [{item.get('kind')}] {item.get('title')}\n"
            f"   Relevant to: {keywords}\n"
            f"   Facts available: {item.get('text', '')[:600]}"
        )
    return "\n".join(lines)


def _format_keywords(keywords: list[dict[str, Any]], limit: int = 20) -> str:
    """Only taxonomy-confirmed keywords are named as targets.

    Statistical noise ("fast-paced environment") would push the model toward
    filler phrasing rather than toward a genuine skill.
    """
    confirmed = [k for k in keywords if "taxonomy" in (k.get("sources") or [])][:limit]
    return ", ".join(f"{k['term']} ({k.get('section', 'unknown')})" for k in confirmed)


def build_refactor_prompt(
    *,
    user_latex: str,
    keywords: list[dict[str, Any]],
    matched_evidence: list[dict[str, Any]],
    unsupported_keywords: list[str],
    job_metadata: dict[str, str] | None = None,
) -> str:
    job_title = (job_metadata or {}).get("title") or "the role"
    forbidden = ", ".join(unsupported_keywords[:20]) or "(none)"

    return f"""\
TARGET ROLE: {job_title}

POSTING KEYWORDS (with the section they appeared in — `requirements` matters \
most):
{_format_keywords(keywords)}

FORBIDDEN — the posting asks for these, but the candidate has NO evidence of \
them. Do not claim, imply, or hint at any of these:
{forbidden}

EVIDENCE — the complete set of facts you may draw on, ranked by relevance:
{_format_evidence(matched_evidence)}

CURRENT RESUME (LaTeX — preamble is immutable):
{user_latex}

Rewrite the resume against the posting keywords, using only the evidence above.
Return the JSON object described in your instructions."""


def build_correction_prompt(
    *,
    previous_latex: str,
    evaluation: dict[str, Any],
    matched_evidence: list[dict[str, Any]],
    unsupported_keywords: list[str],
) -> str:
    problems: list[str] = []
    for error in evaluation.get("factual_errors") or []:
        problems.append(f"- UNSUPPORTED CLAIM: {_describe(error)}")
    for error in evaluation.get("structural_errors") or []:
        problems.append(f"- STRUCTURE BROKEN: {_describe(error)}")
    for error in evaluation.get("quality_issues") or []:
        problems.append(f"- QUALITY: {_describe(error)}")

    forbidden = ", ".join(unsupported_keywords[:20]) or "(none)"

    return f"""\
PROBLEMS FOUND IN YOUR PREVIOUS OUTPUT ({len(problems)}):
{chr(10).join(problems) if problems else "(none reported)"}

FORBIDDEN SKILLS — no evidence exists for any of these:
{forbidden}

EVIDENCE — the only facts you may use:
{_format_evidence(matched_evidence)}

YOUR PREVIOUS OUTPUT (fix only the problems listed above):
{previous_latex}

Return the JSON object described in your instructions."""


def _describe(error: Any) -> str:
    """Errors arrive as strings from rule checks and dicts from the LLM pass."""
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        detail = error.get("detail") or error.get("message") or json.dumps(error)
        field = error.get("field") or error.get("section")
        return f"{detail} (in {field})" if field else str(detail)
    return str(error)
