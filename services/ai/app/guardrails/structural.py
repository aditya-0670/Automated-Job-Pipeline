"""Deterministic checks that the user's template survived the rewrite.

Zero LLM tokens. Every question here is decidable by parsing:

  * Is the preamble byte-identical?
  * Are the same sections present, in the same order?
  * Are environments balanced, and are the template's own macros still defined?

A model asked "did you preserve the template?" will say yes. Parsing knows.

Why the preamble matters so much: the user chose their fonts, margins, colours
and 10 custom macros by eye. Silently altering them produces a resume that is
still *valid* LaTeX and no longer *their* resume -- the failure mode the product
exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Directives whose loss changes the document's appearance even when it still
#: compiles, so they are compared exactly rather than loosely.
_PREAMBLE_SIGNIFICANT = (
    r"\usepackage",
    r"\newcommand",
    r"\renewcommand",
    r"\definecolor",
    r"\titleformat",
    r"\documentclass",
    r"\setlength",
    r"\pagestyle",
)

DOCUMENT_START = r"\begin{document}"


@dataclass
class StructuralReport:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sections_before: list[str] = field(default_factory=list)
    sections_after: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "sections_before": self.sections_before,
            "sections_after": self.sections_after,
        }


def split_preamble(latex: str) -> tuple[str, str]:
    """Return (preamble, body). Body is empty if the marker is absent."""
    index = latex.find(DOCUMENT_START)
    if index == -1:
        return latex, ""
    return latex[:index], latex[index:]


def find_sections(latex: str) -> list[str]:
    return re.findall(r"\\section\*?\s*\{([^}]*)\}", latex)


def find_macro_definitions(latex: str) -> set[str]:
    """Names defined by \\newcommand / \\renewcommand / \\def."""
    names: set[str] = set()
    for pattern in (
        r"\\(?:re)?newcommand\s*\*?\s*\{?\\([a-zA-Z@]+)\}?",
        r"\\def\s*\\([a-zA-Z@]+)",
    ):
        names.update(re.findall(pattern, latex))
    return names


def find_macro_uses(latex: str) -> set[str]:
    return set(re.findall(r"\\([a-zA-Z@]+)", latex))


def _normalise(text: str) -> str:
    """Collapse whitespace so formatting differences are not treated as edits."""
    return re.sub(r"\s+", " ", text).strip()


def _significant_lines(preamble: str) -> list[str]:
    lines: list[str] = []
    for raw in preamble.splitlines():
        line = raw.split("%")[0].strip() if not raw.strip().startswith("%") else ""
        if line and any(directive in line for directive in _PREAMBLE_SIGNIFICANT):
            lines.append(_normalise(line))
    return lines


def check_structure(original: str, generated: str) -> StructuralReport:
    """Compare a generated document against the user's original."""
    errors: list[str] = []
    warnings: list[str] = []

    if not generated.strip():
        return StructuralReport(passed=False, errors=["the generated document is empty"])

    original_preamble, original_body = split_preamble(original)
    generated_preamble, generated_body = split_preamble(generated)

    if not generated_body:
        errors.append(r"\begin{document} is missing from the generated document")
    if r"\end{document}" not in generated:
        errors.append(r"\end{document} is missing from the generated document")

    # ── Preamble ──
    original_lines = _significant_lines(original_preamble)
    generated_lines = _significant_lines(generated_preamble)

    missing = [line for line in original_lines if line not in generated_lines]
    if missing:
        errors.append(
            f"{len(missing)} preamble directive(s) were removed or altered: "
            + "; ".join(m[:70] for m in missing[:4])
        )

    added = [line for line in generated_lines if line not in original_lines]
    if added:
        # An added \usepackage may not exist in the compiler image, and an added
        # \newcommand is the model inventing formatting rather than reusing it.
        errors.append(
            f"{len(added)} preamble directive(s) were added: "
            + "; ".join(a[:70] for a in added[:4])
        )

    # ── Sections ──
    sections_before = find_sections(original)
    sections_after = find_sections(generated)

    if sections_before != sections_after:
        removed = [s for s in sections_before if s not in sections_after]
        inserted = [s for s in sections_after if s not in sections_before]
        if removed:
            errors.append(f"section(s) removed: {', '.join(removed)}")
        if inserted:
            errors.append(f"section(s) added: {', '.join(inserted)}")
        if not removed and not inserted:
            errors.append(
                f"sections were reordered: {' > '.join(sections_before)} "
                f"became {' > '.join(sections_after)}"
            )

    # ── Environments ──
    for name, delta in _environment_imbalance(generated).items():
        errors.append(f"environment '{name}' is unbalanced (begin/end differ by {delta})")

    # ── Macros ──
    original_macros = find_macro_definitions(original)
    generated_macros = find_macro_definitions(generated)

    lost = original_macros - generated_macros
    if lost:
        errors.append(f"custom macro definition(s) lost: {', '.join(sorted(lost))}")

    undefined = [
        name
        for name in find_macro_uses(generated_body) - generated_macros
        if name in original_macros
    ]
    if undefined:
        errors.append(f"macro(s) used but no longer defined: {', '.join(sorted(set(undefined)))}")

    # ── Length ──
    # A body that has grown or shrunk sharply usually means content was invented
    # or dropped. A warning, not an error: the user may have asked for it.
    before, after = len(_normalise(original_body)), len(_normalise(generated_body))
    if before and after:
        ratio = after / before
        if ratio > 1.35:
            warnings.append(f"the body grew {ratio:.0%} -- it may no longer fit one page")
        elif ratio < 0.6:
            warnings.append(f"the body shrank to {ratio:.0%} of its original length")

    return StructuralReport(
        passed=not errors,
        errors=errors,
        warnings=warnings,
        sections_before=sections_before,
        sections_after=sections_after,
    )


def _environment_imbalance(latex: str) -> dict[str, int]:
    begins = re.findall(r"\\begin\s*\{([^}]+)\}", latex)
    ends = re.findall(r"\\end\s*\{([^}]+)\}", latex)
    imbalance: dict[str, int] = {}
    for name in set(begins) | set(ends):
        delta = begins.count(name) - ends.count(name)
        if delta:
            imbalance[name] = delta
    return imbalance
