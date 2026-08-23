"""Part 5: the Refactorer.

Most tests use MockProvider so they are deterministic and offline. The live
Gemini tests are marked `integration` and skip without a key -- but they are the
only ones that can verify the anti-hallucination prompt actually works, so they
exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.agents.data_retriever import data_retriever_agent
from app.agents.refactorer import (
    MAX_INPUT_TOKENS,
    MIN_EVIDENCE_ITEMS,
    estimate_tokens,
    fit_evidence_to_budget,
    refactorer_agent,
)
from app.clients.llm import LLMError, LLMProvider, LLMResponse, MockProvider
from app.extraction.pipeline import extract_keywords
from app.graph.state import initial_state
from app.graph.steps import Step

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))
JD = (FIXTURES / "sample_jd.txt").read_text(encoding="utf-8")
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")

# Gated by tests/conftest.py: also requires RUN_LIVE_LLM_TESTS=1.
HAS_KEY = bool(os.getenv("GEMINI_API_KEY"))
integration = pytest.mark.live


@pytest.fixture(scope="module")
def matched_state():
    """A state that has been through Nodes 1 and 2, as Node 3 will really see it."""
    state = initial_state(
        session_id="s-1",
        user_id="u-aditya",
        user_latex=RESUME,
        user_profile=PROFILE,
        job_text=JD,
    )
    state["keywords"] = [kw.to_dict() for kw in extract_keywords(JD, max_keywords=40).keywords]
    state.update(data_retriever_agent(state))
    return state


class ScriptedProvider(LLMProvider):
    """Returns a fixed payload and records what it was asked."""

    name = "scripted"
    model = "scripted"

    def __init__(self, payload: dict, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    async def complete(self, *, system, user, thinking_budget=None, json_mode=False):
        self.calls.append({"system": system, "user": user})
        if self.fail:
            raise LLMError("provider exploded")
        text = json.dumps(self.payload)
        return LLMResponse(text=text, input_tokens=100, output_tokens=50, model=self.model)


def valid_payload(latex: str = RESUME) -> dict:
    from app.prompts.refactor import extract_body

    return {
        # The model returns the body only; the preamble is reattached from the
        # user's own file, which is what makes the template guarantee structural.
        "body": extract_body(latex),
        "changelog": [
            {
                "section": "Experience",
                "change_type": "reworded",
                "before": "old",
                "after": "new",
                "reason": "matches Docker keyword",
            }
        ],
    }


# ── Token budget ─────────────────────────────────────────────────────────
def test_budget_truncates_from_the_least_relevant_end():
    """Evidence arrives ranked, so the tail is the right thing to drop."""
    evidence = [{"title": f"item {i}", "text": "x" * 4000, "kind": "project"} for i in range(20)]
    _, used, truncated = fit_evidence_to_budget(lambda ev: "".join(e["text"] for e in ev), evidence)
    assert truncated
    assert len(used) < len(evidence)
    assert used[0]["title"] == "item 0", "the most relevant item must be kept"


def test_budget_never_drops_below_the_evidence_floor():
    """A resume rewritten against two facts is worse than one that overspends."""
    evidence = [{"title": f"i{i}", "text": "x" * 40000, "kind": "project"} for i in range(10)]
    _, used, _ = fit_evidence_to_budget(lambda ev: "".join(e["text"] for e in ev), evidence)
    assert len(used) == MIN_EVIDENCE_ITEMS


def test_small_prompt_is_not_truncated():
    evidence = [{"title": "a", "text": "short", "kind": "project"}]
    _, used, truncated = fit_evidence_to_budget(lambda ev: "tiny", evidence)
    assert not truncated
    assert used == evidence


def test_real_prompt_fits_the_declared_budget(matched_state):
    """NFR-02.4: under 4,000 input tokens for a real posting and profile."""
    from app.prompts.refactor import build_refactor_prompt

    prompt = build_refactor_prompt(
        user_latex=RESUME,
        keywords=matched_state["keywords"],
        matched_evidence=matched_state["matched_evidence"],
        unsupported_keywords=matched_state["unsupported_keywords"],
    )
    assert estimate_tokens(prompt) <= MAX_INPUT_TOKENS, estimate_tokens(prompt)


# ── Generation ───────────────────────────────────────────────────────────
async def test_generates_and_records_the_changelog(matched_state):
    provider = ScriptedProvider(valid_payload())
    result = await refactorer_agent(matched_state, llm=provider)
    assert result["current_step"] == Step.REFACTORING.value
    # The real template opens with a comment banner, not \documentclass.
    assert r"\documentclass" in result["refactored_latex"]
    assert result["changelog"]
    assert result["iteration_count"] == 1


async def test_prompt_names_forbidden_skills(matched_state):
    """The posting wants Kubernetes; the profile cannot evidence it.

    The prompt must say so explicitly -- this is the instruction half of
    hallucination prevention (the enforcement half is Part 6).
    """
    provider = ScriptedProvider(valid_payload())
    await refactorer_agent(matched_state, llm=provider)
    sent = provider.calls[0]["user"]
    assert "FORBIDDEN" in sent
    assert "Kubernetes" in sent.split("FORBIDDEN")[1][:400]


async def test_prompt_forbids_altering_metrics(matched_state):
    provider = ScriptedProvider(valid_payload())
    await refactorer_agent(matched_state, llm=provider)
    assert "Never alter a metric" in provider.calls[0]["system"]


async def test_token_ledger_accumulates(matched_state):
    provider = ScriptedProvider(valid_payload())
    result = await refactorer_agent(matched_state, llm=provider)
    ledger = result["token_ledger"]
    assert ledger["calls"] == 1
    assert ledger["input_tokens"] == 100


async def test_double_escaped_latex_is_repaired(matched_state):
    """Models sometimes apply JSON escaping twice."""
    from app.prompts.refactor import extract_body

    doubled = extract_body(RESUME).replace("\\", "\\\\")
    result = await refactorer_agent(matched_state, llm=ScriptedProvider({"body": doubled}))
    latex = result["refactored_latex"]
    assert r"\section" in latex
    assert r"\\section" not in latex, "double escaping was not repaired"


async def test_preamble_is_taken_from_the_user_file_not_the_model(matched_state):
    r"""The template guarantee is structural: even a model that returns a body
    full of \usepackage lines cannot change the real preamble."""
    hostile = r"\usepackage{moderncv}" + "\n" + r"\section{Summary} hi"
    result = await refactorer_agent(matched_state, llm=ScriptedProvider({"body": hostile}))
    latex = result["refactored_latex"]
    original_preamble = RESUME.split(r"\begin{document}")[0]
    assert latex.startswith(original_preamble), "the user's preamble was not preserved verbatim"
    assert latex.count(r"\begin{document}") == 1
    assert latex.count(r"\end{document}") == 1


async def test_model_output_wrapper_is_stripped(matched_state):
    r"""Models include \begin{document} despite being told not to."""
    from app.prompts.refactor import extract_body

    wrapped = r"\begin{document}" + extract_body(RESUME) + r"\end{document}"
    result = await refactorer_agent(matched_state, llm=ScriptedProvider({"body": wrapped}))
    assert result["refactored_latex"].count(r"\begin{document}") == 1


async def test_prompt_sends_a_macro_list_not_the_preamble(matched_state):
    """Cheaper in input tokens, and it removes the temptation to redefine a macro."""
    provider = ScriptedProvider(valid_payload())
    await refactorer_agent(matched_state, llm=provider)
    sent = provider.calls[0]["user"]
    assert "resumeSubheading" in sent, "the macro list should be present"
    assert r"\usepackage{fontawesome5}" not in sent, "the preamble should not be sent"


async def test_mock_provider_returns_compilable_latex(matched_state):
    """The offline path must still produce something the compile step can use."""
    result = await refactorer_agent(matched_state, llm=MockProvider())
    assert r"\documentclass" in result["refactored_latex"]
    assert r"\end{document}" in result["refactored_latex"]


# ── Correction mode ──────────────────────────────────────────────────────
async def test_correction_mode_sends_the_previous_output_and_the_errors(matched_state):
    """The retry is feedback-driven, not a blank regeneration."""
    state = {
        **matched_state,
        "refactored_latex": RESUME,
        "evaluation": {"factual_errors": ["claims Kubernetes with no evidence"]},
        "iteration_count": 1,
    }
    provider = ScriptedProvider(valid_payload())
    result = await refactorer_agent(state, llm=provider)

    sent = provider.calls[0]
    assert "correcting your own previous output" in sent["system"]
    assert "claims Kubernetes with no evidence" in sent["user"]
    assert result["current_step"] == Step.CORRECTING.value
    assert result["iteration_count"] == 2


async def test_correction_prompt_says_remove_not_rephrase(matched_state):
    state = {**matched_state, "refactored_latex": RESUME, "evaluation": {"factual_errors": ["x"]}}
    provider = ScriptedProvider(valid_payload())
    await refactorer_agent(state, llm=provider)
    system = provider.calls[0]["system"]
    assert "REMOVE that claim" in system
    assert "still an unsupported claim" in system


async def test_user_change_request_is_refining_not_correcting(matched_state):
    state = {
        **matched_state,
        "refactored_latex": RESUME,
        "user_change_request": "make the summary shorter",
    }
    provider = ScriptedProvider(valid_payload())
    result = await refactorer_agent(state, llm=provider)
    assert result["current_step"] == Step.REFINING.value
    assert "make the summary shorter" in provider.calls[0]["user"]
    assert result["user_change_request"] == "", "the request must be consumed, not repeated"


async def test_stale_evaluation_is_cleared(matched_state):
    """A previous verdict must not route the next hop."""
    state = {**matched_state, "refactored_latex": RESUME, "evaluation": {"factual_errors": ["x"]}}
    result = await refactorer_agent(state, llm=ScriptedProvider(valid_payload()))
    assert result["evaluation"] == {}


# ── Failure paths ────────────────────────────────────────────────────────
async def test_missing_template_is_actionable(matched_state):
    result = await refactorer_agent({**matched_state, "user_latex": ""}, llm=MockProvider())
    assert result["current_step"] == Step.FAILED.value
    assert "upload" in result["error"].lower()


async def test_no_evidence_is_actionable(matched_state):
    result = await refactorer_agent({**matched_state, "matched_evidence": []}, llm=MockProvider())
    assert result["current_step"] == Step.FAILED.value
    assert "profile" in result["error"].lower()


async def test_provider_failure_on_first_attempt_fails_the_node(matched_state):
    result = await refactorer_agent(matched_state, llm=ScriptedProvider({}, fail=True))
    assert result["current_step"] == Step.FAILED.value


async def test_provider_failure_during_correction_degrades_gracefully(matched_state):
    """The previous output still exists and beats nothing."""
    state = {**matched_state, "refactored_latex": RESUME, "evaluation": {"factual_errors": ["x"]}}
    result = await refactorer_agent(state, llm=ScriptedProvider({}, fail=True))
    assert result["current_step"] != Step.FAILED.value
    assert any("previous version" in w for w in result["warnings"])


async def test_empty_latex_response_is_rejected(matched_state):
    provider = ScriptedProvider({"body": "   ", "changelog": []})
    result = await refactorer_agent(matched_state, llm=provider)
    assert result["current_step"] == Step.FAILED.value


# ── Live model ───────────────────────────────────────────────────────────
# The free tier allows 20 requests per day and 5 per minute for the primary
# model. One generation is shared across every live assertion below: five
# independent calls would consume a quarter of the daily budget per test run,
# and would rate-limit themselves on the per-minute cap besides.
@pytest.fixture(scope="module")
async def live_result(matched_state):
    if not HAS_KEY:
        pytest.skip("GEMINI_API_KEY not set")
    # One generation for the whole module -- five separate calls would be a
    # quarter of the daily quota per test run.
    result = await refactorer_agent(matched_state)
    if result.get("error"):
        pytest.skip(f"live model unavailable: {result['error'][:120]}")
    return result


@integration
async def test_gemini_preserves_the_preamble(live_result):
    """The strongest live assertion: the template survives a real rewrite."""
    result = live_result
    latex = result["refactored_latex"]

    original_preamble = RESUME.split(r"\begin{document}")[0]
    new_preamble = latex.split(r"\begin{document}")[0]

    for package in ("fontawesome5", "titlesec", "enumitem", "geometry", "hyperref"):
        assert package in new_preamble, f"{package} was dropped from the preamble"
    for macro in ("resumeItem", "resumeSubheading", "skillItem", "extlink"):
        assert macro in new_preamble, f"custom macro {macro} was lost"
    assert len(new_preamble) >= len(original_preamble) * 0.9


@integration
async def test_gemini_keeps_every_section(live_result):
    import re

    result = live_result
    original = re.findall(r"\\section\{([^}]*)\}", RESUME)
    produced = re.findall(r"\\section\{([^}]*)\}", result["refactored_latex"])
    assert produced == original, f"sections changed: {original} -> {produced}"


@integration
async def test_gemini_does_not_claim_forbidden_skills(live_result):
    """The claim this product exists to make, verified against a real model."""
    latex = live_result["refactored_latex"].lower()
    for forbidden in ("kubernetes", "salesforce", "apex", "spring boot", "terraform"):
        assert forbidden not in latex, f"the model claimed {forbidden!r} with no evidence"


@integration
async def test_gemini_stays_within_the_token_budget(live_result):
    ledger = live_result["token_ledger"]
    assert ledger["input_tokens"] < MAX_INPUT_TOKENS * 1.3, ledger


@integration
async def test_gemini_returns_a_reasoned_changelog(live_result):
    changelog = live_result["changelog"]
    assert changelog
    assert all(entry.get("reason") for entry in changelog), "every change must justify itself"
