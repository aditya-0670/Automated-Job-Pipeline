"""`ResumeForgeState` -- the object every node reads from and writes to.

LangGraph merges each node's returned partial dict into this state and then
checkpoints the whole thing to Postgres. Two consequences shape the design:

  * **Everything here must be JSON-serialisable.** No dataclasses, no sets, no
    datetimes. Domain objects (`Keyword`, `SkillMatch`) are converted to plain
    dicts at the node boundary. This is why the state looks flatter than the
    internal types.

  * **Fields are additive, not authoritative.** A node returns only the keys it
    changed; LangGraph does the merge. Returning a whole state object from a node
    would clobber concurrent writes.

Trimmed from the v0.2.0 design (docs/02-agent-architecture.md §3.1) to the
fields the MVP actually uses -- an unused state field still gets serialised on
every checkpoint.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from app.graph.steps import Step


def keep_last(_current: Any, incoming: Any) -> Any:
    """Last write wins. LangGraph's default, made explicit where it matters."""
    return incoming


def add_events(current: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    """Append-only reducer for the audit trail.

    Every node contributes events; without an append reducer each node would
    overwrite the previous node's history, and "workflow visibility" would be a
    single row deep.
    """
    return [*(current or []), *(incoming or [])]


UserDecision = Literal["accept", "request_changes", "edit", "modify_keywords"]


class ResumeForgeState(TypedDict, total=False):
    """Shared state across all nodes. `total=False` because nodes return partials."""

    # ─── Identity ───
    session_id: str  # also the LangGraph thread_id
    user_id: str

    # ─── User context (supplied by the API gateway, never fetched here) ───
    user_latex: str  # the LaTeX template to preserve
    user_profile: dict[str, Any]  # experiences, projects, skills, education

    # ─── Job context ───
    job_url: str
    job_text: str  # scraped, or pasted by the user
    job_metadata: dict[str, str]  # title, company, page_title
    scrape_tier: str  # "http" | "playwright" | "manual"

    # ─── Keyword extraction (Part 1) ───
    keywords: list[dict[str, Any]]  # serialised Keyword objects, ranked
    keywords_by_category: dict[str, list[str]]
    keywords_confirmed: bool  # user passed the Layer 4 checkpoint
    extraction_stats: dict[str, Any]  # includes llm_tokens_used: 0

    # ─── Evidence retrieval (Part 4) ───
    matched_evidence: list[dict[str, Any]]  # ranked profile items with provenance
    suggestions: list[dict[str, Any]]  # add | emphasise | drop
    unsupported_keywords: list[str]  # in the JD, no evidence in profile.
    # ^ Deliberately surfaced rather than hidden: these are the skills the resume
    #   must NOT claim, and the honest signal of a genuine gap for the user.

    # ─── Refactoring (Part 5) ───
    refactored_latex: str
    changelog: list[dict[str, Any]]  # {section, change_type, before, after, reason}

    # ─── Evaluation (Part 6) ───
    evaluation: dict[str, Any]  # {passed, factual_errors, structural_errors, ...}
    evaluator_feedback: str  # structured feedback driving the retry

    # ─── Control flow (Part 7) ───
    current_step: Annotated[Step, keep_last]
    iteration_count: int  # self-correction attempts used
    max_iterations: int
    error: str | None
    warnings: list[str]  # non-fatal issues carried to the user

    # ─── Human-in-the-loop (Part 8) ───
    user_decision: UserDecision | None
    user_change_request: str  # natural-language change instruction
    edited_latex: str  # user's manual edit
    review_iteration: int

    # ─── Output (Part 9) ───
    final_latex: str
    pdf_path: str

    # ─── Observability ───
    events: Annotated[list[dict[str, Any]], add_events]
    token_ledger: dict[str, Any]  # TokenLedger.to_dict()


def initial_state(
    *,
    session_id: str,
    user_id: str,
    user_latex: str,
    user_profile: dict[str, Any],
    job_url: str = "",
    job_text: str = "",
    max_iterations: int = 3,
) -> ResumeForgeState:
    """Build a fully populated starting state.

    Every field is initialised explicitly. Relying on `total=False` to leave keys
    absent means every downstream node needs a `.get()` with a default, and one
    missed default is a `KeyError` mid-pipeline after the LLM spend has happened.
    """
    return ResumeForgeState(
        session_id=session_id,
        user_id=user_id,
        user_latex=user_latex,
        user_profile=user_profile,
        job_url=job_url,
        job_text=job_text,
        job_metadata={},
        scrape_tier="",
        keywords=[],
        keywords_by_category={},
        keywords_confirmed=False,
        extraction_stats={},
        matched_evidence=[],
        suggestions=[],
        unsupported_keywords=[],
        refactored_latex="",
        changelog=[],
        evaluation={},
        evaluator_feedback="",
        current_step=Step.INIT,
        iteration_count=0,
        max_iterations=max_iterations,
        error=None,
        warnings=[],
        user_decision=None,
        user_change_request="",
        edited_latex="",
        review_iteration=0,
        final_latex="",
        pdf_path="",
        events=[],
        token_ledger={},
    )
