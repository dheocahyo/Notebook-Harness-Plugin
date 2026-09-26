"""Round-2 fixes for writing and undoing cells (verification items V2-V30)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from nh_gateway.policy.errors import NhError
from tests.fakes.turns import Turns, text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project
from tests.gateway.test_gateway import DROP, LOAD

OTHER = "notebooks/other.ipynb"
FAILING = dict(LOAD, code="total = undefined_name + 1\ntotal")


class TypingBackend(FakeBackend):
    """The user types into nh's cell between nh's write and its run."""

    typed: str | None = None

    async def start_execution(self, ref, cell_id, **kwargs):
        if self.typed is not None:
            self.user_edit(ref.rel_path, cell_id, self.typed)
            self.typed = None
        return await super().start_execution(ref, cell_id, **kwargs)


class FailingStartBackend(FakeBackend):
    """The kernel can't start the run (E134) once ``fail`` is set."""

    fail = False

    async def start_execution(self, ref, cell_id, **kwargs):
        if self.fail:
            raise NhError("E134")
        return await super().start_execution(ref, cell_id, **kwargs)


@pytest.fixture
async def open_harness(tmp_path: Path):
    clients: list[Client] = []

    async def make(backend_cls=FakeBackend, toml: str = "", **backend_kw) -> Harness:
        project = make_project(tmp_path, toml)
        backend = backend_cls(project, **backend_kw)
        client = Client(create_server(project, backend))
        await client.__aenter__()
        clients.append(client)
        return Harness(project, backend, client, Turns(project, tmp_path / "data"))

    yield make
    for client in clients:
        await client.__aexit__(None, None, None)


def code_cells(h: Harness, rel: str = NOTEBOOK) -> list[dict]:
    if rel not in h.backend.notebooks:
        return []
    return [c for c in h.backend.notebook(rel)["cells"] if c["cell_type"] == "code"]


def last_cell(h: Harness) -> dict:
    return json.loads((h.project / ".nh" / "state" / "last_cell.json").read_text())


def sha_of(view: str) -> str:
    line = next(line for line in view.splitlines() if "sha=" in line)
    return line.split("sha=")[1].split()[0]


# V3: a stale base_sha is no user change while the cell still holds nh's last write
async def test_stale_base_sha_without_a_user_change_is_accepted(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, "mine = 1\nmine")
    nh.turns.prompt("p1")
    old_sha = sha_of(text(await nh.call("nh_inspect", "p1", view="cell", cell_id=human)))
    first = await nh.call(
        "nh_edit_cell", "p1", cell_id=human, code="mine = 2\nmine", base_sha=old_sha, intent="two"
    )
    assert not first.is_error, text(first)
    nh.turns.prompt("p2")
    second = await nh.call(
        "nh_edit_cell", "p2", cell_id=human, code="mine = 3\nmine", base_sha=old_sha, intent="three"
    )
    assert not second.is_error, text(second)
    assert code_cells(nh)[0]["source"] == "mine = 3\nmine"


async def test_a_real_user_change_still_refuses_with_its_diff(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, "mine = 1\nmine")
    nh.turns.prompt("p1")
    old_sha = sha_of(text(await nh.call("nh_inspect", "p1", view="cell", cell_id=human)))
    await nh.call(
        "nh_edit_cell", "p1", cell_id=human, code="mine = 2\nmine", base_sha=old_sha, intent="two"
    )
    nh.backend.user_edit(NOTEBOOK, human, "mine = 20\nmine")
    nh.turns.prompt("p2")
    result = await nh.call(
        "nh_edit_cell", "p2", cell_id=human, code="mine = 3\nmine", base_sha=old_sha, intent="three"
    )
    assert result.is_error and "nh: E141" in text(result)
    assert "-mine = 2" in text(result) and "+mine = 20" in text(result)


# V10: the user typed into nh's new cell before it ran: the write is in history
async def test_conflict_at_start_of_an_add_is_undoable(open_harness) -> None:
    h = await open_harness(TypingBackend)
    h.turns.prompt("p1")
    await h.call("nh_add_cell", "p1", **LOAD)
    h.turns.prompt("p2")
    h.backend.typed = "df_clean = 'mine'"
    result = await h.call("nh_add_cell", "p2", **DROP)
    assert result.is_error and text(result).startswith("Not run:")
    drop_uid = code_cells(h)[1]["id"]
    assert (h.project / ".nh" / "history" / f"{drop_uid}.jsonl").exists()
    assert last_cell(h)["status"] == "conflict" and last_cell(h)["cell_id"] == drop_uid

    h.turns.prompt("p3")
    refused = text(await h.call("nh_undo", "p3"))
    assert "Drop rows with missing price" in refused and "Load sales data" not in refused
    h.turns.prompt("p4")
    removed = text(await h.call("nh_undo", "p4", force=True))
    assert 'Removed "Drop rows with missing price"' in removed
    assert [c["source"] for c in code_cells(h)] == [LOAD["code"]]


async def test_conflict_at_start_of_an_edit_keeps_the_previous_version(open_harness) -> None:
    h = await open_harness(TypingBackend)
    h.turns.prompt("p1")
    await h.call("nh_add_cell", "p1", **LOAD)
    uid = code_cells(h)[0]["id"]
    h.turns.prompt("p2")
    h.backend.typed = "df = 'mine'"
    result = await h.call("nh_edit_cell", "p2", cell_id=uid, code="df = 2\ndf", intent="two")
    assert result.is_error and "nh: E141" in text(result)
    h.turns.prompt("p3")
    restored = text(await h.call("nh_undo", "p3", force=True))
    assert "Restored the previous version" in restored
    assert code_cells(h)[0]["source"] == LOAD["code"]


