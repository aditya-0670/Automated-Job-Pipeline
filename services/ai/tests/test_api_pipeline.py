"""Part 11: the pipeline HTTP surface.

The graph is replaced with stub nodes and an in-memory checkpointer, so these
tests exercise the *surface* -- status codes, the trust boundary, SSE framing,
background execution, resumption -- without spending a token or needing Postgres.
The nodes themselves are covered by Parts 3-9.

An `httpx.AsyncClient` over the ASGI app is used rather than `TestClient`,
because the endpoints hand work to background tasks and the assertions need to
share one event loop with them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.human_review import human_review_agent
from app.config import get_settings
from app.graph.builder import (
    COMPILE_PDF,
    DATA_RETRIEVER,
    EVALUATOR,
    HUMAN_REVIEW,
    REFACTORER,
    SCRAPER_KEYWORD,
    build_graph,
)
from app.graph.events import step_event
from app.graph.runner import RunRegistry
from app.graph.steps import Step
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))
REWRITTEN = RESUME.replace(r"\end{document}", "% rewritten\n" + r"\end{document}")

KEY = "test-internal-key"
AUTH = {"x-internal-key": KEY}


def run_body(**overrides):
    return {
        "user_id": "u-aditya",
        "user_latex": RESUME,
        "user_profile": PROFILE,
        "job_text": "We need a backend engineer with C++, Docker and Kafka experience. " * 10,
        **overrides,
    }


def stub(step: Step, **writes):
    async def node(state):
        return {
            "current_step": step.value,
            "events": [step_event(state, step, detail=f"stub {step.value.lower()}")],
            **writes,
        }

    return node


def stub_graph(*, pdf_path: str = "", compile_fails: bool = False):
    async def compile_node(state):
        if compile_fails:
            raise RuntimeError("pdflatex exploded")
        return {
            "current_step": Step.COMPLETE.value,
            "pdf_path": pdf_path,
            "events": [step_event(state, Step.COMPLETE, detail="stub compile")],
        }

    return build_graph(
        {
            SCRAPER_KEYWORD: stub(Step.EXTRACTING, keywords=[{"term": "Docker", "score": 9.0}]),
            DATA_RETRIEVER: stub(Step.MATCHING),
            REFACTORER: stub(Step.REFACTORING, refactored_latex=REWRITTEN),
            EVALUATOR: stub(Step.EVALUATING, evaluation={}),
            HUMAN_REVIEW: human_review_agent,
            COMPILE_PDF: compile_node,
        },
        checkpointer=InMemorySaver(),
    )


@pytest.fixture
async def client(monkeypatch, tmp_path):
    """The real app, with the graph swapped for stubs."""
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    app.state.matcher = None
    app.state.runs = RunRegistry()
    app.state.graph = stub_graph(pdf_path=str(tmp_path / "resume.pdf"))
    app.state.ready = True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ai") as c:
        c.pdf_path = tmp_path / "resume.pdf"  # type: ignore[attr-defined]
        yield c
    await app.state.runs.shutdown()
    get_settings.cache_clear()


async def wait_for(client, session_id, predicate, *, what, timeout=5.0):
    """Poll the status endpoint until the background run reaches a state."""
    deadline = asyncio.get_running_loop().time() + timeout
    body = None
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/internal/pipeline/{session_id}", headers=AUTH)).json()
        if predicate(body):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"session {session_id} never reached {what}: {body}")


async def wait_until_paused(client, session_id, at, **kw):
    return await wait_for(
        client, session_id, lambda b: b["paused_at"] == at, what=f"pause at {at}", **kw
    )


async def wait_until_complete(client, session_id, **kw):
    return await wait_for(client, session_id, lambda b: b["is_complete"], what="completion", **kw)


async def start(client, **overrides):
    response = await client.post("/internal/pipeline/run", json=run_body(**overrides), headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()["session_id"]


# ── Trust boundary ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/internal/pipeline/run"),
        ("post", "/internal/pipeline/abc/resume"),
        ("get", "/internal/pipeline/abc"),
        ("get", "/internal/pipeline/abc/events"),
        ("get", "/internal/pipeline/abc/pdf"),
    ],
)
async def test_every_pipeline_route_requires_the_internal_key(client, method, path):
    response = await client.request(method, path, json={} if method == "post" else None)
    assert response.status_code == 401


async def test_the_pipeline_is_503_when_there_is_no_checkpointer(client):
    """A pipeline that cannot survive a restart is worse than an honest refusal."""
    app.state.graph = None
    try:
        response = await client.post("/internal/pipeline/run", json=run_body(), headers=AUTH)
        assert response.status_code == 503
        assert "durable" in response.json()["detail"]
    finally:
        app.state.graph = stub_graph()


# ── Starting a run ───────────────────────────────────────────────────────
async def test_run_accepts_the_work_and_returns_immediately(client):
    response = await client.post("/internal/pipeline/run", json=run_body(), headers=AUTH)
    body = response.json()
    # 202: accepted, not completed. The client follows events_url to watch it.
    assert response.status_code == 202
    assert body["events_url"] == f"/internal/pipeline/{body['session_id']}/events"
    await wait_until_paused(client, body["session_id"], "keyword_review")


async def test_a_run_needs_a_posting(client):
    response = await client.post("/internal/pipeline/run", json=run_body(job_text=""), headers=AUTH)
    assert response.status_code == 422


async def test_reusing_a_session_id_is_refused_rather_than_branching(client):
    """A session id is a checkpoint thread id: reuse would resume, not start."""
    session_id = await start(client, session_id="fixed-1")
    await wait_until_paused(client, session_id, "keyword_review")

    response = await client.post(
        "/internal/pipeline/run", json=run_body(session_id="fixed-1"), headers=AUTH
    )
    assert response.status_code == 409
    assert "resume" in response.json()["detail"]


# ── Status ───────────────────────────────────────────────────────────────
async def test_status_is_404_for_an_unknown_session(client):
    response = await client.get("/internal/pipeline/nope", headers=AUTH)
    assert response.status_code == 404


async def test_status_carries_the_keyword_gate_payload(client):
    session_id = await start(client)
    body = await wait_until_paused(client, session_id, "keyword_review")
    assert body["keyword_review"]["keywords"] == [{"term": "Docker", "score": 9.0}]
    assert 0.0 < body["progress"] < 1.0
    assert body["label"]


async def test_status_carries_the_review_diff_only_at_the_review_gate(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    assert (
        "human_review"
        not in (await client.get(f"/internal/pipeline/{session_id}", headers=AUTH)).json()
    )

    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    body = await wait_until_paused(client, session_id, "human_review")
    assert body["human_review"]["summary"]["total_sections"] > 1
    assert body["human_review"]["latex"] == REWRITTEN


async def test_the_diff_body_can_be_omitted_for_a_cheap_poll(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")

    body = (
        await client.get(
            f"/internal/pipeline/{session_id}", params={"include_diff": False}, headers=AUTH
        )
    ).json()
    assert all("diff" not in s for s in body["human_review"]["sections"])


# ── Resuming ─────────────────────────────────────────────────────────────
async def test_resume_dispatches_on_where_the_session_is_paused(client):
    """One endpoint: the server knows where the session is, so the client need not."""
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")

    response = await client.post(
        f"/internal/pipeline/{session_id}/resume",
        json={"keywords": [{"term": "Kafka"}]},
        headers=AUTH,
    )
    assert response.status_code == 202
    body = await wait_until_paused(client, session_id, "human_review")
    assert body["is_paused"]


async def test_accepting_the_review_runs_the_session_to_completion(client):
    client.pdf_path.write_bytes(b"%PDF-1.7 stub")
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")

    await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )
    body = await wait_until_complete(client, session_id)
    assert body["step"] == Step.COMPLETE
    assert body["pdf_ready"]


async def test_resuming_an_unknown_session_is_404(client):
    response = await client.post("/internal/pipeline/nope/resume", json={}, headers=AUTH)
    assert response.status_code == 404


async def test_resuming_a_finished_session_is_409(client):
    client.pdf_path.write_bytes(b"%PDF-1.7 stub")
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")
    await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )
    await wait_until_complete(client, session_id)

    response = await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "nonsense"},
        {"decision": "request_changes"},
        {"decision": "edit"},
    ],
)
async def test_invalid_review_input_is_rejected_before_any_work_starts(client, body):
    """A 400 the caller can act on, not a failure discovered on the stream."""
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")

    response = await client.post(f"/internal/pipeline/{session_id}/resume", json=body, headers=AUTH)
    assert response.status_code == 400
    # Still paused and still resumable: nothing was consumed by the bad request.
    assert (await wait_until_paused(client, session_id, "human_review"))["is_paused"]


async def test_an_empty_keyword_set_is_rejected(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    response = await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"keywords": []}, headers=AUTH
    )
    assert response.status_code == 400


# ── SSE ──────────────────────────────────────────────────────────────────
def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Minimal SSE parser: (event name, decoded data) per frame."""
    frames = []
    # Frames are CRLF-separated on the wire, per the SSE spec.
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith(":"):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        if "data" in fields:
            frames.append((fields.get("event", "message"), json.loads(fields["data"])))
    return frames


