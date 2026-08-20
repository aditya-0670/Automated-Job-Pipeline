"""Keyword -> evidence lookup over the user's profile.

The naive approach is a nested loop: for each of ~35 keywords, scan each profile
item. That is O(keywords x items x text) and it re-reads the same profile text
once per keyword.

Instead the profile is indexed **once** into a second Aho-Corasick automaton --
the same technique as the job-description scan, pointed the other way. One pass
over each profile item finds every skill it mentions, and the inverted index is
then a dict lookup per keyword.

Why this matters beyond speed: this index is the **evidence set**. A skill that
does not appear here has no support in the user's profile, so the Refactorer must
not be allowed to claim it. Part 6's factual guardrail re-uses exactly this
structure to verify the generated resume. Anti-hallucination is therefore a
set-membership test, not a judgement call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.extraction.aho import TaxonomyMatcher, get_matcher

logger = logging.getLogger(__name__)

#: Weight by where in the profile a skill was found. A skill listed in the
#: skills array is a claim; the same skill described in an experience bullet is
#: evidence. Evidence outranks claims.
SOURCE_WEIGHTS: dict[str, float] = {
    "experience": 1.0,
    "project": 0.9,
    "achievement": 0.6,
    "education": 0.5,
    "skill_list": 0.4,
}

#: Evidence reached through an implication (Kafka -> Message Queue) is real but
#: weaker than a direct mention, so it is discounted rather than treated equally.
IMPLIED_EVIDENCE_DISCOUNT = 0.7

#: Self-declared proficiency, used only to break ties between equal evidence.
PROFICIENCY_WEIGHTS: dict[str, float] = {
    "proficient": 1.0,
    "familiar": 0.7,
    "learning": 0.4,
}


@dataclass
class ProfileItem:
    """One indexable unit of the user's profile."""

    item_id: str
    kind: str  # experience | project | achievement | education | skill_list
    title: str
    text: str  # the full searchable text
    recency: float = 0.5  # 0..1, 1 = current
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_weight(self) -> float:
        return SOURCE_WEIGHTS.get(self.kind, 0.5)


@dataclass
class Evidence:
    """One reason a profile item supports a keyword."""

    item_id: str
    kind: str
    title: str
    matched_text: str  # the surface form found, e.g. "postgres"
    snippet: str  # surrounding text, so the UI can show provenance
    weight: float
    #: Set when this evidence was reached by implication rather than directly --
    #: e.g. Message Queue supported by a Kafka mention. Carried through to the
    #: UI so the user can see the reasoning instead of being asked to trust it.
    implied_by: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.implied_by is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "title": self.title,
            "matched_text": self.matched_text,
            "snippet": self.snippet,
            "weight": round(self.weight, 3),
            "implied_by": self.implied_by,
        }


def flatten_profile(profile: dict[str, Any]) -> list[ProfileItem]:
    """Turn the stored profile into a flat list of indexable items.

    Kept separate from indexing so the profile's shape is defined in one place;
    every downstream consumer sees `ProfileItem`, not raw JSON.
    """
    items: list[ProfileItem] = []

    for exp in profile.get("experiences") or []:
        body = " ".join([*(exp.get("bullets") or []), exp.get("detail") or ""])
        items.append(
            ProfileItem(
                item_id=exp.get("id") or f"exp-{exp.get('company', '?')}",
                kind="experience",
                title=f"{exp.get('role', '')} at {exp.get('company', '')}".strip(),
                text=f"{exp.get('role', '')} {exp.get('company', '')} {body}",
                recency=1.0 if exp.get("current") else 0.7,
                metadata={"company": exp.get("company"), "role": exp.get("role")},
            )
        )

    for proj in profile.get("projects") or []:
        body = " ".join([*(proj.get("bullets") or []), proj.get("detail") or ""])
        tech = " ".join(proj.get("tech") or [])
        items.append(
            ProfileItem(
                item_id=proj.get("id") or f"proj-{proj.get('name', '?')}",
                kind="project",
                title=proj.get("name", ""),
                text=f"{proj.get('name', '')} {tech} {body}",
                recency=1.0 if proj.get("current") else 0.7,
                metadata={"tech": proj.get("tech") or []},
            )
        )

    for ach in profile.get("achievements") or []:
        items.append(
            ProfileItem(
                item_id=ach.get("id") or f"ach-{ach.get('title', '?')}",
                kind="achievement",
                title=ach.get("title", ""),
                text=f"{ach.get('title', '')} {ach.get('text', '')}",
                recency=0.8,
            )
        )

    for edu in profile.get("education") or []:
        items.append(
            ProfileItem(
                item_id=edu.get("id") or "edu",
                kind="education",
                title=edu.get("institution", ""),
                text=" ".join(
                    str(v)
                    for v in (
                        edu.get("institution"),
                        edu.get("degree"),
                        edu.get("field"),
                        edu.get("text"),
                    )
                    if v
                ),
                recency=0.9,
                metadata={"gpa": edu.get("gpa")},
            )
        )

    # The skills array is indexed as one item per skill rather than one blob, so
    # a match can carry its declared proficiency.
    for skill in profile.get("skills") or []:
        name = skill.get("name", "")
        if not name:
            continue
        items.append(
            ProfileItem(
                item_id=f"skill-{name}",
                kind="skill_list",
                title=name,
                text=name,
                recency=0.6,
                metadata={"proficiency": skill.get("proficiency", "familiar")},
            )
        )

    return items


