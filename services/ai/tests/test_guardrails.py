"""Part 6: the deterministic guardrails.

These are the checks that make "no hallucinations" a verifiable property rather
than a hope. Every test here runs with zero LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.guardrails.factual import check_facts, extract_numbers, strip_latex
from app.guardrails.structural import (
    check_structure,
    find_macro_definitions,
    find_sections,
    split_preamble,
)

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))


# ── Structural: parsing ──────────────────────────────────────────────────
def test_splits_the_real_preamble():
    preamble, body = split_preamble(RESUME)
    assert r"\documentclass" in preamble
    assert "fontawesome5" in preamble
    assert body.startswith(r"\begin{document}")


def test_finds_all_real_sections():
    assert find_sections(RESUME) == [
        "Summary",
        "Education",
        "Experience",
        "Projects",
        "Skills",
        "Achievements",
    ]


def test_finds_all_real_macros():
    """The template defines 10; losing any changes how the resume renders."""
    macros = find_macro_definitions(RESUME)
    for expected in (
        "extlink",
        "resumeItem",
        "resumeSubheading",
        "resumeProjectHeading",
        "skillItem",
        "achievementItem",
        "resumeSubHeadingListStart",
        "resumeItemListStart",
    ):
        assert expected in macros, f"{expected} not detected"


# ── Structural: the checks ───────────────────────────────────────────────
def test_identical_document_passes():
    report = check_structure(RESUME, RESUME)
    assert report.passed, report.errors


def test_whitespace_changes_are_not_edits():
    modified = RESUME.replace("\\usepackage{titlesec}", "\\usepackage{titlesec}   ")
    assert check_structure(RESUME, modified).passed


def test_reworded_bullet_passes():
    """The permitted change: prose inside a bullet."""
    modified = RESUME.replace(
        "Resolved \\textbf{500+ compiler warnings}",
        "Fixed \\textbf{500+ compiler warnings}",
    )
    assert check_structure(RESUME, modified).passed


def test_removed_package_is_an_error():
    modified = RESUME.replace("\\usepackage{fontawesome5}\n", "")
    report = check_structure(RESUME, modified)
    assert not report.passed
    assert any("removed or altered" in e for e in report.errors)


def test_added_package_is_an_error():
    """An added package may not exist in the compiler image."""
    modified = RESUME.replace(
        "\\usepackage{tabularx}", "\\usepackage{tabularx}\n\\usepackage{moderncv}"
    )
    report = check_structure(RESUME, modified)
    assert not report.passed
    assert any("added" in e for e in report.errors)


def test_removed_section_is_an_error():
    modified = RESUME.replace("\\section{Achievements}", "")
    report = check_structure(RESUME, modified)
    assert not report.passed
    assert any("Achievements" in e for e in report.errors)


def test_reordered_sections_are_detected():
    modified = (
        RESUME.replace("\\section{Skills}", "\\section{ZZZ}")
        .replace("\\section{Achievements}", "\\section{Skills}")
        .replace("\\section{ZZZ}", "\\section{Achievements}")
    )
    report = check_structure(RESUME, modified)
    assert not report.passed


def test_lost_macro_definition_is_an_error():
    modified = RESUME.replace("\\newcommand{\\resumeItem}[1]{\\item\\small{{#1}}}", "")
    report = check_structure(RESUME, modified)
    assert not report.passed
    assert any("resumeItem" in e for e in report.errors)


def test_unbalanced_environment_is_an_error():
    modified = RESUME.replace("\\end{itemize}", "", 1)
    report = check_structure(RESUME, modified)
    assert not report.passed
    assert any("unbalanced" in e for e in report.errors)


def test_empty_output_is_an_error():
    assert not check_structure(RESUME, "").passed


def test_large_growth_warns_but_does_not_block():
    """The user may have asked for more content; that is their call."""
    body_start = RESUME.index(r"\begin{document}")
    modified = RESUME[:body_start] + RESUME[body_start:].replace(
        r"\end{document}", "Extra content. " * 400 + r"\end{document}"
    )
    report = check_structure(RESUME, modified)
    assert report.passed, report.errors
    assert any("grew" in w for w in report.warnings)


# ── Factual: LaTeX stripping ─────────────────────────────────────────────
def test_strip_removes_markup_but_keeps_visible_text():
    text = strip_latex(RESUME)
    assert "Oracle" in text
    assert "compiler warnings" in text
    assert "\\textbf" not in text
    assert "documentclass" not in text


def test_strip_discards_package_names():
    """Otherwise \\usepackage{tabularx} reads as a claim about tabularx."""
    text = strip_latex(RESUME)
    assert "tabularx" not in text
    assert "fontawesome5" not in text


def test_strip_discards_urls():
    """github.com in an href is a link, not a claim of GitHub experience."""
    assert "linkedin.com" not in strip_latex(RESUME)
    assert "github.com" not in strip_latex(RESUME)


# ── Factual: numbers ─────────────────────────────────────────────────────
def test_extracts_metric_numbers():
    found = extract_numbers("Resolved 500+ warnings, cut costs 60%, 5X speedup")
    assert {"500", "60", "5"} <= found


def test_ignores_trivially_small_numbers():
    """'3 years' and 'one page' are prose, not claims worth policing."""
    assert not extract_numbers("worked for 2 years on 1 project")


def test_normalises_thousands_separators():
    assert "18000" in extract_numbers("out of 18,000+ teams")


# ── Factual: the anti-hallucination check ────────────────────────────────
def test_the_real_resume_passes_against_its_own_profile():
    """The baseline that makes every other assertion here meaningful."""
    report = check_facts(generated_latex=RESUME, profile=PROFILE, original_latex=RESUME)
    assert report.passed, (
        f"unsupported={report.unsupported_skills} "
        f"metrics={report.altered_metrics} employers={report.invented_employers}"
    )
    assert report.supported_count > 15


@pytest.mark.parametrize(
    "injected",
    [
        r"\resumeItem{Deployed services to \textbf{Kubernetes} clusters.}",
        r"\resumeItem{Built Salesforce Apex triggers.}",
        r"\resumeItem{Managed infrastructure with Terraform.}",
        r"\skillItem{Cloud}{Azure, GCP}",
    ],
)
def test_hallucinated_skills_are_caught_with_no_llm_call(injected):
    """The central claim of the project, and it costs zero tokens."""
    modified = RESUME.replace(r"\end{document}", injected + "\n" + r"\end{document}")
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    assert not report.passed
    assert report.unsupported_skills


def test_unsupported_skill_error_is_actionable():
    """The message must tell the Refactorer exactly what to remove."""
    modified = RESUME.replace(
        r"\end{document}", r"\resumeItem{Ran \textbf{Kubernetes} in production.}\end{document}"
    )
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    error = next(e for e in report.errors if "Kubernetes" in e)
    assert "Remove this claim" in error


def test_implied_skills_are_accepted():
    """The profile has Kafka, so claiming message-queue work is legitimate."""
    modified = RESUME.replace(
        r"\end{document}", r"\resumeItem{Designed \textbf{message queue} pipelines.}\end{document}"
    )
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    assert "Message Queue" not in [s["skill"] for s in report.unsupported_skills]


def test_inflated_metric_is_caught():
    """The most damaging possible error: changing a number."""
    modified = RESUME.replace("500+ compiler warnings", "800+ compiler warnings")
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    assert not report.passed
    assert any(m["value"] == "800" for m in report.altered_metrics)


def test_original_numbers_are_permitted_even_if_reworded():
    """A number the user already published is theirs."""
    modified = RESUME.replace(
        "Resolved \\textbf{500+ compiler warnings}",
        "Cleared \\textbf{500+ compiler warnings}",
    )
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    assert not report.altered_metrics, report.altered_metrics


def test_invented_employer_is_caught():
    modified = RESUME.replace(
        r"\end{document}", r"\resumeSubheading{Globex Corp}{Remote}{Engineer}{2024}\end{document}"
    )
    report = check_facts(generated_latex=modified, profile=PROFILE, original_latex=RESUME)
    assert not report.passed
    assert any("Globex" in e for e in report.invented_employers)


def test_report_serialises_for_state():
    report = check_facts(generated_latex=RESUME, profile=PROFILE, original_latex=RESUME)
    payload = report.to_dict()
    json.dumps(payload)
    assert set(payload) >= {"passed", "unsupported_skills", "altered_metrics", "errors"}
