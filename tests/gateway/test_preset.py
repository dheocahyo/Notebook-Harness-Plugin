"""The preset through the real gateway (design §6.9): a bad level's config problem in the results
and in nh_inspect's status, the senior comment budget, the level's live reload (ConfigCache's
mtime), an explicit [lint] comment_ratio winning, and strict mode making the budget hard."""

from __future__ import annotations

import os
import time
from pathlib import Path

from tests.fakes.turns import text
from tests.gateway.conftest import NOTEBOOK, Harness

LEVEL_PROBLEM = "--- config ---\nharness.toml: preset.level must be junior|senior"
HINT = "comment lines for 16 lines of code (budget 1)"


def cell(title: str, comments: int, code_lines: int = 16) -> dict:
    """``code_lines`` lines of code under ``comments`` comment lines: 16 code lines allow 2
    comment lines at junior's ratio (8), 1 at senior's (16)."""
    notes = [f"# Step {i} keeps the raw values for the check below" for i in range(comments)]
    body = [f"value_{i} = {i}" for i in range(code_lines - 1)] + ["value_0"]
    return {
        "title": title,
        "notes": ["Sets a few values for the check.", "Shows the first value."],
        "intent": "set values",
        "code": "\n".join(notes + body),
    }


def set_harness(project: Path, extra: str) -> None:
    """Rewrite harness.toml with a later mtime, as an edit between two tool calls gives."""
    path = project / "harness.toml"
    path.write_text(f'version = 1\n[project]\nnotebook = "{NOTEBOOK}"\n{extra}')
    later = time.time() + 5
    os.utime(path, (later, later))


async def test_a_bad_level_shows_in_the_results_and_the_status(nh: Harness) -> None:
    set_harness(nh.project, '[preset]\nlevel = "expert"\n')
    nh.turns.prompt("p1")
    result = await nh.call("nh_add_cell", "p1", **cell("Set values", 1))
    body = text(result)
    assert not result.is_error, body
    assert LEVEL_PROBLEM in body, body
    assert HINT not in body  # read as junior: 2 comment lines allowed for 16 lines
    status = text(await nh.call("nh_inspect", "p1", view="status"))
    assert "harness.toml: preset.level must be junior|senior" in status, status


async def test_the_senior_budget_follows_the_file_from_the_next_call(nh: Harness) -> None:
    set_harness(nh.project, '[preset]\nlevel = "senior"\n')
    nh.turns.prompt("p1")
    senior = text(await nh.call("nh_add_cell", "p1", **cell("Set values one", 2)))
    assert "The cell has 2 " + HINT in senior, senior
    assert "--- config ---" not in senior
    set_harness(nh.project, '[preset]\nlevel = "junior"\n')  # e.g. nhctl preset junior
    nh.turns.prompt("p2")
    junior = text(await nh.call("nh_add_cell", "p2", **cell("Set values two", 2)))
    assert "comment lines for" not in junior, junior
    set_harness(nh.project, '[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = 8\n')
    nh.turns.prompt("p3")
    explicit = text(await nh.call("nh_add_cell", "p3", **cell("Set values three", 2)))
    assert "comment lines for" not in explicit, explicit  # the explicit ratio wins


async def test_strict_mode_makes_the_senior_budget_hard(nh: Harness) -> None:
    set_harness(nh.project, '[preset]\nlevel = "senior"\n[lint]\nmode = "strict"\n')
    nh.turns.prompt("p1")
    result = await nh.call("nh_add_cell", "p1", **cell("Set values", 2))
    body = text(result)
    assert result.is_error and "E120" in body and "L105" in body, body
    assert nh.cells() == []  # nothing written
    nh.turns.prompt("p2")
    ok = await nh.call("nh_add_cell", "p2", **cell("Set values", 1))
    assert not ok.is_error, text(ok)
