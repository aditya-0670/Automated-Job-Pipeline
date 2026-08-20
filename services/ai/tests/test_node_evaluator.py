"""Part 6: the Evaluator node -- rules first, model second."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.evaluator import LOW_COVERAGE_THRESHOLD, evaluator_agent
from app.clients.llm import LLMError, LLMProvider, LLMResponse
from app.graph.state import initial_state
from app.graph.steps import Step

FIXTURES = Path(__file__).parent / "fixtures"
RESUME = (FIXTURES / "real_resume.tex").read_text(encoding="utf-8")
PROFILE = json.loads((FIXTURES / "real_profile.json").read_text(encoding="utf-8"))


class CountingProvider(LLMProvider):
    """Records whether it was called at all -- the point of several tests."""

    name = "counting"
    model = "counting"

    def __init__(self, payload: dict | None = None, *, fail: bool = False) -> None:
        self.payload = (
            payload
            if payload is not None
            else {
                "quality_issues": [],
                "keyword_coverage": 0.8,
                "strengths": ["clear bullets"],
                "feedback": "Looks good.",
            }
        )
        self.fail = fail
        self.calls = 0

    async def complete(self, *, system, user, thinking_budget=None, json_mode=False):
        self.calls += 1
        if self.fail:
            raise LLMError("quality service down")
        return LLMResponse(
            text=json.dumps(self.payload), input_tokens=200, output_tokens=80, model=self.model
        )


def state_with(generated: str, **overrides):
    state = initial_state(
        session_id="s-1",
        user_id="u-aditya",
        user_latex=RESUME,
        user_profile=PROFILE,
    )
    state["refactored_latex"] = generated
    state.update(overrides)
    return state


def hallucinated() -> str:
    return RESUME.replace(
        r"\end{document}",
        r"\resumeItem{Orchestrated \textbf{Kubernetes} clusters with Terraform.}"
        + "\n"
        + r"\end{document}",
    )


# ── Ordering: rules before the model ─────────────────────────────────────
async def test_clean_resume_passes_and_runs_the_quality_pass():
    provider = CountingProvider()
    result = await evaluator_agent(state_with(RESUME), llm=provider)
    evaluation = result["evaluation"]
    assert evaluation["passed"] is True
    assert evaluation["factual_errors"] == []
    assert provider.calls == 1


async def test_hallucination_is_caught_without_calling_the_model():
    """The guarantee never depends on a model call succeeding.

    It is also the cheap path: the output is about to be regenerated, so
    spending tokens on its tone would be waste.
    """
    provider = CountingProvider()
    result = await evaluator_agent(state_with(hallucinated()), llm=provider)
    evaluation = result["evaluation"]
    assert evaluation["passed"] is False
    assert evaluation["factual_errors"]
    assert provider.calls == 0, "the LLM must not be called when rules already failed"


async def test_structural_break_is_caught_without_calling_the_model():
    broken = RESUME.replace("\\usepackage{fontawesome5}\n", "")
    provider = CountingProvider()
    result = await evaluator_agent(state_with(broken), llm=provider)
    assert result["evaluation"]["structural_errors"]
    assert provider.calls == 0


async def test_errors_are_phrased_for_the_correction_prompt():
    result = await evaluator_agent(state_with(hallucinated()), llm=CountingProvider())
    errors = result["evaluation"]["factual_errors"]
    assert any("Kubernetes" in e for e in errors)
    assert all(isinstance(e, str) and len(e) > 20 for e in errors)


# ── The quality pass ─────────────────────────────────────────────────────
async def test_quality_issues_do_not_block():
    """Taste is not correctness. Looping on it has no defined endpoint."""
    provider = CountingProvider(
        {
            "quality_issues": [
                {"section": "Summary", "detail": "vague", "suggestion": "be specific"}
            ],
            "keyword_coverage": 0.7,
            "feedback": "Could be sharper.",
        }
    )
    result = await evaluator_agent(state_with(RESUME), llm=provider)
    evaluation = result["evaluation"]
    assert evaluation["quality_issues"]
    assert evaluation["passed"] is True, "quality issues must not trigger a retry"


async def test_low_coverage_warns_the_user():
    provider = CountingProvider({"quality_issues": [], "keyword_coverage": 0.2, "feedback": "thin"})
    result = await evaluator_agent(state_with(RESUME), llm=provider)
    assert any("priorities" in w for w in result["warnings"])
    assert result["evaluation"]["passed"] is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.82, 0.82), ("0.82", 0.82), (82, 0.82), (150, 1.0), (-1, 0.0), ("bad", None), (None, None)],
)
async def test_coverage_is_normalised(raw, expected):
    """Models return 0.82, "0.82", or 82."""
    provider = CountingProvider({"quality_issues": [], "keyword_coverage": raw})
    result = await evaluator_agent(state_with(RESUME), llm=provider)
    coverage = result["evaluation"]["keyword_coverage"]
    if expected is None:
        assert coverage is None
    else:
        assert coverage == pytest.approx(expected, abs=0.01)


async def test_quality_pass_failure_does_not_fail_the_pipeline():
    """The deterministic checks passed, so the resume is sound. Quality is advisory."""
    result = await evaluator_agent(state_with(RESUME), llm=CountingProvider(fail=True))
    assert result["evaluation"]["passed"] is True
    assert result["current_step"] == Step.EVALUATING.value
    assert "could not be completed" in result["evaluation"]["feedback"]


async def test_malformed_quality_issues_are_discarded():
    provider = CountingProvider({"quality_issues": ["not a dict", 42], "keyword_coverage": 0.5})
    result = await evaluator_agent(state_with(RESUME), llm=provider)
    assert result["evaluation"]["quality_issues"] == []


async def test_skip_llm_runs_rules_only():
    provider = CountingProvider()
    result = await evaluator_agent(state_with(RESUME), llm=provider, skip_llm=True)
    assert provider.calls == 0
    assert result["evaluation"]["passed"] is True


# ── Contract with routing and state ──────────────────────────────────────
async def test_evaluation_shape_matches_what_routing_reads():
    from app.graph.routing import has_blocking_errors

    clean = await evaluator_agent(state_with(RESUME), llm=CountingProvider())
    dirty = await evaluator_agent(state_with(hallucinated()), llm=CountingProvider())
    assert not has_blocking_errors(clean["evaluation"])
    assert has_blocking_errors(dirty["evaluation"])


async def test_evaluation_is_json_serialisable():
    """It goes into checkpointed state, so it must round-trip."""
    result = await evaluator_agent(state_with(hallucinated()), llm=CountingProvider())
    json.dumps(result["evaluation"])


async def test_evaluates_a_user_edit_when_no_generated_latex_exists():
    """A hand-edited template still goes through the guardrails."""
    state = state_with("", edited_latex=hallucinated())
    result = await evaluator_agent(state, llm=CountingProvider())
    assert result["evaluation"]["factual_errors"]


async def test_no_input_is_an_error():
    result = await evaluator_agent(state_with(""), llm=CountingProvider())
    assert result["current_step"] == Step.FAILED.value


async def test_token_ledger_records_only_when_the_model_ran():
    blocked = await evaluator_agent(state_with(hallucinated()), llm=CountingProvider())
    assert blocked["token_ledger"]["calls"] == 0
    clean = await evaluator_agent(state_with(RESUME), llm=CountingProvider())
    assert clean["token_ledger"]["calls"] == 1


def test_low_coverage_threshold_is_below_half():
    """A candidate missing half the requirements should not be told their resume
    is bad -- the gap is experience, not writing."""
    assert LOW_COVERAGE_THRESHOLD < 0.5
