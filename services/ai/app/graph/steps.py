"""The pipeline lifecycle -- one enum, used everywhere.

`current_step` is written by every node, checkpointed to Postgres, streamed to
the browser over SSE, and rendered as the progress indicator. Defining it once
means the UI cannot drift from the graph, and a step rename is a type error
rather than a silently broken progress bar.
"""

from __future__ import annotations

from enum import StrEnum


class Step(StrEnum):
    INIT = "INIT"
    SCRAPING = "SCRAPING"
    EXTRACTING = "EXTRACTING"
    KEYWORDS_PENDING = "KEYWORDS_PENDING"  # interrupt: user confirms keywords
    MATCHING = "MATCHING"
    REFACTORING = "REFACTORING"
    EVALUATING = "EVALUATING"
    CORRECTING = "CORRECTING"  # self-correction loop iteration
    HUMAN_REVIEW = "HUMAN_REVIEW"  # interrupt: user reviews the diff
    REFINING = "REFINING"  # user asked for changes
    COMPILING = "COMPILING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


#: Steps at which the graph is paused waiting on a person, not on compute.
INTERRUPT_STEPS: frozenset[Step] = frozenset({Step.KEYWORDS_PENDING, Step.HUMAN_REVIEW})

#: Steps after which no further work happens.
TERMINAL_STEPS: frozenset[Step] = frozenset({Step.COMPLETE, Step.FAILED})

#: Display order for the UI progress indicator. The loop steps (CORRECTING,
#: REFINING) are intentionally absent -- they re-enter an earlier stage rather
#: than advancing, so showing them as separate positions would be misleading.
PROGRESS_SEQUENCE: tuple[Step, ...] = (
    Step.SCRAPING,
    Step.EXTRACTING,
    Step.KEYWORDS_PENDING,
    Step.MATCHING,
    Step.REFACTORING,
    Step.EVALUATING,
    Step.HUMAN_REVIEW,
    Step.COMPILING,
    Step.COMPLETE,
)

HUMAN_READABLE: dict[Step, str] = {
    Step.INIT: "Starting",
    Step.SCRAPING: "Reading the job posting",
    Step.EXTRACTING: "Extracting keywords",
    Step.KEYWORDS_PENDING: "Waiting for you to confirm keywords",
    Step.MATCHING: "Finding relevant experience in your profile",
    Step.REFACTORING: "Rewriting your resume",
    Step.EVALUATING: "Checking for errors and unsupported claims",
    Step.CORRECTING: "Fixing issues found during review",
    Step.HUMAN_REVIEW: "Waiting for your review",
    Step.REFINING: "Applying your requested changes",
    Step.COMPILING: "Compiling the PDF",
    Step.COMPLETE: "Done",
    Step.FAILED: "Failed",
}


def progress_fraction(step: Step) -> float:
    """0.0-1.0 for the progress bar. Loop steps hold their parent's position."""
    equivalent = {Step.CORRECTING: Step.REFACTORING, Step.REFINING: Step.REFACTORING}
    resolved = equivalent.get(step, step)
    if resolved is Step.INIT:
        return 0.0
    if resolved is Step.FAILED:
        return 1.0
    try:
        index = PROGRESS_SEQUENCE.index(resolved)
    except ValueError:
        return 0.0
    return (index + 1) / len(PROGRESS_SEQUENCE)
