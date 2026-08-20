"""Part 4: profile indexing and evidence retrieval.

Uses the real profile fixture (derived from the actual resume) and the real
Salesforce posting fixture, so the assertions are about genuine relevance
judgements rather than toy data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.data_retriever import MAX_EVIDENCE_ITEMS, data_retriever_agent
from app.extraction.pipeline import extract_keywords
from app.graph.state import initial_state
from app.graph.steps import Step
from app.matching.profile_index import ProfileIndex, flatten_profile

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))
JD = (FIXTURES / "sample_jd.txt").read_text(encoding="utf-8")
RESUME_TEX = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def keywords():
    return [kw.to_dict() for kw in extract_keywords(JD, max_keywords=40).keywords]


@pytest.fixture(scope="module")
def index():
    return ProfileIndex(PROFILE)


def state_with(keywords, **overrides):
    s = initial_state(
        session_id="s-1",
        user_id="u-aditya",
        user_latex=RESUME_TEX,
        user_profile=PROFILE,
        job_text=JD,
    )
    s["keywords"] = keywords
    s.update(overrides)
    return s


# ── Flattening ───────────────────────────────────────────────────────────
def test_flatten_covers_every_profile_section():
    kinds = {item.kind for item in flatten_profile(PROFILE)}
    assert kinds == {"experience", "project", "achievement", "education", "skill_list"}


def test_current_roles_rank_as_more_recent():
    items = {i.item_id: i for i in flatten_profile(PROFILE)}
    assert items["exp-oracle"].recency > items["exp-itjobxs"].recency


def test_flatten_survives_an_empty_profile():
    assert flatten_profile({}) == []


# ── Indexing ─────────────────────────────────────────────────────────────
def test_index_finds_skills_in_experience_text(index):
    """C++ is in the Oracle bullets, so it must be evidenced by that role."""
    evidence = index.evidence_for("C++")
    assert evidence
    assert any(e.item_id == "exp-oracle" for e in evidence)


def test_experience_evidence_outweighs_a_skill_list_entry(index):
    """A skill described in a role is stronger than a skill merely listed."""
    evidence = index.evidence_for("Docker")
    kinds = {e.kind: e.weight for e in evidence}
    if "project" in kinds and "skill_list" in kinds:
        assert kinds["project"] > kinds["skill_list"]


def test_index_normalises_aliases(index):
    """The profile says 'postgres' in prose and 'PostgreSQL' in skills."""
    assert index.supports("PostgreSQL")


def test_evidence_carries_a_snippet_for_provenance(index):
    """The UI must be able to show *why* an item was considered relevant."""
    evidence = index.evidence_for("Multithreading")
    assert evidence
    assert len(evidence[0].snippet) > 20


def test_unsupported_detects_absent_skills(index):
    """The anti-hallucination primitive: the profile has no Kubernetes anywhere."""
    assert not index.supports("Kubernetes")
    assert index.unsupported(["Kubernetes", "Python"]) == ["Kubernetes"]


def test_supported_skills_is_a_set_of_canonical_names(index):
    supported = index.supported_skills
    assert "Python" in supported
    assert "C++" in supported
    assert "Kubernetes" not in supported


# ── The node ─────────────────────────────────────────────────────────────
def test_returns_ranked_evidence(keywords):
    result = data_retriever_agent(state_with(keywords))
    assert result["current_step"] == Step.MATCHING.value
    relevances = [e["relevance"] for e in result["matched_evidence"]]
    assert relevances == sorted(relevances, reverse=True)
    assert relevances[0] > 0


def test_respects_the_token_budget_cap(keywords):
    result = data_retriever_agent(state_with(keywords))
    assert len(result["matched_evidence"]) <= MAX_EVIDENCE_ITEMS


def test_surfaces_genuine_gaps_rather_than_hiding_them(keywords):
    """The posting wants Kubernetes and Jenkins; the profile evidences neither.

    These must be reported so the resume does not claim them -- and so the user
    learns the truth about the gap.
    """
    result = data_retriever_agent(state_with(keywords))
    unsupported = result["unsupported_keywords"]
    assert "Kubernetes" in unsupported
    assert any("not evidence" in w or "does not evidence" in w for w in result["warnings"])


def test_only_taxonomy_keywords_count_as_gaps(keywords):
    """A statistical phrase like 'fast-paced environment' is not a missing skill."""
    result = data_retriever_agent(state_with(keywords))
    for term in result["unsupported_keywords"]:
        matching = next(kw for kw in keywords if kw["term"] == term)
        assert "taxonomy" in matching["sources"], f"{term} was reported as a gap but is statistical"


def test_relevant_experience_is_found_for_a_backend_posting(keywords):
    """The Salesforce posting wants Java/Python/Postgres/Docker backend work.

    Oracle (C++ backend, multithreading) and the LangGraph project (Python,
    Postgres, Docker) are the genuinely relevant items and must both surface.
    """
    result = data_retriever_agent(state_with(keywords))
    ids = [e["item_id"] for e in result["matched_evidence"]]
    assert "exp-oracle" in ids
    assert "proj-resumeforge" in ids


def test_detects_what_is_already_on_the_resume(keywords):
    """Oracle is on the current resume, so it is 'emphasise', not 'add'."""
    result = data_retriever_agent(state_with(keywords))
    oracle = next(e for e in result["matched_evidence"] if e["item_id"] == "exp-oracle")
    assert oracle["already_on_resume"] is True
    suggestion = next(s for s in result["suggestions"] if s["item_id"] == "exp-oracle")
    assert suggestion["action"] == "emphasise"


def test_every_suggestion_explains_itself(keywords):
    """Transparency requirement: the user must see why an item was chosen."""
    for suggestion in data_retriever_agent(state_with(keywords))["suggestions"]:
        assert suggestion["reason"]
        assert suggestion["action"] in {"add", "emphasise", "mention"}


def test_matched_keywords_are_recorded_per_item(keywords):
    result = data_retriever_agent(state_with(keywords))
    for entry in result["matched_evidence"]:
        assert entry["matched_keywords"], f"{entry['item_id']} matched nothing"


def test_spends_zero_llm_tokens(keywords):
    result = data_retriever_agent(state_with(keywords))
    assert result["events"][0]["data"]["llm_tokens_used"] == 0


# ── Failure paths ────────────────────────────────────────────────────────
def test_missing_keywords_is_an_error():
    result = data_retriever_agent(state_with([]))
    assert result["current_step"] == Step.FAILED.value
    assert "keywords" in result["error"].lower()


def test_empty_profile_is_an_actionable_error(keywords):
    result = data_retriever_agent(state_with(keywords, user_profile={}))
    assert result["current_step"] == Step.FAILED.value
    assert "profile" in result["error"].lower()


def test_returns_only_partial_state(keywords):
    result = data_retriever_agent(state_with(keywords))
    assert "user_profile" not in result
    assert "keywords" not in result
