"""Deterministic multi-pattern skill matching via an Aho-Corasick automaton.

Why Aho-Corasick and not N substring searches?
    Naive matching runs one scan of the text per pattern: O(n * m) where n is
    text length and m is the number of patterns. With ~500 taxonomy patterns and
    a 5,000-word job description that is millions of character comparisons.

    Aho-Corasick builds a trie of every pattern once (at process startup), wires
    failure links between trie nodes, and then finds *all* matches in a single
    left-to-right pass: O(n + z) per query, where z is the number of matches, and
    independent of the pattern count. Adding patterns to the taxonomy costs build
    time, not query time.

Two phases, mirroring the 1975 paper:
    1. Build  (once)  -- insert patterns into a trie, add failure links.
    2. Search (per JD) -- walk the automaton one character at a time.

The automaton is immutable after `make_automaton()`, so a single instance is
safe to share across concurrent requests without locking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import ahocorasick

logger = logging.getLogger(__name__)

TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "data" / "skill_taxonomy.json"

# A matched pattern must not be glued to a surrounding word character, otherwise
# "R" matches inside "React" and "Go" matches inside "Google". Hyphens, dots and
# plus signs are *not* boundaries because they appear inside real skill names
# (c++, .net, node.js, ci/cd), so they are excluded from this set deliberately.
_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(frozen=True)
class SkillMatch:
    """One canonical skill found in the text."""

    skill: str  # canonical name, e.g. "Kubernetes"
    category: str  # taxonomy category, e.g. "devops"
    matched_text: str  # the surface form actually seen, e.g. "k8s"
    start: int  # char offset in the lowercased text
    end: int  # exclusive

    @property
    def is_alias(self) -> bool:
        return self.matched_text.lower() != self.skill.lower()


class TaxonomyMatcher:
    """Wraps a compiled Aho-Corasick automaton over the skill taxonomy."""

    def __init__(self, taxonomy: dict[str, dict]) -> None:
        self.taxonomy = taxonomy
        self._automaton, self.pattern_count = self._build(taxonomy)

    # ── Build phase (once) ────────────────────────────────────────────────
    @staticmethod
    def _build(taxonomy: dict[str, dict]) -> tuple[ahocorasick.Automaton, int]:
        automaton = ahocorasick.Automaton()
        count = 0
        # A surface form must belong to exactly one canonical skill. If two
        # entries claim the same pattern, `add_word` silently keeps whichever was
        # inserted last -- so the automaton would return a different (and
        # arbitrary) answer than an exhaustive scan, and keywords would be
        # mislabelled with no error anywhere. Caught for real when adding
        # "Containerization" as a canonical skill collided with an existing
        # Docker alias; see docs/09-challenges.md.
        owners: dict[str, str] = {}
        for canonical, meta in taxonomy.items():
            category = meta.get("category", "unknown")
            surface_forms = [canonical, *meta.get("aliases", [])]
            for form in surface_forms:
                key = form.lower().strip()
                if not key:
                    continue
                if key in owners and owners[key] != canonical:
                    raise ValueError(
                        f"Ambiguous taxonomy pattern {key!r}: claimed by both "
                        f"{owners[key]!r} and {canonical!r}. Each surface form "
                        f"must map to exactly one canonical skill."
                    )
                owners[key] = canonical
                # Payload carries the canonical name so alias hits normalise for
                # free -- "k8s", "kubectl" and "Kubernetes" all resolve to one
                # entry with no post-processing lookup table.
                automaton.add_word(key, (canonical, category, key))
                count += 1
        automaton.make_automaton()
        logger.info(
            "Aho-Corasick automaton built: %d canonical skills, %d patterns",
            len(taxonomy),
            count,
        )
        return automaton, count

    # ── Search phase (per query) ──────────────────────────────────────────
    def find_all(self, text: str) -> list[SkillMatch]:
        """Return every boundary-valid skill occurrence, in text order.

        Single O(n + z) pass over the text. Raw hits are kept as plain tuples so
        that only the matches that survive filtering pay for object allocation --
        on a repetitive posting the automaton can report thousands of hits, and
        allocating a dataclass per hit dominated the measured runtime.
        """
        haystack = text.lower()
        text_len = len(haystack)
        word_chars = _WORD_CHARS
        raw: list[tuple[int, int, str, str, str]] = []

        for end_idx, (canonical, category, pattern) in self._automaton.iter(haystack):
            end = end_idx + 1
            start = end - len(pattern)
            # Inlined boundary check: this runs once per raw hit, so a method
            # call here is measurable overhead.
            if start > 0 and haystack[start - 1] in word_chars:
                continue
            if end < text_len and haystack[end] in word_chars:
                continue
            raw.append((start, end, canonical, category, pattern))

        return [
            SkillMatch(skill=c, category=cat, matched_text=p, start=s, end=e)
            for s, e, c, cat, p in self._keep_longest(raw)
        ]

    def find_skills(self, text: str) -> dict[str, list[SkillMatch]]:
        """Deduplicate to one entry per canonical skill, keyed by skill name."""
        grouped: dict[str, list[SkillMatch]] = {}
        for match in self.find_all(text):
            grouped.setdefault(match.skill, []).append(match)
        return grouped

    # ── Helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _has_word_boundaries(text: str, start: int, end: int) -> bool:
        """Reject matches glued to a surrounding alphanumeric character.

        Kept as the readable reference form; `find_all` inlines this logic on
        its hot path, and the naive baseline calls it directly.
        """
        left_free = start == 0 or text[start - 1] not in _WORD_CHARS
        right_free = end == len(text) or text[end] not in _WORD_CHARS
        return left_free and right_free

    @staticmethod
    def _keep_longest(
        matches: list[tuple[int, int, str, str, str]],
    ) -> list[tuple[int, int, str, str, str]]:
        """Drop matches fully contained inside a longer match.

        Aho-Corasick reports every pattern ending at each position, so
        "spring boot" also yields "spring". The longer, more specific surface
        form is the correct one.

        Sorted by start ascending (longest first on ties), every already-kept
        span begins at or before the current one -- so containment reduces to a
        single comparison against the furthest end seen so far. O(k log k).
        """
        if not matches:
            return []
        matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
        kept: list[tuple[int, int, str, str, str]] = []
        furthest_end = -1
        for match in matches:
            if match[1] <= furthest_end:
                continue
            kept.append(match)
            furthest_end = match[1]
        return kept


def load_taxonomy(path: Path | None = None) -> dict[str, dict]:
    raw = json.loads((path or TAXONOMY_PATH).read_text(encoding="utf-8"))
    return raw["skills"]


@lru_cache(maxsize=1)
def get_matcher() -> TaxonomyMatcher:
    """Process-wide singleton. Build cost is paid once, at first use."""
    return TaxonomyMatcher(load_taxonomy())
