"""Part 2: graph topology, routing rules, and interrupt/resume behaviour.

Runs entirely on stub nodes and an in-memory checkpointer -- no LLM, no
Postgres. The point is that control flow is verifiable independently of the
agents that will later fill the nodes in.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.graph.builder import (
    COMPILE_PDF,
    DATA_RETRIEVER,
    EVALUATOR,
    HUMAN_REVIEW,
    KEYWORD_REVIEW,
    NODE_NAMES,
    REFACTORER,
    SCRAPER_KEYWORD,
    build_graph,
    graph_mermaid,
)
from app.graph.events import make_event, step_event
from app.graph.routing import (
    has_blocking_errors,
    route_after_evaluation,
    route_after_human_review,
    should_skip_scraping,
)
from app.graph.state import ResumeForgeState, add_events, initial_state
from app.graph.steps import (
    INTERRUPT_STEPS,
    PROGRESS_SEQUENCE,
    TERMINAL_STEPS,
    Step,
    progress_fraction,
)


def base_state(**overrides) -> ResumeForgeState:
    state = initial_state(
        session_id="s-1",
        user_id="u-1",
        user_latex=r"\documentclass{article}\begin{document}x\end{document}",
        user_profile={"skills": []},
        job_url="https://example.com/job",
    )
    state.update(overrides)
    return state


# ── State schema ─────────────────────────────────────────────────────────
def test_initial_state_populates_every_field():
    """No key may be absent -- a missing default is a KeyError mid-pipeline,
    after the LLM spend has already happened."""
    state = base_state()
    for key in ResumeForgeState.__annotations__:
        assert key in state, f"initial_state is missing {key}"


def test_initial_state_is_json_serialisable():
    """Everything in state gets checkpointed, so nothing may be an exotic type."""
    import json

    json.dumps(base_state())


def test_current_step_is_stored_as_a_plain_string():
    """LangGraph warns on deserialising unregistered types and will block them.

    An enum instance in checkpointed state is therefore a latent failure, not a
    style question. `Step` is a StrEnum so comparisons still read naturally.
    """
    state = base_state()
    assert type(state["current_step"]) is str
    assert state["current_step"] == Step.INIT  # StrEnum compares by value

    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-serial"}}
    graph.invoke(state, config=config)
    stored = graph.get_state(config).values["current_step"]
    assert type(stored) is str, f"checkpointed a {type(stored)}, not a str"


def test_events_reducer_appends_rather_than_replaces():
    """Without an append reducer each node would overwrite the audit trail."""
    assert add_events([{"a": 1}], [{"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert add_events(None, [{"b": 2}]) == [{"b": 2}]
    assert add_events([{"a": 1}], None) == [{"a": 1}]


# ── Lifecycle ────────────────────────────────────────────────────────────
def test_interrupt_and_terminal_steps_are_disjoint():
    assert not INTERRUPT_STEPS & TERMINAL_STEPS


def test_progress_is_monotonic_across_the_sequence():
    values = [progress_fraction(s) for s in PROGRESS_SEQUENCE]
    assert values == sorted(values)
    assert values[-1] == pytest.approx(1.0)


def test_loop_steps_hold_their_parent_position():
    """CORRECTING must not read as progress -- it re-enters an earlier stage."""
    assert progress_fraction(Step.CORRECTING) == progress_fraction(Step.REFACTORING)
    assert progress_fraction(Step.REFINING) == progress_fraction(Step.REFACTORING)


def test_every_step_has_a_human_label():
    from app.graph.steps import HUMAN_READABLE

    for step in Step:
        assert HUMAN_READABLE.get(step), f"{step} has no label"


# ── Events ───────────────────────────────────────────────────────────────
def test_event_carries_label_and_progress():
    event = make_event(Step.SCRAPING, session_id="s-1", detail="tier=http")
    assert event["step"] == "SCRAPING"
    assert event["label"]
    assert 0 < event["progress"] < 1
    assert event["session_id"] == "s-1"


def test_events_are_reproducible():
    """No timestamp inside checkpointed state: identical replays must compare
    equal, otherwise checkpoint assertions become flaky."""
    assert make_event(Step.SCRAPING) == make_event(Step.SCRAPING)


def test_step_event_numbers_within_the_running_state():
    state = base_state(events=[{"sequence": 1}, {"sequence": 2}])
    assert step_event(state, Step.MATCHING)["sequence"] == 3


# ── Routing: self-correction ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("evaluation", "expected"),
    [
        ({"factual_errors": ["claims Kubernetes"]}, True),
        ({"structural_errors": ["preamble modified"]}, True),
        ({"factual_errors": [], "structural_errors": []}, False),
        ({"keyword_coverage": 0.1}, False),  # quality, not correctness
        ({}, False),
    ],
)
def test_only_factual_and_structural_failures_block(evaluation, expected):
    assert has_blocking_errors(evaluation) is expected


def test_clean_evaluation_goes_to_human():
    state = base_state(evaluation={"passed": True}, refactored_latex="x")
    assert route_after_evaluation(state) == "human_review"


def test_blocking_errors_retry_while_budget_remains():
    state = base_state(
        evaluation={"factual_errors": ["bad"]},
        iteration_count=1,
        max_iterations=3,
        refactored_latex="x",
    )
    assert route_after_evaluation(state) == "refactor_again"


def test_exhausted_retries_degrade_to_human_not_failure():
    """Graceful degradation: the user still gets a resume, with warnings."""
    state = base_state(
        evaluation={"factual_errors": ["bad"]},
        iteration_count=3,
        max_iterations=3,
        refactored_latex="x",
    )
    assert route_after_evaluation(state) == "human_review"


def test_error_with_no_output_fails_rather_than_showing_empty_diff():
    state = base_state(error="scrape failed", refactored_latex="")
    assert route_after_evaluation(state) == "failed"


# ── Routing: human review ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("accept", "compile"),
        ("request_changes", "refactor_again"),
        ("edit", "evaluate_again"),
        ("modify_keywords", "extract_again"),
        (None, "compile"),
    ],
)
def test_human_review_dispatch(decision, expected):
    assert route_after_human_review(base_state(user_decision=decision)) == expected


def test_manual_edit_is_re_evaluated_not_trusted():
    """A user can break their own LaTeX; finding out at compile time is worse."""
    assert route_after_human_review(base_state(user_decision="edit")) == "evaluate_again"


def test_pasted_description_skips_scraping():
    assert should_skip_scraping(base_state(job_url="", job_text="pasted jd"))
    assert not should_skip_scraping(base_state(job_url="https://x", job_text=""))


# ── Graph construction ──────────────────────────────────────────────────
def test_graph_compiles_with_stubs():
    assert build_graph() is not None


def test_rejects_unknown_node_names():
    with pytest.raises(ValueError, match="Unknown node names"):
        build_graph({"not_a_node": lambda s: {}})


def test_mermaid_renders_every_node():
    diagram = graph_mermaid()
    for name in NODE_NAMES:
        assert name in diagram


# ── Execution with stubs ────────────────────────────────────────────────
def test_runs_straight_through_without_interrupts():
    """With interrupts disabled the whole spine executes in one invocation."""
    visited: list[str] = []

    def tracker(name, step):
        def node(state):
            visited.append(name)
            return {"current_step": step}

        return node

    graph = build_graph(
        {
            SCRAPER_KEYWORD: tracker(SCRAPER_KEYWORD, Step.EXTRACTING),
            KEYWORD_REVIEW: tracker(KEYWORD_REVIEW, Step.MATCHING),
            DATA_RETRIEVER: tracker(DATA_RETRIEVER, Step.MATCHING),
            REFACTORER: tracker(REFACTORER, Step.REFACTORING),
            EVALUATOR: tracker(EVALUATOR, Step.EVALUATING),
            HUMAN_REVIEW: tracker(HUMAN_REVIEW, Step.HUMAN_REVIEW),
            COMPILE_PDF: tracker(COMPILE_PDF, Step.COMPLETE),
        },
        checkpointer=InMemorySaver(),
        interrupt_before=(),
    )
    final = graph.invoke(
        base_state(evaluation={"passed": True}, refactored_latex="x", user_decision="accept"),
        config={"configurable": {"thread_id": "t-1"}},
    )
    assert visited == [
        SCRAPER_KEYWORD,
        KEYWORD_REVIEW,
        DATA_RETRIEVER,
        REFACTORER,
        EVALUATOR,
        HUMAN_REVIEW,
        COMPILE_PDF,
    ]
    assert final["current_step"] == Step.COMPLETE


def test_pauses_at_keyword_review_and_resumes():
    """The durable interrupt: state persists, a later call continues it."""
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-2"}}

    graph.invoke(base_state(), config=config)
    snapshot = graph.get_state(config)
    assert snapshot.next == (KEYWORD_REVIEW,), "should be paused before keyword review"

    # Resuming with None continues from the checkpoint rather than restarting.
    graph.invoke(None, config=config)
    assert graph.get_state(config).next == (HUMAN_REVIEW,)


def test_self_correction_loop_runs_then_exits():
    """Two failed evaluations then success: 3 attempts, well inside a cap of 5."""
    refactor_calls = {"n": 0}

    def refactorer(state):
        refactor_calls["n"] += 1
        return {
            "current_step": Step.REFACTORING,
            "refactored_latex": "x",
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    def evaluator(state):
        # Fail while fewer than 3 attempts have been made.
        failing = state.get("iteration_count", 0) < 3
        return {
            "current_step": Step.EVALUATING,
            "evaluation": {"factual_errors": ["bad"] if failing else []},
        }

    graph = build_graph(
        {REFACTORER: refactorer, EVALUATOR: evaluator},
        checkpointer=InMemorySaver(),
        interrupt_before=(HUMAN_REVIEW,),
    )
    graph.invoke(base_state(max_iterations=5), config={"configurable": {"thread_id": "t-3"}})
    assert refactor_calls["n"] == 3


def test_loop_is_bounded_by_max_iterations():
    """A permanently failing evaluator must not loop forever."""
    refactor_calls = {"n": 0}

    def refactorer(state):
        refactor_calls["n"] += 1
        return {
            "current_step": Step.REFACTORING,
            "refactored_latex": "x",
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    def always_fails(state):
        return {
            "current_step": Step.EVALUATING,
            "evaluation": {"factual_errors": ["permanent"]},
        }

    graph = build_graph(
        {REFACTORER: refactorer, EVALUATOR: always_fails},
        checkpointer=InMemorySaver(),
        interrupt_before=(HUMAN_REVIEW,),
    )
    graph.invoke(base_state(max_iterations=2), config={"configurable": {"thread_id": "t-4"}})
    # max_iterations caps TOTAL refactor attempts, not extra retries: 2 means one
    # initial attempt plus one correction. The distinction is a whole LLM call.
    assert refactor_calls["n"] == 2


def test_state_survives_a_rebuilt_graph_object():
    """Checkpoint recovery: a new graph instance resumes the same thread.

    This is the in-memory rehearsal of the Part 10 crash-recovery guarantee.
    """
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t-5"}}

    build_graph(checkpointer=saver).invoke(base_state(job_url="https://kept"), config=config)

    revived = build_graph(checkpointer=saver)
    state = revived.get_state(config)
    assert state.values["job_url"] == "https://kept"
    assert state.next == (KEYWORD_REVIEW,)
