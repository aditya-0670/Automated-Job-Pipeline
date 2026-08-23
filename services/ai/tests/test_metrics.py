"""Part 20: the metrics surface.

Metrics are easy to add and easy to have silently stop working -- a renamed
label or a metric nobody increments produces a dashboard of flat zeros, which
looks exactly like a healthy quiet system. These tests assert the numbers move
when the thing they measure happens.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from app import metrics
from app.config import get_settings
from app.extraction.pipeline import extract_keywords

JD = (Path(__file__).parent / "fixtures" / "sample_jd.txt").read_text(encoding="utf-8")


def scrape() -> str:
    return generate_latest().decode()


def sample(text: str, name: str) -> float:
    """The value of a single named sample, or 0.0 if it has not appeared yet.

    A labelled series does not exist in the scrape until something observes it,
    so "absent" and "zero" are the same statement about a counter -- and a test
    that treats absence as an error can only ever run second.
    """
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_the_endpoint_serves_prometheus_text(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    get_settings.cache_clear()
    from app.main import app

    app.state.ready = True
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "resumeforge_pipeline_runs_total" in response.text
    get_settings.cache_clear()


def test_the_scrape_endpoint_needs_no_credential(monkeypatch):
    """A scraper is infrastructure and cannot present one -- the same reason the
    probes are open. It is safe because the service has no public address."""
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    get_settings.cache_clear()
    from app.main import app

    app.state.ready = True
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200
    get_settings.cache_clear()


def test_extraction_is_measured():
    before = sample(scrape(), "resumeforge_extraction_duration_seconds_count")
    extract_keywords(JD)
    after = sample(scrape(), "resumeforge_extraction_duration_seconds_count")
    assert after == before + 1


def test_token_spend_is_labelled_by_model_step_and_kind():
    class FakeResponse:
        model = "gemini-3.7-flash"
        input_tokens = 1000
        output_tokens = 500
        thinking_tokens = 128

    metrics.record_llm_response("refactor", FakeResponse())
    text = scrape()
    # Thinking tokens are separated from output on purpose: they are the line
    # item that moves when a thinking budget changes.
    assert (
        'resumeforge_llm_tokens_total{kind="thinking",model="gemini-3.7-flash",step="refactor"}'
        in text
    )
    assert 'kind="input"' in text and 'kind="output"' in text


def test_guardrail_failures_are_counted_by_kind():
    metrics.guardrail_failures.labels("factual").inc()
    text = scrape()
    assert 'resumeforge_guardrail_failures_total{kind="factual"}' in text


def test_no_metric_is_labelled_by_session():
    """Session id is unbounded. One series per session is how a Prometheus
    instance falls over, and it is an easy label to add without thinking."""
    assert "session_id" not in scrape()


def test_pipeline_duration_buckets_cover_a_real_run():
    """The default buckets stop at 10s, which would put every full run -- 30 to
    90 seconds -- in +Inf and make the histogram useless for exactly the thing
    it exists to measure."""
    text = scrape()
    buckets = [
        line
        for line in text.splitlines()
        if line.startswith("resumeforge_pipeline_duration_seconds_bucket")
    ]
    assert any('le="60.0"' in b for b in buckets)
    assert any('le="300.0"' in b for b in buckets)


def test_timed_records_even_when_the_block_raises():
    """A node that fails still took time, and it is usually the slow one."""
    before = sample(scrape(), 'resumeforge_node_duration_seconds_count{node="boom"}')
    try:
        with metrics.timed(metrics.node_duration, "boom"):
            raise RuntimeError("the node failed")
    except RuntimeError:
        pass
    after = sample(scrape(), 'resumeforge_node_duration_seconds_count{node="boom"}')
    assert after == before + 1
