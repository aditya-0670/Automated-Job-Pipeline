"""Section-level before/after diff for the human review step.

The user is being asked to approve a rewrite of their own resume, so the review
has to answer one question: *what actually changed?* A raw unified diff over the
whole file answers it badly -- LaTeX rewraps, so a reflowed paragraph looks like
a total rewrite, and the preamble noise buries the two bullets that really moved.

So the diff is computed **per `\\section`**, which is the unit the user thinks in
("you changed my Experience section"), with a line-level diff available inside
each changed section for the ones they want to inspect.

Nothing here is stored in state. The diff is a pure function of `user_latex` and
`refactored_latex`, both of which are already checkpointed; persisting it too
would put a third copy of the resume in every checkpoint row to save a
sub-millisecond computation.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

#: Matches \section{...} and \section*{...}, capturing the title.
_SECTION_RE = re.compile(r"\\section\*?\s*\{([^}]*)\}")

#: The bucket for everything before the first \section -- preamble, \begin
#: {document}, the name/contact header. Named rather than titled "" so the UI
#: has something to show and so a genuinely untitled section cannot collide.
PREAMBLE = "(preamble & header)"

ChangeType = str  # "unchanged" | "modified" | "added" | "removed"


def split_sections(latex: str) -> dict[str, str]:
    """Split a LaTeX document into `title -> body`, preserving order.

    Duplicate titles are suffixed (`Projects (2)`) rather than merged: a resume
    with two `\\section{Projects}` is unusual but legal, and silently collapsing
    them would show the user a diff of content they never had adjacent.
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(latex))

    head = latex[: matches[0].start()] if matches else latex
    if head.strip():
        sections[PREAMBLE] = head

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(latex)
        title = match.group(1).strip() or f"(untitled {index + 1})"
        key, dupe = title, 1
        while key in sections:
            dupe += 1
            key = f"{title} ({dupe})"
        sections[key] = latex[match.start() : end]

    return sections


def _normalise(body: str) -> list[str]:
    """Lines for comparison: whitespace-collapsed, blanks dropped.

    Reindenting a bullet or rewrapping a line is not a change the user needs to
    approve, and reporting it as one trains them to click through the diff
    without reading it.
    """
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return [line for line in lines if line]


def similarity(before: str, after: str) -> float:
    """0.0-1.0 content similarity of two section bodies.

    Character-level, not line-level. Line-level would score "Oracle." changed to
    "Oracle Corp." exactly as low as a total rewrite of that line, and the UI uses
    this number to decide which sections are worth expanding by default.
    """
    return difflib.SequenceMatcher(
        None, " ".join(_normalise(before)), " ".join(_normalise(after))
    ).ratio()


def unified(before: str, after: str, *, title: str = "", context: int = 2) -> str:
    """A conventional unified diff of one section, for the detail view."""
    return "\n".join(
        difflib.unified_diff(
            _normalise(before),
            _normalise(after),
            fromfile=f"before: {title}" if title else "before",
            tofile=f"after: {title}" if title else "after",
            lineterm="",
            n=context,
        )
    )


def diff_sections(
    original: str, revised: str, *, include_unified: bool = True
) -> list[dict[str, Any]]:
    """Section-by-section comparison of two resumes.

    Returns one entry per section in the order a reader meets them: the revised
    document's order, with any sections that were removed appended at the end so
    a deletion is impossible to miss.
    """
    before = split_sections(original)
    after = split_sections(revised)
    entries: list[dict[str, Any]] = []

    for title, new_body in after.items():
        old_body = before.get(title)
        if old_body is None:
            change: ChangeType = "added"
        elif _normalise(old_body) == _normalise(new_body):
            change = "unchanged"
        else:
            change = "modified"

        entry: dict[str, Any] = {
            "section": title,
            "change": change,
            "similarity": round(similarity(old_body or "", new_body), 3),
            "before_lines": len(_normalise(old_body or "")),
            "after_lines": len(_normalise(new_body)),
        }
        if include_unified and change == "modified":
            entry["diff"] = unified(old_body or "", new_body, title=title)
        entries.append(entry)

    for title, old_body in before.items():
        if title not in after:
            entries.append(
                {
                    "section": title,
                    "change": "removed",
                    "similarity": 0.0,
                    "before_lines": len(_normalise(old_body)),
                    "after_lines": 0,
                    # A removed \section is nearly always a bug in the rewrite,
                    # not an edit the user asked for, so it carries its content.
                    "diff": unified(old_body, "", title=title) if include_unified else "",
                }
            )

    return entries


def diff_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for the review header, so the UI needs no logic of its own."""
    counts = {"unchanged": 0, "modified": 0, "added": 0, "removed": 0}
    for entry in entries:
        counts[entry["change"]] = counts.get(entry["change"], 0) + 1
    return {
        **counts,
        "total_sections": len(entries),
        "changed": counts["modified"] + counts["added"] + counts["removed"],
    }
