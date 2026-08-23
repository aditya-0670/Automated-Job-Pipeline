"""Part 8: the section-level diff shown at human review."""

from __future__ import annotations

import json
from pathlib import Path

from app.diff import (
    PREAMBLE,
    diff_sections,
    diff_summary,
    similarity,
    split_sections,
    unified,
)

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")

SIMPLE = r"""
\documentclass{article}
\begin{document}
\section{Experience}
Built things at Oracle.
\section{Skills}
C++, Python
\end{document}
"""


def test_splits_on_sections_and_keeps_a_preamble_bucket():
    parts = split_sections(SIMPLE)
    assert list(parts) == [PREAMBLE, "Experience", "Skills"]
    assert r"\documentclass" in parts[PREAMBLE]
    assert "Oracle" in parts["Experience"]


def test_the_real_resume_splits_into_its_actual_sections():
    parts = split_sections(RESUME)
    titles = [t for t in parts if t != PREAMBLE]
    assert titles == ["Summary", "Education", "Experience", "Projects", "Skills", "Achievements"]
    # \section also appears inside the preamble's \titleformat definition; that
    # is a macro declaration, not a section, and must not become a diff entry.
    assert RESUME.count(r"\section{") == len(titles)


def test_a_document_with_no_sections_is_all_preamble():
    assert list(split_sections("just text")) == [PREAMBLE]


def test_duplicate_titles_are_suffixed_not_merged():
    doc = r"\section{Projects} a \section{Projects} b"
    parts = split_sections(doc)
    assert list(parts) == ["Projects", "Projects (2)"]


def test_identical_documents_report_no_changes():
    entries = diff_sections(SIMPLE, SIMPLE)
    assert diff_summary(entries)["changed"] == 0
    assert all(e["change"] == "unchanged" for e in entries)


def test_reindentation_alone_is_not_a_change():
    """Whitespace churn must not train the user to click through the diff."""
    reflowed = SIMPLE.replace("Built things at Oracle.", "    Built things at Oracle.   ")
    entries = diff_sections(SIMPLE, reflowed)
    assert diff_summary(entries)["changed"] == 0


def test_an_edited_bullet_marks_exactly_one_section_modified():
    revised = SIMPLE.replace("Built things at Oracle.", "Built C++ tooling at Oracle.")
    entries = diff_sections(SIMPLE, revised)
    modified = [e for e in entries if e["change"] == "modified"]
    assert [e["section"] for e in modified] == ["Experience"]
    assert "C++ tooling" in modified[0]["diff"]
    assert diff_summary(entries) == {
        "unchanged": 2,
        "modified": 1,
        "added": 0,
        "removed": 0,
        "total_sections": 3,
        "changed": 1,
    }


def test_an_added_section_is_reported_as_added():
    revised = SIMPLE.replace(r"\end{document}", "\\section{Awards}\nDean's list\n\\end{document}")
    entries = diff_sections(SIMPLE, revised)
    added = [e for e in entries if e["change"] == "added"]
    assert [e["section"] for e in added] == ["Awards"]


def test_a_dropped_section_is_reported_and_carries_its_content():
    """A vanished \\section is nearly always a bug in the rewrite, never silent."""
    revised = SIMPLE.replace("\\section{Skills}\nC++, Python\n", "")
    entries = diff_sections(SIMPLE, revised)
    removed = [e for e in entries if e["change"] == "removed"]
    assert [e["section"] for e in removed] == ["Skills"]
    assert "C++, Python" in removed[0]["diff"]
    # Removals are appended last so they cannot be missed above the fold.
    assert entries[-1]["change"] == "removed"


def test_similarity_ranks_a_tweak_above_a_rewrite():
    tweak = SIMPLE.replace("Oracle.", "Oracle Corp.")
    rewrite = SIMPLE.replace("Built things at Oracle.", "Completely different sentence here.")
    section = "Experience"
    before = split_sections(SIMPLE)[section]
    assert similarity(before, split_sections(tweak)[section]) > similarity(
        before, split_sections(rewrite)[section]
    )


def test_unified_output_is_a_readable_diff():
    text = unified("alpha\nbeta", "alpha\ngamma", title="Skills")
    assert "before: Skills" in text and "-beta" in text and "+gamma" in text


def test_the_diff_is_json_serialisable():
    """It crosses the SSE boundary, so anything unserialisable is a 500 later."""
    revised = SIMPLE.replace("C++, Python", "C++, Python, Docker")
    json.dumps(diff_sections(SIMPLE, revised))


def test_unified_bodies_can_be_omitted_for_a_summary_view():
    revised = SIMPLE.replace("C++, Python", "C++, Python, Docker")
    entries = diff_sections(SIMPLE, revised, include_unified=False)
    assert all("diff" not in e for e in entries)
