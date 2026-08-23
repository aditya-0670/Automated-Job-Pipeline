"""Node 6 — LaTeX to PDF. The last node, and the only one that produces a file.

The compiler itself (`app/compile/`) is sandboxed and already tested; this node
is the graph-facing wrapper. Its one judgement call is what "failure" means here:

A compile failure this late is not a dead session. The user has an approved
LaTeX document -- it is on screen, it is checkpointed, and they can still copy
it out. So a failure routes to the terminal step with an actionable message and
the LaTeX intact, rather than discarding the run.
"""

from __future__ import annotations

import logging
from typing import Any

from app.compile.latex import compile_latex
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)


async def compile_pdf_agent(state: ResumeForgeState) -> dict[str, Any]:
    """Compile the approved LaTeX and record where the PDF landed."""
    # `final_latex` is what the user approved; the fallback covers a session that
    # somehow reached compilation without passing through review.
    latex = state.get("final_latex") or state.get("refactored_latex") or ""
    if not latex.strip():
        return _failure(state, "There is no approved resume to compile.")

    result = await compile_latex(latex, name=state.get("session_id") or "resume")
    warnings = [*(state.get("warnings") or []), *result.warnings]

    if not result.ok:
        logger.warning(
            "Compilation failed for session %s: %s", state.get("session_id"), result.error
        )
        return _failure(
            state,
            result.error or "The PDF could not be compiled.",
            warnings=warnings,
            data=result.to_dict(),
        )

    logger.info(
        "Compiled %s: %d bytes, %d page(s) in %.0fms",
        result.pdf_path,
        result.pdf_bytes,
        result.pages,
        result.duration_ms,
    )
    return {
        "final_latex": latex,
        "pdf_path": result.pdf_path or "",
        "current_step": Step.COMPLETE.value,
        "warnings": warnings,
        "error": None,
        "events": [
            step_event(
                state,
                Step.COMPLETE,
                detail=f"Compiled {result.pages} page(s)",
                data=result.to_dict(),
            )
        ],
    }


def _failure(
    state: ResumeForgeState,
    message: str,
    *,
    warnings: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": message,
        "current_step": Step.FAILED.value,
        "warnings": warnings if warnings is not None else list(state.get("warnings") or []),
        "events": [step_event(state, Step.FAILED, detail=message, data=data)],
    }
