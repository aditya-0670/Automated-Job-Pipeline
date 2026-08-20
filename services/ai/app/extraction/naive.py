"""Naive multi-pattern matcher -- the baseline Aho-Corasick is measured against.

This exists so the performance claim is a measurement, not an assertion. It is
the obvious implementation: for each pattern, scan the whole text. O(n * m) in
text length times pattern count.

It deliberately applies the *same* boundary validation and longest-match-wins
filtering as the automaton, so the two produce identical output. A faster
algorithm that returns different results is not a valid optimisation, and
`test_matches_are_equivalent_to_naive_baseline` enforces that.
"""

from __future__ import annotations

from app.extraction.aho import _WORD_CHARS, SkillMatch, TaxonomyMatcher


def naive_find_all(text: str, taxonomy: dict[str, dict]) -> list[SkillMatch]:
    haystack = text.lower()
    text_len = len(haystack)
    raw: list[tuple[int, int, str, str, str]] = []

    for canonical, meta in taxonomy.items():
        category = meta.get("category", "unknown")
        for form in [canonical, *meta.get("aliases", [])]:
            pattern = form.lower().strip()
            if not pattern:
                continue
            # One full scan of the text per pattern -- the cost this baseline
            # exists to demonstrate.
            start = haystack.find(pattern)
            while start != -1:
                end = start + len(pattern)
                left_ok = start == 0 or haystack[start - 1] not in _WORD_CHARS
                right_ok = end == text_len or haystack[end] not in _WORD_CHARS
                if left_ok and right_ok:
                    raw.append((start, end, canonical, category, pattern))
                start = haystack.find(pattern, start + 1)

    return [
        SkillMatch(skill=c, category=cat, matched_text=p, start=s, end=e)
        for s, e, c, cat, p in TaxonomyMatcher._keep_longest(raw)
    ]


def naive_find_skills(text: str, taxonomy: dict[str, dict]) -> dict[str, list[SkillMatch]]:
    """Functionally equivalent to TaxonomyMatcher.find_skills, one scan per pattern."""
    grouped: dict[str, list[SkillMatch]] = {}
    for match in naive_find_all(text, taxonomy):
        grouped.setdefault(match.skill, []).append(match)
    return grouped
