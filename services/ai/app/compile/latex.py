"""LaTeX -> PDF compilation, sandboxed and with actionable errors.

Two things make this harder than "run pdflatex":

  1. **It compiles untrusted input.** See `sanitize.py` for the layered defence;
     this module supplies the process-level half -- no shell escape, restricted
     file writes, a temp directory it cannot escape, and a hard timeout.

  2. **pdflatex's errors are unusable as-is.** A single missing package produces
     several hundred lines of log, and the actual cause is one line in the
     middle. NFR-05.4 requires actionable messages, so the log is parsed rather
     than forwarded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.compile.sanitize import sanitize_latex
from app.config import get_settings

logger = logging.getLogger(__name__)

#: Two passes. The first resolves content, the second fixes references and page
#: numbers that depend on the first pass's .aux file. Resume templates using
#: \pageref or hyperref anchors render wrongly with only one pass.
PASSES = 2


#: pdfTeX derives the /ID trailer from the output path and other entropy, so two
#: compiles of identical source in different temp directories differ in exactly
#: those 32 hex characters and nowhere else. Excluding /ID gives a hash that
#: means "same content", which is what caching and change-detection want.
_PDF_ID_RE = re.compile(rb"/ID\s*\[<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\]")


def pdf_content_hash(pdf: bytes) -> str:
    """Stable hash of a PDF's content, ignoring the non-deterministic /ID."""
    return hashlib.sha256(_PDF_ID_RE.sub(b"/ID[<><>]", pdf)).hexdigest()


@dataclass
class CompileResult:
    ok: bool
    pdf_path: str | None = None
    pdf_bytes: int = 0
    content_hash: str = ""
    pages: int = 0
    error: str | None = None  # single actionable sentence
    log_excerpt: str = ""  # the relevant lines, for a "details" panel
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "pdf_path": self.pdf_path,
            "pdf_bytes": self.pdf_bytes,
            "content_hash": self.content_hash,
            "pages": self.pages,
            "error": self.error,
            "log_excerpt": self.log_excerpt,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 1),
        }


