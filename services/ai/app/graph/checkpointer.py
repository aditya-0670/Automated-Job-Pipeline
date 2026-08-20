"""Durable graph state -- the mechanism behind "fault-tolerant".

LangGraph writes a checkpoint after **every node**, keyed by `thread_id`. Three
properties follow, and they are the whole reason this file exists:

  1. **Crash recovery.** If the process dies between the refactorer and the
     evaluator, the next call with the same `thread_id` resumes from the last
     completed node. The LLM spend already incurred is not repeated.
  2. **Durable human-in-the-loop.** The interrupts at `keyword_review` and
     `human_review` can last minutes or survive a deploy, because the pause is a
     database row rather than a suspended coroutine.
  3. **Statelessness.** The process holds no session state, so any replica can
     serve any session. That is what makes horizontal scaling possible -- and it
     is a more precise claim than "distributed execution".

`thread_id` is the session id. Two users never share a thread, so isolation is a
property of the key rather than of application logic.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def thread_config(session_id: str, **extra: Any) -> dict[str, Any]:
    """Build the config that binds an invocation to one session's checkpoint.

    Every graph call needs this. Passing a bare dict at each call site is how
    a typo silently starts a fresh thread and loses the user's session.
    """
    if not session_id:
        raise ValueError("session_id is required: it is the checkpoint thread_id")
    return {"configurable": {"thread_id": session_id, **extra}}


@asynccontextmanager
async def async_checkpointer(dsn: str | None = None):
    """Yield an `AsyncPostgresSaver`, creating its tables on first use.

    `.setup()` is idempotent -- it creates the checkpoint tables and applies
    LangGraph's own migrations. Calling it on every boot is deliberate: it means
    a fresh database needs no separate migration step, and the AI service owns
    its schema rather than depending on the API service's Prisma migrations
    having run first. Two owners, one database, no shared tables.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    settings = get_settings()
    conn_string = dsn or settings.psycopg_dsn

    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        await saver.setup()
        logger.info("Postgres checkpointer ready")
        yield saver


def sync_checkpointer(dsn: str | None = None):
    """Sync variant, for scripts and migrations. Caller closes the context."""
    from langgraph.checkpoint.postgres import PostgresSaver

    settings = get_settings()
    return PostgresSaver.from_conn_string(dsn or settings.psycopg_dsn)


async def latest_state(graph: Any, session_id: str) -> dict[str, Any] | None:
    """Read a session's current state without advancing the graph.

    Used by the API gateway to answer "where is my session?" after a page
    reload, which must not re-run a node.
    """
    snapshot = await graph.aget_state(thread_config(session_id))
    if snapshot is None or not snapshot.values:
        return None
    return {
        "values": snapshot.values,
        # `next` is empty when the graph has finished; otherwise it names the
        # node the graph is paused before.
        "next": list(snapshot.next),
        "is_paused": bool(snapshot.next),
    }
