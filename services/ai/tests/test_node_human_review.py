"""Part 8: the human review interrupt, its node, and resuming from it.

The integration tests here are the acceptance criterion for Part 8: the graph
pauses with state persisted, and a **separately constructed graph object** --
standing in for a different process or replica -- resumes it from the checkpoint
and carries on. Nothing but the checkpointer is shared between them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.human_review import human_review_agent, review_payload
from app.graph.builder import (
    COMPILE_PDF,
    EVALUATOR,
    HUMAN_REVIEW,
    REFACTORER,
    SCRAPER_KEYWORD,
    build_graph,
)
from app.graph.resume import ResumeError, paused_at, resume_keywords, resume_review
from app.graph.state import initial_state
from app.graph.steps import Step

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))

REWRITTEN = RESUME.replace(r"\end{document}", "% rewritten\n" + r"\end{document}")


def state(**overrides):
    base = initial_state(
        session_id="review-1",
        user_id="u-aditya",
        user_latex=RESUME,
        user_profile=PROFILE,
    )
    base["refactored_latex"] = REWRITTEN
    base.update(overrides)
    return base


# ─────────────────────────── the node in isolation ───────────────────────────


async def test_accept_freezes_the_approved_latex_and_heads_for_compilation():
    result = await human_review_agent(state(user_decision="accept"))
    assert result["current_step"] == Step.COMPILING
    # final_latex, not a reuse of refactored_latex: it records what was signed off.
    assert result["final_latex"] == REWRITTEN
    assert result["review_iteration"] == 1


async def test_request_changes_keeps_the_instruction_for_the_refactorer():
    result = await human_review_agent(
        state(user_decision="request_changes", user_change_request="Shorten the Oracle bullets")
    )
    assert result["current_step"] == Step.REFINING
    # Not cleared here -- the refactorer clears it once it has been applied.
    assert "user_change_request" not in result
    assert result["evaluation"] == {}


async def test_an_edit_becomes_the_working_copy_and_is_re_evaluated():
    """The user can break their own LaTeX; the guardrails are free, so run them."""
    edited = REWRITTEN.replace("% rewritten", "% hand-edited")
    result = await human_review_agent(state(user_decision="edit", edited_latex=edited))
    assert result["current_step"] == Step.EVALUATING
    assert result["refactored_latex"] == edited
    assert result["edited_latex"] == ""


async def test_modify_keywords_reopens_the_confirmation_gate():
    result = await human_review_agent(state(user_decision="modify_keywords"))
    assert result["current_step"] == Step.SCRAPING
    assert result["keywords_confirmed"] is False
    # Cleared so the refactorer regenerates rather than "correcting" an output
    # built from a different keyword set.
    assert result["evaluation"] == {}


async def test_a_user_revision_restores_the_self_correction_budget():
    """Otherwise the third revision silently gets no automatic retries at all."""
    result = await human_review_agent(
        state(user_decision="request_changes", user_change_request="More Kafka", iteration_count=3)
    )
    assert result["iteration_count"] == 0


@pytest.mark.parametrize("decision", [None, "", "delete_everything"])
async def test_an_invalid_decision_degrades_to_accept_rather_than_stranding_the_session(decision):
    result = await human_review_agent(state(user_decision=decision))
    assert result["user_decision"] == "accept"
    assert result["current_step"] == Step.COMPILING


async def test_request_changes_with_no_instruction_accepts_and_says_so():
    result = await human_review_agent(
        state(user_decision="request_changes", user_change_request="  ")
    )
    assert result["user_decision"] == "accept"
    assert any("no change instruction" in w.lower() for w in result["warnings"])


async def test_edit_with_no_latex_accepts_and_says_so():
    result = await human_review_agent(state(user_decision="edit", edited_latex=""))
    assert result["user_decision"] == "accept"
    assert any("no edited latex" in w.lower() for w in result["warnings"])


async def test_the_node_records_the_decision_in_the_audit_trail():
    result = await human_review_agent(state(user_decision="accept"))
    (event,) = result["events"]
    assert event["data"]["decision"] == "accept"
    assert event["data"]["review_iteration"] == 1


# ───────────────────────────── the review payload ─────────────────────────────


def test_the_payload_carries_the_diff_and_the_changelog():
    payload = review_payload(state(changelog=[{"section": "Skills", "reason": "added Docker"}]))
    assert payload["summary"]["total_sections"] > 1
    assert payload["changelog"][0]["reason"] == "added Docker"
    assert payload["latex"] == REWRITTEN


def test_unresolved_errors_are_surfaced_at_the_moment_of_sign_off():
    """Part 7 degrades to review with problems attached; they must be visible."""
    payload = review_payload(
        state(evaluation={"passed": False, "factual_errors": ["claims Kubernetes"]})
    )
    assert payload["unresolved"]["factual_errors"] == ["claims Kubernetes"]
    assert payload["quality"]["passed"] is False


def test_the_payload_is_json_serialisable():
    json.dumps(review_payload(state()))


# ────────────────────────── pause, persist, resume ──────────────────────────


def stub(step: Step, **writes):
    async def node(_state):
        return {"current_step": step.value, **writes}

    return node


def pipeline_graph(checkpointer, *, compiled: list[str] | None = None):
    """The tail of the pipeline: evaluator -> human review -> compile."""

    async def compile_node(state):
        if compiled is not None:
            compiled.append(state.get("final_latex") or "")
        return {"current_step": Step.COMPLETE.value, "pdf_path": "/tmp/out.pdf"}

    return build_graph(
        {
            SCRAPER_KEYWORD: stub(Step.EXTRACTING, keywords=[{"term": "Docker"}]),
            REFACTORER: stub(Step.REFACTORING, refactored_latex=REWRITTEN),
            EVALUATOR: stub(Step.EVALUATING, evaluation={}),
            HUMAN_REVIEW: human_review_agent,
            COMPILE_PDF: compile_node,
        },
        checkpointer=checkpointer,
    )


async def run_to_review(graph, session_id="review-1"):
    """Drive a fresh session up to the human_review pause."""
    from app.graph.checkpointer import thread_config

    start = state(session_id=session_id, refactored_latex="")
    config = thread_config(session_id)
    await graph.ainvoke(start, config)  # pauses at keyword_review
    await resume_keywords(graph, session_id)  # runs on to human_review
    return config


async def test_the_graph_pauses_before_human_review_with_state_persisted():
    graph = pipeline_graph(InMemorySaver())
    await run_to_review(graph)
    assert await paused_at(graph, "review-1") == HUMAN_REVIEW

    snapshot = await graph.aget_state({"configurable": {"thread_id": "review-1"}})
    # The pause is a checkpoint row, not a suspended coroutine: everything the
    # review needs is already durable.
    assert snapshot.values["refactored_latex"] == REWRITTEN
    assert snapshot.values["pdf_path"] == ""


async def test_a_second_process_resumes_the_pause_and_finishes_the_run():
    """Part 8's acceptance criterion: a different graph object resumes it."""
    saver = InMemorySaver()
    compiled: list[str] = []
    await run_to_review(pipeline_graph(saver))

    # Nothing carried over but the checkpointer -- as if the first process died.
    reborn = pipeline_graph(saver, compiled=compiled)
    final = await resume_review(reborn, "review-1", "accept")

    assert final["current_step"] == Step.COMPLETE
    assert final["pdf_path"] == "/tmp/out.pdf"
    assert compiled == [REWRITTEN]
    assert await paused_at(reborn, "review-1") is None


