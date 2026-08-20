"""HTTP surface: probes, the trust boundary, and the extraction endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

JD = (Path(__file__).parent / "fixtures" / "sample_jd.txt").read_text(encoding="utf-8")
KEY = "test-internal-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", KEY)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    # The context manager runs lifespan, so the automaton is built as in prod.
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def auth() -> dict[str, str]:
    return {"x-internal-key": KEY}


# ── Probes ───────────────────────────────────────────────────────────────
def test_health_needs_no_credential(client):
    """A load balancer cannot present a key, so probes must be open."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_the_built_automaton(client):
    body = client.get("/ready").json()
    assert body["status"] == "ready"
    assert body["taxonomy_skills"] > 100
    assert body["taxonomy_patterns"] > 400


def test_ready_reports_mock_when_unconfigured(client):
    assert "mock" in client.get("/ready").json()["llm"]


def test_request_id_is_echoed_for_tracing(client):
    response = client.get("/health", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


def test_request_id_is_generated_when_absent(client):
    assert client.get("/health").headers["x-request-id"]


# ── Trust boundary ───────────────────────────────────────────────────────
def test_extract_rejects_missing_key(client):
    response = client.post("/internal/extract", json={"job_text": JD})
    assert response.status_code == 401


def test_extract_rejects_wrong_key(client):
    response = client.post(
        "/internal/extract", json={"job_text": JD}, headers={"x-internal-key": "wrong"}
    )
    assert response.status_code == 401


def test_extract_rejects_key_of_different_length(client):
    """compare_digest raises on length mismatch if misused; must still 401."""
    response = client.post(
        "/internal/extract", json={"job_text": JD}, headers={"x-internal-key": "x"}
    )
    assert response.status_code == 401


# ── Extraction ───────────────────────────────────────────────────────────
def test_extract_returns_ranked_keywords(client):
    body = client.post("/internal/extract", json={"job_text": JD}, headers=auth()).json()
    assert body["scrape_tier"] == "manual"
    assert body["stats"]["llm_tokens_used"] == 0
    terms = [kw["term"] for kw in body["keywords"]]
    assert "Kubernetes" in terms
    scores = [kw["score"] for kw in body["keywords"]]
    assert scores == sorted(scores, reverse=True)


def test_extract_honours_max_keywords(client):
    body = client.post(
        "/internal/extract", json={"job_text": JD, "max_keywords": 5}, headers=auth()
    ).json()
    assert len(body["keywords"]) == 5


def test_extract_requires_some_input(client):
    response = client.post("/internal/extract", json={}, headers=auth())
    assert response.status_code == 422


def test_extract_rejects_out_of_range_max_keywords(client):
    response = client.post(
        "/internal/extract", json={"job_text": JD, "max_keywords": 9999}, headers=auth()
    )
    assert response.status_code == 422


def test_unscrapable_url_is_422_not_500(client, monkeypatch):
    """The request was well-formed; the page was not usable. That is not a bug."""
    from app.clients import scraper

    async def boom(url):
        raise scraper.ScrapeError("Could not read this job posting automatically.")

    monkeypatch.setattr(scraper, "scrape_job_posting", boom)
    response = client.post(
        "/internal/extract", json={"job_url": "https://blocked.example"}, headers=auth()
    )
    assert response.status_code == 422
    assert "Could not read" in response.json()["detail"]


def test_openapi_documents_the_service(client):
    schema = client.get("/openapi.json").json()
    assert "/internal/extract" in schema["paths"]
    assert "/health" in schema["paths"]
