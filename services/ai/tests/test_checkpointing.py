"""Part 10: durable state against a real Postgres.

These are integration tests by necessity. The claim being verified is that a
session survives the process that created it, and a mocked checkpointer would
verify nothing about serialisation, which is where this actually breaks.

Skipped when no database is reachable, so the default suite stays hermetic.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

from app.graph.builder import (
    HUMAN_REVIEW,
    KEYWORD_REVIEW,
    REFACTORER,
    build_graph,
)
from app.graph.checkpointer import async_checkpointer, latest_state, thread_config
from app.graph.state import initial_state
from app.graph.steps import Step

DSN = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")


async def _database_available() -> bool:
    if not DSN:
        return False
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not DSN, reason="DATABASE_URL not set; checkpointing tests need real Postgres"
)


#: A fresh suffix per process, so every test gets thread ids no previous run has
#: used. Fixed ids look harmless -- most assertions here are on last-write-wins
#: fields, which a leftover thread cannot corrupt -- but `events` uses an
#: *append* reducer, so re-running against a database that already holds the
#: thread accumulates the previous run's events and the test fails. That made
#: this suite green exactly once per database: fine in CI, which gets a clean
#: service container, and misleading on a developer's long-lived Compose stack.
#: Unique ids are also what the product itself demands -- a thread id is a
#: session id, and Part 11 answers a reused one with 409 rather than resuming it.
RUN = uuid.uuid4().hex[:8]

#: Thread ids handed out during the test that is currently running.
THREADS: set[str] = set()


def tid(name: str) -> str:
    thread = f"{name}-{RUN}"
    THREADS.add(thread)
    return thread


@pytest.fixture
async def saver():
    if not await _database_available():
        pytest.skip("Postgres not reachable")
    async with async_checkpointer(DSN) as checkpointer:
        yield checkpointer
        # Leave the database as we found it; otherwise every run would strand a
        # thread on a developer's stack forever.
        for thread in THREADS:
            with contextlib.suppress(Exception):
                await checkpointer.adelete_thread(thread)
        THREADS.clear()


def state_for(session_id: str, **overrides):
    s = initial_state(
        session_id=session_id,
        user_id="u-1",
        user_latex=r"\documentclass{article}\begin{document}x\end{document}",
        user_profile={"skills": ["Python"]},
        job_url="https://example.com/job",
    )
    s.update(overrides)
    return s


# ── Config helper ────────────────────────────────────────────────────────
def test_thread_config_requires_a_session_id():
    """A typo'd empty id would silently start a fresh thread and lose the session."""
    with pytest.raises(ValueError, match="session_id is required"):
        thread_config("")


def test_thread_config_shape():
    assert thread_config("s-1") == {"configurable": {"thread_id": "s-1"}}


# ── Durability ───────────────────────────────────────────────────────────
async def test_state_persists_to_postgres(saver):
    graph = build_graph(checkpointer=saver)
    config = thread_config(tid("cp-persist"))

    await graph.ainvoke(state_for(tid("cp-persist"), job_url="https://kept.example"), config=config)

    snapshot = await graph.aget_state(config)
    assert snapshot.values["job_url"] == "https://kept.example"
    assert snapshot.next == (KEYWORD_REVIEW,)


async def test_a_new_graph_object_resumes_the_same_session(saver):
    """The crash-recovery guarantee: state lives in the database, not the process.

    A second graph instance -- standing in for a restarted container or a
    different replica -- must pick the session up exactly where it paused.
    """
    config = thread_config(tid("cp-recover"))

    first = build_graph(checkpointer=saver)
    await first.ainvoke(
        state_for(tid("cp-recover"), job_url="https://survives.example"), config=config
    )

    # Nothing is carried over but the thread id.
    revived = build_graph(checkpointer=saver)
    snapshot = await revived.aget_state(config)

    assert snapshot.values["job_url"] == "https://survives.example"
    assert snapshot.next == (KEYWORD_REVIEW,), "must resume paused, not restart"


async def test_resume_continues_rather_than_restarting(saver):
    """Invoking with None resumes; it must not re-run completed nodes."""
    runs: list[str] = []

    def counting_node(state):
        runs.append("scraper")
        return {"current_step": Step.EXTRACTING.value}

    from app.graph.builder import SCRAPER_KEYWORD

    config = thread_config(tid("cp-resume"))
    graph = build_graph({SCRAPER_KEYWORD: counting_node}, checkpointer=saver)

    await graph.ainvoke(state_for(tid("cp-resume")), config=config)
    assert runs == ["scraper"]

    # Resume from the interrupt. The scraper already ran and must not run again --
    # re-running it would repeat work the user already paid for.
    await graph.ainvoke(None, config=config)
    assert runs == ["scraper"], "a completed node must not re-execute on resume"


