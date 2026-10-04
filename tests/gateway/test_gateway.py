"""The gateway end to end in memory: real hook stamps, the turn gate, the tools and FakeBackend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway.app import INSTRUCTIONS, create_server
from nh_gateway.backend.fake import FakeBackend
from tests.fakes.turns import text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project

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


def nh_cells(h: Harness) -> list[dict]:
    return [c for c in h.cells() if c.get("metadata", {}).get("nh")]


async def test_tool_surface(nh: Harness) -> None:
    tools = {t.name: t for t in await nh.client.list_tools()}
    assert sorted(tools) == ["nh_add_cell", "nh_edit_cell", "nh_inspect", "nh_run", "nh_undo"]
    assert len(INSTRUCTIONS) <= 2048
    for tool in tools.values():
        assert len(tool.description or "") <= 2048
        assert tool.meta.get("anthropic/alwaysLoad") is True
        assert "anthropic/requiresUserInteraction" not in tool.meta
    assert set(tools["nh_add_cell"].input_schema["required"]) == {
        "title",
        "notes",
        "intent",
        "code",
    }


async def test_approval_flag_only_on_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path, "[approval]\napprove_before_run = true\n")
    server = create_server(project, FakeBackend(project))
    async with Client(server) as client:
        metas = {t.name: t.meta for t in await client.list_tools()}
    assert metas["nh_add_cell"]["anthropic/requiresUserInteraction"] is True
    assert metas["nh_edit_cell"]["anthropic/requiresUserInteraction"] is True
    assert "anthropic/requiresUserInteraction" not in metas["nh_run"]
    monkeypatch.setenv("NH_HEADLESS", "1")
    server = create_server(project, FakeBackend(project))
    async with Client(server) as client:
        metas = {t.name: t.meta for t in await client.list_tools()}
    assert "anthropic/requiresUserInteraction" not in metas["nh_add_cell"]


async def test_add_cell_writes_note_and_code_and_runs(nh: Harness) -> None:
    nh.turns.prompt("p1")
    result = await nh.call("nh_add_cell", "p1", **LOAD)
    body = text(result)
    assert not result.is_error, body
    assert body.startswith('Added "Load sales data" [1] at the bottom')
    assert "ran ok" in body and "(3, 2)" in body
    note, code = h_cells = nh.cells()[-2:]
    assert note["cell_type"] == "markdown"
    assert note["source"] == (
        "### Load sales data\n\n- Builds a small frame of prices by region.\n"
        "- Keeps the missing price so later steps can drop it."
    )
    meta = code["metadata"]["nh"]
    assert (
        meta["intent"] == "load the sales data"
        and meta["turn_id"] == "p1"
        and meta["role"] == "code"
    )
    assert meta["rationale"] == LOAD["notes"] and code["metadata"]["tags"] == ["nh-agent"]
    assert note["metadata"]["nh"]["pair_uid"] == code["id"] and h_cells
    events = [
        json.loads(line) for line in (nh.project / ".nh" / "log.jsonl").read_text().splitlines()
    ]
    assert any(e["event"] == "cell_added" and e["note_words"] > 0 for e in events)
    last = json.loads((nh.project / ".nh" / "state" / "last_cell.json").read_text())
    assert last["status"] == "ok" and last["title"] == "Load sales data"


async def test_one_cell_per_message(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    second = await nh.call("nh_add_cell", "p1", **DROP)
    assert second.is_error
    assert "one new cell per message" in text(second) and "NH-E110" not in text(second)
    assert "nh: E110" in text(second)
    assert len(nh_cells(nh)) == 2  # note + code from the first call only
    nh.turns.prompt("p2")
    assert not (await nh.call("nh_add_cell", "p2", **DROP)).is_error
    assert len(nh_cells(nh)) == 4


async def test_parallel_adds_in_one_message(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    nh.turns.stamp("nh_add_cell", DROP, "p1")
    results = await asyncio.gather(
        nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False),
        nh.client.call_tool("nh_add_cell", DROP, raise_on_error=False),
    )
    assert sorted(r.is_error for r in results) == [False, True]
    assert len([c for c in nh_cells(nh) if c["cell_type"] == "code"]) == 1


async def test_lint_rejection_does_not_use_the_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    bad = dict(LOAD, notes=[f"point {i}" for i in range(6)])
    result = await nh.call("nh_add_cell", "p1", **bad)
    assert result.is_error and "nh: E120" in text(result) and "L004" in text(result)
    assert not nh_cells(nh)
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error


async def test_unstamped_call_fails_closed(nh: Harness) -> None:
    nh.turns.prompt("p1")
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert result.is_error and "nh: E101" in text(result)
    assert not nh_cells(nh)


async def test_subagent_and_plan_mode_are_read_only(nh: Harness) -> None:
    nh.turns.prompt("p1")
    denied = nh.turns.stamp("nh_add_cell", LOAD, "p1", agent_id="agent-7")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert "nh: E101" in text(result)  # the hook refused to stamp it
    result = await nh.call("nh_add_cell", "p1", **LOAD)  # fresh stamp in plan mode below
    assert not result.is_error
    nh.turns.prompt("p2")
    nh.turns.stamp("nh_add_cell", DROP, "p2", permission_mode="plan")
    result = await nh.client.call_tool("nh_add_cell", DROP, raise_on_error=False)
    assert "nh: E104" in text(result)


async def test_stamp_from_an_earlier_message_is_refused(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    await asyncio.sleep(0.05)
    nh.turns.prompt("p2")
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    assert "nh: E102" in text(result)


# --- D1: a message nh missed fails closed (design §6.1) -------------------------------------

MISSED = "- nh missed this message; send it again."
MISSED_NEXT = (
    "Next: Tell the user nh missed their last message and ask them to send it again; write "
    "nothing until they do."
)


async def test_a_message_nh_missed_is_refused(nh: Harness) -> None:
    nh.turns.prompt("p1")
    # p2's UserPromptSubmit hook never ran (failed or timed out): its call is newer than p1's.
    result = await nh.call("nh_add_cell", "p2", **LOAD)
    body = text(result)
    assert result.is_error and "nh: E102" in body, body
    assert body.splitlines()[:4] == [
        "This call belongs to an earlier message, so nh wrote nothing.",
        "nh: E102",
        MISSED,
        MISSED_NEXT,  # the user must resend, so the model must say so (not just wait)
    ]
    assert not nh_cells(nh)
    nh.turns.prompt("p2")  # sent again: the hook records it
    assert not (await nh.call("nh_add_cell", "p2", **LOAD)).is_error


async def test_a_missed_message_cannot_wait_either(nh: Harness) -> None:
    nh.turns.prompt("p1")
    result = await nh.call("nh_run", "p9", mode="wait")
    assert result.is_error and MISSED in text(result)


async def test_no_turn_record_is_allowed(nh: Harness) -> None:
    # The /nh:init message: its hook ran before .nh/ existed, so there is no record.
    result = await nh.call("nh_add_cell", "p1", **LOAD)
    assert not result.is_error, text(result)


async def test_known_ids_pass_d1(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.notification("note-1")
    result = await nh.call("nh_add_cell", "note-1", **LOAD)  # an alias of p1
    assert not result.is_error, text(result)
    nh.turns.prompt("p2", text="yes")
    nh.turns.notification("note-2")
    # Typed mid-turn: absorbed, still p2. No mode: an explain one would now be E109 (§6.2).
    nh.turns.prompt("p2", text="also keep the region column")
    result = await nh.call("nh_add_cell", "note-2", **DROP)
    assert not result.is_error, text(result)
    later = await nh.call("nh_add_cell", "note-1", **dict(DROP, title="Late"))  # earlier alias
    assert later.is_error and "nh: E110" in text(later)  # p1's budget, not D1
    assert MISSED not in text(later)


async def test_a_message_typed_into_a_notifications_turn_gets_no_budget(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    nh.turns.notification("note-1")
    # Typed while Claude handles note-1: Claude Code resubmits note-1, an alias of p1.
    nh.turns.prompt("note-1", text="yes, add the drop step too")
    result = await nh.call("nh_add_cell", "note-1", **DROP)
    assert result.is_error and "nh: E110" in text(result)  # still p1's one cell
    assert MISSED not in text(result)


def _stamp_and_record_ts(nh: Harness, offset: float) -> None:
    """Set the turn record's ts to the one waiting stamp's ts + ``offset``."""
    (stamp_file,) = (nh.project / ".nh" / "state" / "stamps").glob("*--*.json")
    stamp_ts = json.loads(stamp_file.read_text())["ts"]
    turn_file = nh.project / ".nh" / "state" / "turns" / "sess-1.json"
    record = json.loads(turn_file.read_text())
    turn_file.write_text(json.dumps(dict(record, ts=stamp_ts + offset)))


@pytest.mark.parametrize(("offset", "missed"), [(0.0, True), (0.001, False)])
async def test_d1_starts_at_the_records_own_time(nh: Harness, offset: float, missed: bool) -> None:
    """A stamp exactly as new as the record is a missed message; a hair older is v0.1's
    earlier-message E102."""
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p2")
    _stamp_and_record_ts(nh, offset)
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    body = text(result)
    assert result.is_error and "nh: E102" in body, body
    assert (MISSED in body, MISSED_NEXT in body) == (missed, missed)


# v0.1's E102 (not D1): an unknown id older than the record is an earlier message's call.


async def test_v01_an_old_unknown_call_gets_the_plain_e102(nh: Harness) -> None:
    nh.turns.stamp("nh_add_cell", LOAD, "p0")  # stamped before any message was recorded
    await asyncio.sleep(0.05)
    nh.turns.prompt("p1")
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    body = text(result)
    assert result.is_error and "nh: E102" in body and MISSED not in body


async def test_v01_a_call_from_two_messages_back_gets_the_plain_e102(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    await asyncio.sleep(0.05)
    nh.turns.prompt("p2")
    nh.turns.prompt("p3")  # p1 is neither the turn nor the previous one
    result = await nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False)
    body = text(result)
    assert result.is_error and "nh: E102" in body and MISSED not in body


async def test_after_an_orphan_an_unknown_call_keeps_v01s_rule(nh: Harness) -> None:
    # A background task can finish during the /nh:init message: the record is an orphan.
    nh.turns.notification("note-1")
    result = await nh.call("nh_add_cell", "p1", **LOAD)
    assert not result.is_error, text(result)


async def test_the_writers_calls_skip_d1(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    # A workflow agent's stamp may carry a prompt id the turn record never saw.
    result = await nh.turns.writer_call(nh.client, "w-1", "wf_run-1", "nh_add_cell", LOAD, "p-w1")
    assert not result.is_error, text(result)
    assert nh_cells(nh)[-1]["metadata"]["nh"]["turn_id"] == "p1"


async def test_error_retry_then_done(nh: Harness) -> None:
    nh.turns.prompt("p1")
    broken = dict(
        LOAD, code="import pandas as pd\ndf = pd.DataFrame({'price': [1, 2]})\ndf['prce'].mean()"
    )
    first = await nh.call("nh_add_cell", "p1", **broken)
    body = text(first)
    assert not first.is_error and "it failed with KeyError" in body
    assert "failing code: df['prce'].mean()" in body
    uid = [c for c in nh.cells() if c["cell_type"] == "code"][-1]["id"]
    fixed = await nh.call(
        "nh_edit_cell",
        "p1",
        cell_id=uid,
        code="import pandas as pd\ndf = pd.DataFrame({'price': [1, 2]})\ndf['price'].mean()",
    )
    assert not fixed.is_error and "(retry 1 of 2)" in text(fixed) and "ran ok" in text(fixed)
    again = await nh.call("nh_edit_cell", "p1", cell_id=uid, code="df['price'].sum()")
    assert again.is_error and "nh: E112" in text(again)


async def test_retries_run_out(nh: Harness) -> None:
    nh.turns.prompt("p1")
    broken = dict(LOAD, code="undefined_name + 1")
    assert "it failed" in text(await nh.call("nh_add_cell", "p1", **broken))
    uid = [c for c in nh.cells() if c["cell_type"] == "code"][-1]["id"]
    for attempt in (1, 2):
        result = await nh.call(
            "nh_edit_cell", "p1", cell_id=uid, code=f"undefined_name + {attempt}"
        )
        assert not result.is_error and "it failed" in text(result)
    result = await nh.call("nh_edit_cell", "p1", cell_id=uid, code="undefined_name + 3")
    assert result.is_error and "nh: E111" in text(result)


async def test_undo_after_retry_removes_the_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="df = 1\nboom()"))
    uid = [c for c in nh.cells() if c["cell_type"] == "code"][-1]["id"]
    await nh.call("nh_edit_cell", "p1", cell_id=uid, code="df = 1\ndf")
    nh.turns.prompt("p2")
    result = await nh.call("nh_undo", "p2")
    body = text(result)
    assert not result.is_error, body
    assert body.startswith("Kernel ≠ notebook: `df`")
    assert '\nRemoved "Load sales data"' in body and "and its note" in body
    assert "Restart Kernel and Run Up to Selected Cell" in body
    assert not nh_cells(nh)
    inspect = text(await nh.call("nh_inspect", "p3", view="overview"))
    assert inspect.startswith("Kernel ≠ notebook")


async def test_undo_marks_downstream_stale(nh: Harness) -> None:
    for prompt, cell in (("p1", LOAD), ("p2", DROP)):
        nh.turns.prompt(prompt)
        assert not (await nh.call("nh_add_cell", prompt, **cell)).is_error
    load_uid = [c for c in nh.cells() if c["cell_type"] == "code"][0]["id"]
    nh.turns.prompt("p3")
    result = await nh.call("nh_undo", "p3", cell_id=load_uid)
    assert 'Now outdated: "Drop rows with missing price"' in text(result)
    outline = text(await nh.call("nh_inspect", "p4", view="outline"))
    assert "STALE" in outline


async def test_user_edit_is_not_overwritten(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    uid = [c for c in nh.cells() if c["cell_type"] == "code"][-1]["id"]
    nh.backend.user_edit(NOTEBOOK, uid, "df = 'mine'")
    nh.turns.prompt("p2")
    result = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="df = 'agent'")
    assert result.is_error and "nh: E141" in text(result)
    sha = (
        [
            line
            for line in text(
                await nh.call("nh_inspect", "p2", view="cell", cell_id=uid)
            ).splitlines()
            if "sha=" in line
        ][0]
        .split("sha=")[1]
        .split()[0]
    )
    result = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="df = 'agent'", base_sha=sha)
    assert not result.is_error, text(result)
    nh.turns.prompt("p3")
    result = await nh.call("nh_undo", "p3")
    assert not result.is_error and "Restored the previous version" in text(result)
    assert [c for c in nh.cells() if c["cell_type"] == "code"][-1]["source"] == "df = 'mine'"


async def test_rerun_of_older_cell_uses_the_turn(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    first = [c for c in nh.cells() if c["cell_type"] == "code"][-1]["id"]
    nh.turns.prompt("p2")
    await nh.call("nh_add_cell", "p2", **DROP)
    result = await nh.call("nh_run", "p2", cell_id=first)
    assert result.is_error and "nh: E114" in text(result)
    nh.turns.prompt("p3")
    result = await nh.call("nh_run", "p3", cell_id=first)
    assert not result.is_error and text(result).startswith('Re-ran "Load sales data"')


async def test_inspect_views(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    overview = text(await nh.call("nh_inspect", "p1"))
    assert "notebook notebooks/eda.ipynb: 2 cells" in overview
    assert "df: pandas DataFrame 3x2" in overview and "nulls price 1" in overview
    intents = text(await nh.call("nh_inspect", "p1", view="intents"))
    assert "intent: load the sales data" in intents and "- Builds a small frame" in intents
    var = text(await nh.call("nh_inspect", "p1", view="var", name="df", rows=2))
    assert "--- head ---" in var
    status = text(await nh.call("nh_inspect", "p1", view="status"))
    assert status.startswith("nh status") and "hooks: last turn record" in status


async def test_not_an_nh_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("NH_PROJECT_DIR", str(plain))
    server = create_server(None, FakeBackend(plain))
    async with Client(server) as client:
        result = await client.call_tool("nh_inspect", {}, raise_on_error=False)
        assert result.is_error and "nh: E105" in text(result)
        tools = {t.name: t.meta for t in await client.list_tools()}
        assert "anthropic/alwaysLoad" not in tools["nh_add_cell"]


async def test_markdown_and_install_smuggling_rejected(nh: Harness) -> None:
    nh.turns.prompt("p1")
    for code, rule in (
        ("!pip install seaborn", "L009"),
        (
            "from IPython.display import Markdown\nMarkdown('## Findings: the data shows a clear upward trend in prices')",
            "L010",
        ),
        ("x = 1\n# %%\ny = 2", "L002"),
    ):
        result = await nh.call("nh_add_cell", "p1", **dict(LOAD, code=code))
        assert result.is_error and rule in text(result), text(result)


# --- the approved batch (design §6.3) ----------------------------------------------------------

TOTALS = dict(
    title="Total price by region",
    notes=["Sums the price per region.", "Shows which region sells the most."],
    intent="total price by region",
    code="totals = df.groupby('region')['price'].sum()\ntotals",
)


def batch_message(nh: Harness, n: int = 3) -> None:
    """p1 asks for the batch; p2, the user's yes, may write ``n`` cells."""
    nh.turns.prompt("p1", text=f"run the next {n}")
    nh.turns.prompt("p2", text="yes")


