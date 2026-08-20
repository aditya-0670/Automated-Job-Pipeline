r"""The refactorer's response protocol.

LaTeX inside JSON is escape-hostile: both use the backslash as an escape
character, so `\section` must be written `\\section` in a JSON string. Models get
that wrong intermittently, which broke generation at random. Worse, `\v`, `\b`
and `\f` are *valid* JSON escapes with different meanings, so some malformed
output parses successfully into corrupted LaTeX -- a silent failure.

The response is therefore delimiter-framed, and these tests pin that.
"""

from __future__ import annotations

import json

from app.prompts.refactor import (
    BODY_MARKER,
    CHANGELOG_MARKER,
    SYSTEM_PROMPT,
    extract_body,
    parse_response,
    reassemble,
)


# ── The delimiter format ─────────────────────────────────────────────────
def test_parses_body_and_changelog():
    text = (
        f"{BODY_MARKER}\n"
        r"\section{Summary} Backend engineer." + "\n"
        f"{CHANGELOG_MARKER}\n"
        "Summary | reworded | Led with backend | Posting requires Java\n"
    )
    body, changelog = parse_response(text)
    assert r"\section{Summary}" in body
    assert len(changelog) == 1
    assert changelog[0]["section"] == "Summary"
    assert changelog[0]["change_type"] == "reworded"
    assert "Java" in changelog[0]["reason"]


def test_raw_latex_needs_no_escaping():
    r"""The whole point: \section survives verbatim, with no \\section dance."""
    latex = r"\resumeItem{Fixed \textbf{500+} warnings \& improved \emph{builds}}"
    body, _ = parse_response(f"{BODY_MARKER}\n{latex}\n{CHANGELOG_MARKER}\n")
    assert body == latex


def test_latex_that_would_break_json_survives():
    r"""`\b` and `\f` are valid JSON escapes (backspace, form feed), so LaTeX
    beginning `\begin` or `\faIcon` parses *successfully* into corrupted text.
    `\v` and `\s` are not valid JSON at all and raise instead."""
    latex = r"\begin{itemize} \faIcon{book} \bfseries \vspace{5pt}"
    body, _ = parse_response(f"{BODY_MARKER}\n{latex}\n{CHANGELOG_MARKER}\n")
    assert body == latex
    for fragment in (r"\vspace", r"\begin", r"\bfseries"):
        assert fragment in body


def test_json_silently_corrupts_that_same_latex():
    r"""The dangerous case, demonstrated rather than asserted abstractly.

    `\begin` and `\faIcon` contain `\b` and `\f`, which are valid JSON escapes.
    A model that forgets to double its backslashes produces JSON that parses
    cleanly into text where those sequences have become control characters --
    corruption with no error anywhere.
    """
    latex = r"\begin{itemize}\faIcon{book}"
    decoded = json.loads('{"body": "' + latex + '"}')["body"]
    assert decoded != latex, "expected JSON to reinterpret the escapes"
    assert "\b" in decoded, r"\begin lost its backslash-b to a backspace"
    assert "\f" in decoded, r"\faIcon lost its backslash-f to a form feed"
    assert r"\begin" not in decoded


def test_json_outright_rejects_other_latex():
    r"""`\v` and `\s` are not valid JSON escapes at all, so these fail loudly --
    which is how the intermittent 'did not return parseable JSON' errors arose."""
    import pytest as _pytest

    with _pytest.raises(json.JSONDecodeError):
        json.loads('{"body": "' + r"\vspace{5pt}" + '"}')


def test_missing_changelog_marker_still_yields_a_body():
    body, changelog = parse_response(f"{BODY_MARKER}\n\\section{{X}} hi")
    assert r"\section{X}" in body
    assert changelog == []


def test_malformed_changelog_lines_are_skipped():
    text = (
        f"{BODY_MARKER}\nbody\n{CHANGELOG_MARKER}\n"
        "too few fields\n"
        "Experience | reworded | did a thing | because\n"
        "<placeholder line from the prompt example>\n"
    )
    _, changelog = parse_response(text)
    assert len(changelog) == 1
    assert changelog[0]["section"] == "Experience"


def test_prose_before_the_marker_is_discarded():
    """Models sometimes preface the answer with 'Here is the rewritten body:'."""
    body, _ = parse_response(f"Sure! Here you go:\n{BODY_MARKER}\n\\section{{X}}")
    assert body.startswith(r"\section")


# ── JSON fallback ────────────────────────────────────────────────────────
def test_falls_back_to_json_when_markers_are_absent():
    """A model that ignores the format instruction must still work."""
    body, changelog = parse_response(
        json.dumps({"body": "\\section{X} hi", "changelog": [{"section": "X"}]})
    )
    assert r"\section{X}" in body
    assert len(changelog) == 1


def test_falls_back_to_fenced_json():
    body, _ = parse_response('```json\n{"body": "\\\\section{X}"}\n```')
    assert r"\section{X}" in body


def test_accepts_the_older_latex_key():
    body, _ = parse_response(json.dumps({"latex": "\\section{X}"}))
    assert r"\section{X}" in body


def test_unparseable_response_yields_empty_body():
    """The caller treats an empty body as a node failure, with a clear message."""
    body, changelog = parse_response("I'm afraid I can't do that.")
    assert body == ""
    assert changelog == []


# ── The instruction itself ───────────────────────────────────────────────
def test_system_prompt_forbids_escaping_backslashes():
    assert BODY_MARKER in SYSTEM_PROMPT
    assert CHANGELOG_MARKER in SYSTEM_PROMPT
    assert "Do NOT escape backslashes" in SYSTEM_PROMPT


def test_system_prompt_asks_for_the_body_only():
    assert "never see or return the document preamble" in SYSTEM_PROMPT.lower()


# ── Reassembly ───────────────────────────────────────────────────────────
def test_reassembly_restores_the_users_preamble():
    original = (
        "\\documentclass{article}\n\\usepackage{mine}\n\\begin{document}\nold\n\\end{document}\n"
    )
    result = reassemble(original, "new body")
    assert r"\usepackage{mine}" in result
    assert "new body" in result
    assert "old" not in result


def test_reassembly_strips_a_wrapper_the_model_added():
    original = "\\documentclass{article}\n\\begin{document}\nold\n\\end{document}\n"
    result = reassemble(original, "\\begin{document}\nnew\n\\end{document}")
    assert result.count(r"\begin{document}") == 1
    assert result.count(r"\end{document}") == 1


def test_reassembly_without_an_anchor_returns_the_body():
    """Never fabricate a preamble the user did not write."""
    assert reassemble("no markers here", "just this") == "just this"


def test_extract_body_round_trips():
    original = "\\documentclass{a}\n\\begin{document}\n  content here  \n\\end{document}\n"
    assert extract_body(original) == "content here"
    assert extract_body(reassemble(original, "content here")) == "content here"