class LatexCompiler:
    """Compiles LaTeX in an isolated temp directory."""

    def __init__(self, output_dir: str | Path | None = None) -> None:
        settings = get_settings()
        self.output_dir = Path(output_dir or "/app/out")
        self.timeout = settings.latex_compile_timeout_seconds

    async def compile(self, latex: str, *, name: str | None = None) -> CompileResult:
        started = asyncio.get_running_loop().time()
        name = name or f"resume-{uuid.uuid4().hex[:12]}"

        # ── Layer 1: reject dangerous or malformed input before running TeX ──
        check = sanitize_latex(latex)
        if not check.ok:
            return CompileResult(
                ok=False,
                error=check.message,
                warnings=check.warnings,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

        with tempfile.TemporaryDirectory(prefix="resumeforge-tex-") as workdir:
            work = Path(workdir)
            source = work / "resume.tex"
            source.write_text(latex, encoding="utf-8")

            log_text = ""
            for attempt in range(1, PASSES + 1):
                returncode, log_text = await self._run_pdflatex(work)
                pdf = work / "resume.pdf"
                # pdflatex can exit non-zero while still producing a usable PDF
                # (unresolved references, overfull boxes). The artifact is the
                # authority, not the exit code -- but a first-pass failure with
                # no PDF is fatal and there is no point running pass two.
                if returncode != 0 and not pdf.exists():
                    error, excerpt = self._explain(log_text)
                    logger.warning("LaTeX compile failed on pass %d: %s", attempt, error)
                    return CompileResult(
                        ok=False,
                        error=error,
                        log_excerpt=excerpt,
                        warnings=check.warnings,
                        duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                    )

            pdf = work / "resume.pdf"
            if not pdf.exists():
                error, excerpt = self._explain(log_text)
                return CompileResult(
                    ok=False,
                    error=error or "Compilation produced no PDF.",
                    log_excerpt=excerpt,
                    warnings=check.warnings,
                    duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                )

            self.output_dir.mkdir(parents=True, exist_ok=True)
            destination = self.output_dir / f"{name}.pdf"
            shutil.copy2(pdf, destination)

            data = destination.read_bytes()
            warnings = [*check.warnings, *self._collect_warnings(log_text)]
            pages = self._page_count(data)

            # A resume that has silently grown to three pages is a real problem
            # for the user even though compilation succeeded.
            if pages > 2:
                warnings.append(f"The resume is {pages} pages. Most ATS-screened roles expect one.")

            logger.info("Compiled %s (%d bytes, %d pages)", destination.name, len(data), pages)
            return CompileResult(
                ok=True,
                pdf_path=str(destination),
                pdf_bytes=len(data),
                content_hash=pdf_content_hash(data),
                pages=pages,
                warnings=warnings,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

    async def _run_pdflatex(self, work: Path) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "pdflatex",
            "-interaction=nonstopmode",  # never wait for input on error
            "-no-shell-escape",  # layer 2 of the sandbox
            "-halt-on-error",
            "-output-directory",
            str(work),
            "resume.tex",
            cwd=str(work),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # openout_any=p forbids writing outside the working directory even if
            # a write primitive slipped past the sanitizer.
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(work),
                "TEXMFVAR": str(work / "texmf"),
                "openout_any": "p",
                "openin_any": "p",
                # Reproducible output. pdfTeX only honours SOURCE_DATE_EPOCH when
                # FORCE_SOURCE_DATE is also set; with the epoch alone it still
                # stamps the real time and every compile differs. That matters
                # because a diff view cannot distinguish a genuine content change
                # from a new timestamp.
                "SOURCE_DATE_EPOCH": "0",
                "FORCE_SOURCE_DATE": "1",
            },
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            # A TeX infinite loop is a real possibility with user macros, so the
            # timeout is a correctness control rather than a nicety.
            return 1, f"TIMEOUT: compilation exceeded {self.timeout}s"

        return process.returncode or 0, stdout.decode("utf-8", errors="replace")

    # ── Error explanation ────────────────────────────────────────────────
    #: Ordered most-specific first: a missing package also produces generic
    #: "Emergency stop" noise, and the specific cause is the useful one.
    ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
        (
            r"! LaTeX Error: File `([^']+)' not found",
            "This template needs the LaTeX package '{0}', which is not installed "
            "in the compiler image.",
        ),
        (
            r"! Undefined control sequence.*?\n.*?(\\[a-zA-Z@]+)",
            "Unknown LaTeX command '{0}'. It may be misspelled, or it may need a "
            "package that is not loaded.",
        ),
        (
            r"! LaTeX Error: Environment ([^ ]+) undefined",
            "The environment '{0}' is not defined. A required package may be missing.",
        ),
        (
            r"! LaTeX Error: \\begin\{([^}]+)\} on input line (\d+) ended by",
            "The '{0}' environment opened on line {1} is not closed properly.",
        ),
        (
            r"! Missing \$ inserted",
            "A maths-mode character (such as _ or ^) is used in plain text.",
        ),
        (r"! Extra \}, or forgotten", "There is an unmatched closing brace."),
        (r"! Missing \} inserted", "There is an unmatched opening brace."),
        (r"! Paragraph ended before", "A command is missing an argument."),
        (r"! TeX capacity exceeded.*?\[(\w+)", "The document exhausted TeX's '{0}' capacity."),
        (r"TIMEOUT: compilation exceeded (\d+)s", "Compilation timed out after {0} seconds."),
        (r"! Emergency stop", "Compilation stopped on a fatal error."),
    )

    def _explain(self, log: str) -> tuple[str, str]:
        """Turn several hundred lines of TeX log into one actionable sentence."""
        for pattern, template in self.ERROR_PATTERNS:
            match = re.search(pattern, log, re.S)
            if match:
                groups = [g for g in match.groups() if g is not None]
                try:
                    message = template.format(*groups)
                except (IndexError, KeyError):
                    message = template
                return message, self._excerpt(log, match.start())

        first_error = re.search(r"^!.*$", log, re.M)
        if first_error:
            return (
                f"LaTeX reported: {first_error.group(0).lstrip('! ').strip()}",
                self._excerpt(log, first_error.start()),
            )
        return "Compilation failed for an unrecognised reason.", log[-1200:]

    @staticmethod
    def _excerpt(log: str, position: int, context: int = 700) -> str:
        start = max(0, position - context // 3)
        return log[start : position + context].strip()

    @staticmethod
    def _collect_warnings(log: str) -> list[str]:
        """Surface only warnings a user can act on."""
        warnings: list[str] = []
        if re.search(r"Overfull \\hbox \((\d+\.\d+)pt", log):
            worst = max(
                (float(m) for m in re.findall(r"Overfull \\hbox \((\d+\.\d+)pt", log)),
                default=0.0,
            )
            # Under ~15pt is invisible in practice; reporting it is noise.
            if worst > 15:
                warnings.append(
                    f"Some text overflows its margin by up to {worst:.0f}pt. "
                    "A bullet may need shortening."
                )
        if "Citation" in log and "undefined" in log:
            warnings.append("There is an undefined citation reference.")
        if re.search(r"Font shape .* undefined", log):
            warnings.append("A requested font style is unavailable and was substituted.")
        return warnings

    @staticmethod
    def _page_count(pdf: bytes) -> int:
        """Count pages without a PDF library.

        `/Type /Page` (not `/Pages`) appears once per page object. Crude, but it
        avoids a dependency for a number used only in a warning.
        """
        return len(re.findall(rb"/Type\s*/Page[^s]", pdf)) or 1


async def compile_latex(latex: str, *, name: str | None = None, output_dir=None) -> CompileResult:
    return await LatexCompiler(output_dir).compile(latex, name=name)
