r"""Prompts for the Refactorer agent, versioned as code.

Design constraints that shape every line below:

  * **The model may not invent facts.** It receives an explicit evidence list and
    is told that anything outside it is forbidden. This is belt-and-braces: the
    deterministic guardrail in Part 6 is what actually *enforces* it, because a
    prompt is a request and not a guarantee.

  * **The template survives structurally, not by instruction.** The model never
    sees the preamble and is never asked to reproduce it. It returns only the
    document *body*, which is reassembled onto the user's original preamble.
    Corrupting 12 \usepackage lines and 10 custom macro definitions is therefore
    not something the model *can* do, rather than something it is asked not to.

    An earlier version requested the whole document. It cost roughly twice the
    output tokens, and a fallback model truncated mid-JSON on it -- an entire
    resume escaped inside a JSON string is a large thing to ask for.

  * **The token budget is real.** NFR-02.4 caps input at ~4,000 tokens, so the
    prompt sends ranked evidence rather than the whole profile, a macro *list*
    rather than the preamble, and asks for a changelog alongside the body so the
    diff does not need a second call.

  * **The response is delimiter-framed, not JSON.** This is a correctness
    decision, not a style one. LaTeX and JSON both use the backslash as their
    escape character, so `\section` inside a JSON string has to be written
    `\\section` -- and models get that wrong often enough to break generation
    intermittently. Worse, `\v`, `\b` and `\f` are *valid* JSON escapes with
    entirely different meanings, so some malformed output parses successfully
    into corrupted LaTeX. Framing the response with unambiguous markers means
    the LaTeX needs no escaping at all and the failure mode disappears.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Bumped whenever wording changes materially, so a bad generation can be traced
#: to the prompt that produced it.
PROMPT_VERSION = "2.0.0"

DOCUMENT_START = r"\begin{document}"
DOCUMENT_END = r"\end{document}"

SYSTEM_PROMPT = """\
You are a resume editor. You rewrite the BODY of an existing LaTeX resume so it \
aligns with a specific job posting, without inventing anything.

You never see or return the document preamble -- it is reattached \
automatically. Return only what sits between \\begin{document} and \
\\end{document}, exclusive.

ABSOLUTE RULES -- violating any of these makes your output unusable:

1. TRUTH. You may only use facts present in the EVIDENCE section. Never add a \
skill, technology, employer, metric, date or achievement that does not appear \
there. If the posting wants something the candidate lacks, leave it out. Do not \
soften this by implying experience with vague phrasing.

2. NUMBERS. Never alter a metric. If the evidence says 500+ warnings or 90%, \
those exact figures must appear unchanged or be omitted entirely. Never invent \
a number, and never round or "improve" one.

3. USE ONLY EXISTING MACROS. A list of the macros this template defines is \
supplied below. Use those and standard LaTeX only. Never define a new command, \
never use a macro absent from the list, and never emit \\usepackage, \
\\documentclass, \\begin{document} or \\end{document}.

4. STRUCTURE. Keep the same \\section blocks in the same order. Do not add or \
remove sections. Keep roughly the same total length: this is a one-page resume, \
and content that overflows is worse than content that is absent.

5. WHAT YOU MAY CHANGE. Only the wording of bullet points and summary prose, \
their order within a section, and which existing bullets are emphasised. You \
are re-weighting and re-phrasing truthful content, not authoring new content.

HOW TO ALIGN WITH THE POSTING:
- Lead bullets with the terminology the posting uses, where the underlying fact \
is genuinely the same.
- Prefer active verbs and keep each bullet to one line where possible.
- Put the most relevant experience earliest within its section.

OUTPUT FORMAT -- reply with exactly these two blocks and nothing else. No JSON, \
no markdown fences, no commentary.

===BODY===
<the rewritten document body, as raw LaTeX. Write it exactly as it should appear \
in the file. Do NOT escape backslashes: write \\section, not \\\\section.>
===CHANGELOG===
<one change per line, four fields separated by a pipe character:
SECTION | CHANGE_TYPE | WHAT CHANGED | WHY, citing the keyword and the evidence
CHANGE_TYPE is one of: reworded, reordered, emphasised, removed.
Leave this block empty if you changed nothing.>

Example changelog line:
Experience | reworded | Led with containerization work | Posting requires Docker; \
evidence item 1 shows Docker in the pipeline project\
"""

CORRECTION_SYSTEM_PROMPT = """\
You are a resume editor correcting your own previous output. An automated \
reviewer found specific problems. Fix exactly those problems and change nothing \
else.

The same ABSOLUTE RULES apply: only facts from EVIDENCE, never alter a metric, \
use only the macros the template already defines, keep the same sections in the \
same order, and return the body only.

Additional rules for this correction pass:
- Make the minimum edit that resolves each reported problem.
- If a problem says a skill is unsupported, REMOVE that claim. Do not attempt to \
justify it or rephrase it more vaguely -- an unsupported claim reworded is still \
an unsupported claim.
- Do not introduce new changes, new wording, or new improvements while fixing.

