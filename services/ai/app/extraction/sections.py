"""Layer 3: section-aware weighting.

A skill named under "Requirements" matters more than the same skill mentioned in
"About Us" boilerplate. This layer does not extract anything new -- it re-ranks
what layers 1 and 2 found, so that if recall is imperfect the terms that get
dropped are the low-priority ones.

Job postings are unstructured, so sections are detected with heuristic heading
patterns and each character offset is assigned to the most recent heading.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

# Weight per section kind. Tuned by intent, not fitted: a "must have" is worth
# roughly twice a "nice to have", and company boilerplate is near-noise.
SECTION_WEIGHTS: dict[str, float] = {
    "requirements": 2.0,
    "responsibilities": 1.5,
    "preferred": 1.0,
    "benefits": 0.4,
    "about": 0.3,
    "unknown": 1.0,
}

_HEADING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("requirements", re.compile(
        r"^\s*(?:#+\s*)?(?:minimum\s+|basic\s+|required\s+)?"
        r"(?:qualifications?|requirements?|what you(?:'| a)?ll need|must[- ]haves?|"
        r"skills?\s*(?:&|and)?\s*(?:experience)?|who you are)\s*:?\s*$",
        re.I | re.M)),
    ("responsibilities", re.compile(
        r"^\s*(?:#+\s*)?(?:key\s+|core\s+)?"
        r"(?:responsibilities|what you(?:'| wi)?ll do|the role|role overview|"
        r"your impact|day[- ]to[- ]day|duties)\s*:?\s*$",
        re.I | re.M)),
    ("preferred", re.compile(
        r"^\s*(?:#+\s*)?(?:preferred\s+qualifications?|nice[- ]to[- ]haves?|"
        r"bonus(?:\s+points)?|good to have|desirable|pluses?)\s*:?\s*$",
        re.I | re.M)),
    ("benefits", re.compile(
        r"^\s*(?:#+\s*)?(?:benefits?|perks?|what we offer|compensation|"
        r"salary|equal opportunity|eeo)\s*:?\s*$",
        re.I | re.M)),
    ("about", re.compile(
        r"^\s*(?:#+\s*)?(?:about (?:us|the company|\w+)|who we are|our mission|"
        r"company overview)\s*:?\s*$",
        re.I | re.M)),
]


@dataclass(frozen=True)
class Section:
    kind: str
    start: int
    end: int

    @property
    def weight(self) -> float:
        return SECTION_WEIGHTS.get(self.kind, 1.0)


def detect_sections(text: str) -> list[Section]:
    """Partition the text into contiguous weighted sections."""
    boundaries: list[tuple[int, str]] = []
    for kind, pattern in _HEADING_PATTERNS:
        for match in pattern.finditer(text):
            boundaries.append((match.start(), kind))

    if not boundaries:
        return [Section(kind="unknown", start=0, end=len(text))]

    boundaries.sort()
    sections: list[Section] = []
    if boundaries[0][0] > 0:
        sections.append(Section(kind="unknown", start=0, end=boundaries[0][0]))
    for idx, (start, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(text)
        sections.append(Section(kind=kind, start=start, end=end))
    return sections


class SectionIndex:
    """O(log s) offset -> section lookup, so weighting a match list is cheap."""

    def __init__(self, text: str) -> None:
        self.sections = detect_sections(text)
        self._starts = [s.start for s in self.sections]

    def section_at(self, offset: int) -> Section:
        idx = bisect_right(self._starts, offset) - 1
        return self.sections[max(idx, 0)]

    def weight_at(self, offset: int) -> float:
        return self.section_at(offset).weight

    def best_weight(self, offsets: list[int]) -> float:
        """A skill in several sections is judged by its most important mention."""
        return max((self.weight_at(o) for o in offsets), default=1.0)