def first_lines(result) -> list[str]:
    return text(result).splitlines()


async def test_without_a_batch_the_cap_is_max_code_cells(nh: Harness) -> None:
    nh.turns.prompt("p1", text="yes")  # a yes with no batch asked before it
    first = await nh.call("nh_add_cell", "p1", **LOAD)
    assert "turn=1/1 retries" in text(first)
    second = await nh.call("nh_add_cell", "p1", **DROP)
    assert "nh: E110" in text(second) and "batch" not in text(second)


async def test_a_batch_raises_e110s_cap_to_its_size(nh: Harness) -> None:
    batch_message(nh, 2)
    one = await nh.call("nh_add_cell", "p2", **LOAD)
    two = await nh.call("nh_add_cell", "p2", **DROP)
    assert "turn=1/2 batch retries=0/2" in text(one) and "turn=2/2 batch retries=0/2" in text(two)
    three = await nh.call("nh_add_cell", "p2", **TOTALS)
    assert first_lines(three)[1:] == [
        "nh: E110",
        "- The approved batch's 2 steps are written; the rest of the plan waits for the user's "
        "next message.",
        "Next: Don't write more cells. Reply with the remaining steps as a numbered list and ask "
        "which to do next.",
    ]
    assert len([c for c in nh_cells(nh) if c["cell_type"] == "code"]) == 2


