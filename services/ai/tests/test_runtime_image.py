"""Runtime-image capabilities: Chromium and pdflatex, as the service user.

These skip on the `test` image, which deliberately omits both (ADR-008). They
run against the `runtime` image:

    docker run --rm resumeforge-ai:latest pytest tests/test_runtime_image.py -v

The regression they guard is specific and was real: `playwright install` was run
as root, so Chromium landed in root's ~/.cache while the service runs as
appuser. The image contained the browser and still could not launch it -- and
nothing in the fast suite could have noticed, because the fast image has no
browser to find.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    import os

    browsers = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"))
    return browsers.exists() and any(browsers.glob("chromium*"))


needs_chromium = pytest.mark.skipif(
    not _playwright_available(), reason="Chromium not installed (test image)"
)
needs_latex = pytest.mark.skipif(
    shutil.which("pdflatex") is None, reason="pdflatex not installed (test image)"
)


@needs_chromium
def test_chromium_launches_as_the_service_user():
    """The browser must be launchable by the non-root user that serves traffic."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            assert browser.version
            page = browser.new_page()
            page.set_content("<h1>ResumeForge</h1>")
            assert "ResumeForge" in page.content()
        finally:
            browser.close()


@needs_latex
def test_pdflatex_compiles_a_minimal_document(tmp_path):
    """Proves TeX Live is usable, not merely installed."""
    source = tmp_path / "doc.tex"
    source.write_text(
        r"""\documentclass{article}
\begin{document}
Hello from ResumeForge.
\end{document}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "doc.tex"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-1500:]
    pdf = tmp_path / "doc.pdf"
    assert pdf.exists()
    assert pdf.read_bytes().startswith(b"%PDF-")


#: Style packages the image ships and resume templates commonly need.
SHIPPED_PACKAGES = (
    "titlesec",
    "enumitem",
    "hyperref",
    "geometry",
    "xcolor",
    "tabularx",
    "multirow",
    "ragged2e",
    # Required by the real template. lmodern is a standalone Debian package;
    # fontawesome5 is installed from CTAN rather than via texlive-fonts-extra,
    # which would have cost 1.5GB to obtain one icon font.
    "lmodern",
    "fancyhdr",
    "fontawesome5",
)

#: Packages the image does NOT ship, recorded deliberately.
#:
#: `moderncv` is a whole resume class the real template does not use. If a future
#: template needs it, it can be installed from CTAN the same way fontawesome5 is.
#: Part 9 reads the missing-package name out of the pdflatex log and reports it
#: as an actionable error rather than "compilation failed", so this degrades
#: honestly rather than silently.
NOT_SHIPPED_PACKAGES = ("moderncv",)


@needs_latex
@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_shipped_latex_packages_are_present(package):
    """Resume templates lean on latex-extra styles; a missing one fails at compile."""
    result = subprocess.run(["kpsewhich", f"{package}.sty"], capture_output=True, text=True)
    assert result.returncode == 0, f"{package}.sty is missing from the image"


@needs_latex
def test_fontawesome_mapping_file_is_present():
    """The .sty alone is not enough.

    fontawesome5.sty loads fontawesome5-mapping.def via \\file_input. Copying
    only *.sty from the CTAN package built an image that had fontawesome5 and
    still could not compile a document using it.
    """
    result = subprocess.run(
        ["kpsewhich", "fontawesome5-mapping.def"], capture_output=True, text=True
    )
    assert result.returncode == 0, "fontawesome5-mapping.def is missing"


@needs_latex
@pytest.mark.parametrize("package", NOT_SHIPPED_PACKAGES)
def test_known_absent_packages_stay_documented(package):
    """Pins the known gap.

    If a future image adds texlive-fonts-extra this test fails, which is the
    signal to move the package into SHIPPED_PACKAGES and update the docs --
    rather than the gap quietly closing and the docs going stale.
    """
    result = subprocess.run(["kpsewhich", f"{package}.sty"], capture_output=True, text=True)
    assert result.returncode != 0, (
        f"{package}.sty is now present -- move it to SHIPPED_PACKAGES "
        f"and update docs/08-infrastructure.md"
    )


@needs_latex
def test_fontawesome_glyphs_actually_render(tmp_path):
    """The install must be complete, not merely importable.

    Loading fontawesome5.sty without errors proves nothing about the glyphs: the
    real resume declares \\usepackage{fontawesome5} and defines an \\extlink
    macro around \\faIcon, but never calls it -- so no icon font is embedded and
    a broken font install would look identical to a working one.

    This document actually uses icons, so the FontAwesome fonts must appear in
    the output PDF.
    """
    import re
    import zlib

    source = tmp_path / "fa.tex"
    source.write_text(
        r"""\documentclass{article}
\usepackage{fontawesome5}
\begin{document}
\faIcon{envelope} \faIcon{external-link-alt} \faGithub
\end{document}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "fa.tex"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-1200:]

    pdf = (tmp_path / "fa.pdf").read_bytes()
    names: set[bytes] = set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9+#-]+)", pdf))
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            names |= set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9+#-]+)", zlib.decompress(stream)))
        except Exception:
            continue

    embedded = {name.decode() for name in names}
    assert any("FontAwesome" in name for name in embedded), (
        f"no FontAwesome font was embedded; fonts found: {sorted(embedded)}"
    )


# ── Node 6, against the real compiler ─────────────────────────────────────
# The fast suite covers `app/compile/` with the compiler stubbed and covers the
# graph with the node stubbed, so nothing there ever puts the two together. This
# is the one place the approved-LaTeX-to-file path runs for real.
@needs_latex
async def test_the_compile_node_turns_an_approved_resume_into_a_pdf(tmp_path):
    import json

    from app.agents.compile_pdf import compile_pdf_agent
    from app.graph.state import initial_state
    from app.graph.steps import Step

    fixtures = Path(__file__).parent / "fixtures"
    state = initial_state(
        session_id="runtime-compile",
        user_id="u-aditya",
        user_latex=(fixtures / "real_resume.tex").read_text(encoding="utf-8"),
        user_profile=json.loads((fixtures / "real_profile.json").read_text(encoding="utf-8")),
        job_text="x",
    )
    # What the user approved at human review is what gets compiled.
    state["final_latex"] = state["user_latex"]

    result = await compile_pdf_agent(state)

    assert result["current_step"] == Step.COMPLETE.value
    assert result["error"] is None
    pdf = Path(result["pdf_path"])
    assert pdf.is_file() and pdf.read_bytes().startswith(b"%PDF")
    assert result["events"][-1]["data"]["pages"] == 1


@needs_latex
async def test_a_broken_resume_fails_the_node_with_an_actionable_message(tmp_path):
    """A compile failure this late must not discard the run: the user still has
    an approved document, so the node reports why rather than losing it."""
    from app.agents.compile_pdf import compile_pdf_agent
    from app.graph.state import initial_state
    from app.graph.steps import Step

    state = initial_state(
        session_id="runtime-compile-bad",
        user_id="u",
        user_latex="x",
        user_profile={},
        job_text="x",
    )
    state["final_latex"] = r"\documentclass{article}\usepackage{nosuchpackage}" + (
        r"\begin{document}x\end{document}"
    )

    result = await compile_pdf_agent(state)

    assert result["current_step"] == Step.FAILED.value
    # The actionable line, not 400 lines of TeX noise (NFR-05.4).
    assert "nosuchpackage" in result["error"]
    assert len(result["error"].splitlines()) == 1
