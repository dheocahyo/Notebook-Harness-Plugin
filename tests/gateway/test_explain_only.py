"""v0.2 explain-only (design §6.2): an explain, plan or ask message changes nothing in the
notebook (E109), through the real hooks and the gateway (FakeBackend)."""

from __future__ import annotations

import re

import pytest

from nh_gateway.app import INSTRUCTIONS
from nh_gateway.policy.errors import CATALOGUE, E109_NEXT, RETURN_TO_WORKFLOW, WRITER_LINE
from tests.fakes.turns import text
from tests.gateway.conftest import Harness

LOAD = dict(
    title="Load sales data",
    notes=[
        "Builds a small frame of prices by region.",
        "Keeps the missing price so later steps can drop it.",
    ],
    intent="load the sales data",
    code="import pandas as pd\n\ndf = pd.DataFrame({'region': ['a', 'b', 'a'], 'price': [1.0, None, 3.0]})\ndf.shape",
)
DROP = dict(
    title="Drop rows with missing price",
    notes=["Removes rows where price is empty.", "Price is the target, so imputing would bias it."],
    intent="drop rows with null price",
    code="df_clean = df.dropna(subset=['price'])\ndf_clean.shape",
)
RUN = "wf_run-1"
FIRST = "Not {verb}: no notebook change in an explain, plan or ask message."
# Design §6.2's Next lines, pinned: what the model does instead of writing.
NEXT = {
    "explain": "Answer in chat with a numbered walkthrough; write nothing this message.",
    "plan": "Reply with the numbered plan; write nothing this message.",
    "ask": "Ask the user the one question; write nothing until they reply.",
}
MISSED = "nh missed this message; send it again."


def code_cells(h: Harness) -> list[dict]:
    return [
        c
        for c in h.cells()
        if c["cell_type"] == "code" and c.get("metadata", {}).get("nh", {}).get("role") == "code"
    ]


def assert_e109(result, verb: str, mode: str = "explain") -> None:
    body = text(result)
    assert result.is_error and body.splitlines()[:3] == [
        FIRST.format(verb=verb),
        "nh: E109",
        f"Next: {NEXT[mode]}",
    ], body


def assert_allowed(result, machine: str) -> None:
    """Not refused at all (no E-code), and the tool's own machine line, e.g. "nh: wait"."""
    body = text(result)
    assert not result.is_error and not re.search(r"^nh: E\d{3}", body, flags=re.M), body
    assert re.search(rf"^{re.escape(machine)}", body, flags=re.M), body


def test_e109_text_is_pinned() -> None:
    assert E109_NEXT == NEXT
    assert CATALOGUE["E109"] == (FIRST, NEXT["explain"])


async def loaded(nh: Harness) -> str:
    """A plain first message whose cell stands; returns that cell's id."""
    nh.turns.prompt("p1", text="load the sales data")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    return code_cells(nh)[-1]["id"]


async def test_an_explain_message_changes_nothing(nh: Harness) -> None:
    uid = await loaded(nh)
    source = code_cells(nh)[-1]["source"]
    nh.turns.prompt("p2", text="explain the load cell")
    assert_e109(await nh.call("nh_add_cell", "p2", **DROP), "written")
    assert_e109(await nh.call("nh_edit_cell", "p2", cell_id=uid, code="df.shape"), "written")
    assert_e109(await nh.call("nh_run", "p2", cell_id=uid), "run")
    assert_e109(await nh.call("nh_run", "p2", cell_id=uid, mode="run"), "run")
    assert_e109(await nh.call("nh_undo", "p2"), "undone")
    assert [c["source"] for c in code_cells(nh)] == [source]


@pytest.mark.parametrize("mode", ["wait", "interrupt"])
async def test_an_explain_message_may_still_wait_or_interrupt(nh: Harness, mode: str) -> None:
    uid = await loaded(nh)
    nh.turns.prompt("p2", text="/nh:explain the load cell")
    assert_allowed(await nh.call("nh_run", "p2", cell_id=uid, mode=mode), f"nh: {mode} ")


async def test_inspect_is_never_gated(nh: Harness) -> None:
    await loaded(nh)
    nh.turns.prompt("p2", text="explain df")
    result = await nh.call("nh_inspect", "p2", view="outline")
    assert not result.is_error and "Load sales data" in text(result)


