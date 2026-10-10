"""Round-3 fixes through the real gateway (W2, W4, W7-W12, W16)."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp import Client

from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from tests.fakes.turns import Turns, text
from tests.gateway import test_r2_writeundo as r2
from tests.gateway.conftest import NOTEBOOK, Harness, make_project
from tests.gateway.test_gateway import DROP, LOAD
from tests.gateway.test_r2_writeundo import OTHER, TypingBackend, code_cells, sha_of

open_harness = r2.open_harness  # the fixture, shared


# W2 + W7: after undos, nh compares with what the undo restored, not with its undone code
async def test_stale_sha_after_two_undos_is_accepted(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, "mine = 1\nmine")
    nh.turns.prompt("p1")
    sha1 = sha_of(text(await nh.call("nh_inspect", "p1", view="cell", cell_id=human)))
    await nh.call(
        "nh_edit_cell", "p1", cell_id=human, code="mine = 2\nmine", base_sha=sha1, intent="2"
    )
    nh.turns.prompt("p2")
    sha2 = sha_of(text(await nh.call("nh_inspect", "p2", view="cell", cell_id=human)))
    await nh.call(
        "nh_edit_cell", "p2", cell_id=human, code="mine = 3\nmine", base_sha=sha2, intent="3"
    )
    for prompt in ("p3", "p4"):
        nh.turns.prompt(prompt)
        await nh.call("nh_undo", prompt, cell_id=human)
    assert code_cells(nh)[0]["source"] == "mine = 1\nmine"
    nh.turns.prompt("p5")
    result = await nh.call(
        "nh_edit_cell", "p5", cell_id=human, code="mine = 4\nmine", base_sha=sha2, intent="4"
    )
    assert not result.is_error, text(result)


async def test_user_change_after_an_undo_diffs_against_the_restored_code(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    await nh.call("nh_edit_cell", "p2", cell_id=uid, code="df = 2\ndf", intent="two")
    nh.turns.prompt("p3")
    await nh.call("nh_undo", "p3")
    nh.backend.user_edit(NOTEBOOK, uid, LOAD["code"].replace("df.shape", "df.head()"))
    nh.turns.prompt("p4")
    body = text(await nh.call("nh_edit_cell", "p4", cell_id=uid, code="df = 5\ndf", intent="five"))
    assert "nh: E141" in body
    assert "-df.shape" in body and "+df.head()" in body and "df = 2" not in body


# W4: a queued cell is called queued, not running
async def test_write_while_queued_says_queued(open_harness) -> None:
    h = await open_harness(toml="[exec]\nsoft_timeout_s = 1\n", kernel_busy=True)
    h.turns.prompt("p1")
    first = text(await h.call("nh_add_cell", "p1", **LOAD))
    assert "queued" in first.splitlines()[0]
    h.turns.prompt("p2")
    refused = text(await h.call("nh_add_cell", "p2", **DROP))
    assert refused.startswith('The kernel is busy with another cell; "Load sales data" is queued')
    assert "has not started" in refused and "still running" not in refused
    h.backend.kernel_busy = False


# W8: the deleted cell's own older step is not "the step before"
async def test_deleted_after_edit_offers_the_real_previous_step(nh: Harness) -> None:
    for prompt, cell in (("p1", LOAD), ("p2", DROP)):
        nh.turns.prompt(prompt)
        await nh.call("nh_add_cell", prompt, **cell)
    drop_uid = code_cells(nh)[1]["id"]
    nh.turns.prompt("p3")
    await nh.call("nh_edit_cell", "p3", cell_id=drop_uid, code="df_clean = df.dropna()\ndf_clean")
    nh.backend.user_delete(NOTEBOOK, drop_uid)
    nh.turns.prompt("p4")
    offer = text(await nh.call("nh_undo", "p4"))
    assert 'Ask whether to undo "Load sales data" [1] next' in offer
    nh.turns.prompt("p5")
    assert 'Removed "Load sales data"' in text(await nh.call("nh_undo", "p5"))


# W9: untitled cells read naturally in the leftovers line and the "already deleted" result
async def test_untitled_cell_wording_in_undo(nh: Harness) -> None:
    human = nh.backend.user_insert(NOTEBOOK, 0, "mine = 1\nmine")
    nh.turns.prompt("p1")
    sha = sha_of(text(await nh.call("nh_inspect", "p1", view="cell", cell_id=human)))
    await nh.call(
        "nh_edit_cell", "p1", cell_id=human, code="mine = 2\nmine", base_sha=sha, intent="2"
    )
    nh.turns.prompt("p2")
    body = text(await nh.call("nh_undo", "p2", cell_id=human))
    assert "from the undone cell `mine = 2​" not in body  # sanity: no stray characters
    assert "the undone the cell" not in body and "from the undone cell `" in body

    nh.turns.prompt("p3")
    sha = sha_of(text(await nh.call("nh_inspect", "p3", view="cell", cell_id=human)))
    await nh.call(
        "nh_edit_cell", "p3", cell_id=human, code="mine = 5\nmine", base_sha=sha, intent="5"
    )
    nh.backend.user_delete(NOTEBOOK, human)
    nh.turns.prompt("p4")
    gone = text(await nh.call("nh_undo", "p4"))
    assert "Nothing undone: the cell `mine = 5` was already deleted" in gone
    assert "nh's last cell" not in gone


# W10: nothing is "restored" when the user's rewritten note was all the edit changed
async def test_title_only_undo_with_a_user_note(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    await nh.call("nh_edit_cell", "p2", cell_id=uid, code=LOAD["code"], title="Load the table")
    note = next(c for c in nh.cells() if c["id"] == f"{uid}-n")
    nh.backend.user_edit(NOTEBOOK, note["id"], "### My own title\n\n- mine\n- also mine")
    nh.turns.prompt("p3")
    body = text(await nh.call("nh_undo", "p3"))
    assert "Nothing to restore" in body and "Restored" not in body


# W11: saying yes to an offer made in another notebook works after a gateway restart
async def test_offer_in_another_notebook_survives_a_restart(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    backend = FakeBackend(project)
    turns = Turns(project, tmp_path / "data")

    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, turns)
        for prompt, cell in (("p1", LOAD), ("p2", DROP)):
            h.turns.prompt(prompt)
            await h.call("nh_add_cell", prompt, **dict(cell, notebook=OTHER))
        backend.user_delete(OTHER, code_cells(h, OTHER)[1]["id"])
        h.turns.prompt("p3")
        offer = text(await h.call("nh_undo", "p3", notebook=OTHER))
        assert f"in {OTHER} was already deleted" in offer

    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, turns)
        h.turns.prompt("p4")
        body = text(await h.call("nh_undo", "p4"))
        assert 'Removed "Load sales data"' in body and code_cells(h, OTHER) == []


# W12: after the user typed into nh's cell, nh asks before running it
async def test_conflicted_cell_is_not_run_unasked(open_harness) -> None:
    h = await open_harness(TypingBackend)
    h.turns.prompt("p1")
    h.backend.typed = "boom = 'user text'"
    await h.call("nh_add_cell", "p1", **LOAD)
    uid = code_cells(h)[0]["id"]
    body = text(await h.call("nh_run", "p1", cell_id=uid))
    assert "nh: E118" in body and body.startswith("Not run: the user typed into")
    assert "user text" not in json.dumps(code_cells(h)[0].get("outputs"))


# W16: bools read True/False in the self-check
async def test_bools_read_true_and_false(nh: Harness) -> None:
    nh.turns.prompt("p1")
    body = text(await nh.call("nh_add_cell", "p1", **dict(LOAD, code="USE_LOG = False\nUSE_LOG")))
    assert "USE_LOG: new bool False" in body


# design §6.3: the approved batch's next blocks, through the gateway
def _next(result) -> str:
    return text(result).split("--- next ---\n", 1)[1]


async def test_a_batchs_next_blocks_go_on_then_close(nh: Harness) -> None:
    nh.turns.prompt("p1", text="run the next 2")
    nh.turns.prompt("p2", text="go")
    first = await nh.call("nh_add_cell", "p2", **LOAD)
    assert _next(first) == (
        'Step 1 of 2 of the approved batch ran OK. Give the user a short report on "Load sales '
        'data" [1]: what it did and the real numbers, surprises first, named by title and [n]. '
        "Then write the batch's next step (step 2 of 2) with nh_add_cell, without waiting for "
        "the user."
    )
    last = await nh.call("nh_add_cell", "p2", **DROP)
    assert _next(last).startswith(
        'That was step 2 of 2, the last of the approved batch. Reply to the user about "Drop '
        'rows with missing price" [2]: (1) what the cell does;'
    )
    assert _next(last).endswith("Do not write another cell.")


async def test_a_batch_after_go_counts_its_own_steps(nh: Harness) -> None:
    """V14's order: /nh:plan, "go" (plan step 1, one cell), then "run the next 3" and "yes". The
    batch's first cell is plan step 2, and nh counts it as the batch's step 1 of 3, so its next
    block names the batch's next step, never a plan step already written (C6c review)."""
    nh.turns.prompt("p1", text="/nh:plan clean the sales data")
    nh.turns.prompt("p2", text="go")
    first = await nh.call("nh_add_cell", "p2", **LOAD)
    assert " turn=1/1 " in text(first)
    nh.turns.prompt("p3", text="run the next 3")
    nh.turns.prompt("p4", text="yes")
    step = await nh.call("nh_add_cell", "p4", **DROP)
    assert " turn=1/3 batch " in text(step)
    assert _next(step) == (
        'Step 1 of 3 of the approved batch ran OK. Give the user a short report on "Drop rows '
        'with missing price" [2]: what it did and the real numbers, surprises first, named by '
        "title and [n]. Then write the batch's next step (step 2 of 3) with nh_add_cell, "
        "without waiting for the user."
    )


async def test_a_batchs_error_block_says_no_retry(nh: Harness) -> None:
    nh.turns.prompt("p1", text="run steps 2-4")
    nh.turns.prompt("p2", text="yes")
    failed = await nh.call("nh_add_cell", "p2", **dict(LOAD, code="df = pd.read_csv(MISSING)"))
    assert failed.meta["nh/status"] == "error"
    assert _next(failed) == (
        'The approved batch stops at step 1 of 3: "Load sales data" [1] failed. Don\'t fix it '
        "in this message: a batch has no retries. Explain in plain words: quote the failing "
        "code, what Python said, the likely cause and one fix; say which planned steps did not "
        "run; then wait."
    )
    assert "retries left" not in _next(failed)
