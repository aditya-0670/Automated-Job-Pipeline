"""Graph construction.

The graph is built from a node *registry* rather than by importing agent
functions directly. That keeps this module free of LLM and database imports, so
the topology can be tested with stub nodes -- which is how the routing and
interrupt behaviour get covered without spending a token (Part 2 ships with
stubs; Parts 3-9 swap in the real agents one at a time).

Two interrupt points, both durable:
  * `keyword_review`  -- Layer 4 of extraction: the user confirms keywords.
  * `human_review`    -- the user approves, edits, or requests changes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.routing import route_after_evaluation, route_after_human_review
from app.graph.state import ResumeForgeState
from app.graph.steps import Step

logger = logging.getLogger(__name__)

NodeFn = Callable[[ResumeForgeState], Any]

#: Node names, in one place so the graph, the tests and the UI agree on spelling.
SCRAPER_KEYWORD = "scraper_keyword"
KEYWORD_REVIEW = "keyword_review"
DATA_RETRIEVER = "data_retriever"
REFACTORER = "refactorer"
EVALUATOR = "evaluator"
HUMAN_REVIEW = "human_review"
COMPILE_PDF = "compile_pdf"
FAILED = "failed"

NODE_NAMES: tuple[str, ...] = (
    SCRAPER_KEYWORD,
    KEYWORD_REVIEW,
    DATA_RETRIEVER,
    REFACTORER,
    EVALUATOR,
    HUMAN_REVIEW,
    COMPILE_PDF,
    FAILED,
)


def _passthrough(step: Step) -> NodeFn:
    """A stub node that only advances `current_step`."""

    def node(state: ResumeForgeState) -> dict[str, Any]:
        # .value, not the enum: see the serialisation note in state.py.
        return {"current_step": step.value}

    node.__name__ = f"stub_{step.value.lower()}"
    return node


def default_nodes() -> dict[str, NodeFn]:
    """Stub implementations for every node. Overridden as real agents land."""
    return {
        SCRAPER_KEYWORD: _passthrough(Step.EXTRACTING),
        KEYWORD_REVIEW: _passthrough(Step.KEYWORDS_PENDING),
        DATA_RETRIEVER: _passthrough(Step.MATCHING),
        REFACTORER: _passthrough(Step.REFACTORING),
        EVALUATOR: _passthrough(Step.EVALUATING),
        HUMAN_REVIEW: _passthrough(Step.HUMAN_REVIEW),
        COMPILE_PDF: _passthrough(Step.COMPLETE),
        FAILED: _passthrough(Step.FAILED),
    }


def build_graph(
    nodes: dict[str, NodeFn] | None = None,
    *,
    checkpointer: Any = None,
    interrupt_before: tuple[str, ...] = (KEYWORD_REVIEW, HUMAN_REVIEW),
):
    """Wire and compile the pipeline graph.

    Args:
        nodes: node name -> callable. Missing entries fall back to stubs, so a
            partially implemented pipeline is still runnable end to end.
        checkpointer: a LangGraph checkpointer. Without one the graph cannot be
            interrupted and resumed, so callers that need the human-in-the-loop
            behaviour must supply `InMemorySaver` at minimum.
        interrupt_before: nodes to pause at. Overridable so tests can run the
            pipeline straight through.
    """
    resolved = default_nodes()
    if nodes:
        unknown = set(nodes) - set(NODE_NAMES)
        if unknown:
            raise ValueError(f"Unknown node names: {sorted(unknown)}")
        resolved.update(nodes)

    builder = StateGraph(ResumeForgeState)
    for name, fn in resolved.items():
        builder.add_node(name, fn)

    # ── Linear spine ──
    builder.add_edge(START, SCRAPER_KEYWORD)
    builder.add_edge(SCRAPER_KEYWORD, KEYWORD_REVIEW)
    builder.add_edge(KEYWORD_REVIEW, DATA_RETRIEVER)
    builder.add_edge(DATA_RETRIEVER, REFACTORER)
    builder.add_edge(REFACTORER, EVALUATOR)

    # ── The self-correction loop (Part 7) ──
    builder.add_conditional_edges(
        EVALUATOR,
        route_after_evaluation,
        {
            "refactor_again": REFACTORER,  # cycle back with feedback
            "human_review": HUMAN_REVIEW,
            "failed": FAILED,
        },
    )

    # ── Human review dispatch (Part 8) ──
    builder.add_conditional_edges(
        HUMAN_REVIEW,
        route_after_human_review,
        {
            "compile": COMPILE_PDF,
            "refactor_again": REFACTORER,
            "evaluate_again": EVALUATOR,
            "extract_again": SCRAPER_KEYWORD,
        },
    )

    builder.add_edge(COMPILE_PDF, END)
    builder.add_edge(FAILED, END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before),
    )


def graph_mermaid(nodes: dict[str, NodeFn] | None = None) -> str:
    """Render the topology as Mermaid, for the docs and for sanity checking."""
    return build_graph(nodes).get_graph().draw_mermaid()
