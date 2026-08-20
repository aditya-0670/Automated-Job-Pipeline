"""Correctness of the Aho-Corasick taxonomy matcher.

The critical properties are (1) alias normalisation, (2) word-boundary safety --
single-letter skills like R and C are the reason this is not a plain substring
scan -- and (3) exact equivalence with the naive baseline, which is what makes
the performance benchmark a fair comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.aho import TaxonomyMatcher, get_matcher, load_taxonomy
from app.extraction.naive import naive_find_skills

FIXTURE = Path(__file__).parent / "fixtures" / "sample_jd.txt"


@pytest.fixture(scope="module")
def matcher() -> TaxonomyMatcher:
    return get_matcher()


@pytest.fixture(scope="module")
def taxonomy() -> dict[str, dict]:
    return load_taxonomy()


@pytest.fixture(scope="module")
def jd_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_automaton_builds_expected_pattern_count(matcher, taxonomy):
    expected = sum(1 + len(v.get("aliases", [])) for v in taxonomy.values())
    assert matcher.pattern_count == expected
    assert matcher.pattern_count > 400, "taxonomy should be substantial enough to matter"


def test_finds_canonical_name(matcher):
    found = matcher.find_skills("We use Kubernetes in production.")
    assert "Kubernetes" in found


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Strong k8s experience required", "Kubernetes"),
        ("Deep knowledge of golang", "Go"),
        ("Built services in nodejs", "Node.js"),
        ("Familiarity with tf and IaC", "Terraform"),
        ("Experience with postgres at scale", "PostgreSQL"),
        ("Worked on LWC components", "Lightning Web Components"),
        ("Understanding of oop principles", "Object-Oriented Programming"),
        ("Set up cicd for the team", "CI/CD"),
    ],
)
def test_alias_normalises_to_canonical_skill(matcher, text, expected):
    assert expected in matcher.find_skills(text)


def test_alias_flagged_as_alias(matcher):
    matches = matcher.find_skills("we run k8s")["Kubernetes"]
    assert matches[0].is_alias
    assert matches[0].matched_text == "k8s"


@pytest.mark.parametrize(
    ("text", "must_not_contain"),
    [
        # "R" must not match inside these words.
        ("We build with React and Rails", "R"),
        ("Visit google.com for the full posting", "Go"),
        # "C" must not match inside "Customer" / "Company".
        ("The Customer Company culture", "C"),
        # "Java" must not fire on "JavaScript".
        ("Strong JavaScript fundamentals", "Java"),
    ],
)
def test_word_boundaries_prevent_false_positives(matcher, text, must_not_contain):
    assert must_not_contain not in matcher.find_skills(text)


def test_single_letter_skill_still_matches_when_standalone(matcher):
    found = matcher.find_skills("Statistical work in R and C is required.")
    assert "R" in found
    assert "C" in found


def test_longest_match_wins(matcher):
    """ "spring boot" must not also register the shorter "spring"."""
    found = matcher.find_skills("Experience with Spring Boot required")
    assert "Spring Boot" in found
    assert "React Native" not in found


def test_matches_are_equivalent_to_naive_baseline(matcher, taxonomy, jd_text):
    """Aho-Corasick and the O(n*m) baseline must agree on the skill set.

    Guards the benchmark: a faster algorithm that finds different results is not
    a valid optimisation.
    """
    aho_skills = set(matcher.find_skills(jd_text))
    naive_skills = set(naive_find_skills(jd_text, taxonomy))
    assert aho_skills == naive_skills


def test_real_jd_finds_expected_skills(matcher, jd_text):
    found = set(matcher.find_skills(jd_text))
    for expected in [
        "Java",
        "Python",
        "PostgreSQL",
        "Oracle",
        "Docker",
        "Kubernetes",
        "Jenkins",
        "GitHub Actions",
        "CI/CD",
        "Prometheus",
        "Grafana",
        "AWS",
        "GCP",
        "Azure",
        "Kafka",
        "Terraform",
        "Redis",
        "Apex",
        "SOQL",
        "Lightning Web Components",
        "Spring Boot",
        "Hibernate",
        "REST API",
        "Distributed Systems",
        "Agile",
        "Code Review",
    ]:
        assert expected in found, f"missed {expected}"


def test_empty_text_is_safe(matcher):
    assert matcher.find_skills("") == {}
    assert matcher.find_all("") == []


def test_matcher_is_reusable_across_calls(matcher):
    """The automaton is immutable after build, so repeated use must be stable."""
    first = matcher.find_skills("docker and kubernetes")
    second = matcher.find_skills("docker and kubernetes")
    assert set(first) == set(second)


def test_singleton_matcher_is_cached():
    assert get_matcher() is get_matcher()


def test_ambiguous_patterns_are_rejected_at_build_time():
    """A surface form claimed by two skills must fail loudly, not silently.

    `add_word` keeps the last insertion, so an ambiguous taxonomy makes the
    automaton disagree with an exhaustive scan and mislabels keywords with no
    error raised anywhere. This was a real bug, caught by the equivalence test.
    """
    with pytest.raises(ValueError, match="Ambiguous taxonomy pattern"):
        TaxonomyMatcher(
            {
                "Docker": {"category": "devops", "aliases": ["containers"]},
                "Containerization": {"category": "devops", "aliases": ["containers"]},
            }
        )


def test_a_skill_may_repeat_its_own_name_as_an_alias():
    """Self-collision is redundant, not ambiguous, and must not raise."""
    matcher = TaxonomyMatcher({"gRPC": {"category": "concept", "aliases": ["grpc"]}})
    assert "gRPC" in matcher.find_skills("we use gRPC internally")


def test_shipped_taxonomy_has_no_ambiguous_patterns():
    """Guards the real data file, not just the code path."""
    taxonomy = load_taxonomy()
    owners: dict[str, str] = {}
    for name, meta in taxonomy.items():
        for form in [name, *meta.get("aliases", [])]:
            key = form.lower().strip()
            assert owners.get(key, name) == name, (
                f"{key!r} is claimed by both {owners[key]!r} and {name!r}"
            )
            owners[key] = name


def test_every_implication_target_is_a_canonical_skill():
    """An implication pointing at a non-existent skill would expand to nothing."""
    taxonomy = load_taxonomy()
    for name, meta in taxonomy.items():
        for target in meta.get("implies") or []:
            assert target in taxonomy, f"{name} implies {target!r}, which is not a skill"


def test_implications_are_not_self_referential():
    taxonomy = load_taxonomy()
    for name, meta in taxonomy.items():
        assert name not in (meta.get("implies") or []), f"{name} implies itself"
