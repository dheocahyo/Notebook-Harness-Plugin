"""nh v0.1.1 cell QA: background-task aliasing, run binding and the writer's rules, through the
real hooks and the gateway (FakeBackend)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway._shared import turn_record
from nh_gateway._shared.paths import Layout, atomic_write_json
from nh_gateway._shared.stamp_spec import stamp_filename, stamp_key
from nh_gateway.app import INSTRUCTIONS, create_server
from nh_gateway.history import HistoryStore
from nh_gateway.policy import stamps
from nh_gateway.policy.errors import RETURN_TO_WORKFLOW, WRITER_LINE, NhError
from tests.fakes.turns import text
from tests.gateway.conftest import NOTEBOOK, Harness
from tests.unit.test_qa_cell_workflow import finding, needs_node, qa, run_script

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


def nh_code_cells(h: Harness) -> list[dict]:
    return [
        c
        for c in h.cells()
        if c["cell_type"] == "code" and c.get("metadata", {}).get("nh", {}).get("role") == "code"
    ]


def raw_stamp(
    project: Path,
    tool: str,
    args: dict,
    prompt_id: str,
    *,
    agent_id: str | None,
    tool_use_id: str,
    session_id: str = "sess-1",
    ts: float | None = None,
    agent_type: str | None = None,
) -> Path:
    """A stamp file as pre_tool writes it, for callers the real hook doesn't stamp yet."""
    key = stamp_key(tool, args)
    path = Layout(project).stamps / stamp_filename(key, tool_use_id)
    atomic_write_json(
        path,
        {
            "v": 1,
            "key": key,
            "tool": tool,
            "session_id": session_id,
            "prompt_id": prompt_id,
            "agent_id": agent_id,
            "agent_type": agent_type or (turn_record.WRITER_AGENT if agent_id else None),
            "tool_use_id": tool_use_id,
            "permission_mode": "default",
            "cc_pid": None,
            "ts": time.time() if ts is None else ts,
        },
    )
    return path


# --- background task notifications share the human message's budget -----------------------