async def read_stream(client, session_id, headers=None, timeout=5.0):
    async with client.stream(
        "GET",
        f"/internal/pipeline/{session_id}/events",
        headers={**AUTH, **(headers or {})},
        timeout=timeout,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse((await response.aread()).decode())


async def test_the_stream_replays_progress_then_ends_at_the_pause(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")

    frames = await read_stream(client, session_id)
    kinds = [name for name, _ in frames]
    assert "progress" in kinds
    # A pause is a normal ending: the graph is waiting on a person, and holding
    # the connection open would be waiting on them too.
    assert kinds[-1] == "paused"
    assert frames[-1][1]["paused_at"] == "keyword_review"


async def test_a_reconnecting_client_receives_exactly_the_gap(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    # Past the second gate, so there is a real backlog to resume into.
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")

    first = await read_stream(client, session_id)
    progress = [data for name, data in first if name == "progress"]
    assert len(progress) >= 2

    resumed = await read_stream(
        client, session_id, headers={"last-event-id": str(progress[0]["sequence"])}
    )
    seen = [data["sequence"] for name, data in resumed if name == "progress"]
    assert seen == [e["sequence"] for e in progress[1:]]


async def test_a_resuming_client_is_not_told_about_the_stop_it_just_answered(client):
    """The race that made the browser sit at a gate it had already answered.

    A client answers a gate and reopens its stream at once; the background task
    has not written a checkpoint yet, so a naive reader sees the *old* pause and
    calls it the end. Being caught up means "what happens next?", not "where am
    I?" -- so the already-known stop is waited through.
    """
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")

    frames = await read_stream(client, session_id)
    last = [d["sequence"] for name, d in frames if name == "progress"][-1]

    # Resume and immediately reconnect, exactly as the browser does.
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    resumed = await read_stream(client, session_id, headers={"last-event-id": str(last)})

    kinds = [name for name, _ in resumed]
    # It must carry the *new* work, not an instant replay of the old pause.
    assert "progress" in kinds, kinds
    assert kinds[-1] == "paused"
    assert resumed[-1][1]["paused_at"] == "human_review"


async def test_a_fresh_reader_is_still_told_where_a_paused_session_is(client):
    """The other half: a page load starts at sequence 0 and must be told."""
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    frames = await read_stream(client, session_id)
    assert frames[-1][0] == "paused"


async def test_an_unparseable_last_event_id_replays_rather_than_failing(client):
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")

    frames = await read_stream(client, session_id, headers={"last-event-id": "garbage"})
    assert [name for name, _ in frames].count("progress") >= 2


async def test_the_stream_reports_an_unknown_session_rather_than_hanging(client):
    frames = await read_stream(client, "nope")
    assert frames[0][0] == "error"


# ── PDF ──────────────────────────────────────────────────────────────────
async def test_the_pdf_is_409_while_the_session_is_still_running(client):
    """409, not 404: a 404 would tell a polling client to give up."""
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    response = await client.get(f"/internal/pipeline/{session_id}/pdf", headers=AUTH)
    assert response.status_code == 409


async def test_the_pdf_is_served_once_compiled(client):
    client.pdf_path.write_bytes(b"%PDF-1.7 stub")
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")
    await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )
    await wait_until_complete(client, session_id)

    response = await client.get(f"/internal/pipeline/{session_id}/pdf", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


async def test_a_checkpointed_path_with_no_file_is_410_not_500(client):
    """A restarted container with a non-persistent volume, told honestly."""
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")
    await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )
    await wait_until_complete(client, session_id)

    response = await client.get(f"/internal/pipeline/{session_id}/pdf", headers=AUTH)
    assert response.status_code == 410


# ── Background failure ───────────────────────────────────────────────────
async def test_a_crash_in_a_background_run_becomes_durable_state(client):
    """Otherwise the HTTP request has returned 202 and the crash is invisible."""
    app.state.graph = stub_graph(compile_fails=True)
    session_id = await start(client)
    await wait_until_paused(client, session_id, "keyword_review")
    await client.post(f"/internal/pipeline/{session_id}/resume", json={}, headers=AUTH)
    await wait_until_paused(client, session_id, "human_review")
    await client.post(
        f"/internal/pipeline/{session_id}/resume", json={"decision": "accept"}, headers=AUTH
    )

    body = await wait_for(
        client, session_id, lambda b: b["step"] == Step.FAILED, what="a recorded failure"
    )
    assert "pdflatex exploded" in body["error"]
    # And the stream terminates with a reason rather than waiting forever.
    frames = await read_stream(client, session_id)
    assert frames[-1][0] == "done"
    assert "pdflatex exploded" in frames[-1][1]["error"]
