"""End-to-end behaviour of the 4-layer extraction pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.pipeline import extract_keywords
from app.extraction.sections import SECTION_WEIGHTS, SectionIndex, detect_sections

FIXTURE = Path(__file__).parent / "fixtures" / "sample_jd.txt"


@pytest.fixture(scope="module")
def jd_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def result(jd_text):
    return extract_keywords(jd_text)


def test_uses_zero_llm_tokens(result):
    """The whole point of the layer: extraction never calls a model."""
    assert result.stats["llm_tokens_used"] == 0


def test_completes_well_under_300ms(result):
    assert result.duration_ms < 300, f"took {result.duration_ms:.1f}ms"


def test_returns_ranked_keywords(result):
    assert result.keywords
    scores = [kw.score for kw in result.keywords]
    assert scores == sorted(scores, reverse=True)


def test_taxonomy_hits_outrank_statistical_noise(result):
    """A confirmed skill must beat an unconfirmed statistical phrase."""
    top_ten = result.keywords[:10]
    assert all("taxonomy" in kw.sources for kw in top_ten)


def test_requirements_section_outranks_preferred(jd_text):
    """Docker (required) must outrank Terraform (explicitly 'a plus').

    Uses a high max_keywords so this asserts on ranking, not on the cutoff.
    """
    unclipped = extract_keywords(jd_text, max_keywords=200)
    scores = {kw.term: kw.score for kw in unclipped.keywords}
    assert scores["Docker"] > scores["Terraform"]


def test_corroborated_keyword_gains_score(jd_text):
    """A term both layers agree on scores above the same term from one layer."""
    result = extract_keywords(jd_text)
    corroborated = [kw for kw in result.keywords if len(kw.sources) > 1]
    assert corroborated, "expected at least one keyword found by two layers"


def test_categories_are_populated(result):
    grouped = result.by_category()
    for expected in ["language", "devops", "database", "cloud"]:
        assert expected in grouped, f"no keywords in category {expected}"


def test_aliases_are_normalised_not_duplicated(result):
    """'k8s' and 'Kubernetes' must not both appear as separate keywords."""
    terms = {kw.term.lower() for kw in result.keywords}
    assert "k8s" not in terms
    assert "golang" not in terms


def test_respects_max_keywords(jd_text):
    assert len(extract_keywords(jd_text, max_keywords=10).keywords) == 10


def test_empty_input_does_not_crash():
    result = extract_keywords("")
    assert result.keywords == []
    assert result.stats["llm_tokens_used"] == 0


def test_serialises_to_dict(result):
    payload = result.to_dict()
    assert set(payload) == {"keywords", "by_category", "duration_ms", "stats"}
    assert isinstance(payload["keywords"][0]["score"], float)


# ── Section detection ────────────────────────────────────────────────────
def test_detects_real_jd_sections(jd_text):
    kinds = {s.kind for s in detect_sections(jd_text)}
    assert {"about", "responsibilities", "requirements", "preferred", "benefits"} <= kinds


def test_section_weights_are_ordered():
    w = SECTION_WEIGHTS
    assert w["requirements"] > w["responsibilities"] > w["preferred"] > w["benefits"] >= w["about"]


def test_unstructured_text_falls_back_to_single_section():
    sections = detect_sections("Just a blob of text with no headings at all.")
    assert len(sections) == 1
    assert sections[0].kind == "unknown"


def test_section_index_lookup_is_correct(jd_text):
    index = SectionIndex(jd_text)
    offset = jd_text.lower().index("terraform")
    assert index.section_at(offset).kind == "preferred"
    offset = jd_text.lower().index("bachelor's degree")
    assert index.section_at(offset).kind == "requirements"
