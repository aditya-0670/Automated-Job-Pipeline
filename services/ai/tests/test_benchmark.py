"""Measurements backing the performance claims. Not correctness tests.

Run with `-s` to see the numbers:
    pytest tests/test_benchmark.py -s
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.extraction.aho import TaxonomyMatcher, load_taxonomy
from app.extraction.naive import naive_find_skills

FIXTURE = Path(__file__).parent / "fixtures" / "sample_jd.txt"
ITERATIONS = 50


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


@pytest.fixture(scope="module")
def matcher(taxonomy):
    return TaxonomyMatcher(taxonomy)


@pytest.fixture(scope="module")
def long_jd():
    """~5,000 words, the upper end of a realistic posting."""
    base = FIXTURE.read_text(encoding="utf-8")
    return base * max(1, 5000 // len(base.split()))


def _time(fn, iterations=ITERATIONS) -> float:
    """Mean milliseconds per call."""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) * 1000 / iterations


def test_aho_beats_naive_and_report(matcher, taxonomy, long_jd):
    aho_ms = _time(lambda: matcher.find_skills(long_jd))
    naive_ms = _time(lambda: naive_find_skills(long_jd, taxonomy))

    build_start = time.perf_counter()
    TaxonomyMatcher(taxonomy)
    build_ms = (time.perf_counter() - build_start) * 1000

    words = len(long_jd.split())
    print(
        f"\n{'':-<62}\n"
        f"  Aho-Corasick vs naive multi-pattern matching\n"
        f"{'':-<62}\n"
        f"  text size            : {words:,} words / {len(long_jd):,} chars\n"
        f"  taxonomy             : {len(taxonomy)} skills / {matcher.pattern_count} patterns\n"
        f"  automaton build      : {build_ms:.1f} ms   (once, at startup)\n"
        f"  naive search         : {naive_ms:.2f} ms/JD\n"
        f"  aho-corasick search  : {aho_ms:.2f} ms/JD\n"
        f"  speedup              : {naive_ms / aho_ms:.1f}x\n"
        f"{'':-<62}"
    )

    assert aho_ms < naive_ms, "Aho-Corasick should be faster than the naive baseline"


def test_search_scales_with_text_not_pattern_count(taxonomy, long_jd):
    """The defining property: query time is independent of pattern count.

    Quartering the taxonomy should not meaningfully change search time, whereas
    the naive baseline scales linearly with it.
    """
    quarter = dict(list(taxonomy.items())[: len(taxonomy) // 4])
    small = TaxonomyMatcher(quarter)
    full = TaxonomyMatcher(taxonomy)

    small_ms = _time(lambda: small.find_skills(long_jd))
    full_ms = _time(lambda: full.find_skills(long_jd))

    print(
        f"\n  patterns {small.pattern_count:>4} -> {small_ms:.2f} ms"
        f"\n  patterns {full.pattern_count:>4} -> {full_ms:.2f} ms"
        f"\n  ratio: {full_ms / small_ms:.2f}x for {full.pattern_count / small.pattern_count:.1f}x the patterns"
    )

    # 4x the patterns must cost far less than 4x the time.
    assert full_ms < small_ms * 2.5


def test_scaling_curve_as_taxonomy_grows(taxonomy, long_jd):
    """The asymptotic argument, measured.

    Query time for the automaton depends on text length and match count, not on
    how many patterns exist. The naive baseline pays for every pattern on every
    query. So as a taxonomy grows -- which is the realistic direction, since new
    tools appear constantly -- the gap widens.

    Synthetic padding patterns are used to grow the pattern set without growing
    the match count, isolating the pattern-count variable.
    """
    rows: list[tuple[int, float, float]] = []

    for multiple in (1, 4, 10):
        padded = dict(taxonomy)
        # Padding patterns are deliberately absent from the text, so they add
        # pattern-set size without adding matches.
        for i in range(len(taxonomy) * (multiple - 1)):
            padded[f"__SyntheticSkill{i}__"] = {"category": "synthetic", "aliases": []}

        matcher = TaxonomyMatcher(padded)
        # Loop variables are bound as defaults rather than captured: correct as
        # written only because _time() invokes immediately, and a trap the moment
        # anything defers the call.
        aho_ms = _time(lambda m=matcher: m.find_skills(long_jd), iterations=10)
        naive_ms = _time(lambda t=padded: naive_find_skills(long_jd, t), iterations=10)
        rows.append((matcher.pattern_count, aho_ms, naive_ms))

    header = f"\n{'patterns':>10} {'aho (ms)':>10} {'naive (ms)':>12} {'speedup':>9}"
    print(header + "\n" + "-" * len(header.strip()))
    for patterns, aho_ms, naive_ms in rows:
        print(f"{patterns:>10} {aho_ms:>10.2f} {naive_ms:>12.2f} {naive_ms / aho_ms:>8.1f}x")

    base_patterns, base_aho, base_naive = rows[0]
    top_patterns, top_aho, top_naive = rows[-1]
    pattern_growth = top_patterns / base_patterns

    print(
        f"\n  {pattern_growth:.0f}x the patterns costs the automaton "
        f"{top_aho / base_aho:.1f}x time, the baseline {top_naive / base_naive:.1f}x time"
    )

    # The automaton must stay far closer to flat than the linear baseline.
    assert top_aho / base_aho < top_naive / base_naive
    assert top_naive / top_aho > base_naive / base_aho, "the gap must widen, not narrow"
