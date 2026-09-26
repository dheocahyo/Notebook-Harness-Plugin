"""Text helpers shared by lint, render and nhctl: note bullets, the note cell's Markdown, clipping."""

from __future__ import annotations

import pytest

from nh_gateway._shared.text import (
    clip,
    count_words,
    escape_markdown,
    normalize_title,
    render_note,
    split_notes,
    unescape_markdown,
)

# --- split_notes ----------------------------------------------------------------------------


def test_a_wrapped_bullet_stays_one_bullet():
    # Review finding 62: the continuation line used to become a bullet of its own.
    notes = (
        "- Reads the raw CSV with pandas, keeping every column\n"
        "  as loaded so nothing is lost.\n"
        "- Shows the shape and the first rows."
    )
    assert split_notes(notes) == [
        "Reads the raw CSV with pandas, keeping every column as loaded so nothing is lost.",
        "Shows the shape and the first rows.",
    ]


def test_a_wrapped_line_does_not_push_five_bullets_to_six():
    notes = "\n".join(f"- Point {i}." for i in range(1, 5)) + "\n- Point five goes on\nand on."
    assert len(split_notes(notes)) == 5
    assert split_notes(notes)[-1] == "Point five goes on and on."


@pytest.mark.parametrize(
    ("notes", "bullets"),
    [
        ("Reads the CSV.\nShows the shape.", ["Reads the CSV.", "Shows the shape."]),  # no markers
        ("Reads the CSV. Shows the shape.", ["Reads the CSV.", "Shows the shape."]),
        ("1. Reads the CSV.\n2) Shows the shape.", ["Reads the CSV.", "Shows the shape."]),
        ("* Reads the CSV\n\n  from disk.\n• Shows it.", ["Reads the CSV from disk.", "Shows it."]),
        ("Intro line\n- Reads the CSV.", ["Intro line", "Reads the CSV."]),
        (["- Reads the CSV.", "Shows the shape."], ["Reads the CSV.", "Shows the shape."]),
        (None, []),
        ("", []),
    ],
)
def test_split_notes(notes, bullets):
    assert split_notes(notes) == bullets


def test_split_notes_keeps_raw_text_for_metadata():
    assert split_notes(["Prices range from $5 to $5,000.", "About ~40 orders."]) == [
        "Prices range from $5 to $5,000.",
        "About ~40 orders.",
    ]


# --- render_note ----------------------------------------------------------------------------


def test_note_escapes_dollars_and_tildes():
    # Review finding 61: JupyterLab typesets $…$ as math and strikes ~…~ through.
    note = render_note(
        "Plot revenue in $",
        [
            "Prices range from $5 to $5,000, so the axis is log.",
            "Drops ~5% of rows (~40 orders).",
        ],
    )
    assert note == (
        "### Plot revenue in \\$\n\n"
        "- Prices range from \\$5 to \\$5,000, so the axis is log.\n"
        "- Drops \\~5% of rows (\\~40 orders)."
    )


def test_note_leaves_code_spans_alone():
    # JupyterLab protects code spans from math itself; a backslash there would show.
    note = render_note("Filter prices", ["Keeps `df[df.price > 5]` and `x ~ y` rows costing $5+."])
    assert note.endswith("- Keeps `df[df.price > 5]` and `x ~ y` rows costing \\$5+.")
    assert "`df[df.price > 5]`" in note
    assert escape_markdown("`$HOME` costs $5") == "`$HOME` costs \\$5"


def test_escaping_is_idempotent_and_reversible():
    once = escape_markdown("from $5 to ~$9")
    assert escape_markdown(once) == once == "from \\$5 to \\~\\$9"
    assert unescape_markdown(once) == "from $5 to ~$9"


def test_a_title_read_back_from_the_note_renders_the_same_note():
    # nh_edit_cell re-renders the note from the title it reads off the heading.
    note = render_note("Revenue in $ (~5% cut)", ["One.", "Two."])
    heading = note.splitlines()[0]
    assert normalize_title(heading) == "Revenue in $ (~5% cut)"
    assert render_note(heading, ["One.", "Two."]) == note


def test_note_without_bullets_is_just_the_heading():
    assert render_note("## Load data", [], level=2) == "## Load data"


def test_escapes_do_not_change_word_counts():
    assert count_words(render_note("Revenue in $", ["Costs $5."])) == count_words(
        "### Revenue in $\n\n- Costs $5."
    )


# --- clip -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "clipped"),
    [
        ("short", 10, "short"),
        ("one\n  two", 20, "one two"),
        ("day is out of range for month", 20, "day is out of range…"),
        ("day is out of range: for month", 21, "day is out of range…"),
        ("x" * 30, 10, "xxxxxxxxx…"),
        ("abc", 1, "…"),
        ("abc", 0, ""),
    ],
)
def test_clip_cuts_at_a_word_boundary(text, limit, clipped):
    assert clip(text, limit) == clipped
    assert len(clip(text, limit)) <= max(limit, 0)