# V11: an edit whose run can't start leaves the cell as it was, outputs included
async def test_edit_rollback_keeps_outputs(open_harness) -> None:
    h = await open_harness(FailingStartBackend)
    h.turns.prompt("p1")
    await h.call("nh_add_cell", "p1", **LOAD)
    before = json.loads(json.dumps(code_cells(h)[0]))
    assert before["outputs"] and before["execution_count"] == 1
    h.backend.fail = True
    h.turns.prompt("p2")
    result = await h.call(
        "nh_edit_cell", "p2", cell_id=before["id"], code="df = 2\ndf", intent="two"
    )
    assert result.is_error and "nh: E134" in text(result)
    after = code_cells(h)[0]
    assert after["source"] == before["source"]
    assert after["outputs"] == before["outputs"]
    assert after["execution_count"] == 1


# V12: a refusal names the notebook that holds this message's cell
async def test_cross_notebook_refusal_names_the_other_notebook(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    result = await nh.call("nh_add_cell", "p1", **dict(DROP, notebook=OTHER))
    first = text(result).splitlines()[0]
    assert result.is_error and "nh: E110" in text(result)
    assert first.endswith(f'this message\'s cell is "Load sales data" [1] in {NOTEBOOK}.')
    assert "written earlier in this message" not in first


# V18 + V20: a cell the user deleted is "deleted", not "undone", and the reminder says so
async def test_already_deleted_wording_and_last_cell(nh: Harness) -> None:
    for prompt, cell in (("p1", LOAD), ("p2", DROP)):
        nh.turns.prompt(prompt)
        await nh.call("nh_add_cell", prompt, **cell)
    nh.backend.user_delete(NOTEBOOK, code_cells(nh)[1]["id"])
    nh.turns.prompt("p3")
    body = text(await nh.call("nh_undo", "p3"))
    assert "Nothing undone" in body
    assert "from the deleted" in body and "from the undone" not in body
    assert last_cell(nh)["status"] == "deleted"


# V19: undoing a title-only edit changes no code, so nothing is stale or drifting
async def test_undo_of_a_title_only_edit(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    nh.turns.prompt("p2")
    await nh.call("nh_add_cell", "p2", **DROP)
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p3")
    edited = await nh.call(
        "nh_edit_cell", "p3", cell_id=uid, code=LOAD["code"], title="Load the sales table"
    )
    assert not edited.is_error, text(edited)
    nh.turns.prompt("p4")
    body = text(await nh.call("nh_undo", "p4"))
    assert "Restored the previous title and notes" in body
    assert "Kernel ≠ notebook" not in body and "Now outdated" not in body
    assert code_cells(nh)[0]["execution_count"] is not None
    stale = json.loads((nh.project / ".nh" / "state" / "stale.json").read_text() or "{}")
    assert not (stale.get(NOTEBOOK) or {}).get("cells")


# V21: the error first line is a full sentence
async def test_error_first_line_ends_with_a_full_stop(nh: Harness) -> None:
    nh.turns.prompt("p1")
    first = text(await nh.call("nh_add_cell", "p1", **FAILING)).splitlines()[0]
    assert "it failed with NameError" in first and first.endswith(".")


# V22: the "Note unchanged" notice quotes the bullet as written, and not on a retry
async def test_note_unchanged_notice(nh: Harness) -> None:
    load = dict(
        LOAD,
        notes=["Prices above $5,000 are data-entry errors (~3% of rows).", "Keeps the rest."],
    )
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **load)
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    body = text(await nh.call("nh_edit_cell", "p2", cell_id=uid, code="df = 1\ndf", intent="one"))
    assert 'Note unchanged ("Prices above $5,000 are data-entry errors (~3% of rows).")' in body

    nh.turns.prompt("p3")
    await nh.call("nh_add_cell", "p3", **dict(DROP, code="x = undefined_name"))
    failed = code_cells(nh)[1]["id"]
    retry = text(await nh.call("nh_edit_cell", "p3", cell_id=failed, code="x = 1\nx"))
    assert "retry 1 of 2" in retry and "Note unchanged" not in retry


async def test_outline_shows_note_titles_unescaped(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, title="Prices above $50"))
    outline = text(await nh.call("nh_inspect", "p1", view="outline"))
    assert "### Prices above $50" in outline and "\\$" not in outline


# V26: an untitled cell is named with backticks, not nested quotes
async def test_untitled_cell_label_uses_backticks(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, 'df.groupby("region")["price"].mean()')
    nh.turns.prompt("p1")
    first = text(await nh.call("nh_edit_cell", "p1", cell_id=human, code="1")).splitlines()[0]
    assert first.startswith('Not written: the cell `df.groupby("region")["price"].mean()`')


# V30: a cell the user stopped is not "already ran OK"
async def test_interrupted_cell_refusals(open_harness) -> None:
    h = await open_harness(toml="[exec]\nsoft_timeout_s = 5\n", exec_delay_s=3)
    h.turns.prompt("p1")
    call = asyncio.create_task(h.call("nh_add_cell", "p1", **LOAD))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if h.backend.user_interrupt():
            break
    body = text(await call)
    assert "the user stopped it" in body
    uid = code_cells(h)[0]["id"]
    edit = text(await h.call("nh_edit_cell", "p1", cell_id=uid, code="df = 1\ndf"))
    assert "nh: E117" in edit and edit.startswith("Not written:") and "ran OK" not in edit
    rerun = text(await h.call("nh_run", "p1", cell_id=uid))
    assert "nh: E117" in rerun and rerun.startswith("Not run:")
