"""Part 7: the self-correction loop, with the real agents wired in.

This is the integration test behind the resume claim about retry handling and
failure recovery. It uses the real Evaluator (so the real deterministic
guardrails run) and a scripted Refactorer, so the loop's behaviour is observable
and deterministic without spending quota.
"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.evaluator import evaluator_agent
from app.graph.builder import (
    EVALUATOR,
    HUMAN_REVIEW,
    REFACTORER,
    build_graph,
)
from app.graph.state import initial_state
from app.graph.steps import Step

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))

HALLUCINATED = RESUME.replace(
    r"\end{document}",
    r"\resumeItem{Ran \textbf{Kubernetes} and \textbf{Terraform} in production.}"
    + "\n"
    + r"\end{document}",
)


def base_state(**overrides):
    state = initial_state(
        session_id="loop-1",
        user_id="u-aditya",
        user_latex=RESUME,
        user_profile=PROFILE,
        max_iterations=3,
    )
    state["matched_evidence"] = [
        {
            "item_id": "exp-oracle",
            "kind": "experience",
            "title": "Oracle",
            "text": "C++ GDB",
            "matched_keywords": ["C++"],
            "relevance": 2.0,
            "already_on_resume": True,
            "evidence": {},
        },
    ]
    state["keywords"] = [{"term": "Docker", "score": 20.0, "sources": ["taxonomy"]}]
    state.update(overrides)
    return state


def scripted_refactorer(outputs: list[str], calls: list[dict]):
    """Returns each output in turn, recording what it was given."""

    def node(state):
        index = min(len(calls), len(outputs) - 1)
        calls.append(
            {
                "iteration": state.get("iteration_count", 0),
                "evaluation": state.get("evaluation") or {},
                "previous": state.get("refactored_latex") or "",
            }
        )
        return {
            "refactored_latex": outputs[index],
            "iteration_count": state.get("iteration_count", 0) + 1,
            "current_step": Step.REFACTORING.value,
        }

    return node


async def async_evaluator(state):
    """The real evaluator with the quality pass disabled: real rules, no tokens."""
    return await evaluator_agent(state, skip_llm=True)


async def run_graph(refactor_outputs: list[str], *, max_iterations: int = 3):
    """Drive the graph with ainvoke -- the evaluator is async, and LangGraph
    refuses to run an async node from the sync API."""
    calls: list[dict] = []
    thread = f"loop-{len(refactor_outputs)}-{max_iterations}"
    graph = build_graph(
        {REFACTORER: scripted_refactorer(refactor_outputs, calls), EVALUATOR: async_evaluator},
        checkpointer=InMemorySaver(),
        interrupt_before=(HUMAN_REVIEW,),
    )
    config = {"configurable": {"thread_id": thread}}
    final = await graph.ainvoke(base_state(max_iterations=max_iterations), config=config)
    return final, calls, graph, config


# ── The loop ─────────────────────────────────────────────────────────────
async def test_clean_output_reaches_review_without_retrying():
    final, calls, _, _ = await run_graph([RESUME])
    assert len(calls) == 1
    assert final["evaluation"]["passed"] is True


async def test_hallucination_triggers_exactly_one_retry_then_passes():
    """The loop's normal case: caught, corrected, done."""
    final, calls, _, _ = await run_graph([HALLUCINATED, RESUME])
    assert len(calls) == 2, "expected one retry"
    assert final["evaluation"]["passed"] is True
    assert not final["evaluation"]["factual_errors"]


async def test_the_retry_receives_the_specific_errors():
    """Feedback-driven, not a blank regeneration. This is the whole distinction
    from a naive exception retry."""
    _, calls, _, _ = await run_graph([HALLUCINATED, RESUME])
    second_call = calls[1]
    errors = second_call["evaluation"].get("factual_errors") or []
    assert errors, "the retry was given no feedback"
    assert any("Kubernetes" in e for e in errors)
    assert second_call["previous"] == HALLUCINATED, "the retry must see its own prior output"


async def test_a_permanently_failing_model_stops_at_the_cap():
    """No infinite loop, and the bound is on total attempts."""
    final, calls, _, _ = await run_graph([HALLUCINATED], max_iterations=3)
    assert len(calls) == 3
    assert final["iteration_count"] == 3


async def test_exhausted_retries_degrade_to_review_rather_than_failing():
    """Graceful degradation: the user gets a resume plus the outstanding problems,
    which beats an error page."""
    final, _, graph, config = await run_graph([HALLUCINATED], max_iterations=2)
    assert final["current_step"] != Step.FAILED.value
    assert final["refactored_latex"]
    assert final["evaluation"]["factual_errors"], "the unresolved issues must still be reported"
    snapshot = await graph.aget_state(config)
    assert snapshot.next == (HUMAN_REVIEW,)


async def test_max_iterations_one_means_no_retry():
    _, calls, _, _ = await run_graph([HALLUCINATED], max_iterations=1)
    assert len(calls) == 1


# ── Cost of the loop ─────────────────────────────────────────────────────
async def test_the_loop_spends_no_tokens_detecting_the_problem():
    """Detection is deterministic. Only the *fix* costs a model call, which is
    what makes bounded retries affordable."""
    final, _, _, _ = await run_graph([HALLUCINATED, RESUME])
    assert final["token_ledger"].get("calls", 0) == 0


async def test_state_survives_every_loop_iteration():
    """Each pass through the loop is checkpointed, so a crash mid-correction
    resumes rather than restarting."""
    final, _, graph, config = await run_graph([HALLUCINATED, RESUME])
    history = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert len(history) > 3, "each node transition should be checkpointed"
    assert final["user_profile"], "user context must survive the loop"