Use the same ===BODY=== / ===CHANGELOG=== format as before. Raw LaTeX, no JSON, \
no escaped backslashes.\
"""


# ── Document surgery ─────────────────────────────────────────────────────
def extract_body(latex: str) -> str:
    """The text between the document markers, exclusive."""
    start = latex.find(DOCUMENT_START)
    end = latex.rfind(DOCUMENT_END)
    if start == -1 or end == -1 or end < start:
        return latex.strip()
    return latex[start + len(DOCUMENT_START) : end].strip()


def reassemble(original_latex: str, body: str) -> str:
    """Put a generated body back onto the user's own preamble.

    This is where the template guarantee is enforced rather than requested: the
    preamble is copied verbatim from the user's file, so no model output can
    alter it.
    """
    start = original_latex.find(DOCUMENT_START)
    if start == -1:
        # Nothing to anchor on. Return the model's text rather than fabricating
        # a preamble the user never wrote.
        return body.strip()

    preamble = original_latex[:start]
    cleaned = body.strip()
    # Models sometimes include the wrapper despite instructions.
    if cleaned.startswith(DOCUMENT_START):
        cleaned = cleaned[len(DOCUMENT_START) :].strip()
    if cleaned.endswith(DOCUMENT_END):
        cleaned = cleaned[: -len(DOCUMENT_END)].strip()

    return f"{preamble}{DOCUMENT_START}\n\n{cleaned}\n\n{DOCUMENT_END}\n"


def describe_macros(latex: str) -> str:
    r"""List the macros the template defines, with their arity.

    Sent instead of the preamble itself: the model needs to know what it may
    use, not how those macros are implemented. Cheaper in input tokens, and it
    removes any temptation to "improve" a definition.
    """
    described: list[str] = []
    pattern = r"\\(?:re)?newcommand\s*\*?\s*\{?\\([a-zA-Z@]+)\}?\s*(?:\[(\d+)\])?"
    for match in re.finditer(pattern, latex):
        name, arity = match.group(1), match.group(2)
        described.append("\\" + name + ("{...}" * int(arity) if arity else ""))
    return ", ".join(described) or "(none defined)"


# ── Prompt assembly ──────────────────────────────────────────────────────
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

POSTING KEYWORDS (with the section they appeared in; `requirements` matters most):
{_format_keywords(keywords)}

FORBIDDEN -- the posting asks for these, but the candidate has NO evidence of
them. Do not claim, imply, or hint at any of these:
{forbidden}

EVIDENCE -- the complete set of facts you may draw on, ranked by relevance:
{_format_evidence(matched_evidence)}

MACROS THIS TEMPLATE DEFINES (use these; define nothing new):
{describe_macros(user_latex)}

CURRENT RESUME BODY (rewrite this; the preamble is handled for you):
{extract_body(user_latex)}

Rewrite the body against the posting keywords, using only the evidence above.
Reply with the ===BODY=== and ===CHANGELOG=== blocks described in your
instructions."""


def build_correction_prompt(
    *,
    previous_latex: str,
    evaluation: dict[str, Any],
    matched_evidence: list[dict[str, Any]],
    unsupported_keywords: list[str],
    user_latex: str = "",
) -> str:
    problems: list[str] = []
    for error in evaluation.get("factual_errors") or []:
        problems.append(f"- UNSUPPORTED CLAIM: {_describe(error)}")
    for error in evaluation.get("structural_errors") or []:
        problems.append(f"- STRUCTURE BROKEN: {_describe(error)}")
    for error in evaluation.get("quality_issues") or []:
        problems.append(f"- QUALITY: {_describe(error)}")

    forbidden = ", ".join(unsupported_keywords[:20]) or "(none)"
    macros = describe_macros(user_latex or previous_latex)

    return f"""\
PROBLEMS FOUND IN YOUR PREVIOUS OUTPUT ({len(problems)}):
{chr(10).join(problems) if problems else "(none reported)"}

FORBIDDEN SKILLS -- no evidence exists for any of these:
{forbidden}

EVIDENCE -- the only facts you may use:
{_format_evidence(matched_evidence)}

MACROS THIS TEMPLATE DEFINES (use these; define nothing new):
{macros}

YOUR PREVIOUS OUTPUT, body only (fix only the problems listed above):
{extract_body(previous_latex)}

Reply with the ===BODY=== and ===CHANGELOG=== blocks described in your
instructions."""


BODY_MARKER = "===BODY==="
CHANGELOG_MARKER = "===CHANGELOG==="


def parse_response(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse a delimiter-framed response into (body, changelog).

    Falls back to JSON so that a model which ignores the format instruction, or
    a cached response from the older prompt version, still works.
    """
    if BODY_MARKER in text:
        after_body = text.split(BODY_MARKER, 1)[1]
        if CHANGELOG_MARKER in after_body:
            body, _, changelog_text = after_body.partition(CHANGELOG_MARKER)
        else:
            body, changelog_text = after_body, ""
        return body.strip(), _parse_changelog_lines(changelog_text)

    # Fallback: the older JSON contract.
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if not fenced:
            return "", []
        try:
            payload = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            return "", []

    if not isinstance(payload, dict):
        return "", []
    body = payload.get("body") or payload.get("latex") or ""
    raw = payload.get("changelog")
    changelog = [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
    return str(body).strip(), changelog


def _parse_changelog_lines(text: str) -> list[dict[str, Any]]:
    """Pipe-delimited lines to changelog dicts. Malformed lines are skipped."""
    entries: list[dict[str, Any]] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<") or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in stripped.split("|")]
        if len(fields) < 3:
            continue
        section, change_type, *rest = fields
        entries.append(
            {
                "section": section,
                "change_type": change_type.lower(),
                "after": rest[0] if rest else "",
                "reason": rest[1] if len(rest) > 1 else "",
            }
        )
    return entries


def _describe(error: Any) -> str:
    """Errors arrive as strings from rule checks and dicts from the LLM pass."""
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        detail = error.get("detail") or error.get("message") or json.dumps(error)
        field = error.get("field") or error.get("section")
        return f"{detail} (in {field})" if field else str(detail)
    return str(error)