@pytest.mark.parametrize(
    ("message", "mode"),
    [
        ("/nh:plan a churn model", "plan"),
        ("run the next 3", "ask"),
        ("re-run the stale cells", "ask"),
    ],
)
async def test_plan_and_ask_messages_change_nothing(nh: Harness, message: str, mode: str) -> None:
    uid = await loaded(nh)
    nh.turns.prompt("p2", text=message)
    assert_e109(await nh.call("nh_add_cell", "p2", **DROP), "written", mode)
    assert_e109(await nh.call("nh_run", "p2", cell_id=uid), "run", mode)


@pytest.mark.parametrize("reply", ["go", "yes", "drop the rows with missing price"])
async def test_the_next_message_without_a_mode_writes_again(nh: Harness, reply: str) -> None:
    await loaded(nh)
    nh.turns.prompt("p2", text="explain the load cell")
    assert_e109(await nh.call("nh_add_cell", "p2", **DROP), "written")
    nh.turns.prompt("p3", text=reply)
    result = await nh.call("nh_add_cell", "p3", **DROP)
    assert not result.is_error, text(result)
    assert len(code_cells(nh)) == 2


async def test_an_explain_typed_mid_turn_blocks_the_rest_of_the_turn(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the sales data")
    failed = await nh.call("nh_add_cell", "p1", **dict(LOAD, code="undefined_name"))
    assert not failed.is_error and "nh: cell=" in text(failed)
    uid = code_cells(nh)[-1]["id"]
    nh.turns.prompt("p1", text="explain what went wrong")  # typed mid-turn: absorbed into p1
    # The retry of the cell written before the message arrived is refused too (tighten-only).
    assert_e109(await nh.call("nh_edit_cell", "p1", cell_id=uid, code=LOAD["code"]), "written")
    assert_e109(await nh.call("nh_run", "p1", cell_id=uid), "run")
    assert_e109(await nh.call("nh_undo", "p1"), "undone")
    assert [c["source"] for c in code_cells(nh)] == ["undefined_name"]


async def test_the_mode_is_keyed_on_the_calls_canonical_turn(nh: Harness) -> None:
    """A notification's alias is its human turn: an explain message continued under its
    background task's prompt id still writes nothing, and an earlier turn's alias keeps that
    turn's own outcome (E110 for its spent budget), not the newer message's mode."""
    nh.turns.prompt("p1", text="load the sales data")
    nh.turns.notification("note-1")
    assert not (await nh.call("nh_add_cell", "note-1", **LOAD)).is_error
    nh.turns.prompt("p2", text="explain the load cell")
    nh.turns.notification("note-2")  # an alias of p2
    assert_e109(await nh.call("nh_add_cell", "note-2", **DROP), "written")
    late = await nh.call("nh_add_cell", "note-1", **dict(DROP, title="Late"))  # p1's alias
    body = text(late)
    assert late.is_error and "nh: E110" in body, body
    assert "nh: E109" not in body and MISSED not in body
    assert len(code_cells(nh)) == 1


async def test_a_plain_message_typed_mid_turn_keeps_the_explain(nh: Harness) -> None:
    await loaded(nh)
    nh.turns.prompt("p2", text="explain the load cell")
    nh.turns.prompt("p2", text="and the region column too")  # no mode: the turn's stays
    assert_e109(await nh.call("nh_add_cell", "p2", **DROP), "written")


async def test_e109_comes_before_e108(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the sales data")
    nh.turns.workflow_launched("p1")
    nh.turns.prompt("p1", text="explain what the workflow does")  # absorbed
    assert_e109(await nh.call("nh_add_cell", "p1", **LOAD), "written")


async def test_plan_permission_mode_comes_before_e109(nh: Harness) -> None:
    """Gate order (design §6.2): E104 before E109, so plan mode keeps its own answer."""
    await loaded(nh)
    nh.turns.prompt("p2", text="explain the load cell")
    nh.turns.stamp("nh_add_cell", DROP, "p2", permission_mode="plan")
    result = await nh.client.call_tool("nh_add_cell", DROP, raise_on_error=False)
    body = text(result)
    assert result.is_error and "nh: E104" in body and "nh: E109" not in body, body
    assert len(code_cells(nh)) == 1


async def test_a_reported_writer_run_gets_e107_before_e109(nh: Harness) -> None:
    """The writer's checks (design §6.2): E107 for a run already reported comes before the
    writer's E109, even when an explain message was absorbed into its turn."""
    nh.turns.prompt("p1", text="load the sales data")
    nh.turns.workflow_launched("p1")
    nh.turns.prompt("p1", text="explain what it is doing")  # absorbed into p1
    nh.turns.notification("note-1")  # the run's completion: reported
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    body = text(result)
    assert result.is_error and "nh: E107" in body and "nh: E109" not in body, body
    assert not code_cells(nh)


async def test_the_writer_in_an_explain_message_may_only_wait(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the sales data")
    nh.turns.workflow_launched("p1")
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not added.is_error, text(added)
    uid = code_cells(nh)[-1]["id"]
    nh.turns.prompt("p1", text="explain it before you go on")  # absorbed into p1
    edit = {"cell_id": uid, "code": LOAD["code"] + "\ndf.dtypes"}
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_edit_cell", edit, "p1")
    body = text(result)
    assert result.is_error and body.splitlines()[:3] == [
        FIRST.format(verb="written"),
        "nh: E109",
        f"Next: {RETURN_TO_WORKFLOW}",
    ], body
    assert WRITER_LINE not in body
    wait = {"cell_id": uid, "mode": "wait"}
    waited = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_run", wait, "p1")
    assert_allowed(waited, "nh: wait cell=")
    assert len(code_cells(nh)) == 1


async def test_a_writer_launched_in_an_explain_message_writes_nothing(nh: Harness) -> None:
    nh.turns.prompt("p1", text="explain the sales data")
    nh.turns.workflow_launched("p1")  # the launch guard is advisory; the gateway enforces
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert result.is_error and "nh: E109" in text(result)
    assert f"Next: {RETURN_TO_WORKFLOW}" in text(result)
    assert not code_cells(nh)


async def test_with_no_turn_record_writes_are_allowed(nh: Harness) -> None:
    # The /nh:init message: its hook ran before .nh/ existed, so there is no record or mode.
    # A failing cell, so its re-run is a retry rather than E112 (it already ran OK).
    assert_allowed(
        await nh.call("nh_add_cell", "p1", **dict(LOAD, code="undefined_name")), "nh: cell="
    )
    uid = code_cells(nh)[-1]["id"]
    assert_allowed(await nh.call("nh_run", "p1", cell_id=uid), "nh: cell=")
    assert_allowed(await nh.call("nh_edit_cell", "p1", cell_id=uid, code=LOAD["code"]), "nh: cell=")
    assert_allowed(await nh.call("nh_undo", "p1"), "nh: cell=")
    assert not code_cells(nh)


# The rules C2 changed (design §6.2); rule 10 and the other lines are pinned where they live.
V02_RULES = [
    "1. One code cell per user message: nh_add_cell (new) or nh_edit_cell (existing). If its run "
    "fails, fix that same cell with nh_edit_cell (at most 2 times), then stop and explain. Only "
    "exception: a batch or re-run list the user approved when nh asked.",
    "5. Readable, simple code: one idea per line, named intermediates, plain pandas, UPPER_CASE "
    "constants for judgment calls, no functions until reused, end with a visible check.",
    "7. RUNNING or QUEUED cell: tell the user, add nothing. Interrupted: ask before re-running or "
    "changing it. Deleted by the user: a no. Lost (kernel gone) or not run (the user typed into "
    "it first): tell the user and ask.",
    "9. Never Read, Write, Edit, NotebookEdit or Bash-modify .ipynb files, or print env vars or "
    "credentials. Ask before installing packages or writing outside the project.",
]


def test_the_instructions_carry_the_v02_rules() -> None:
    lines = INSTRUCTIONS.splitlines()
    for rule in V02_RULES:
        assert rule in lines, rule
    assert [line.split(".")[0] for line in lines[1:-1]] == [str(n) for n in range(1, 11)]
    assert len(INSTRUCTIONS) <= 2048
