"""Node 2 — Data Retriever.

Answers two questions, both without an LLM:

  1. **What of the user's experience is relevant to this posting?**
     Ranked by keyword importance x evidence strength x recency.
  2. **What does this posting want that the user cannot support?**
     The `unsupported_keywords` list. Surfaced deliberately rather than hidden:
     these are the skills the resume must *not* claim, and they are the honest
     signal of a real gap for the user.

Why no LLM here. Relevance ranking is arithmetic over an index, and the output
of this node defines the *bounds* of what the Refactorer may say. If a model
chose the evidence set, hallucination prevention downstream would rest on a
probabilistic step -- and the guarantee in Part 6 would be unverifiable.
"""

from __future__ import annotations

import logging
from typing import Any

from app.extraction.aho import TaxonomyMatcher
from app.graph.events import step_event
from app.graph.state import ResumeForgeState
from app.graph.steps import Step
from app.matching.profile_index import Evidence, ProfileIndex

logger = logging.getLogger(__name__)

#: Cap on evidence items handed to the Refactorer. The token budget is the
#: binding constraint (NFR-02.4: under 4,000 input tokens), and beyond the top
#: dozen items relevance falls off sharply.
MAX_EVIDENCE_ITEMS = 12

#: Below this a match is noise -- typically a low-proficiency skill-list entry
#: matching a low-priority keyword.
MIN_RELEVANCE = 0.5


def _suggestion_kind(item_kind: str, on_resume: bool) -> str:
    """add: relevant but missing. emphasise: present, should rank higher."""
    if on_resume:
        return "emphasise"
    return "add" if item_kind in ("experience", "project") else "mention"


def data_retriever_agent(
    state: ResumeForgeState,
    *,
    matcher: TaxonomyMatcher | None = None,
) -> dict[str, Any]:
    """Rank profile evidence against the confirmed keywords."""
    keywords: list[dict[str, Any]] = list(state.get("keywords") or [])
    profile: dict[str, Any] = state.get("user_profile") or {}
    current_latex: str = state.get("user_latex") or ""

    if not keywords:
        return {
            "error": "No keywords to match against. Extraction must run first.",
            "current_step": Step.FAILED.value,
        }
    if not profile:
        return {
            "error": "Your profile is empty. Add experiences, projects and skills first.",
            "current_step": Step.FAILED.value,
        }

    index = ProfileIndex(profile, matcher=matcher)

    # ── Score each profile item by the keywords it supports ──
    # An item is relevant in proportion to the importance of the keywords it can
    # evidence, so scores accumulate per item rather than per keyword.
    item_scores: dict[str, float] = {}
    item_keywords: dict[str, list[str]] = {}
    item_evidence: dict[str, Evidence] = {}

    max_keyword_score = max((kw.get("score", 0.0) for kw in keywords), default=1.0) or 1.0
    unsupported: list[str] = []

    for keyword in keywords:
        term = keyword.get("term", "")
        # Normalise so keyword weight is a 0-1 multiplier rather than a raw score.
        importance = keyword.get("score", 0.0) / max_keyword_score
        evidence_list = index.evidence_for(term)

        if not evidence_list:
            # Only taxonomy-confirmed skills count as a genuine gap. An
            # unmatched statistical phrase ("fast-paced environment") is not a
            # missing skill, and reporting it as one would be noise.
            if "taxonomy" in (keyword.get("sources") or []):
                unsupported.append(term)
            continue

        for evidence in evidence_list:
            contribution = importance * evidence.weight
            item_scores[evidence.item_id] = item_scores.get(evidence.item_id, 0.0) + contribution
            item_keywords.setdefault(evidence.item_id, []).append(term)
            # Keep the strongest single piece of evidence per item for the UI.
            if (
                evidence.item_id not in item_evidence
                or evidence.weight > item_evidence[evidence.item_id].weight
            ):
                item_evidence[evidence.item_id] = evidence

    # ── Assemble the ranked evidence set ──
    ranked = sorted(item_scores.items(), key=lambda kv: kv[1], reverse=True)
    matched_evidence: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []

    for item_id, score in ranked:
        if score < MIN_RELEVANCE:
            continue
        item = index.item_by_id(item_id)
        if item is None:
            continue

        # Skill-list entries are supporting detail, not standalone evidence: they
        # would otherwise crowd out real experience in the token budget.
        if item.kind == "skill_list" and len(matched_evidence) >= MAX_EVIDENCE_ITEMS // 2:
            continue

        on_resume = _appears_in_latex(item, current_latex)
        evidence = item_evidence[item_id]
        entry = {
            "item_id": item_id,
            "kind": item.kind,
            "title": item.title,
            "relevance": round(score, 3),
            "matched_keywords": sorted(set(item_keywords[item_id])),
            "evidence": evidence.to_dict(),
            "already_on_resume": on_resume,
            "text": item.text,
        }
        matched_evidence.append(entry)

        suggestions.append(
            {
                "item_id": item_id,
                "action": _suggestion_kind(item.kind, on_resume),
                "title": item.title,
                "reason": (
                    f"Supports {len(set(item_keywords[item_id]))} keyword(s) from this "
                    f"posting: {', '.join(sorted(set(item_keywords[item_id]))[:4])}"
                ),
                "relevance": round(score, 3),
            }
        )

        if len(matched_evidence) >= MAX_EVIDENCE_ITEMS:
            break

    logger.info(
        "Matched %d profile items for %d keywords; %d keywords unsupported (0 LLM tokens)",
        len(matched_evidence),
        len(keywords),
        len(unsupported),
    )

    warnings = list(state.get("warnings") or [])
    if unsupported:
        warnings.append(
            "This posting asks for skills your profile does not evidence: "
            f"{', '.join(unsupported[:8])}. These will not be added to your resume."
        )

    return {
        "matched_evidence": matched_evidence,
        "suggestions": suggestions,
        "unsupported_keywords": unsupported,
        "warnings": warnings,
        "current_step": Step.MATCHING.value,
        "error": None,
        "events": [
            step_event(
                state,
                Step.MATCHING,
                detail=f"Matched {len(matched_evidence)} items from your profile",
                data={
                    "matched": len(matched_evidence),
                    "unsupported": len(unsupported),
                    "llm_tokens_used": 0,
                },
            )
        ],
    }


def _appears_in_latex(item: Any, latex: str) -> bool:
    """Rough check for whether an item is already on the resume.

    Deliberately conservative -- matching the title is enough. Being wrong here
    only changes an "add" suggestion into an "emphasise" one; it never affects
    what the Refactorer is permitted to claim.
    """
    if not latex or not item.title:
        return False
    haystack = latex.lower()
    # Company or project names are the reliable signal; roles repeat across jobs.
    for token in (item.metadata.get("company"), item.title):
        if token and len(str(token)) > 3 and str(token).lower() in haystack:
            return True
    return False
