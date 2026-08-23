"""The 4-layer keyword extraction pipeline. Zero LLM tokens.

    Layer 1  Statistical  YAKE + RAKE ensemble -- catches unknown-to-us terms.
    Layer 2  Taxonomy     Aho-Corasick scan    -- catches known domain skills
                                                  regardless of frequency, and
                                                  normalises aliases.
    Layer 3  Weighting    Section awareness    -- ranks by where a term appeared.
    Layer 4  Confirmation User edits           -- handled at the API boundary;
                                                  the human is the final recall
                                                  backstop, not this module.

Why not an LLM for this step? Cost and determinism. A taxonomy hit must be
reproducible: if "Kubernetes" is in the posting it must be extracted 100% of the
time, not 98% of the time depending on sampling. Resume content decisions are
made downstream from these keywords, so non-determinism here is not acceptable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app import metrics
from app.extraction.aho import SkillMatch, TaxonomyMatcher, get_matcher
from app.extraction.sections import SectionIndex
from app.extraction.statistical import ScoredTerm, extract_statistical

# A confirmed taxonomy hit is trusted far more than a statistical guess, so it
# enters ranking with a large base score. Statistical-only terms are candidates.
TAXONOMY_BASE_SCORE = 10.0
STATISTICAL_BASE_SCORE = 1.0
# A term found by BOTH layers is the strongest signal available without an LLM.
CORROBORATION_BONUS = 3.0


@dataclass
class Keyword:
    """One extracted keyword, ready for user confirmation (Layer 4)."""

    term: str
    category: str
    score: float
    sources: list[str] = field(default_factory=list)
    section: str = "unknown"
    occurrences: int = 0
    aliases_matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "category": self.category,
            "score": round(self.score, 3),
            "sources": self.sources,
            "section": self.section,
            "occurrences": self.occurrences,
            "aliases_matched": self.aliases_matched,
        }


@dataclass
class ExtractionResult:
    keywords: list[Keyword]
    duration_ms: float
    stats: dict[str, Any]

    def by_category(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for kw in self.keywords:
            grouped.setdefault(kw.category, []).append(kw.term)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "keywords": [kw.to_dict() for kw in self.keywords],
            "by_category": self.by_category(),
            "duration_ms": round(self.duration_ms, 2),
            "stats": self.stats,
        }


def extract_keywords(
    job_text: str,
    *,
    matcher: TaxonomyMatcher | None = None,
    max_keywords: int = 35,
    min_statistical_score: float = 0.35,
) -> ExtractionResult:
    """Run layers 1-3 and return a ranked, deduplicated keyword list."""
    started = time.perf_counter()
    matcher = matcher or get_matcher()
    sections = SectionIndex(job_text)

    # ── Layer 2: taxonomy scan (single O(n) pass over the text) ──
    taxonomy_hits: dict[str, list[SkillMatch]] = matcher.find_skills(job_text)

    keywords: dict[str, Keyword] = {}
    for skill, matches in taxonomy_hits.items():
        offsets = [m.start for m in matches]
        weight = sections.best_weight(offsets)
        aliases = sorted({m.matched_text for m in matches if m.is_alias})
        keywords[skill.lower()] = Keyword(
            term=skill,
            category=matches[0].category,
            score=TAXONOMY_BASE_SCORE * weight,
            sources=["taxonomy"],
            section=sections.section_at(offsets[0]).kind,
            occurrences=len(matches),
            aliases_matched=aliases,
        )

    # ── Layer 1: statistical ensemble ──
    statistical: list[ScoredTerm] = extract_statistical(job_text)
    lowered = job_text.lower()
    for term in statistical:
        key = term.term.lower()
        existing = keywords.get(key)
        if existing is not None:
            # Corroborated by both layers -- promote it.
            existing.score += CORROBORATION_BONUS
            if term.source not in existing.sources:
                existing.sources.append(term.source)
            continue
        if term.score < min_statistical_score:
            continue
        offset = lowered.find(key)
        weight = sections.weight_at(offset) if offset >= 0 else 1.0
        keywords[key] = Keyword(
            term=term.term,
            category="uncategorized",
            score=STATISTICAL_BASE_SCORE * term.score * weight,
            sources=[term.source],
            section=sections.section_at(offset).kind if offset >= 0 else "unknown",
            occurrences=lowered.count(key),
        )

    ranked = sorted(keywords.values(), key=lambda k: (-k.score, k.term))[:max_keywords]
    duration_ms = (time.perf_counter() - started) * 1000
    # Recorded here rather than in the node, so the demo endpoint and the graph
    # contribute to the same histogram -- this is the number behind the claim
    # that extraction is deterministic and effectively free.
    metrics.extraction_duration.observe(duration_ms / 1000)
    metrics.keywords_extracted.observe(len(ranked))

    return ExtractionResult(
        keywords=ranked,
        duration_ms=duration_ms,
        stats={
            "text_chars": len(job_text),
            "taxonomy_patterns": matcher.pattern_count,
            "taxonomy_hits": len(taxonomy_hits),
            "statistical_candidates": len(statistical),
            "sections_detected": [s.kind for s in sections.sections],
            "llm_tokens_used": 0,
        },
    )
