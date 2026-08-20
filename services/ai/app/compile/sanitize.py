"""LaTeX input validation before compilation.

TeX is a full programming language with filesystem and shell access. Compiling
user-supplied LaTeX is therefore arbitrary code execution unless constrained.
Defence is in three layers, and this module is only the first:

  1. **This module** -- reject dangerous primitives outright (fail fast, with a
     message naming what was rejected).
  2. **pdflatex flags** -- `-no-shell-escape` plus a restricted `openout_any`,
     so even a missed primitive cannot execute a subprocess.
  3. **The container** -- non-root user, no network, temp-directory-only writes,
     hard timeout.

A denylist alone would be weak. Layered behind two independent controls that do
not depend on enumerating every attack, it is reasonable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Primitives with no legitimate place in a resume, ordered by severity.
#: The value explains the risk, and is shown to the user -- a rejection the user
#: cannot understand is indistinguishable from a broken product.
FORBIDDEN_PRIMITIVES: dict[str, str] = {
    r"\write18": "shell command execution",
    r"\immediate\write18": "shell command execution",
    r"\input": "reads arbitrary files from the filesystem",
    r"\include": "reads arbitrary files from the filesystem",
    r"\openin": "opens files for reading",
    r"\openout": "writes arbitrary files",
    r"\read": "reads from a file stream",
    r"\catcode": "redefines character semantics, which can bypass this check",
    r"\csname": "constructs command names dynamically, bypassing static checks",
    r"\expandafter\csname": "constructs command names dynamically",
    r"\directlua": "executes Lua code",
    r"\latelua": "executes Lua code",
    r"\pdfshellescape": "queries or enables shell escape",
    r"\usepackage{shellesc}": "enables shell escape",
    r"\ShellEscape": "shell command execution",
    r"\special": "passes raw commands to the output driver",
}

#: `\input{glyphtounicode}` is idiomatic in resume templates -- it improves PDF
#: text extraction for ATS parsers, which is the entire point of this product.
#: Allowed only for this specific, known-safe argument.
INPUT_ALLOWLIST: frozenset[str] = frozenset({"glyphtounicode", "glyphtounicode.tex"})

MAX_LATEX_CHARS = 200_000
MAX_MACRO_DEPTH = 30


@dataclass
class SanitizeResult:
    ok: bool
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        if self.ok:
            return "LaTeX passed validation"
        return "This LaTeX cannot be compiled: " + "; ".join(self.rejections)


def _strip_comments(latex: str) -> str:
    """Remove comments so a commented-out primitive is not flagged.

    An escaped percent (`\\%`) is a literal, not a comment start -- getting this
    wrong would truncate any line containing a percentage, and resumes are full
    of them ("reduced costs by 60\\%").
    """
    out: list[str] = []
    for line in latex.splitlines():
        result: list[str] = []
        escaped = False
        for char in line:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == "%":
                break
            result.append(char)
        out.append("".join(result))
    return "\n".join(out)


def sanitize_latex(latex: str) -> SanitizeResult:
    """Validate LaTeX before handing it to pdflatex."""
    rejections: list[str] = []
    warnings: list[str] = []

    if not latex or not latex.strip():
        return SanitizeResult(ok=False, rejections=["the document is empty"])

    if len(latex) > MAX_LATEX_CHARS:
        rejections.append(
            f"the document is {len(latex):,} characters, over the {MAX_LATEX_CHARS:,} limit"
        )

    body = _strip_comments(latex)
    lowered = body.lower()

    for primitive, risk in FORBIDDEN_PRIMITIVES.items():
        if primitive.lower() not in lowered:
            continue
        if primitive in (r"\input", r"\include") and _only_allowlisted_inputs(body, primitive):
            continue
        rejections.append(f"{primitive} is not permitted ({risk})")

    if r"\documentclass" not in body:
        rejections.append("no \\documentclass -- this is not a complete document")
    if r"\begin{document}" not in body or r"\end{document}" not in body:
        rejections.append("missing \\begin{document} or \\end{document}")

    unbalanced = _unbalanced_environments(body)
    if unbalanced:
        rejections.append(f"unbalanced environments: {', '.join(unbalanced)}")

    braces = body.count("{") - body.count("}")
    if braces != 0:
        # A warning, not a rejection: brace counting is confounded by escaped
        # braces and verbatim, and pdflatex reports the real error better.
        warnings.append(f"brace count is off by {braces}; compilation may fail")

    return SanitizeResult(ok=not rejections, rejections=rejections, warnings=warnings)


def _only_allowlisted_inputs(body: str, primitive: str) -> bool:
    """True when every use of \\input/\\include targets an allowlisted file."""
    pattern = re.compile(re.escape(primitive) + r"\s*\{([^}]*)\}")
    found = pattern.findall(body)
    bare = re.search(re.escape(primitive) + r"(?!\s*\{)", body)
    if bare:
        return False  # \input without braces takes a bare filename
    return bool(found) and all(arg.strip() in INPUT_ALLOWLIST for arg in found)


def _unbalanced_environments(body: str) -> list[str]:
    """Find \\begin{x} without a matching \\end{x}.

    Reported before compiling because pdflatex's own message for this is
    "\\end occurred inside a group", which tells a user nothing.
    """
    begins = re.findall(r"\\begin\s*\{([^}]+)\}", body)
    ends = re.findall(r"\\end\s*\{([^}]+)\}", body)
    unbalanced: list[str] = []
    for name in set(begins) | set(ends):
        if begins.count(name) != ends.count(name):
            unbalanced.append(name)
    return sorted(unbalanced)