class ProfileIndex:
    """Inverted index from canonical skill -> supporting profile evidence."""

    def __init__(
        self,
        profile: dict[str, Any],
        *,
        matcher: TaxonomyMatcher | None = None,
        snippet_chars: int = 140,
    ) -> None:
        self.matcher = matcher or get_matcher()
        self.items = flatten_profile(profile)
        self.snippet_chars = snippet_chars
        self._index: dict[str, list[Evidence]] = {}
        self._build()

    def _build(self) -> None:
        """One automaton pass per profile item, not one per keyword."""
        for item in self.items:
            matches = self.matcher.find_skills(item.text)
            for skill, occurrences in matches.items():
                first = occurrences[0]
                weight = item.source_weight * item.recency
                if item.kind == "skill_list":
                    weight *= PROFICIENCY_WEIGHTS.get(
                        item.metadata.get("proficiency", "familiar"), 0.7
                    )
                snippet = self._snippet(item.text, first.start, first.end)

                self._add(
                    skill,
                    Evidence(
                        item_id=item.item_id,
                        kind=item.kind,
                        title=item.title,
                        matched_text=first.matched_text,
                        snippet=snippet,
                        weight=weight,
                    ),
                )

                # Expand along implications. Without this, a profile evidencing
                # Kafka reports "Message Queue" as a missing skill, and one
                # evidencing GitHub Actions reports "CI/CD" as missing -- both
                # false gaps that would understate the user's real experience.
                for implied in self._implications(skill):
                    self._add(
                        implied,
                        Evidence(
                            item_id=item.item_id,
                            kind=item.kind,
                            title=item.title,
                            matched_text=first.matched_text,
                            snippet=snippet,
                            weight=weight * IMPLIED_EVIDENCE_DISCOUNT,
                            implied_by=skill,
                        ),
                    )

        for evidence in self._index.values():
            # Direct evidence first, then by weight: if an item is claimed, the
            # strongest direct mention should be what the UI shows.
            evidence.sort(key=lambda e: (e.is_direct, e.weight), reverse=True)

        logger.info(
            "Profile indexed: %d items, %d distinct skills with evidence "
            "(%d reachable only by implication)",
            len(self.items),
            len(self._index),
            sum(1 for ev in self._index.values() if all(not e.is_direct for e in ev)),
        )

    def _implications(self, skill: str) -> list[str]:
        """Skills that possessing `skill` genuinely evidences.

        One level only, deliberately. Transitive closure would let
        Next.js -> React -> JavaScript chains drift far from the original
        evidence, and the further a claim is from a real mention the harder it is
        to justify to a recruiter.
        """
        entry = self.matcher.taxonomy.get(skill) or {}
        return list(entry.get("implies") or [])

    def _add(self, skill: str, evidence: Evidence) -> None:
        self._index.setdefault(skill, []).append(evidence)

    def _snippet(self, text: str, start: int, end: int) -> str:
        pad = self.snippet_chars // 2
        left = max(0, start - pad)
        right = min(len(text), end + pad)
        prefix = "…" if left > 0 else ""
        suffix = "…" if right < len(text) else ""
        return f"{prefix}{text[left:right].strip()}{suffix}"

    # ── Queries ───────────────────────────────────────────────────────────
    def evidence_for(self, skill: str) -> list[Evidence]:
        return self._index.get(skill, [])

    def supports(self, skill: str) -> bool:
        """The anti-hallucination primitive: is this claim backed by the profile?"""
        return skill in self._index

    def supports_directly(self, skill: str) -> bool:
        """True only for an explicit mention, ignoring implications.

        The stricter test. Used where a claim needs to trace to words the user
        actually wrote rather than to a technology relationship.
        """
        return any(e.is_direct for e in self._index.get(skill, []))

    @property
    def supported_skills(self) -> set[str]:
        return set(self._index)

    def unsupported(self, skills: list[str]) -> list[str]:
        """Skills with no profile evidence -- the resume must not claim these."""
        return [s for s in skills if s not in self._index]

    def item_by_id(self, item_id: str) -> ProfileItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)
