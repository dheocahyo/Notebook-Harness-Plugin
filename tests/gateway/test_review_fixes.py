"""Regression tests for the gateway findings of the v0.1 adversarial review (numbers = findings)."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from fastmcp import Client

from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from tests.fakes.turns import Turns, text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project
from tests.gateway.test_gateway import DROP, LOAD

OTHER = "notebooks/other.ipynb"


def code_cells(h: Harness, rel: str = NOTEBOOK) -> list[dict]:
    if rel not in h.backend.notebooks:
        return []
    return [c for c in h.backend.notebook(rel)["cells"] if c["cell_type"] == "code"]


def last_cell(h: Harness) -> dict:
    return json.loads((h.project / ".nh" / "state" / "last_cell.json").read_text())


async def harness_with(tmp_path: Path, toml: str = "", **backend_kw):
    project = make_project(tmp_path, toml)
    backend = FakeBackend(project, **backend_kw)
    client = Client(create_server(project, backend))
    await client.__aenter__()
    return Harness(project, backend, client, Turns(project, tmp_path / "data")), client


# 1 (P0): the one-cell budget spans notebooks
async def test_parallel_adds_to_two_notebooks(nh: Harness) -> None:
    nh.turns.prompt("p1")
    other = dict(DROP, code="x = 1\nx", notebook=OTHER)
    nh.turns.stamp("nh_add_cell", LOAD, "p1")
    nh.turns.stamp("nh_add_cell", other, "p1")
    results = await asyncio.gather(
        nh.client.call_tool("nh_add_cell", LOAD, raise_on_error=False),
        nh.client.call_tool("nh_add_cell", other, raise_on_error=False),
    )
    assert sorted(r.is_error for r in results) == [False, True]
    assert len(code_cells(nh)) + len(code_cells(nh, OTHER)) == 1


# 3: a copy/paste of an nh cell is its own cell
async def test_edit_of_a_copied_cell_runs_the_copy(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="print('ORIGINAL')"))
    cells = nh.backend.notebooks[NOTEBOOK]["cells"]
    original = next(c for c in cells if c["cell_type"] == "code")
    dup = copy.deepcopy(original)
    dup["id"] = "copy1"
    cells.append(dup)
    nh.turns.prompt("p2")
    sha = (
        next(
            line
            for line in text(
                await nh.call("nh_inspect", "p2", view="cell", cell_id="copy1")
            ).splitlines()
            if "sha=" in line
        )
        .split("sha=")[1]
        .split()[0]
    )
    result = await nh.call(
        "nh_edit_cell", "p2", cell_id="copy1", code="print('COPY')", base_sha=sha
    )
    assert not result.is_error, text(result)
    assert "COPY" in text(result) and "ORIGINAL" not in text(result)
    now = {c["id"]: c for c in nh.backend.notebook(NOTEBOOK)["cells"]}
    assert now["copy1"]["metadata"]["nh"]["copied_from"] == original["id"]
    assert now[original["id"]]["source"] == "print('ORIGINAL')"


# 4/16 + 5/8/49: nothing is written while an nh cell runs; the reminder says RUNNING
async def test_no_write_while_running(tmp_path: Path) -> None:
    h, client = await harness_with(tmp_path, "[exec]\nsoft_timeout_s = 5\n", exec_delay_s=8)
    try:
        h.turns.prompt("p1")
        first = await h.call("nh_add_cell", "p1", **LOAD)
        assert "still running" in text(first)
        assert last_cell(h)["status"] == "running"
        reminder = h.turns.prompt("p2")["hookSpecificOutput"]["additionalContext"]
        assert "RUNNING" in reminder
        second = await h.call("nh_add_cell", "p2", **DROP)
        assert second.is_error and "nh: E133" in text(second)
        assert len(code_cells(h)) == 1  # nothing written
        uid = code_cells(h)[0]["id"]
        edit = await h.call("nh_edit_cell", "p2", cell_id=uid, code="df = 2")
        assert edit.is_error and "nh: E133" in text(edit)
        assert code_cells(h)[0]["source"] == LOAD["code"]
        waited = await h.call("nh_run", "p2", cell_id=uid, mode="wait")
        assert "ran ok" in text(waited)
    finally:
        await client.__aexit__(None, None, None)


# 6: a retry never overwrites the user's edit
async def test_retry_respects_user_edit(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="total = undefined_x"))
    uid = code_cells(nh)[0]["id"]
    nh.backend.user_edit(NOTEBOOK, uid, "total = 41 + 1  # my fix")
    result = await nh.call("nh_edit_cell", "p1", cell_id=uid, code="total = 0\ntotal")
    assert result.is_error and "nh: E141" in text(result)
    assert "+total = 41 + 1  # my fix" in text(result)
    assert code_cells(nh)[0]["source"] == "total = 41 + 1  # my fix"


# 7/9/12/22: drift names only if bound; a kernel restart clears them
async def test_undo_leftovers_are_real_and_clear_on_restart(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="never = 1 / 0"))
    nh.turns.prompt("p2")
    body = text(await nh.call("nh_undo", "p2"))
    assert "Kernel ≠ notebook" not in body  # `never` was never bound
    nh.turns.prompt("p3")
    await nh.call("nh_add_cell", "p3", **dict(LOAD, code="total = 10\ntotal"))
    nh.turns.prompt("p4")
    body = text(await nh.call("nh_undo", "p4"))
    assert body.startswith("Kernel ≠ notebook: `total`")
    nh.backend.restart_kernel()
    reminder = nh.turns.prompt("p5")["hookSpecificOutput"]["additionalContext"]
    assert "total" in reminder  # the hook can't know yet; the next nh call finds out
    body = text(await nh.call("nh_add_cell", "p5", **dict(LOAD, code="y = 1\ny")))
    assert body.startswith("NEW kernel")
    assert "Kernel ≠ notebook" not in body
    reminder = nh.turns.prompt("p6")["hookSpecificOutput"]["additionalContext"]
    assert "Kernel ≠ notebook" not in reminder


# 14: the user's note edits are never overwritten
async def test_user_note_edits_are_kept(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    note = next(c for c in nh.backend.notebooks[NOTEBOOK]["cells"] if c["cell_type"] == "markdown")
    nh.backend.user_edit(NOTEBOOK, note["id"], note["source"] + "\n- MY NOTE: check with finance.")
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    result = await nh.call(
        "nh_edit_cell",
        "p2",
        cell_id=uid,
        code=LOAD["code"] + "\ndf.head()",
        notes=["New bullet one here.", "New bullet two here."],
    )
    assert result.is_error and "nh: E141" in text(result) and "the note above" in text(result)
    nh.turns.prompt("p3")
    body = text(await nh.call("nh_undo", "p3"))
    assert "Kept the note above it" in body
    assert any("MY NOTE" in c["source"] for c in nh.backend.notebook(NOTEBOOK)["cells"])


# 15 + 27: code-only edits keep the note and the original intent; title-only edits work
async def test_edit_keeps_note_and_intent(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    body = text(
        await nh.call(
            "nh_edit_cell",
            "p2",
            cell_id=uid,
            code=LOAD["code"] + "\ndf.tail()",
            intent="show the tail instead",
        )
    )
    assert "Note unchanged" in body
    nh_meta = code_cells(nh)[0]["metadata"]["nh"]
    assert nh_meta["intent"] == "load the sales data"
    assert nh_meta["edit_intents"] == ["show the tail instead"]
    nh.turns.prompt("p3")
    result = await nh.call(
        "nh_edit_cell", "p3", cell_id=uid, code=LOAD["code"], title="Load the sales table"
    )
    assert not result.is_error, text(result)
    note = next(c for c in nh.backend.notebook(NOTEBOOK)["cells"] if c["cell_type"] == "markdown")
    assert note["source"].startswith("### Load the sales table\n\n- Builds a small frame")


# 23: inspecting another notebook doesn't redirect the next write
async def test_inspect_does_not_switch_notebook(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_inspect", "p1", view="outline", notebook=OTHER)
    await nh.call("nh_add_cell", "p1", **LOAD)
    assert len(code_cells(nh)) == 1 and not code_cells(nh, OTHER)


# 24/55: a cell the user deleted is named, and the next undo moves on
async def test_undo_after_user_deleted_the_cell(nh: Harness) -> None:
    for prompt, cell in (("p1", LOAD), ("p2", DROP)):
        nh.turns.prompt(prompt)
        await nh.call("nh_add_cell", prompt, **cell)
    drop_uid = code_cells(nh)[1]["id"]
    nh.backend.user_delete(NOTEBOOK, drop_uid)
    nh.turns.prompt("p3")
    result = await nh.call("nh_undo", "p3")
    body = text(result)
    assert not result.is_error and body.startswith("Kernel ≠ notebook: `df_clean`")
    assert 'Nothing undone: "Drop rows with missing price" was already deleted' in body
    assert "Load sales data" in body and "nh_undo() again" in body
    # The user says yes: a plain nh_undo() undoes the offered step, although p1 is outside the
    # three-message window (p4, p3, p2).
    nh.turns.prompt("p4")
    result = await nh.call("nh_undo", "p4")
    assert not result.is_error and 'Removed "Load sales data"' in text(result)
    assert code_cells(nh) == []


# 24: the offer lapses once the user asks for something else
async def test_undo_offer_expires_after_the_next_message(nh: Harness) -> None:
    for prompt, cell in (("p1", LOAD), ("p2", DROP)):
        nh.turns.prompt(prompt)
        await nh.call("nh_add_cell", prompt, **cell)
    nh.backend.user_delete(NOTEBOOK, code_cells(nh)[1]["id"])
    nh.turns.prompt("p3")
    await nh.call("nh_undo", "p3")
    nh.turns.prompt("p4")  # the user asks for a different step instead
    await nh.call("nh_add_cell", "p4", **DROP)
    nh.turns.prompt("p5")
    result = await nh.call("nh_undo", "p5")
    assert 'Removed "Drop rows with missing price"' in text(result)
    assert [c["metadata"]["nh"]["turn_id"] for c in code_cells(nh)] == ["p1"]


# 25: undo with a note's id undoes its cell
async def test_undo_by_note_id(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    note_id = code_cells(nh)[0]["id"] + "-n"
    nh.turns.prompt("p2")
    result = await nh.call("nh_undo", "p2", cell_id=note_id)
    assert not result.is_error and "Removed" in text(result)


# 28 + 65: human cells need base_sha and never get the nh-agent tag
async def test_human_cell_edits(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, "a = 1\na")
    nh.turns.prompt("p1")
    result = await nh.call("nh_edit_cell", "p1", cell_id=human, code="a = 2\na")
    assert result.is_error and "nh: E144" in text(result)
    for prompt, value in (("p2", 2), ("p3", 3)):
        nh.turns.prompt(prompt)
        view = text(await nh.call("nh_inspect", prompt, view="cell", cell_id=human))
        sha = next(line for line in view.splitlines() if "sha=" in line).split("sha=")[1].split()[0]
        result = await nh.call(
            "nh_edit_cell",
            prompt,
            cell_id=human,
            code=f"a = {value}\na",
            base_sha=sha,
            intent=f"set a to {value}",
        )
        assert not result.is_error, text(result)
    cell = next(c for c in nh.backend.notebook(NOTEBOOK)["cells"] if c["id"] == human)
    assert "nh-agent" not in cell["metadata"].get("tags", [])
    assert cell["metadata"]["nh"]["created_by"] == "human"


# 26 + 32: stale after an undone edit, and clear-count
async def test_clear_count_and_undone_edit_stale(tmp_path: Path) -> None:
    h, client = await harness_with(tmp_path, '[stale]\nmark = "clear-count"\n')
    try:
        for prompt, cell in (("p1", LOAD), ("p2", DROP)):
            h.turns.prompt(prompt)
            await h.call("nh_add_cell", prompt, **cell)
        load_uid = code_cells(h)[0]["id"]
        h.turns.prompt("p3")
        await h.call("nh_edit_cell", "p3", cell_id=load_uid, code=LOAD["code"] + "\ndf.head()")
        assert code_cells(h)[1]["execution_count"] is None  # downstream count cleared
        h.turns.prompt("p4")
        await h.call("nh_undo", "p4")
        outline = text(await h.call("nh_inspect", "p5", view="outline"))
        assert outline.count("STALE") == 2
    finally:
        await client.__aexit__(None, None, None)


# 33: rows defaults to [inspect].head_rows
async def test_head_rows_default(tmp_path: Path) -> None:
    h, client = await harness_with(tmp_path, "[inspect]\nhead_rows = 3\n")
    try:
        h.turns.prompt("p1")
        await h.call(
            "nh_add_cell",
            "p1",
            **dict(LOAD, code="import pandas as pd\ndf = pd.DataFrame({'a': range(12)})\ndf.shape"),
        )
        view = text(await h.call("nh_inspect", "p1", view="var", name="df"))
        assert "\n2 " in view and "\n3 " not in view.split("--- head ---")[1]
    finally:
        await client.__aexit__(None, None, None)


# 54 + 56 + 44: refusal wording and reasons
async def test_refusal_texts(nh: Harness) -> None:
    nh.turns.prompt("p1")
    result = await nh.call("nh_run", "p1", cell_id="nh-deadbeef00")
    first, machine = text(result).splitlines()[:2]
    assert "nh-deadbeef00" not in first and "cell_id=nh-deadbeef00" in machine
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="df = undefined_y"))
    second = await nh.call("nh_add_cell", "p1", **DROP)
    assert "nh: E110" in text(second) and "nh_edit_cell" in text(second)
    nh.turns.prompt("p2")  # a fresh message, so the lint check (not the one-cell check) answers
    await nh.call("nh_add_cell", "p2", **dict(LOAD, notes=["one"] * 6))
    events = [
        json.loads(line) for line in (nh.project / ".nh" / "log.jsonl").read_text().splitlines()
    ]
    assert any(e["event"] == "cell_rejected" and e["reason"] == ["L004"] for e in events)


# 72: waiting after the cell finished still reports its output
async def test_wait_after_finish_reports_output(tmp_path: Path) -> None:
    h, client = await harness_with(tmp_path, "[exec]\nsoft_timeout_s = 5\n", exec_delay_s=6)
    try:
        h.turns.prompt("p1")
        first = await h.call("nh_add_cell", "p1", **dict(LOAD, code="print('answer is 42')"))
        assert "still running" in text(first)
        await asyncio.sleep(2.5)
        uid = code_cells(h)[0]["id"]
        waited = text(await h.call("nh_run", "p1", cell_id=uid, mode="wait"))
        assert "answer is 42" in waited and "ran ok" in waited
    finally:
        await client.__aexit__(None, None, None)
