"""Layer 1: statistical keyword extraction (YAKE + RAKE ensemble).

Neither algorithm knows anything about software skills -- that is the point.
They catch terms the curated taxonomy has never heard of (a framework released
last month, a company-specific system name), while the taxonomy layer catches
the domain skills that are too infrequent for statistics to notice.

YAKE scores single terms using position, frequency and context spread; it is
good at important one-word domain terms. RAKE splits the text on stopwords to
build candidate phrases and scores them by word degree over frequency; it is
good at multi-word technical phrases like "distributed systems". Their blind
spots are close to complementary, so the union is used.

RAKE is implemented here rather than pulled from a library so the container does
not need to download an NLTK corpus at build or run time -- the algorithm is
~40 lines and a build-time network fetch is a reproducibility hazard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import yake

logger = logging.getLogger(__name__)

# Minimal English stoplist. RAKE only needs it to find phrase boundaries, so
# precision here matters far less than it would for a scoring model.
_STOPWORDS: frozenset[str] = frozenset(
    """
a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing down
during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself just me more most my myself
no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then
there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yourself
yourselves able across along also among amount another anyone around become
becomes bring come comes ensure etc get getting give given go going great help
including keep like look made make making many may might much must need needs
new one part per plus provide provides quickly really strong take taking use
used using want well within work working years year experience experiences
role roles team teams job jobs candidate candidates ideal preferred required
requirements responsibilities qualifications skills skill ability
""".split()
)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#./-]*")
_SENTENCE_RE = re.compile(r"[.!?;:\n\r\t()\[\]{}<>,|]+")


@dataclass(frozen=True)
class ScoredTerm:
    term: str
    score: float  # normalised 0..1, higher = more relevant
    source: str  # "yake" | "rake"


def extract_yake(text: str, top_n: int = 40) -> list[ScoredTerm]:
    """Single- and two-word candidates via YAKE. Lower raw score is better."""
    if not text.strip():
        return []
    extractor = yake.KeywordExtractor(
        lan="en",
        n=2,  # allow bigrams; RAKE covers longer phrases
        dedupLim=0.85,
        top=top_n,
        features=None,
    )
    try:
        raw = extractor.extract_keywords(text)
    except Exception:  # yake is fragile on degenerate input
        logger.warning("YAKE extraction failed; falling back to RAKE only", exc_info=True)
        return []

    if not raw:
        return []
    # YAKE returns (term, score) with score ascending = more relevant. Invert
    # and normalise so every layer speaks the same "higher is better" language.
    worst = max(score for _, score in raw) or 1.0
    return [
        ScoredTerm(term=term.lower().strip(), score=1.0 - (score / worst), source="yake")
        for term, score in raw
        if term.strip()
    ]


def extract_rake(text: str, top_n: int = 40, max_phrase_words: int = 4) -> list[ScoredTerm]:
    """Multi-word candidate phrases via RAKE (degree / frequency scoring)."""
    if not text.strip():
        return []

    # 1. Split into candidate phrases on punctuation, then on stopwords.
    phrases: list[list[str]] = []
    for chunk in _SENTENCE_RE.split(text.lower()):
        current: list[str] = []
        for token in _TOKEN_RE.findall(chunk):
            if token in _STOPWORDS or token.isdigit():
                if current:
                    phrases.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            phrases.append(current)

    phrases = [p for p in phrases if 0 < len(p) <= max_phrase_words]
    if not phrases:
        return []

    # 2. Score each word: degree (co-occurrence span) over frequency.
    freq: dict[str, int] = {}
    degree: dict[str, int] = {}
    for phrase in phrases:
        span = len(phrase) - 1
        for word in phrase:
            freq[word] = freq.get(word, 0) + 1
            degree[word] = degree.get(word, 0) + span
    word_score = {w: (degree[w] + freq[w]) / freq[w] for w in freq}

    # 3. A phrase scores as the sum of its member word scores.
    scored: dict[str, float] = {}
    for phrase in phrases:
        joined = " ".join(phrase)
        scored[joined] = max(scored.get(joined, 0.0), sum(word_score[w] for w in phrase))

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    if not ranked:
        return []
    best = ranked[0][1] or 1.0
    return [ScoredTerm(term=term, score=score / best, source="rake") for term, score in ranked]


def extract_statistical(text: str, top_n: int = 40) -> list[ScoredTerm]:
    """Union of both extractors, best score kept per term."""
    merged: dict[str, ScoredTerm] = {}
    for term in [*extract_yake(text, top_n), *extract_rake(text, top_n)]:
        existing = merged.get(term.term)
        if existing is None or term.score > existing.score:
            merged[term.term] = term
    return sorted(merged.values(), key=lambda t: t.score, reverse=True)