async def test_a_lint_refusal_stops_the_batch_and_says_not_to_call_again(nh: Harness) -> None:
    batch_message(nh)
    assert not (await nh.call("nh_add_cell", "p2", **LOAD)).is_error
    refused = await nh.call("nh_add_cell", "p2", **dict(DROP, notes=["Only one bullet."]))
    body = first_lines(refused)
    assert body[:2] == ["Not written: the cell broke nh's hard rules.", "nh: E120"]
    assert body[-2:] == [
        "- The approved batch stops here, at step 2 of 3: nh changes nothing more this message.",
        "Next: Don't call again: the approved batch stops here. Tell the user what each step "
        "did, which step nh refused and why, and what you would change; then wait.",
    ]
    fixed = await nh.call("nh_add_cell", "p2", **DROP)
    assert first_lines(fixed)[:2] == [
        "Not written: the approved batch stopped at step 2 of 3; nh changes nothing more this "
        "message.",
        "nh: E123",
    ]
    assert first_lines(fixed)[-1] == (
        "Next: Report the batch to the user: what each step did, then where and why it stopped "
        "(the error, the 'check this' finding or nh's question). No retry and no new cell this "
        "message; wait for the user."
    )


async def test_e125_as_the_first_call_decides_and_stops_the_batch(nh: Harness) -> None:
    """E125 comes before the notebook is resolved (design §6.8); in a batch it still stops it,
    under the turn's lock, and decides the batch when it is the message's first gated call."""
    batch_message(nh)
    marked = await nh.call("nh_add_cell", "p2", **dict(LOAD, code="key = '[redacted:API_KEY]'"))
    body = first_lines(marked)
    assert body[1] == "nh: E125"
    assert body[-2:] == [
        "- The approved batch stops here, at step 1 of 3: nh changes nothing more this message.",
        "Next: Don't call again: the approved batch stops here. Tell the user what each step "
        "did, which step nh refused and why, and what you would change; then wait.",
    ]
    after = await nh.call("nh_add_cell", "p2", **LOAD)
    assert "nh: E123" in text(after) and "step 1 of 3" in text(after)
    assert not nh_cells(nh)


async def test_e125_outside_a_batch_is_unchanged(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load it")
    marked = await nh.call("nh_add_cell", "p1", **dict(LOAD, code="key = '[redacted:API_KEY]'"))
    assert "batch" not in text(marked) and first_lines(marked)[-1].startswith("Next: Read the")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
