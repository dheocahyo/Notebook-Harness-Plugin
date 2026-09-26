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
