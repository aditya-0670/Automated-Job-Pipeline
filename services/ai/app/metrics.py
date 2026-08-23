"""Prometheus metrics for the pipeline.

The metrics are chosen from the questions someone would actually ask at 3am, not
from what is easy to instrument:

  * **Is it working?** — runs started vs finished, by outcome.
  * **Is it slow?** — pipeline duration, and duration per node, because "slow"
    almost always means one node and the average hides which.
  * **What is it costing?** — tokens by model and step. This is the number that
    turns into a bill, and it is the one the whole deterministic-extraction
    design exists to keep down; a dashboard that cannot show it cannot show
    whether that design is still paying off.
  * **Is the model behaving?** — guardrail failures by kind, and self-correction
    iterations. A rise in factual failures means the model started hallucinating
    more, and that is invisible in error rates because the pipeline *handles* it.

Labels are deliberately low-cardinality. `session_id` is not a label: it is
unbounded, and one series per session is how a Prometheus instance falls over.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

# ── Pipeline ─────────────────────────────────────────────────────────────
pipeline_runs = Counter(
    "resumeforge_pipeline_runs_total",
    "Pipeline runs, by how they ended.",
    ["outcome"],  # started | completed | failed
)

pipeline_duration = Histogram(
    "resumeforge_pipeline_duration_seconds",
    "Wall-clock time from run start to a terminal state.",
    # Buckets chosen around observed reality: a run reaches the keyword gate in
    # under a second, and a full generate-evaluate-compile cycle is 30-90s. The
    # default buckets top out at 10s and would put every real run in +Inf.
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

node_duration = Histogram(
    "resumeforge_node_duration_seconds",
    "Time spent in each graph node.",
    ["node"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 120),
)

sessions_paused = Gauge(
    "resumeforge_sessions_awaiting_input",
    "Sessions currently paused at a human gate, by gate.",
    ["gate"],
)

# ── Cost ─────────────────────────────────────────────────────────────────
llm_tokens = Counter(
    "resumeforge_llm_tokens_total",
    "Tokens billed, by model, pipeline step and kind.",
    ["model", "step", "kind"],  # kind: input | output | thinking
)

llm_calls = Counter(
    "resumeforge_llm_calls_total",
    "Model calls, by model and outcome.",
    ["model", "outcome"],  # ok | error | fallback
)

llm_latency = Histogram(
    "resumeforge_llm_latency_seconds",
    "Model call latency.",
    ["model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)

# ── Determinism, and what it saves ───────────────────────────────────────
extraction_duration = Histogram(
    "resumeforge_extraction_duration_seconds",
    "Deterministic keyword extraction. Costs zero tokens by construction.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
)

keywords_extracted = Histogram(
    "resumeforge_keywords_extracted",
    "Keywords returned per posting.",
    buckets=(5, 10, 20, 30, 40, 60, 100),
)

# ── Quality ──────────────────────────────────────────────────────────────
guardrail_failures = Counter(
    "resumeforge_guardrail_failures_total",
    "Deterministic guardrail rejections, by kind. Detected at zero token cost.",
    ["kind"],  # factual | structural
)

self_corrections = Histogram(
    "resumeforge_self_correction_iterations",
    "Refactor attempts per session. 1 means it was right first time.",
    buckets=(1, 2, 3, 4, 5),
)

compilations = Counter(
    "resumeforge_pdf_compilations_total",
    "LaTeX compilations, by outcome.",
    ["outcome"],  # ok | failed
)


@contextmanager
def timed(histogram: Histogram, *labels: str):
    """Observe a duration, including when the block raises.

    A `try/finally` rather than the library's own decorator so a node that fails
    still records how long it took to fail -- which is usually the slow one.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        target = histogram.labels(*labels) if labels else histogram
        target.observe(time.perf_counter() - started)


def record_llm_response(step: str, response) -> None:
    """Record one model call's cost. Called from the provider, so no agent can
    spend tokens without it being counted."""
    model = getattr(response, "model", "unknown") or "unknown"
    llm_tokens.labels(model, step, "input").inc(getattr(response, "input_tokens", 0))
    llm_tokens.labels(model, step, "output").inc(getattr(response, "output_tokens", 0))
    thinking = getattr(response, "thinking_tokens", 0)
    if thinking:
        # Billed as output on Gemini 3.x, but separated here: it is the line item
        # that moves when a thinking budget changes, and folding it into output
        # hides that.
        llm_tokens.labels(model, step, "thinking").inc(thinking)
