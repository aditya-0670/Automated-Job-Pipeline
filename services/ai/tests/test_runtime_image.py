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
)

#: Packages the image does NOT ship, recorded deliberately.
#:
#: `fontawesome5` (icons in contact lines) and `moderncv` live in
#: texlive-fonts-extra, which is over a gigabyte on an image that is already
#: 2.9GB. Since a template either uses them or does not, the cost is only worth
#: paying once we know the actual template. Part 9 detects a missing package
#: from the pdflatex log and reports it as an actionable error rather than
#: "compilation failed", so this degrades honestly.
NOT_SHIPPED_PACKAGES = ("fontawesome5", "moderncv")


@needs_latex
@pytest.mark.parametrize("package", SHIPPED_PACKAGES)
def test_shipped_latex_packages_are_present(package):
    """Resume templates lean on latex-extra styles; a missing one fails at compile."""
    result = subprocess.run(["kpsewhich", f"{package}.sty"], capture_output=True, text=True)
    assert result.returncode == 0, f"{package}.sty is missing from the image"


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