async def test_llm_work_is_not_repeated_after_a_crash(saver):
    """The concrete cost argument: an expensive node runs once across a restart."""
    llm_calls = {"n": 0}

    def expensive_refactorer(state):
        llm_calls["n"] += 1
        return {
            "current_step": Step.REFACTORING.value,
            "refactored_latex": "generated",
            "iteration_count": state.get("iteration_count", 0) + 1,
        }

    config = thread_config(tid("cp-nowaste"))

    # Run until the human-review interrupt, which is after the refactorer.
    graph = build_graph({REFACTORER: expensive_refactorer}, checkpointer=saver)
    await graph.ainvoke(state_for(tid("cp-nowaste")), config=config)
    await graph.ainvoke(None, config=config)  # past keyword_review
    assert llm_calls["n"] == 1

    # Simulate the process dying and coming back.
    restarted = build_graph({REFACTORER: expensive_refactorer}, checkpointer=saver)
    snapshot = await restarted.aget_state(config)
    assert snapshot.next == (HUMAN_REVIEW,)
    assert snapshot.values["refactored_latex"] == "generated"
    assert llm_calls["n"] == 1, "the expensive node must not re-run after recovery"


async def test_sessions_are_isolated_by_thread_id(saver):
    """Two users, two threads, no shared state. Isolation is a property of the key."""
    graph = build_graph(checkpointer=saver)

    await graph.ainvoke(
        state_for(tid("cp-user-a"), job_url="https://a.example"),
        config=thread_config(tid("cp-user-a")),
    )
    await graph.ainvoke(
        state_for(tid("cp-user-b"), job_url="https://b.example"),
        config=thread_config(tid("cp-user-b")),
    )

    a = await graph.aget_state(thread_config(tid("cp-user-a")))
    b = await graph.aget_state(thread_config(tid("cp-user-b")))
    assert a.values["job_url"] == "https://a.example"
    assert b.values["job_url"] == "https://b.example"
    assert a.values["user_id"] == b.values["user_id"] == "u-1"  # same user, distinct sessions


async def test_checkpoint_history_is_retained(saver):
    """A checkpoint per node, which is what makes time-travel debugging possible."""
    config = thread_config(tid("cp-history"))
    graph = build_graph(checkpointer=saver)
    await graph.ainvoke(state_for(tid("cp-history")), config=config)

    history = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert len(history) >= 2, "expected a checkpoint per completed node"
    # History is newest-first.
    assert history[0].values["current_step"] != Step.INIT.value


async def test_events_accumulate_rather_than_overwrite(saver):
    """The append reducer must survive a round trip through serialisation."""
    from app.graph.builder import DATA_RETRIEVER, SCRAPER_KEYWORD

    def emit(name):
        def node(state):
            return {"events": [{"node": name}], "current_step": Step.EXTRACTING.value}

        return node

    config = thread_config(tid("cp-events"))
    graph = build_graph(
        {SCRAPER_KEYWORD: emit("first"), DATA_RETRIEVER: emit("second")},
        checkpointer=saver,
        interrupt_before=(HUMAN_REVIEW,),
    )
    await graph.ainvoke(state_for(tid("cp-events")), config=config)

    snapshot = await graph.aget_state(config)
    nodes = [e.get("node") for e in snapshot.values["events"]]
    assert nodes == ["first", "second"], f"events did not accumulate: {nodes}"


async def test_latest_state_reads_without_advancing(saver):
    """A page reload must be able to ask "where am I?" without running a node."""
    runs = {"n": 0}

    def counting(state):
        runs["n"] += 1
        return {"current_step": Step.EXTRACTING.value}

    from app.graph.builder import SCRAPER_KEYWORD

    graph = build_graph({SCRAPER_KEYWORD: counting}, checkpointer=saver)
    await graph.ainvoke(state_for(tid("cp-read")), config=thread_config(tid("cp-read")))

    view = await latest_state(graph, tid("cp-read"))
    assert view is not None
    assert view["is_paused"] is True
    assert view["next"] == [KEYWORD_REVIEW]
    assert runs["n"] == 1, "reading state must not execute a node"


async def test_unknown_session_reads_as_none(saver):
    graph = build_graph(checkpointer=saver)
    assert await latest_state(graph, "cp-does-not-exist") is None


async def test_full_state_survives_serialisation(saver):
    """Every field must round-trip. A non-serialisable value fails only here."""
    config = thread_config(tid("cp-roundtrip"))
    graph = build_graph(checkpointer=saver)
    original = state_for(
        tid("cp-roundtrip"),
        keywords=[{"term": "Kubernetes", "score": 20.0, "sources": ["taxonomy"]}],
        extraction_stats={"llm_tokens_used": 0, "sections_detected": ["requirements"]},
        warnings=["one warning"],
    )
    await graph.ainvoke(original, config=config)

    values = (await graph.aget_state(config)).values
    assert values["keywords"][0]["term"] == "Kubernetes"
    assert values["extraction_stats"]["llm_tokens_used"] == 0
    assert values["warnings"] == ["one warning"]
    assert type(values["current_step"]) is str