async def test_a_notification_shares_the_human_turns_budget(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.notification("note-1")
    added = await nh.call("nh_add_cell", "note-1", **LOAD)
    assert not added.is_error, text(added)
    [cell] = nh_code_cells(nh)
    assert cell["metadata"]["nh"]["turn_id"] == "p1"  # the canonical turn, not the alias
    second = await nh.call("nh_add_cell", "p1", **DROP)
    assert second.is_error and "nh: E110" in text(second)
    nh.turns.notification("note-2", task_id="bash-7", tool_use_id="toolu_bash")
    third = await nh.call("nh_add_cell", "note-2", **DROP)
    assert third.is_error and "nh: E110" in text(third)
    assert len(nh_code_cells(nh)) == 1


async def test_more_than_twenty_notifications_still_share_it(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    for i in range(21):
        nh.turns.notification(f"note-{i}", task_id=f"task-{i}")
    for prompt_id in ("note-0", "note-20"):
        result = await nh.call("nh_add_cell", prompt_id, **DROP)
        assert result.is_error and "nh: E110" in text(result), text(result)


async def test_a_stamp_from_before_the_notification_still_counts(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    nh.turns.notification("note-1")
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert not result.is_error, text(result)


async def test_the_next_human_message_gets_its_own_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.notification("note-1")
    assert not (await nh.call("nh_add_cell", "note-1", **LOAD)).is_error
    late = dict(DROP, title="Late write")
    nh.turns.stamp("nh_add_cell", late, "note-1")  # called before the user wrote again
    nh.turns.prompt("p2")
    assert not (await nh.call("nh_add_cell", "p2", **DROP)).is_error
    stale = await nh.client.call_tool("nh_add_cell", late, raise_on_error=False)
    assert stale.is_error and "nh: E102" in text(stale)
    # Called after it, the earlier notification's prompt still has only its message's cell.
    again = await nh.call("nh_add_cell", "note-1", **late)
    assert again.is_error and "nh: E110" in text(again)
    assert "Load sales data" in text(again)
    assert len(nh_code_cells(nh)) == 2


async def test_an_orphan_notification_cannot_write(nh: Harness) -> None:
    nh.turns.notification("note-1")
    result = await nh.call("nh_add_cell", "note-1", **LOAD)
    body = text(result)
    assert result.is_error and "nh: E102" in body
    assert "A background task finished; no user message is open." in body
    assert not nh_code_cells(nh)


async def test_a_notification_after_clear_is_an_orphan(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.notification("note-1", session_id="sess-2")  # /clear starts a new session
    result = await nh.turns.call(nh.client, "nh_add_cell", LOAD, "note-1", session_id="sess-2")
    assert result.is_error and "nh: E102" in text(result)
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error


# --- identical calls from different agents --------------------------------------------------


async def test_an_earlier_messages_leftover_stamp_does_not_trip_the_writer(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")  # the user rejected this call at its prompt
    nh.turns.prompt("p2")
    nh.turns.workflow_launched("p2", RUN)
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p2")
    assert not added.is_error, text(added)
    assert [c["metadata"]["nh"]["turn_id"] for c in nh_code_cells(nh)] == ["p2"]
    assert not list(Layout(nh.project).stamps.glob("*.json"))


async def test_identical_stamps_from_different_agents_are_retried(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    raw_stamp(nh.project, "nh_add_cell", LOAD, "p1", agent_id="a-writer", tool_use_id="toolu_w")
    for _ in range(2):  # this call and its twin
        result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
        body = text(result)
        assert result.is_error and "nh: E101" in body, body
        assert "Two identical calls arrived together" in body
        assert "Next: Retry this call once." in body
    assert not list(Layout(nh.project).stamps.glob("*.json"))
    unstamped = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert "nh: E101" in text(unstamped) and "Retry this call once" not in text(unstamped)
    retried = await nh.call("nh_add_cell", "p1", **LOAD)
    assert not retried.is_error, text(retried)
    assert len(nh_code_cells(nh)) == 1


def test_claim_sets_ambiguous_stamps_aside(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    layout = Layout(project)
    atomic_write_json(layout.turn_file("sess-1"), turn_record.opened("sess-1", "p1", 1.0))
    args = {"mode": "wait", "cell_id": "nh-1"}

    def claim(now: float | None = None):
        return stamps.claim(layout, "nh_run", args, my_cc_pid=None, ttl=1800, now=now)

    # Parallel identical calls from the main conversation keep their own stamps.
    for use in ("toolu_1", "toolu_2"):
        raw_stamp(project, "nh_run", args, "p1", agent_id=None, tool_use_id=use)
    first, second = claim(), claim()
    assert isinstance(first, stamps.Stamp) and isinstance(second, stamps.Stamp)
    assert {first.tool_use_id, second.tool_use_id} == {"toolu_1", "toolu_2"}
    assert claim() is None

    # Identical calls of one turn from two agents are ambiguous: one retry clears it.
    raw_stamp(project, "nh_run", args, "p1", agent_id="a-w", tool_use_id="toolu_w")
    raw_stamp(project, "nh_run", args, "p1", agent_id=None, tool_use_id="toolu_3")
    now = time.time()
    assert claim(now) is stamps.AMBIGUOUS
    assert claim(now + 1) is stamps.AMBIGUOUS  # the twin's share
    assert claim(now + 2) is None
    raw_stamp(project, "nh_run", args, "p1", agent_id=None, tool_use_id="toolu_4")
    retried = claim()
    assert isinstance(retried, stamps.Stamp) and retried.tool_use_id == "toolu_4"

    # A leftover of an earlier message (a call rejected at its permission prompt) is no
    # twin: it is purged. Nor is a stamp of this turn from over a minute before.
    stale = raw_stamp(
        project, "nh_run", args, "p0", agent_id="a-old", tool_use_id="toolu_old", ts=now - 5
    )
    before = time.time() - stamps.AMBIGUOUS_WINDOW_S - 5
    raw_stamp(project, "nh_run", args, "p1", agent_id="a-w", tool_use_id="toolu_early", ts=before)
    raw_stamp(project, "nh_run", args, "p1", agent_id=None, tool_use_id="toolu_7")
    claimed = claim()
    assert isinstance(claimed, stamps.Stamp) and claimed.tool_use_id == "toolu_7"
    assert not stale.exists()
    claimed = claim()
    assert isinstance(claimed, stamps.Stamp) and claimed.tool_use_id == "toolu_early"
    assert claim() is None

    # A set-aside share only answers for a minute.
    raw_stamp(project, "nh_run", args, "p1", agent_id=None, tool_use_id="toolu_5")
    raw_stamp(project, "nh_run", args, "p1", agent_id="a-w", tool_use_id="toolu_6")
    now = time.time()
    assert claim(now) is stamps.AMBIGUOUS
    assert claim(now + stamps.AMBIGUOUS_WINDOW_S + 1) is None
    assert not list(layout.ambiguous.glob("*.json"))


def test_claim_prefers_stamps_of_the_current_turn_through_aliases(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    layout = Layout(project)
    record = turn_record.aliased(turn_record.opened("sess-1", "p1", 1.0), "note-1", 2.0)
    atomic_write_json(layout.turn_file("sess-1"), record)
    args = {"mode": "wait", "cell_id": "nh-1"}
    old = raw_stamp(
        project, "nh_run", args, "p0", agent_id=None, tool_use_id="t_old", ts=time.time()
    )
    raw_stamp(
        project, "nh_run", args, "note-1", agent_id=None, tool_use_id="t_alias", ts=time.time() - 5
    )
    raw_stamp(
        project, "nh_run", args, "p1", agent_id=None, tool_use_id="t_turn", ts=time.time() - 9
    )
    claimed = stamps.claim(layout, "nh_run", args, my_cc_pid=None, ttl=1800)
    assert isinstance(claimed, stamps.Stamp) and claimed.tool_use_id == "t_alias"
    assert old.exists()  # newer than the claimed stamp: kept
    claimed = stamps.claim(layout, "nh_run", args, my_cc_pid=None, ttl=1800)
    assert isinstance(claimed, stamps.Stamp) and claimed.tool_use_id == "t_turn"
    assert json.loads(old.read_text())["prompt_id"] == "p0"


def test_claim_purges_older_stamps_of_an_earlier_turn_only(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    layout = Layout(project)
    record = turn_record.aliased(turn_record.opened("sess-1", "p2", 1.0), "note-1", 2.0)
    atomic_write_json(layout.turn_file("sess-1"), record)
    args = {"mode": "wait", "cell_id": "nh-1"}
    now = time.time()
    older_turn = raw_stamp(
        project, "nh_run", args, "p1", agent_id=None, tool_use_id="t1", ts=now - 9
    )
    same_turn = raw_stamp(
        project, "nh_run", args, "p2", agent_id=None, tool_use_id="t2", ts=now - 5
    )
    raw_stamp(project, "nh_run", args, "note-1", agent_id=None, tool_use_id="t3", ts=now)
    claimed = stamps.claim(layout, "nh_run", args, my_cc_pid=None, ttl=1800)
    assert isinstance(claimed, stamps.Stamp) and claimed.tool_use_id == "t3"
    assert not older_turn.exists()  # an earlier message's leftover
    assert same_turn.exists()  # the alias's own turn: a parallel identical call


# --- the cell writer: bound to its run, and through the run to one human message -----------

RUN = "wf_run-1"
NEXT_RETURN = f"Next: {RETURN_TO_WORKFLOW}"


@pytest.fixture
def quick_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbound writer is refused after 0.2 s instead of the 2 s PostToolUse grace."""
    monkeypatch.setattr(turn_record, "META_WAIT_S", 0.2)


def code_uid(h: Harness) -> str:
    return nh_code_cells(h)[-1]["id"]


async def test_the_writer_writes_the_messages_one_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not added.is_error, text(added)
    [cell] = nh_code_cells(nh)
    assert cell["metadata"]["nh"]["turn_id"] == "p1"
    # One cell per human message, whoever writes it; the refusal goes back to the workflow.
    second = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", DROP, "p1")
    body = text(second)
    assert second.is_error and "nh: E110" in body
    assert body.splitlines()[-1] == NEXT_RETURN and "Writer:" not in body
    # The report arrives: the main conversation may write again, and shares the used budget.
    nh.turns.notification("note-1")
    main = await nh.call("nh_add_cell", "note-1", **DROP)
    assert main.is_error and "nh: E110" in text(main) and "Writer:" not in text(main)
    assert len(nh_code_cells(nh)) == 1


async def test_a_cell_the_main_conversation_wrote_is_the_writers_budget_too(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    nh.turns.workflow_launched("p1")
    late = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", DROP, "p1")
    assert late.is_error and "nh: E110" in text(late)
    assert len(nh_code_cells(nh)) == 1


async def test_a_failed_cell_sends_the_writer_back_to_the_workflow_not_to_a_new_cell(
    nh: Harness,
) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    broken = dict(LOAD, code="never = 1 / 0")
    failed = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", broken, "p1")
    assert not failed.is_error and "Fix it with nh_edit_cell" in text(failed)
    # A second add: the main conversation is pointed at the fix; the writer only returns.
    second = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", DROP, "p1")
    body = text(second)
    assert second.is_error and "nh: E110" in body
    assert body.splitlines()[-1] == NEXT_RETURN and "Fix it with" not in body


async def test_other_writer_refusals_end_with_the_writer_line(nh: Harness) -> None:
    nh.turns.prompt("p0")
    assert not (await nh.call("nh_add_cell", "p0", **LOAD)).is_error
    earlier = code_uid(nh)
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    assert not (
        await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", DROP, "p1")
    ).is_error
    other = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(earlier, "\ndf.dtypes"), "p1"
    )
    lines = text(other).splitlines()
    assert other.is_error and "nh: E113" in lines
    assert lines[-2].startswith("Next: Propose the change") and lines[-1] == WRITER_LINE


async def test_a_lint_rejection_is_the_writers_to_fix(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    bad = dict(LOAD, notes=[f"point {i}" for i in range(6)])
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", bad, "p1")
    assert result.is_error and "nh: E120" in text(result) and "Writer:" not in text(result)
    fixed = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not fixed.is_error, text(fixed)


async def test_the_writer_resolves_a_run_recorded_while_it_waits(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.writer_stamp("w-1", RUN, "nh_add_cell", LOAD, "p1")  # before PostToolUse ran
    call = asyncio.create_task(nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False))
    await asyncio.sleep(0.3)
    assert not call.done()  # still re-reading the runs file
    nh.turns.workflow_launched("p1")
    result = await call
    assert not result.is_error, text(result)
    assert len(nh_code_cells(nh)) == 1


async def test_a_writer_with_no_recorded_run_is_refused(nh: Harness, quick_binding: None) -> None:
    nh.turns.prompt("p1")
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    body = text(result)
    assert result.is_error and "nh: E103" in body
    assert "not inside nh's qa-cell workflow" in body and NEXT_RETURN in body
    assert not nh_code_cells(nh)


async def test_a_writer_listed_by_two_runs_is_refused(nh: Harness, quick_binding: None) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1", RUN, tool_use_id="toolu_1")
    nh.turns.workflow_launched("p1", "wf_run-2", tool_use_id="toolu_2")
    nh.turns.writer_meta("w-1", "wf_run-2")
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert result.is_error and "not inside nh's qa-cell workflow" in text(result)
    assert not nh_code_cells(nh)


async def test_a_meta_file_of_another_agent_type_binds_nothing(
    nh: Harness, quick_binding: None
) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    nh.turns.writer_meta("w-1", RUN, agent_type="nh:cell-qa")
    nh.turns.stamp("nh_add_cell", LOAD, "p1", agent_id="w-1", agent_type=turn_record.WRITER_AGENT)
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert result.is_error and "not inside nh's qa-cell workflow" in text(result)


@pytest.mark.parametrize("launched_by", ["script", "scriptPath"])
async def test_a_run_of_another_script_may_not_write(nh: Harness, launched_by: str) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1", launched_by=launched_by)
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    body = text(result)
    assert result.is_error and "nh: E103" in body
    assert "this run's script is not nh's" in body and NEXT_RETURN in body
    # It doesn't hold the main conversation either.
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error


@pytest.mark.parametrize("agent_type", ["general-purpose", turn_record.QA_AGENT])
async def test_other_subagents_are_read_only(nh: Harness, agent_type: str) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    nh.turns.writer_meta("a-1", RUN, agent_type=agent_type)
    raw_stamp(
        nh.project,
        "nh_add_cell",
        LOAD,
        "p1",
        agent_id="a-1",
        agent_type=agent_type,
        tool_use_id="t1",
    )  # the hook denies them; a stamp that got through anyway
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    body = text(result)
    assert result.is_error and "nh: E103" in body
    assert body.splitlines()[0].startswith("Only the main conversation, or nh's cell writer")
    assert "Next: Return your findings to the main agent; it writes the cell." in body
    assert not nh_code_cells(nh)


async def test_the_writer_only_waits_and_never_undoes(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    assert not (
        await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    ).is_error
    uid = code_uid(nh)
    for mode in ("run", "interrupt"):
        args = {"cell_id": uid, "mode": mode}
        result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_run", args, "p1")
        body = text(result)
        assert (
            result.is_error
            and "nh: E103" in body
            and 'only wait for its cell (mode="wait")' in body
        )
        assert NEXT_RETURN in body
    rerun = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_run", {"cell_id": uid}, "p1")
    assert rerun.is_error and "nh: E103" in text(rerun)  # mode defaults to run
    waited = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_run", {"cell_id": uid, "mode": "wait"}, "p1"
    )
    assert "nh: E103" not in text(waited)
    assert "return your last result for it to the workflow" in text(waited)
    assert "wait for the user" not in text(waited)
    raw_stamp(nh.project, "nh_undo", {}, "p1", agent_id="w-1", tool_use_id="t_undo")
    undo = await nh.client.call_tool("nh_undo", {}, raise_on_error=False)  # the hook denies it too
    assert undo.is_error and "nh: E103" in text(undo) and "can't undo" in text(undo)
    assert len(nh_code_cells(nh)) == 1


@pytest.mark.parametrize("writer_prompt", ["p1", "p2"])
async def test_the_writer_is_refused_once_the_user_writes_again(
    nh: Harness, writer_prompt: str
) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    nh.turns.prompt("p2")
    # Whichever prompt id the writer's calls carry, its run belongs to p1.
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, writer_prompt)
    body = text(result)
    assert result.is_error and "nh: E107" in body
    assert body.splitlines()[0] == (
        "Not written: this nh:qa-cell run belongs to an earlier user message, already "
        "reported, or started over an hour ago."
    )
    assert NEXT_RETURN in body
    assert not nh_code_cells(nh)


async def test_the_writer_is_refused_after_its_run_reported(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    nh.turns.notification("note-1")  # the run's completion
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert result.is_error and "nh: E107" in text(result)
    waited = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_run", {"cell_id": "nh-x", "mode": "wait"}, "p1"
    )
    assert "nh: E107" in text(waited) and text(waited).startswith("Not waited:")


async def test_a_run_older_than_an_hour_no_longer_writes(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    layout = Layout(nh.project)
    [run] = turn_record.find_runs(layout, "sess-1")
    turn_record.record_run(layout, "sess-1", dict(run, ts=time.time() - 3700))
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert result.is_error and "nh: E107" in text(result)
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error  # nor holds the main one


async def test_a_run_launched_with_no_open_message_never_writes(nh: Harness) -> None:
    nh.turns.notification("note-0", task_id="bash-1", tool_use_id="toolu_bash")  # an orphan
    nh.turns.workflow_launched("note-0")
    [run] = turn_record.find_runs(Layout(nh.project), "sess-1")
    assert run["turn_id"] is None
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "note-0")
    assert result.is_error and "nh: E107" in text(result)


async def test_a_late_writer_of_an_earlier_run_cannot_take_the_next_message(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1", RUN, tool_use_id="toolu_r1")
    first = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not first.is_error, text(first)
    uid = code_uid(nh)
    # QA1 asked for a revision; meanwhile the user wrote T2, which launched R2.
    nh.turns.prompt("p2")
    nh.turns.workflow_launched("p2", "wf_run-2", tool_use_id="toolu_r2")
    revision = dict(
        cell_id=uid, code=LOAD["code"] + "\ndf.dtypes", title=LOAD["title"], notes=LOAD["notes"]
    )
    late = await nh.turns.writer_call(nh.client, "w-2", RUN, "nh_edit_cell", revision, "p2")
    assert late.is_error and "nh: E107" in text(late), text(late)
    assert "df.dtypes" not in nh_code_cells(nh)[0]["source"]
    own = await nh.turns.writer_call(nh.client, "w-3", "wf_run-2", "nh_add_cell", DROP, "p2")
    assert not own.is_error, text(own)
    assert [c["metadata"]["nh"]["turn_id"] for c in nh_code_cells(nh)] == ["p1", "p2"]


async def test_the_main_conversation_waits_while_the_workflow_writes(nh: Harness) -> None:
    nh.turns.prompt("p0")
    assert not (await nh.call("nh_add_cell", "p0", **LOAD)).is_error
    uid = code_uid(nh)
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    edit = dict(cell_id=uid, code=LOAD["code"] + "\ndf.dtypes")
    for tool, args, verb in (
        ("nh_add_cell", DROP, "written"),
        ("nh_edit_cell", edit, "written"),
        ("nh_run", {"cell_id": uid}, "run"),
        ("nh_run", {"cell_id": uid, "mode": "run"}, "run"),
        ("nh_undo", {}, "undone"),
    ):
        result = await nh.call(tool, "p1", **args)
        body = text(result)
        assert result.is_error and "nh: E108" in body, (tool, body)
        assert body.splitlines()[0] == (
            f"Not {verb}: the nh:qa-cell workflow is writing this message's cell."
        )
        assert "reply when its report arrives. To change course, stop it first." in body
        assert "Writer:" not in body
    for mode in ("wait", "interrupt"):
        result = await nh.call("nh_run", "p1", cell_id=uid, mode=mode)
        assert "nh: E108" not in text(result), text(result)
    assert len(nh_code_cells(nh)) == 1
    # Finished, the run reports; this message's cell is unused, so the main conversation
    # writes it.
    nh.turns.notification("note-1")
    assert not (await nh.call("nh_add_cell", "note-1", **DROP)).is_error


async def test_stopping_the_run_lets_the_main_conversation_write(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1", RUN)
    held = await nh.call("nh_add_cell", "p1", **LOAD)
    assert held.is_error and "To change course, stop it first." in text(held)
    # A stopped workflow sends no report: the TaskStop hook marks its run done.
    nh.turns.task_stopped("p1", f"task-{RUN}")
    added = await nh.call("nh_add_cell", "p1", **LOAD)
    assert not added.is_error, text(added)
    late = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", DROP, "p1")
    assert late.is_error and "nh: E107" in text(late), text(late)
    assert [c["metadata"]["nh"]["turn_id"] for c in nh_code_cells(nh)] == ["p1"]


async def test_an_open_run_holds_only_its_own_message(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    nh.turns.prompt("p2")
    assert not (await nh.call("nh_add_cell", "p2", **LOAD)).is_error


async def test_plan_mode_sends_the_writer_back_to_the_workflow(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    result = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1", permission_mode="plan"
    )
    assert result.is_error and "nh: E104" in text(result) and NEXT_RETURN in text(result)


def test_rule_ten_starts_the_workflow_under_ultracode() -> None:
    assert (
        "10. When a reminder says ultracode is on, or on /nh:qa-cell, launch the nh:qa-cell "
        "workflow instead of writing; write nothing until its report arrives.\n"
    ) in INSTRUCTIONS
    assert len(INSTRUCTIONS) <= 2048


def test_old_runs_files_are_trimmed(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "proj")
    now = time.time()
    for session, age in (("old", stamps.RUNS_KEEP_S + 60), ("recent", 3600.0)):
        turn_record.record_run(layout, session, {"run_id": f"wf_{session}", "ts": now - age})
        os.utime(layout.workflow_file(session), (now - age, now - age))
    assert stamps.gc_runs(layout, now=now) == 1
    assert not layout.workflow_file("old").exists()
    assert turn_record.find_runs(layout, "recent")
    assert stamps.gc_runs(Layout(tmp_path / "empty"), now=now) == 0


# --- the writer's revisions: its result, its rules, its budget ------------------------------

# qa-cell.js reads the budget off the machine line with this pattern.
REVISIONS = re.compile(r"^nh: cell=[^\n]*?\brevisions=(\d+)/(\d+)", re.MULTILINE)
V01_OK_NEXT = (
    "Reply to the user about {cell}: (1) what the cell does; (2) why this approach; "
    "(3) judgment calls they may want to change; (4) the real numbers from the output, "
    "surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, "
    'as a title they can approve with "go". Name cells by title and [n]. '
    "Do not write a second cell."
)
V01_MACHINE = re.compile(r"nh: cell=nh-\w+ exec=\d+ turn=1/1 retries=0/2 waits=0/2 undos=0/3")


def revise(uid: str, extra: str) -> dict:
    return dict(cell_id=uid, code=LOAD["code"] + extra)


def next_section(body: str) -> str:
    return body.split("--- next ---\n", 1)[1].strip()


def cell_events(h: Harness) -> list[dict]:
    lines = Layout(h.project).log_file.read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["event"] in ("cell_added", "cell_edited")]


async def writer_adds(h: Harness, prompt_id: str = "p1") -> str:
    h.turns.prompt(prompt_id)
    h.turns.workflow_launched(prompt_id)
    added = await h.turns.writer_call(h.client, "w-1", RUN, "nh_add_cell", LOAD, prompt_id)
    assert not added.is_error, text(added)
    return code_uid(h)


async def test_the_writers_result_goes_back_to_the_workflow(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    body = text(added)
    assert not added.is_error, body
    assert body.splitlines()[1].endswith(" revisions=0/2")
    assert REVISIONS.search(body).groups() == ("0", "2")
    assert next_section(body).startswith('"Load sales data" [1] ran OK. Return this whole result')
    assert "Reply to the user" not in body
    [cell] = nh_code_cells(nh)
    assert cell["metadata"]["nh"]["agent"] == turn_record.WRITER_AGENT
    [event] = cell_events(nh)
    assert (event["agent"], event["revision"]) == (turn_record.WRITER_AGENT, None)


async def test_main_conversation_results_keep_the_v01_rendering(nh: Harness) -> None:
    nh.turns.prompt("p1")
    added = await nh.call("nh_add_cell", "p1", **LOAD)
    body = text(added)
    assert not added.is_error, body
    assert V01_MACHINE.fullmatch(body.splitlines()[1]), body.splitlines()[1]
    assert "revisions=" not in body and "workflow" not in body
    assert next_section(body) == V01_OK_NEXT.format(cell='"Load sales data" [1]')
    assert "agent" not in nh_code_cells(nh)[0]["metadata"]["nh"]
    [event] = cell_events(nh)
    assert (event["agent"], event["revision"]) == (None, None)
    # An OK cell stays the main conversation's E112, word for word.
    edit = await nh.call("nh_edit_cell", "p1", **revise(code_uid(nh), "\ndf.dtypes"))
    assert edit.is_error
    assert text(edit) == (
        'Not written (by design): "Load sales data" [1] already ran OK. Changes wait for the '
        "user's next message.\nnh: E112\nNext: Report the result and wait."
    )


async def test_the_writer_revises_its_ok_cell_up_to_max_revisions(nh: Harness) -> None:
    uid = await writer_adds(nh)
    for n, extra in ((1, "\ndf.dtypes"), (2, "\ndf.head()")):
        result = await nh.turns.writer_call(
            nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, extra), "p1"
        )
        body = text(result)
        assert not result.is_error, body
        assert f" (revision {n} of 2)" in body.splitlines()[0]
        assert REVISIONS.search(body).groups() == (str(n), "2")
        assert "retries=0/2" in body  # revisions don't use the retries
        assert "Reply to the user" not in body
        nh_meta = nh_code_cells(nh)[0]["metadata"]["nh"]
        assert nh_meta["revision"] == {"turn": "p1", "n": n}
        assert nh_meta["agent"] == turn_record.WRITER_AGENT
    last = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.tail()"), "p1"
    )
    body = text(last)
    assert last.is_error and "nh: E112" in body
    assert "- No revisions left (2 of 2 used; [turn] max_revisions)." in body
    assert body.splitlines()[-1] == NEXT_RETURN  # no Writer: line after it
    assert "df.tail()" not in nh_code_cells(nh)[0]["source"]
    events = cell_events(nh)
    assert [(e["event"], e["revision"]) for e in events] == [
        ("cell_added", None),
        ("cell_edited", 1),
        ("cell_edited", 2),
    ]
    assert {e["agent"] for e in events} == {turn_record.WRITER_AGENT}


async def test_a_failed_revision_uses_the_retries(nh: Harness) -> None:
    uid = await writer_adds(nh)
    broken = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\nboom()"), "p1"
    )
    body = text(broken)
    assert not broken.is_error and REVISIONS.search(body).groups() == ("1", "2")
    assert "Fix it with nh_edit_cell on the same cell (2 retries left" in next_section(body)
    fixed = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.dtypes"), "p1"
    )
    body = text(fixed)
    assert not fixed.is_error and " (retry 1 of 2)" in body.splitlines()[0]
    assert "retries=1/2" in body and REVISIONS.search(body).groups() == ("1", "2")


async def test_a_revision_that_cannot_start_leaves_the_budget_and_the_cell(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = await writer_adds(nh)
    before = dict(nh_code_cells(nh)[0]["metadata"]["nh"])
    start = nh.backend.start_execution

    async def busy(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(nh.backend, "start_execution", start)
        raise NhError("E133", detail=" (a test kernel is busy)")

    monkeypatch.setattr(nh.backend, "start_execution", busy)
    failed = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.dtypes"), "p1"
    )
    assert failed.is_error and "nh: E133" in text(failed)
    [cell] = nh_code_cells(nh)
    assert cell["source"] == LOAD["code"] and cell["metadata"]["nh"] == before
    # Still an OK cell with both revisions left.
    again = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.dtypes"), "p1"
    )
    assert not again.is_error, text(again)
    assert REVISIONS.search(text(again)).groups() == ("1", "2")


async def test_the_writer_ignores_base_sha_and_leaves_a_user_change(nh: Harness) -> None:
    nh.turns.prompt("p0")
    assert not (await nh.call("nh_add_cell", "p0", **LOAD)).is_error
    uid = code_uid(nh)
    nh.backend.user_edit(NOTEBOOK, uid, "df = 'mine'")
    view = text(await nh.call("nh_inspect", "p1", view="cell", cell_id=uid))
    sha = next(line for line in view.splitlines() if "sha=" in line).split("sha=")[1].split()[0]
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    edit = dict(revise(uid, "\ndf.dtypes"), base_sha=sha)
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_edit_cell", edit, "p1")
    body = text(result)
    assert result.is_error and "nh: E141" in body, body
    assert body.splitlines()[-1] == NEXT_RETURN
    assert nh_code_cells(nh)[0]["source"] == "df = 'mine'"
    # Once the run reports, the main conversation may, with the user's go-ahead (base_sha).
    nh.turns.notification("note-1")
    assert not (await nh.call("nh_edit_cell", "note-1", **edit)).is_error


async def test_the_writer_never_changes_a_users_cell(nh: Harness) -> None:
    nh.turns.prompt("p0")
    assert not (await nh.call("nh_add_cell", "p0", **LOAD)).is_error
    mine = nh.backend.user_insert(NOTEBOOK, len(nh.cells()), "df.describe()")
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    edit = dict(cell_id=mine, code="df.describe(include='all')", intent="describe every column")
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_edit_cell", edit, "p1")
    body = text(result)
    assert result.is_error and "nh: E144" in body, body
    assert "- nh:cell-writer never changes a cell the user wrote." in body
    assert body.splitlines()[-1] == NEXT_RETURN
    assert nh.cells()[-1]["source"] == "df.describe()"


async def test_revisions_survive_a_gateway_restart(nh: Harness) -> None:
    uid = await writer_adds(nh)
    first = await nh.turns.writer_call(
        nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.dtypes"), "p1"
    )
    assert not first.is_error, text(first)
    Layout(nh.project).ledger_file("sess-1").unlink()
    async with Client(create_server(nh.project, nh.backend)) as client:
        second = await nh.turns.writer_call(
            client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.head()"), "p1"
        )
        assert not second.is_error, text(second)
        assert REVISIONS.search(text(second)).groups() == ("2", "2")
        third = await nh.turns.writer_call(
            client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.tail()"), "p1"
        )
        assert third.is_error and "- No revisions left (2 of 2 used" in text(third)


async def test_the_writer_has_nothing_to_revise_without_an_ok_or_failed_run(nh: Harness) -> None:
    uid = await writer_adds(nh)
    ledger = Layout(nh.project).ledger_file("sess-1")
    saved = json.loads(ledger.read_text())
    saved["turns"]["p1"]["status"][uid] = "aborted"
    ledger.write_text(json.dumps(saved))
    async with Client(create_server(nh.project, nh.backend)) as client:
        result = await nh.turns.writer_call(
            client, "w-1", RUN, "nh_edit_cell", revise(uid, "\ndf.dtypes"), "p1"
        )
    body = text(result)
    assert result.is_error and "nh: E112" in body
    assert body.splitlines()[0] == (
        'Not written: "Load sales data" [1] has no OK run to revise and no failed run to retry.'
    )
    assert "- nh's status for it: aborted." in body and body.splitlines()[-1] == NEXT_RETURN


async def test_one_undo_takes_back_the_writers_cell_and_its_revisions(nh: Harness) -> None:
    uid = await writer_adds(nh)
    for extra in ("\ndf.dtypes", "\ndf.head()"):
        revised = await nh.turns.writer_call(
            nh.client, "w-1", RUN, "nh_edit_cell", revise(uid, extra), "p1"
        )
        assert not revised.is_error, text(revised)
    assert len(HistoryStore(Layout(nh.project)).ops_for(uid)) == 1
    nh.turns.notification("note-1")
    nh.turns.prompt("p2")
    undo = await nh.call("nh_undo", "p2")
    assert not undo.is_error and '\nRemoved "Load sales data"' in text(undo), text(undo)
    assert not nh_code_cells(nh)


async def test_a_second_run_of_the_message_may_not_touch_its_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1", RUN, tool_use_id="toolu_r1")
    nh.turns.workflow_launched("p1", "wf_run-2", tool_use_id="toolu_r2")
    first = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not first.is_error, text(first)
    uid = code_uid(nh)
    for tool, args in (("nh_edit_cell", revise(uid, "\ndf.dtypes")), ("nh_add_cell", DROP)):
        result = await nh.turns.writer_call(nh.client, "w-2", "wf_run-2", tool, args, "p1")
        body = text(result)
        assert result.is_error and "nh: E110" in body, (tool, body)
        assert "- Another nh:qa-cell run owns this message's cell." in body
        assert body.splitlines()[-1] == NEXT_RETURN
    [cell] = nh_code_cells(nh)
    assert cell["source"] == LOAD["code"]


# --- needs_approval: the writer's question reaches the main conversation (design §6.4, C5d) ---

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"
INSTALL = dict(
    title="Install seaborn for the plots",
    notes=["Installs seaborn into the kernel.", "The next plots use its styles."],
    intent="install seaborn",
    code="%pip install seaborn\nsorted(['b', 'a'])",
)
WITH_INSTALL = "%pip install seaborn\n" + LOAD["code"]
QUESTION = (
    "This cell installs `seaborn` into the kernel only, and the next env sync removes it "
    "(`uv add seaborn` keeps it). Run it as it is?"
)


def pending_question(h: Harness) -> dict | None:
    path = Layout(h.project).ledger_file("sess-1")
    return json.loads(path.read_text()).get("pending") if path.exists() else None


def written_answer(result: object, uid: str) -> dict:
    """The writer's answer for a cell it wrote that ran OK, from the gateway's real result."""
    return {
        "status": "ok",
        "wrote": True,
        "result": text(result),
        "changes": "Wrote the load cell.",
        "cell_title": LOAD["title"],
        "cell_id": uid,
        "exec_count": 1,
        "notebook": NOTEBOOK,
        "lead_lines": [],
    }


def asked_answer(refusal: object, call: dict, **extra: object) -> dict:
    """The writer's answer after nh's E122: the real refusal and the exact call it sent."""
    answer = {
        "status": "needs_approval",
        "wrote": False,
        "result": text(refusal),
        "changes": "nh asked for the user's yes before writing the cell (E122).",
        "lead_lines": [],
        "call": call,
    }
    answer.update(extra)
    return answer


async def run_until_nh_asks(h: Harness, shape: str) -> tuple[list, dict]:
    """The workflow's writer in message p1, ending on nh's question: (the fake agents' replies,
    the call the writer sent)."""
    if shape == "add":  # between two cells, so the position shows
        anchors = []
        for prompt, cell in (("p0", LOAD), ("p00", dict(LOAD, title="Load more sales data"))):
            h.turns.prompt(prompt, text="load the data")
            anchors.append((await h.call("nh_add_cell", prompt, **cell)).meta["nh/cell_id"])
        anchor = anchors[0]
        h.turns.prompt("p1", text="install seaborn")
        h.turns.workflow_launched("p1")
        call = dict(INSTALL, after_cell_id=anchor, notebook=NOTEBOOK)
        refusal = await h.turns.writer_call(h.client, "w-1", RUN, "nh_add_cell", call, "p1")
        assert refusal.is_error and "nh: E122" in text(refusal), text(refusal)
        return [asked_answer(refusal, {"tool": "nh_add_cell", **call})], call
    if shape == "launch edit":  # the launch named an earlier message's cell
        h.turns.prompt("p0", text="load the data")
        assert not (await h.call("nh_add_cell", "p0", **LOAD)).is_error
        h.turns.prompt("p1", text="style that cell's plots with seaborn")
        h.turns.workflow_launched("p1")
        call = dict(cell_id=code_uid(h), code=WITH_INSTALL)
        refusal = await h.turns.writer_call(h.client, "w-1", RUN, "nh_edit_cell", call, "p1")
        assert refusal.is_error and "nh: E122" in text(refusal), text(refusal)
        return [asked_answer(refusal, {"tool": "nh_edit_cell", **call})], call
    h.turns.prompt("p1", text="load the data, styled with seaborn")
    h.turns.workflow_launched("p1")
    first = dict(LOAD, code="undefined_name") if shape == "edit own failed cell" else LOAD
    added = await h.turns.writer_call(h.client, "w-1", RUN, "nh_add_cell", first, "p1")
    assert not added.is_error, text(added)
    uid = code_uid(h)
    call = dict(cell_id=uid, code=WITH_INSTALL)
    refusal = await h.turns.writer_call(h.client, "w-1", RUN, "nh_edit_cell", call, "p1")
    assert refusal.is_error and "nh: E122" in text(refusal), text(refusal)
    edit = {"tool": "nh_edit_cell", **call}
    if shape == "edit own failed cell":  # one writer: its add failed, its fix asks
        return [
            asked_answer(refusal, edit, wrote=True, cell_title=LOAD["title"], cell_id=uid)
        ], call
    # A revision: the writer's OK cell, QA's finding, then the revision nh asks about.
    return [written_answer(added, uid), qa("revise", finding()), asked_answer(refusal, edit)], call


@needs_node
@pytest.mark.parametrize("answer", ["yes", "no"])
@pytest.mark.parametrize("shape", ["add", "launch edit", "edit own failed cell", "revision"])
async def test_the_writers_question_reaches_the_main_conversation_and_its_call_is_granted(
    nh: Harness, shape: str, answer: str
) -> None:
    """End to end: the writer's real E122, qa-cell.js's report, the reply to it, and the main
    conversation's call built from the report in the user's next message."""
    replies, call = await run_until_nh_asks(nh, shape)
    asked = pending_question(nh)
    assert asked is not None and asked["turn_id"] == "p1"
    report = run_script(replies, args={"ask": "install seaborn", "notebook": NOTEBOOK})["result"]
    assert report["outcome"] == "needs_approval"
    approval = report["approval"]
    assert approval["question"] == QUESTION  # the gateway's own words, unchanged
    assert approval["tool"] == ("nh_add_cell" if shape == "add" else "nh_edit_cell")
    assert approval["args"] == call
    before = [c["source"] for c in nh_code_cells(nh)]
    # The report arrives (an alias of p1): sending the call while replying writes nothing.
    nh.turns.notification("note-1")
    early = await nh.call(approval["tool"], "note-1", **approval["args"])
    if shape == "revision":  # the message's OK cell: E112 for the main conversation
        assert early.is_error and "nh: E112" in text(early), text(early)
    else:  # the same question again
        assert early.is_error and f"Ask the user, then stop: '{QUESTION}'" in text(early)
    assert pending_question(nh) == asked and [c["source"] for c in nh_code_cells(nh)] == before
    nh.turns.prompt("p2", text=answer)
    sent = await nh.call(approval["tool"], "p2", **approval["args"])
    if answer == "no":  # nothing to grant: the call asks its own question
        assert sent.is_error and f"Next: Ask the user, then stop: '{QUESTION}'" in text(sent)
        assert [c["source"] for c in nh_code_cells(nh)] == before
        assert pending_question(nh)["turn_id"] == "p2"
        return
    assert not sent.is_error and "nh: E" not in text(sent), text(sent)
    assert pending_question(nh) is None
    sources = [c["source"] for c in nh_code_cells(nh)]
    if shape == "add":  # where the writer put it, not at the bottom
        assert sources == [LOAD["code"], INSTALL["code"], LOAD["code"]]
    else:
        assert sources == [WITH_INSTALL]
    again = await nh.call(approval["tool"], "p2", **approval["args"])  # granted once
    code = "nh: E110" if approval["tool"] == "nh_add_cell" else "nh: E112"
    assert again.is_error and code in text(again) and "nh: E122" not in text(again), text(again)


QA_EARLIER_PART = "An nh:qa-cell report for an earlier message arrived"  # QA_EARLIER


@needs_node
@pytest.mark.parametrize("later", ["write that cell", "yes"])
@pytest.mark.parametrize("reply_sends", [False, True], ids=["reply sends nothing", "reply sends"])
@pytest.mark.parametrize("word", ["ok", "yes", "go"])
async def test_a_yes_typed_during_the_run_grants_nothing_end_to_end(
    nh: Harness, word: str, reply_sends: bool, later: str
) -> None:
    """End to end (design §6.4, "A yes typed during the run", C5d2): the writer's real E122 in
    p1, a yes typed while the run works (p2), then the run's report, whose notification marks
    the run done after p2 opened. The call built from `approval` grants nothing in the reply
    (qa-workflow.md sends none; one sent anyway asks, as p2's own question). The user's later
    request gets nh's question, and a yes after it is granted once."""
    replies, call = await run_until_nh_asks(nh, "add")
    nh.turns.prompt("p2", text=word)  # typed during the run, before the report
    report = run_script(replies, args={"ask": "install seaborn", "notebook": NOTEBOOK})["result"]
    assert report["outcome"] == "needs_approval"
    approval = report["approval"]
    assert approval["question"] == QUESTION and approval["args"] == call
    head = nh.turns.notification("note-1")  # the report: an alias of p2
    assert head is not None and QA_EARLIER_PART in json.dumps(head)
    layout = Layout(nh.project)
    record = turn_record.read(layout, "sess-1")
    [run] = turn_record.find_runs(layout, "sess-1")
    assert record is not None and record["turn_id"] == "p2"
    assert run["run_id"] == RUN and run["done_ts"] > record["ts"]  # reported after p2 opened
    before = [c["source"] for c in nh_code_cells(nh)]
    if reply_sends:  # qa-workflow.md says not to; the gate refuses it anyway
        early = await nh.call(approval["tool"], "note-1", **approval["args"])
        assert early.is_error and f"Next: Ask the user, then stop: '{QUESTION}'" in text(early)
        assert pending_question(nh)["turn_id"] == "p2" and pending_question(nh)["run_id"] is None
    else:  # the writer's question stays as it was, ungranted
        assert pending_question(nh)["turn_id"] == "p1" and pending_question(nh)["run_id"] == RUN
    assert [c["source"] for c in nh_code_cells(nh)] == before
    nh.turns.prompt("p3", text=later)
    sent = await nh.call(approval["tool"], "p3", **approval["args"])
    if reply_sends and later == "yes":  # the user saw nh's question in that reply
        turn = "p3"
    else:  # nh asks its question now, as the main conversation's own
        assert sent.is_error and f"Next: Ask the user, then stop: '{QUESTION}'" in text(sent)
        assert pending_question(nh)["turn_id"] == "p3"
        assert [c["source"] for c in nh_code_cells(nh)] == before
        nh.turns.prompt("p4", text="yes")
        sent = await nh.call(approval["tool"], "p4", **approval["args"])
        turn = "p4"
    assert not sent.is_error and "nh: E" not in text(sent), text(sent)
    assert pending_question(nh) is None
    assert [c["source"] for c in nh_code_cells(nh)] == [LOAD["code"], INSTALL["code"], LOAD["code"]]
    again = await nh.call(approval["tool"], turn, **approval["args"])  # granted once
    assert again.is_error and "nh: E110" in text(again) and "nh: E122" not in text(again)


def test_the_needs_approval_flow_is_documented_where_the_model_reads_it() -> None:
    from nh_gateway.tools import approvals

    def flat(path: Path) -> str:
        return " ".join(path.read_text(encoding="utf-8").split())

    refs = PLUGIN / "skills" / "notebook" / "reference"
    qa_md = flat(refs / "qa-workflow.md")
    for phrase in (
        "`not_checked`, `needs_approval`, `not_written`",
        "| `approval` | with `needs_approval`: nh's `question`, and the exact call it asked "
        "about (`tool` and its `args`); else null |",
        "| `result` | the writer's last nh result, verbatim, without nh's `--- next ---` or "
        "`Next:` lines; after a revision nh asked about, the checked version's |",
        "what the writer did in each write (and what a revision nh asked about would change)",
        "Not for the user's answer to nh's question from a `needs_approval` report: send that "
        "call yourself (below).",
        "`notes` and `changes` say whether an earlier version is in the notebook.",
        "ask `approval.question` word for word",
        "One question, then stop. Write nothing now: not the call, no other cell, no new run.",
        # The gate refuses a yes typed during the run (C5d2), so the old reason ("may count as
        # a yes to a question they never saw") is gone; the instruction stays.
        "Report for an earlier message: say the cell isn't written because nh needs the user's "
        "yes, and what it would do. Don't ask, and don't send the call in this reply. If the "
        "user then asks for the cell, send the call in that message: nh asks its question then.",
        "call `approval.tool` yourself with `approval.args`, unchanged, before anything else, "
        "with no nh:qa-cell run.",
        "`title`, `notes` and `intent` may change; write your own if one is missing. nh writes "
        "it once, as that message's cell.",
        "say QA didn't check this cell. Anything else: drop the call ([asks.md](asks.md)).",
        "nh asked for the user's yes (E122) but `approval` is null: `notes` say why",
        "Don't ask nh's question: no call can follow a yes to it. Write nothing more for this "
        "message.",
        "If nh kept this message's yes for another cell, send that approved call first",
        "after E122, as the bullet above says",
        "| E122 | nh asks the user first: `needs_approval` (above) |",
    ):
        assert phrase in qa_md, phrase
    assert "never saw" not in qa_md and "may count as a yes" not in qa_md
    writer = flat(PLUGIN / "agents" / "cell-writer.md")
    for phrase in (
        "stop at once and return it with status `needs_approval` and the exact call in `call`",
        "never drop or move what nh asked about (the URL, the path) to get past its question",
        "Never ask the user yourself",
        'If another call got E122 "already waiting" after it anyway, return the first E122 '
        "and its call.",
        "`needs_approval` when your last call got E122, whether or not an earlier call wrote.",
        "with `needs_approval`, the E122 refusal verbatim",
        "`code` character for character",
        "`cell_id`, `after_cell_id`, `notebook`, `title`, `notes` and `intent` you passed",
        # the writer installs nothing (design §6.4, "The writer and installs")
        "Install packages, write outside the project unasked, re-run earlier cells or undo. If "
        "the ask needs a package that isn't installed, write nothing: status `no_write`",
    ):
        assert phrase in writer, phrase
    qa_agent = flat(PLUGIN / "agents" / "cell-qa.md")
    assert "You never see a cell nh hasn't written" in qa_agent
    assert (
        "A fix that installs a package, downloads or writes outside the project can't be "
        "written now (the writer installs nothing; nh asks the user about the rest, E122)"
    ) in qa_agent
    asks = flat(refs / "asks.md")
    assert "nh:cell-writer inside nh:qa-cell can't ask the user" in asks
    assert "The report's outcome is then `needs_approval`" in asks
    assert "if the report is for this message, ask the question; after the user's yes" in asks
    assert "send that call yourself ([qa-workflow.md](qa-workflow.md))" in asks
    errors_md = (refs / "errors.md").read_text(encoding="utf-8").splitlines()
    e122 = next(line for line in errors_md if line.startswith("| E122 |"))
    assert "nh:cell-writer returns it to the workflow, whose report says `needs_approval`" in e122
    assert "if the report is for this message, ask its question; after a yes, send its" in e122
    assert "A report for an earlier message: don't ask and don't send it" in e122
    root = PLUGIN.parents[1]
    trouble = (root / "docs" / "troubleshooting.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in trouble if line.startswith("| **E122** "))
    assert (
        "Under ultracode or `/nh:qa-cell` the workflow's cell writer can't ask you: its report "
        "brings nh's question back, the agent asks you, and after your yes writes that exact "
        "cell itself, without a QA check."
    ) in row
    assert (
        "A yes you type while the workflow still runs, before its report, approves nothing: you "
        "haven't seen nh's question yet, so nh asks it when the agent sends the cell."
    ) in row  # C5d2
    for readme in (root / "README.md", PLUGIN / "README.md"):
        assert (
            "A cell nh asks you about first comes back to Claude, which asks you and, after your "
            "yes, writes it itself without a QA check."
        ) in flat(readme), readme
    # qa-cell.js reads the question from the gateway's own writer line, one LF line at a time.
    assert approvals.WRITER_QUESTION == "- The main conversation asks the user: '{question}'"
    script = (PLUGIN / "workflows" / "qa-cell.js").read_text(encoding="utf-8")
    assert "/^- The main conversation asks the user: '([^\\n]*)'[ \\t]*$/.exec(line)" in script
