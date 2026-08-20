"""Deterministic anti-hallucination. Zero LLM tokens.

The central idea of the whole project: **the same Aho-Corasick automaton that
reads the job description is run against the generated resume.** Every skill it
finds must appear in the evidence set the retriever built from the user's
profile. What remains is a set difference.

Why not ask a model to check. A model asked "did you invent anything?" is being
asked to detect its own hallucination — trusting a probabilistic component to
police itself. Set membership is exact, reproducible, costs nothing, and can be
shown to a user as provenance: "you claim Kubernetes; nothing in your profile
mentions it."

Two further checks that are also decidable without a model:

  * **Metrics.** Numbers in the output must exist in the profile or the original
    resume. A model that turns "500+ warnings" into "800+ warnings" has committed
    the most damaging possible error, and a diff of numbers catches it exactly.
  * **Employers.** Company and institution names cannot be invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.extraction.aho import TaxonomyMatcher, get_matcher
from app.matching.profile_index import ProfileIndex

#: Numbers below this are prose ("3 years", "one page"), not claims worth
#: policing, and treating them as metrics produces constant false positives.
MIN_SIGNIFICANT_NUMBER = 3

#: Skills a resume may name without profile evidence, because they describe the
#: document or the process rather than a capability being claimed.
_ALWAYS_ALLOWED: frozenset[str] = frozenset({"LaTeX"})


@dataclass
class FactualReport:
    passed: bool
    unsupported_skills: list[dict[str, Any]] = field(default_factory=list)
    altered_metrics: list[dict[str, Any]] = field(default_factory=list)
    invented_employers: list[str] = field(default_factory=list)
    supported_count: int = 0

    @property
    def errors(self) -> list[str]:
        """One actionable sentence per problem, for the correction prompt."""
        messages: list[str] = []
        for item in self.unsupported_skills:
            messages.append(
                f"The resume claims '{item['skill']}' (as \"{item['matched_text']}\"), "
                f"but nothing in the profile evidences it. Remove this claim."
            )
        for item in self.altered_metrics:
            messages.append(
                f"The number '{item['value']}' does not appear in the profile or the "
                f"original resume. Never invent or alter a metric."
            )
        for employer in self.invented_employers:
            messages.append(f"'{employer}' is not an employer in the profile. Remove it.")
        return messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "unsupported_skills": self.unsupported_skills,
            "altered_metrics": self.altered_metrics,
            "invented_employers": self.invented_employers,
            "supported_count": self.supported_count,
            "errors": self.errors,
        }


def strip_latex(latex: str) -> str:
    r"""Reduce LaTeX to its readable text.

    Necessary because the checks must see what a *reader* sees. Without this,
    macro names and package names look like content: `\usepackage{tabularx}`
    would read as a claim about tabularx, and `\href{...github.com/x}` would
    read as a claim about GitHub.
    """
    text = re.sub(r"(?m)^\s*%.*$", "", latex)
    _, _, body = text.partition(r"\begin{document}")
    text = body or text

    # Drop the preamble-like directives that can still appear in a body.
    text = re.sub(
        r"\\(?:usepackage|documentclass|input|include)\s*(\[[^\]]*\])?\{[^}]*\}", " ", text
    )
    text = re.sub(r"\\(?:re)?newcommand\s*\*?\s*\{?\\[a-zA-Z@]+\}?(\[\d+\])?", " ", text)
    # URLs name technologies without claiming them (github.com, linkedin.com).
    text = re.sub(r"\\href\s*\{[^}]*\}", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\\faIcon\s*\{[^}]*\}", " ", text)
    # Keep macro *arguments* (the visible text), discard the macro names.
    text = re.sub(r"\\(?:begin|end)\s*\{[^}]*\}(\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\[a-zA-Z@]+\s*(\[[^\]]*\])?", " ", text)
    text = text.replace("{", " ").replace("}", " ").replace("$", " ")
    text = re.sub(r"\\[^a-zA-Z]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_numbers(text: str) -> set[str]:
    """Metric-like numbers: percentages, multipliers, counts, ratings.

    Returned as normalised strings so "500+" and "500" compare equal -- a model
    dropping a plus sign is not the failure being hunted here.
    """
    found: set[str] = set()
    for match in re.finditer(r"(\d[\d,]*\.?\d*)\s*(%|x|X|\+)?", text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if value < MIN_SIGNIFICANT_NUMBER:
            continue
        found.add(raw.rstrip(".") if "." not in raw else raw)
    return found


def check_facts(
    *,
    generated_latex: str,
    profile: dict[str, Any],
    original_latex: str = "",
    matched_evidence: list[dict[str, Any]] | None = None,
    matcher: TaxonomyMatcher | None = None,
) -> FactualReport:
    """Verify every claim in the generated resume traces back to the profile."""
    matcher = matcher or get_matcher()
    index = ProfileIndex(profile, matcher=matcher)
    readable = strip_latex(generated_latex)

    # ── Skills ──
    unsupported: list[dict[str, Any]] = []
    supported = 0
    for skill, occurrences in matcher.find_skills(readable).items():
        if skill in _ALWAYS_ALLOWED or index.supports(skill):
            supported += 1
            continue
        first = occurrences[0]
        unsupported.append(
            {
                "skill": skill,
                "matched_text": first.matched_text,
                "context": readable[max(0, first.start - 60) : first.end + 60].strip(),
            }
        )

    # ── Metrics ──
    # The permitted set is the profile plus the ORIGINAL resume: a number the
    # user already published is theirs, even if the profile phrases it elsewhere.
    permitted = extract_numbers(_profile_text(profile))
    if original_latex:
        permitted |= extract_numbers(strip_latex(original_latex))

    altered = [
        {"value": number, "context": _number_context(readable, number)}
        for number in sorted(extract_numbers(readable) - permitted)
    ]

    # ── Employers ──
    known_employers = {
        (exp.get("company") or "").lower() for exp in profile.get("experiences") or []
    } | {(edu.get("institution") or "").lower() for edu in profile.get("education") or []}
    invented = [
        candidate
        for candidate in _candidate_employers(readable)
        if candidate.lower() not in known_employers
    ]

    return FactualReport(
        passed=not (unsupported or altered or invented),
        unsupported_skills=unsupported,
        altered_metrics=altered,
        invented_employers=invented,
        supported_count=supported,
    )


def _profile_text(profile: dict[str, Any]) -> str:
    """Every string in the profile, flattened. Used for number comparison."""
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float)):
            parts.append(str(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(profile)
    return " ".join(parts)


def _number_context(text: str, number: str) -> str:
    position = text.find(number)
    if position == -1:
        return ""
    return text[max(0, position - 50) : position + 50].strip()


def _candidate_employers(text: str) -> list[str]:
    """Very conservative: only the corporate-suffix pattern.

    Detecting invented company names in free text generally is not tractable
    without a model, and this check must stay deterministic. Restricting it to
    an explicit legal suffix means it almost never fires falsely, and the
    unsupported-skills check is the primary defence regardless.
    """
    pattern = (
        r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*)?\s+(?:Inc|LLC|Ltd|GmbH|Corp)\b)"
    )
    return sorted({match.strip() for match in re.findall(pattern, text)})