async def test_request_changes_loops_back_through_refactor_and_pauses_again():
    saver = InMemorySaver()
    graph = pipeline_graph(saver)
    await run_to_review(graph)

    await resume_review(graph, "review-1", "request_changes", change_request="Shorten it")

    # Round trip complete: refactor -> evaluate -> review, waiting on the user again.
    assert await paused_at(graph, "review-1") == HUMAN_REVIEW
    snapshot = await graph.aget_state({"configurable": {"thread_id": "review-1"}})
    assert snapshot.values["review_iteration"] == 1

    final = await resume_review(graph, "review-1", "accept")
    assert final["current_step"] == Step.COMPLETE
    assert final["review_iteration"] == 2


async def test_modify_keywords_returns_the_session_to_the_keyword_gate():
    graph = pipeline_graph(InMemorySaver())
    await run_to_review(graph)
    await resume_review(graph, "review-1", "modify_keywords")
    assert await paused_at(graph, "review-1") == "keyword_review"


async def test_resuming_with_a_decision_the_session_is_not_waiting_for_is_refused():
    """A review decision written at the keyword gate would be silently ignored."""
    graph = pipeline_graph(InMemorySaver())
    from app.graph.checkpointer import thread_config

    await graph.ainvoke(state(refactored_latex=""), thread_config("review-1"))
    with pytest.raises(ResumeError, match="waiting at 'keyword_review'"):
        await resume_review(graph, "review-1", "accept")


async def test_resuming_a_finished_session_is_refused():
    graph = pipeline_graph(InMemorySaver())
    await run_to_review(graph)
    await resume_review(graph, "review-1", "accept")
    with pytest.raises(ResumeError, match="not waiting for input"):
        await resume_review(graph, "review-1", "accept")


@pytest.mark.parametrize(
    ("decision", "kwargs", "match"),
    [
        ("nonsense", {}, "Unknown review decision"),
        ("request_changes", {}, "needs an instruction"),
        ("edit", {}, "needs the edited LaTeX"),
    ],
)
async def test_incomplete_input_is_rejected_before_the_graph_is_touched(decision, kwargs, match):
    graph = pipeline_graph(InMemorySaver())
    await run_to_review(graph)
    with pytest.raises(ResumeError, match=match):
        await resume_review(graph, "review-1", decision, **kwargs)
    # The session is untouched and still resumable.
    assert await paused_at(graph, "review-1") == HUMAN_REVIEW


async def test_confirming_keywords_can_replace_the_extracted_set():
    graph = pipeline_graph(InMemorySaver())
    from app.graph.checkpointer import thread_config

    await graph.ainvoke(state(refactored_latex=""), thread_config("review-1"))
    await resume_keywords(graph, "review-1", keywords=[{"term": "Kafka"}])
    snapshot = await graph.aget_state(thread_config("review-1"))
    assert snapshot.values["keywords"] == [{"term": "Kafka"}]
    assert snapshot.values["keywords_confirmed"] is True


async def test_an_empty_keyword_set_is_refused():
    graph = pipeline_graph(InMemorySaver())
    from app.graph.checkpointer import thread_config

    await graph.ainvoke(state(refactored_latex=""), thread_config("review-1"))
    with pytest.raises(ResumeError, match="At least one keyword"):
        await resume_keywords(graph, "review-1", keywords=[])
