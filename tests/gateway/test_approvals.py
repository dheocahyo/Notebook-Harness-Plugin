"""FR-12 cell approvals (design §6.4): a cell the lint asks about (L009, L012 and L013 by
default) waits for the user's yes (E122), and the yes in the next message lets exactly that call
through once; a host in the project's approved list needs none. Through the real hooks and the
gateway (FakeBackend)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from nh_gateway import config
from nh_gateway._shared import turn_record
from nh_gateway._shared.paths import Layout, atomic_write_json
from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from nh_gateway.lint.lint import Issue
from nh_gateway.policy import turn as policy_turn
from nh_gateway.policy.errors import CATALOGUE, RETURN_TO_WORKFLOW, WRITER_LINE, NhError
from nh_gateway.policy.turn import TurnState
from nh_gateway.tools import approvals, batch
from tests.fakes.net import serving
from tests.fakes.turns import Turns, text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project

OTHER = "notebooks/other.ipynb"

SESSION = "sess-1"
LOAD = dict(
    title="Load sales data",
    notes=[
        "Builds a small frame of prices by region.",
        "Keeps the missing price so later steps can drop it.",
    ],
    intent="load the sales data",
    code="import pandas as pd\n\ndf = pd.DataFrame({'region': ['a', 'b', 'a'], 'price': [1.0, None, 3.0]})\ndf.shape",
)
INSTALL = dict(
    title="Install seaborn for the plots",
    notes=["Installs seaborn into the kernel.", "The next plots use its styles."],
    intent="install seaborn",
    code="%pip install seaborn\nsorted(['b', 'a'])",
)
PLOTLY = dict(INSTALL, title="Install plotly", code="%pip install plotly\nsorted(['d', 'c'])")
FIRST = "Not written: this needs the user's yes first."
QUESTION = (
    "This cell installs `seaborn` into the kernel only, and the next env sync removes it "
    "(`uv add seaborn` keeps it). Run it as it is?"
)
FINDING = "- L009: The cell installs `seaborn` into the kernel only (`%pip install seaborn`)."
ASK_NEXT = f"Next: Ask the user, then stop: '{QUESTION}'. After a yes, send the same call again."
ASKED = [FIRST, "nh: E122", FINDING, ASK_NEXT]
WRITER_ASKED = [
    FIRST,
    "nh: E122",
    FINDING,
    f"- The main conversation asks the user: '{QUESTION}'",
    f"Next: {RETURN_TO_WORKFLOW}",
]
HELD = [
    "- The user's yes in this message is for the other cell nh asked about, and it covers only "
    "that exact call.",
    "Next: Send the call the user said yes to first, exactly as before (same tool, cell and "
    "code); propose this cell in your reply instead.",
]
YES_ANSWERS = ("yes", "go")
ANSWERS = ("yes", "no", "what would that change?", "go", "go on")


def nh_code_cells(h: Harness) -> list[dict]:
    return [
        c
        for c in h.cells()
        if c["cell_type"] == "code" and c.get("metadata", {}).get("nh", {}).get("role") == "code"
    ]


def ledger(h: Harness) -> dict[str, Any]:
    path = Layout(h.project).ledger_file(SESSION)
    return json.loads(path.read_text()) if path.exists() else {}


def pending(h: Harness) -> dict[str, Any] | None:
    return ledger(h).get("pending")


def events(h: Harness, name: str) -> list[dict[str, Any]]:
    path = Layout(h.project).log_file
    lines = path.read_text().splitlines() if path.exists() else []
    return [e for e in map(json.loads, lines) if e["event"] == name]


def install_key(code: str = INSTALL["code"]) -> str:
    return approvals.cell_key(NOTEBOOK, "add", code)


def lines(result: Any) -> list[str]:
    return text(result).splitlines()


def assert_asked(result: Any, writer: bool = False) -> None:
    assert result.is_error and lines(result) == (WRITER_ASKED if writer else ASKED), text(result)


def assert_written(result: Any) -> None:
    body = text(result)
    assert not result.is_error and "nh: cell=" in body and "nh: E" not in body, body


async def add(h: Harness, who: str, turn: str, run: str, **cell: Any) -> Any:
    """``nh_add_cell`` from the main conversation, or from nh:cell-writer in run ``run`` (which
    is launched in ``turn`` first if it is new)."""
    args = cell or INSTALL
    if who == "main":
        return await h.call("nh_add_cell", turn, **args)
    folder = h.turns.transcript_dir(run)
    if not folder.exists():
        h.turns.workflow_launched(turn, run, tool_use_id=f"toolu_{run}", task_id=f"task-{run}")
    return await h.turns.writer_call(h.client, f"w-{run}", run, "nh_add_cell", args, turn)


def test_e122_texts_are_pinned() -> None:
    assert CATALOGUE["E122"] == (
        "Not {verb}: this needs the user's yes first.",
        "Ask the user nh's question, then stop. After a yes, send the same call again.",
    )
    assert approvals.ASK_NEXT == (
        "Ask the user, then stop: '{question}'. After a yes, send the same call again."
    )
    assert "already waiting for the user's answer" in approvals.WAITING_LINE
    assert approvals.HEADLESS_NEXT.startswith("No one can answer here (NH_HEADLESS=1)")
    assert [approvals.HELD_LINE, f"Next: {approvals.HELD_NEXT}"] == HELD


def test_the_question_is_redacted_when_built() -> None:
    """NhError scrubs the whole refusal too; the question itself must already be clean."""
    leak = "git+https://user:s3cretPassw0rdXYZ@github.com/org/lib.git"
    issue = Issue("L009", "package_install", "ask", "m", "f", f"installs `{leak}` here")
    asked = approvals.question([issue])
    assert "s3cretPassw0rdXYZ" not in asked and "[redacted:" in asked, asked
    assert asked.startswith("This cell installs `git+https://") and asked.endswith("as it is?")


def test_an_ask_without_a_clause_still_reads_as_a_sentence() -> None:
    """Only a hand-built Config gets a non-ask rule to ask (config.ASK_RULES, design §6.4)."""
    issue = Issue("L008", "notebook_write", "ask", "The code writes a notebook file (`x`).", "f")
    assert approvals.question([issue]) == (
        "This cell trips nh's rule L008 (The code writes a notebook file (`x`)). Run it as it is?"
    )


# --- the matrix: answer x when x who ---------------------------------------------------------


@pytest.mark.parametrize("who", ["main", "writer"])
@pytest.mark.parametrize("when", ["same message", "next message", "two messages later"])
@pytest.mark.parametrize("answer", ANSWERS)
async def test_only_a_yes_in_the_next_message_grants_the_cell(
    nh: Harness, answer: str, when: str, who: str
) -> None:
    nh.turns.prompt("p1", text="install seaborn so we can style the plots")
    assert_asked(await add(nh, who, "p1", "wf_run-1"), writer=who == "writer")
    asked = pending(nh)
    assert asked is not None and asked["turn_id"] == "p1" and asked["key"] == install_key()
    if who == "writer" and when != "same message":
        # The user sees the writer's question with the run's report, so the answer comes after
        # it (C5d2: a yes typed before it grants nothing; tested below). In the same message
        # the run is still going, so its writer can call again.
        reported(nh, "wf_run-1")
    if when == "same message":  # typed while Claude works: absorbed, never an answer (6.1)
        nh.turns.prompt("p1", text=answer)
        turn, run = "p1", "wf_run-1"
    elif when == "next message":
        nh.turns.prompt("p2", text=answer)
        turn, run = "p2", "wf_run-2"
    else:
        nh.turns.prompt("p2", text=answer)
        nh.turns.prompt("p3", text=answer)
        turn, run = "p3", "wf_run-3"
    result = await add(nh, who, turn, run)
    if answer in YES_ANSWERS and when == "next message":
        assert_written(result)
        assert "into the kernel only" not in text(result)  # an ask is never shown as a hint
        assert [c["source"] for c in nh_code_cells(nh)] == [INSTALL["code"]]
        assert pending(nh) is None  # used once
        assert [e["turn_id"] for e in events(nh, "cell_granted")] == [turn]
        [added] = events(nh, "cell_added")
        assert "L009" not in added["hints"]  # an ask is never logged as a hint
    else:
        assert_asked(result, writer=who == "writer")
        assert not nh_code_cells(nh)
        now = pending(nh)
        assert now is not None and now["key"] == install_key() and now["turn_id"] == turn


@pytest.mark.parametrize(("asker", "retrier"), [("writer", "main"), ("main", "writer")])
async def test_a_yes_grants_the_same_call_from_either_thread(
    nh: Harness, asker: str, retrier: str
) -> None:
    """nh:cell-writer can't ask the user: its question is recorded for the main conversation,
    whose identical call in the yes message is granted (design §6.4, Writers)."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await add(nh, asker, "p1", "wf_run-1"), writer=asker == "writer")
    if asker == "writer":  # the user sees its question with the run's report (C5d2)
        reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await add(nh, retrier, "p2", "wf_run-2"))
    [cell] = nh_code_cells(nh)
    assert cell["metadata"]["nh"]["turn_id"] == "p2"
    assert pending(nh) is None


async def test_a_yes_typed_mid_turn_never_answers_this_messages_question(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await add(nh, "main", "p1", "r"))
    nh.turns.prompt("p1", text="yes")  # absorbed into p1
    assert_asked(await add(nh, "main", "p1", "r"))
    # Its answer was never recorded, so the next message's "go on" is no yes either.
    nh.turns.prompt("p2", text="go on")
    assert_asked(await add(nh, "main", "p2", "r"))
    assert not nh_code_cells(nh)


# --- one question at a time -------------------------------------------------------------------


async def test_the_first_ask_of_a_message_wins(nh: Harness) -> None:
    nh.turns.prompt("p1", text="set up the plotting libraries")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    waiting = await nh.call("nh_add_cell", "p1", **PLOTLY)
    assert waiting.is_error and lines(waiting) == [
        FIRST,
        "nh: E122",
        "- L009: The cell installs `plotly` into the kernel only (`%pip install plotly`).",
        "- nh is already waiting for the user's answer to this message's first question; it "
        "asks one at a time.",
        "Next: Ask the user only nh's first question of this message, then stop; write nothing "
        "more until they answer.",
    ]
    assert QUESTION not in text(waiting) and "plotly` keeps" not in text(waiting)
    assert pending(nh)["key"] == install_key()  # the first question stays
    nh.turns.prompt("p2", text="yes")
    # The yes answered the seaborn question, not plotly's: plotly is held, the yes kept.
    plotly = await nh.call("nh_add_cell", "p2", **PLOTLY)
    assert plotly.is_error and lines(plotly)[-2:] == HELD, text(plotly)
    assert pending(nh)["key"] == install_key() and pending(nh)["turn_id"] == "p1"
    assert not nh_code_cells(nh)
    nh.turns.prompt("p3", text="go on")  # the yes was never used: the next message drops it
    plotly = await nh.call("nh_add_cell", "p3", **PLOTLY)
    assert plotly.is_error and "This cell installs `plotly` into the kernel only" in text(plotly)
    assert pending(nh)["key"] == install_key(PLOTLY["code"])
    assert not nh_code_cells(nh)


@pytest.mark.parametrize("who", ["main", "writer"])
async def test_a_different_ask_in_the_yes_message_keeps_the_yes(nh: Harness, who: str) -> None:
    """The yes is for the call the user saw: another asked-for call in the yes message doesn't
    replace it (design §6.4, Held), so the approved call still goes through after it."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="yes")
    pinned = dict(INSTALL, code="%pip install seaborn==0.13\nsorted(['b', 'a'])")
    held = await add(nh, who, "p2", "wf_run-2", **pinned)
    assert held.is_error and "nh: E122" in text(held)
    if who == "main":
        assert lines(held)[-2:] == HELD
    else:
        assert lines(held)[-2:] == [HELD[0], f"Next: {RETURN_TO_WORKFLOW}"]
    assert "Run it as it is" not in text(held)  # nothing new to ask
    assert pending(nh)["key"] == install_key() and pending(nh)["turn_id"] == "p1"
    assert_written(await add(nh, who, "p2", "wf_run-2"))  # the approved call, same thread
    assert pending(nh) is None
    again = await add(nh, who, "p2", "wf_run-2", **pinned)
    assert again.is_error and "nh: E122" not in text(again)  # the message has its cell now
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "held"]


async def test_the_second_question_is_refused_and_the_first_still_granted(nh: Harness) -> None:
    nh.turns.prompt("p1", text="set up the plotting libraries")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    assert "already waiting" in text(await nh.call("nh_add_cell", "p1", **PLOTLY))
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "waiting"]


async def test_the_same_call_again_in_the_asking_message_repeats_the_question(
    nh: Harness,
) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    for _ in range(4):  # never a lint reject: no E121 however often it asks
        assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    first = pending(nh)
    assert first is not None and first["turn_id"] == "p1"
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked"] + ["repeated"] * 3
    assert ledger(nh)["turns"]["p1"]["lint_rejects"] == 0


async def test_a_cell_written_in_the_asking_message_clears_its_question(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data, and install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    assert_written(await nh.call("nh_add_cell", "p1", **LOAD))
    assert pending(nh) is None
    nh.turns.prompt("p2", text="yes")  # the question was superseded: nothing to grant
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert pending(nh)["turn_id"] == "p2"


async def test_an_edit_written_in_the_asking_message_clears_its_question(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data")
    failed = await nh.call("nh_add_cell", "p1", **dict(LOAD, code="undefined_name"))
    uid = failed.meta["nh/cell_id"]
    with_install = "%pip install seaborn\n" + LOAD["code"]
    asked = await nh.call("nh_edit_cell", "p1", cell_id=uid, code=with_install)
    assert asked.is_error and "nh: E122" in text(asked)
    assert pending(nh)["key"] == approvals.cell_key(NOTEBOOK, f"edit:{uid}", with_install)
    assert not (await nh.call("nh_edit_cell", "p1", cell_id=uid, code=LOAD["code"])).is_error
    assert pending(nh) is None


async def test_a_write_that_fails_to_start_keeps_the_question(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a write whose run started supersedes the question (design §6.4, Clearing)."""
    nh.turns.prompt("p1", text="load the data, and install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    real = nh.backend.start_execution

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise NhError("E130")

    monkeypatch.setattr(nh.backend, "start_execution", down)
    failed = await nh.call("nh_add_cell", "p1", **LOAD)
    assert failed.is_error and "nh: E130" in text(failed), text(failed)
    assert not nh_code_cells(nh)  # rolled back
    assert pending(nh)["turn_id"] == "p1" and pending(nh)["key"] == install_key()
    monkeypatch.setattr(nh.backend, "start_execution", real)
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))


async def test_an_edit_that_fails_to_start_keeps_the_question(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit path clears the question only once its run started, as the add path does."""
    nh.turns.prompt("p1", text="load the data")
    failed = await nh.call("nh_add_cell", "p1", **dict(LOAD, code="undefined_name"))
    uid = failed.meta["nh/cell_id"]
    with_install = "%pip install seaborn\n" + LOAD["code"]
    asked = await nh.call("nh_edit_cell", "p1", cell_id=uid, code=with_install)
    assert asked.is_error and "nh: E122" in text(asked)
    real = nh.backend.start_execution

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise NhError("E130")

    monkeypatch.setattr(nh.backend, "start_execution", down)
    edited = await nh.call("nh_edit_cell", "p1", cell_id=uid, code=LOAD["code"])
    assert edited.is_error and "nh: E130" in text(edited), text(edited)
    assert [c["source"] for c in nh_code_cells(nh)] == ["undefined_name"]  # rolled back
    key = approvals.cell_key(NOTEBOOK, f"edit:{uid}", with_install)
    assert pending(nh)["turn_id"] == "p1" and pending(nh)["key"] == key
    monkeypatch.setattr(nh.backend, "start_execution", real)
    nh.turns.prompt("p2", text="yes")
    granted = await nh.call("nh_edit_cell", "p2", cell_id=uid, code=with_install)
    assert not granted.is_error and "Updated" in text(granted), text(granted)


# --- single use ------------------------------------------------------------------------------


async def test_a_grant_is_used_once(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    again = await nh.call("nh_add_cell", "p2", **dict(INSTALL, title="Install seaborn again"))
    assert again.is_error and "nh: E110" in text(again)  # the yes message's one cell
    nh.turns.prompt("p3", text="yes")  # nothing was asked in p2: no grant
    assert_asked(await nh.call("nh_add_cell", "p3", **INSTALL))
    assert len(nh_code_cells(nh)) == 1


async def test_a_retry_that_keeps_the_install_asks_again(nh: Harness) -> None:
    broken = dict(INSTALL, code="%pip install seaborn\nnever = 1 / 0")
    nh.turns.prompt("p1", text="install seaborn")
    asked = await nh.call("nh_add_cell", "p1", **broken)
    assert asked.is_error and "nh: E122" in text(asked)
    nh.turns.prompt("p2", text="yes")
    failed = await nh.call("nh_add_cell", "p2", **broken)
    assert not failed.is_error and "Fix it with nh_edit_cell" in text(failed), text(failed)
    uid = failed.meta["nh/cell_id"]
    fixed = await nh.call("nh_edit_cell", "p2", cell_id=uid, code=INSTALL["code"])
    assert fixed.is_error and "nh: E122" in text(fixed)  # the grant is spent
    now = pending(nh)
    assert now["turn_id"] == "p2"
    assert now["key"] == approvals.cell_key(NOTEBOOK, f"edit:{uid}", INSTALL["code"])
    fine = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="sorted(['b', 'a'])")
    assert not fine.is_error, text(fine)  # without the install: written, clears the question
    assert pending(nh) is None


# --- the key: this exact cell ----------------------------------------------------------------


async def test_title_notes_intent_position_and_trailing_space_keep_the_key(nh: Harness) -> None:
    nh.turns.prompt("p0", text="load the data")
    loaded = await nh.call("nh_add_cell", "p0", **LOAD)
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="go")
    same = dict(
        title="Add seaborn to the kernel",
        notes=["Installs seaborn now, as the user approved.", "Plots below use it."],
        intent="yes, install seaborn",
        code="\n" + INSTALL["code"].replace("\n", "   \r\n") + "  \n\n",
        after_cell_id=loaded.meta["nh/cell_id"],
        notebook=NOTEBOOK,
    )
    assert_written(await nh.call("nh_add_cell", "p2", **same))


@pytest.mark.parametrize(
    "code",
    [
        "%pip install seaborn==0.13\nsorted(['b', 'a'])",
        "%pip install seaborn\nsorted(['b', 'a'])  # sorted",
        "%pip install seaborn\nsorted(['b', 'a'])\nlen('x')",
        "%pip   install seaborn\nsorted(['b', 'a'])",
    ],
)
async def test_any_other_code_change_is_a_new_question(nh: Harness, code: str) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="yes")
    changed = await nh.call("nh_add_cell", "p2", **dict(INSTALL, code=code))
    assert changed.is_error and "nh: E122" in text(changed), text(changed)
    assert lines(changed)[-2:] == HELD  # not granted; the yes stays for the call it approved
    assert pending(nh)["turn_id"] == "p1" and pending(nh)["key"] == install_key()
    nh.turns.prompt("p3", text="go on")  # no yes: the changed cell asks its own question
    asked = await nh.call("nh_add_cell", "p3", **dict(INSTALL, code=code))
    assert asked.is_error and "Next: Ask the user, then stop:" in text(asked), text(asked)
    now = pending(nh)
    assert now["turn_id"] == "p3" and now["key"] == install_key(code)
    assert not nh_code_cells(nh)


def test_the_key_hashes_notebook_target_and_normalised_code() -> None:
    code = INSTALL["code"]
    key = approvals.cell_key(NOTEBOOK, "add", code)
    assert key == approvals.cell_key(NOTEBOOK, "add", f"\n\n{code}  \n\n")
    assert key == approvals.cell_key(NOTEBOOK, "add", code.replace("\n", "\r\n"))
    assert key != approvals.cell_key("notebooks/other.ipynb", "add", code)
    assert key != approvals.cell_key(NOTEBOOK, "edit:nh-0001", code)
    assert key != approvals.cell_key(NOTEBOOK, "add", "  " + code)  # indentation counts
    assert key != approvals.cell_key(NOTEBOOK, "add", code.replace("\n", "\n\n"))
    assert len(key) == 64 and code not in key


async def test_an_edit_question_is_keyed_to_its_cell(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.turns.prompt("p2", text="install seaborn in the load cell")
    edit = dict(cell_id=uid, code=INSTALL["code"] + "\n" + LOAD["code"])
    asked = await nh.call("nh_edit_cell", "p2", **edit)
    assert asked.is_error and "nh: E122" in text(asked) and QUESTION in text(asked)
    assert pending(nh)["key"] == approvals.cell_key(NOTEBOOK, f"edit:{uid}", edit["code"])
    nh.turns.prompt("p3", text="yes")
    as_add = await nh.call("nh_add_cell", "p3", **dict(INSTALL, code=edit["code"]))
    assert as_add.is_error and "nh: E122" in text(as_add)  # another cell: not granted
    assert [c["source"] for c in nh_code_cells(nh)] == [LOAD["code"]]


async def test_a_granted_edit_changes_the_cell(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.turns.prompt("p2", text="install seaborn in the load cell")
    code = INSTALL["code"] + "\n" + LOAD["code"]
    assert "nh: E122" in text(await nh.call("nh_edit_cell", "p2", cell_id=uid, code=code))
    nh.turns.prompt("p3", text="yes")
    edited = await nh.call("nh_edit_cell", "p3", cell_id=uid, code=code, title="Load and style")
    assert not edited.is_error and "Updated" in text(edited), text(edited)
    assert [c["source"] for c in nh_code_cells(nh)] == [code]
    assert pending(nh) is None


# --- headless --------------------------------------------------------------------------------

HEADLESS_NEXT = (
    "Next: No one can answer here (NH_HEADLESS=1): write nothing, and tell the user this cell "
    f"needs their yes in an interactive session: '{QUESTION}'"
)


@pytest.mark.parametrize("who", ["main", "writer"])
async def test_headless_fails_closed(
    nh: Harness, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    monkeypatch.setenv("NH_HEADLESS", "1")
    nh.turns.prompt("p1", text="install seaborn")
    refused = await add(nh, who, "p1", "wf_run-1")
    if who == "main":
        assert lines(refused) == [FIRST, "nh: E122", FINDING, HEADLESS_NEXT]
    else:
        assert lines(refused) == [
            FIRST,
            "nh: E122",
            FINDING,
            "- No one can answer here (NH_HEADLESS=1); the cell needs the user's yes in an "
            f"interactive session: '{QUESTION}'",
            f"Next: {RETURN_TO_WORKFLOW}",
        ]
    assert pending(nh) is None  # nothing is recorded: no yes can arrive
    nh.turns.prompt("p2", text="yes")
    again = await add(nh, who, "p2", "wf_run-2")
    assert again.is_error and "NH_HEADLESS=1" in text(again)
    assert not nh_code_cells(nh)
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["headless", "headless"]


async def test_headless_grants_nothing_even_with_a_yes_record(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))  # asked while interactive
    nh.turns.prompt("p2", text="yes")
    monkeypatch.setenv("NH_HEADLESS", "1")
    refused = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert lines(refused) == [FIRST, "nh: E122", FINDING, HEADLESS_NEXT]
    assert not nh_code_cells(nh)
    assert pending(nh)["turn_id"] == "p1"  # the ledger isn't touched while headless
    monkeypatch.delenv("NH_HEADLESS")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))


@contextlib.asynccontextmanager
async def process(tmp_path: Path, project: Path, backend: FakeBackend) -> AsyncIterator[Harness]:
    """One `claude -p` process of a session: a new gateway on the same project and the same
    hook data, as `claude -p --resume` starts one (design §6.4, "Spike V13")."""
    async with Client(create_server(project, backend)) as client:
        yield Harness(project, backend, client, Turns(project, tmp_path / "data"))


@pytest.mark.parametrize("first", ["headless", "interactive"])
async def test_headless_mixed_across_processes_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """NH_HEADLESS changed between a session's processes: nothing is written unasked either
    way. After a headless E122 nothing is recorded, so the first interactive yes gets nh's
    question; a headless yes to an interactive question is spent, so the next yes asks anew."""
    project = make_project(tmp_path)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    for prompt_id, words, headless in zip(
        ("m1", "m2", "m3"),
        ("install seaborn", "yes", "yes"),
        (first == "headless", first == "interactive", False),
        strict=True,
    ):
        if headless:
            monkeypatch.setenv("NH_HEADLESS", "1")
        else:
            monkeypatch.delenv("NH_HEADLESS", raising=False)
        async with process(tmp_path, project, backend) as h:
            h.turns.prompt(prompt_id, text=words)
            result = await h.call("nh_add_cell", prompt_id, **INSTALL)
            if headless:
                assert lines(result) == [FIRST, "nh: E122", FINDING, HEADLESS_NEXT]
            elif first == "headless" and prompt_id == "m3":
                assert_written(result)
            else:
                assert_asked(result)
    if first == "headless":
        assert [e["outcome"] for e in events(h, "cell_asked")] == ["headless", "asked"]
        assert [e["turn_id"] for e in events(h, "cell_granted")] == ["m3"]
        assert [c["source"] for c in nh_code_cells(h)] == [INSTALL["code"]]
        assert pending(h) is None
    else:
        assert [e["outcome"] for e in events(h, "cell_asked")] == ["asked", "headless", "asked"]
        assert events(h, "cell_granted") == [] and nh_code_cells(h) == []
        assert pending(h)["turn_id"] == "m3"


async def test_a_new_gateway_holds_a_call_whose_active_notebook_it_forgot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active notebook is one gateway's (design §6.4, Known gaps). Asked in a process whose
    active notebook was OTHER, the identical call with no `notebook` is another key in a new
    gateway (harness.toml's notebook): "held", every time, so the yes is lost and nothing is
    written. The call that names the notebook keeps the key and is granted."""
    project = make_project(tmp_path)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with process(tmp_path, project, backend) as h:
        h.turns.prompt("m0", text="load the data in the other notebook")
        assert_written(await h.call("nh_add_cell", "m0", **dict(LOAD, notebook=OTHER)))
        h.turns.prompt("m1", text="install seaborn")
        assert_asked(await h.call("nh_add_cell", "m1", **INSTALL))  # in OTHER, the active one
    assert pending(h)["key"] == approvals.cell_key(OTHER, "add", INSTALL["code"])
    async with process(tmp_path, project, backend) as h:
        h.turns.prompt("m2", text="yes")
        for _ in range(2):  # HELD's Next has the model send it again exactly as before
            held = await h.call("nh_add_cell", "m2", **INSTALL)
            assert held.is_error and lines(held)[-2:] == HELD, text(held)
        assert pending(h)["turn_id"] == "m1" and nh_code_cells(h) == []
        assert_written(await h.call("nh_add_cell", "m2", **dict(INSTALL, notebook=OTHER)))
    assert pending(h) is None
    sources = [c["source"] for c in backend.notebook(OTHER)["cells"] if c["cell_type"] == "code"]
    assert sources == [LOAD["code"], INSTALL["code"]]
    assert [e["outcome"] for e in events(h, "cell_asked")] == ["asked", "held", "held"]


# --- the ledger ------------------------------------------------------------------------------


async def test_a_v1_ledger_file_loads_and_the_flow_works(nh: Harness) -> None:
    old = TurnState(session_id=SESSION, prompt_id="p0", opened_at=time.time() - 60)
    # A v1 file has no pending question, whatever it holds: this one is never granted.
    stray = {"kind": "cell", "key": install_key(), "turn_id": "p1", "ts": time.time()}
    atomic_write_json(
        Layout(nh.project).ledger_file(SESSION),
        {"v": 1, "turns": {"p0": old.__dict__}, "pending": stray},
    )
    nh.turns.prompt("p1", text="install seaborn")
    nh.turns.prompt("p2", text="yes")
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    saved = ledger(nh)
    assert saved["v"] == 2 and "p0" in saved["turns"] and saved["pending"]["turn_id"] == "p2"
    nh.turns.prompt("p3", text="yes")
    assert_written(await nh.call("nh_add_cell", "p3", **INSTALL))


async def test_no_code_or_question_is_stored(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    assert "nh: E122" in text(await nh.call("nh_add_cell", "p1", **PLOTLY))
    stored = Layout(nh.project).ledger_file(SESSION).read_text()
    assert set(json.loads(stored)["pending"]) == {"kind", "key", "turn_id", "ts", "run_id"}
    assert json.loads(stored)["pending"]["run_id"] is None  # the main conversation's question
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    log = Layout(nh.project).log_file.read_text()
    for raw in (stored, Layout(nh.project).ledger_file(SESSION).read_text(), log):
        for word in ("seaborn", "plotly", "pip install", "Run it as it is", "env sync"):
            assert word not in raw, word
    assert install_key() in stored
    asked = events(nh, "cell_asked")
    assert asked and all(set(e) >= {"rules", "outcome"} and e["rules"] == ["L009"] for e in asked)


async def test_a_writers_question_stores_its_run_and_no_code_or_question(nh: Harness) -> None:
    """The pending question records the writer's run id (design §6.4, C5d2): ids and a hash,
    still no code, package or question text."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    stored = Layout(nh.project).ledger_file(SESSION).read_text()
    saved = json.loads(stored)["pending"]
    assert set(saved) == {"kind", "key", "turn_id", "ts", "run_id"}
    assert (saved["key"], saved["turn_id"], saved["run_id"]) == (install_key(), "p1", "wf_run-1")
    reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    layout = Layout(nh.project)
    files = (layout.ledger_file(SESSION), layout.log_file, layout.workflow_file(SESSION))
    for raw in (stored, *(path.read_text() for path in files)):
        for word in ("seaborn", "plotly", "pip install", "Run it as it is", "env sync"):
            assert word not in raw, word


# --- what comes first ------------------------------------------------------------------------


async def test_e110_still_holds_in_the_yes_message(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **LOAD))
    late = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert late.is_error and "nh: E110" in text(late)  # no third exception to E110
    assert len(nh_code_cells(nh)) == 1
    assert pending(nh)["turn_id"] == "p1"  # the grant wasn't used; the next message drops it
    nh.turns.prompt("p3", text="go on")
    assert_asked(await nh.call("nh_add_cell", "p3", **INSTALL))
    assert pending(nh)["turn_id"] == "p3"


@pytest.mark.parametrize("message", ["run the next 3", "explain the install", "/nh:plan a model"])
async def test_e109_comes_first_in_a_no_write_message(nh: Harness, message: str) -> None:
    nh.turns.prompt("p1", text=message)
    refused = await nh.call("nh_add_cell", "p1", **INSTALL)
    assert refused.is_error and "nh: E109" in text(refused) and "E122" not in text(refused)
    assert pending(nh) is None


async def test_lint_errors_come_before_the_question(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    refused = await nh.call("nh_add_cell", "p1", **dict(INSTALL, notes=["Only one bullet."]))
    assert refused.is_error and "nh: E120" in text(refused) and "L009" not in text(refused)
    assert pending(nh) is None


async def test_the_question_carries_the_calls_lead_lines(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data")
    assert_written(await nh.call("nh_add_cell", "p1", **LOAD))
    nh.backend.restart_kernel()
    nh.turns.prompt("p2", text="install seaborn")
    asked = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert asked.is_error and lines(asked)[0].startswith("NEW kernel: earlier variables are gone")
    assert lines(asked)[1:] == ASKED


async def test_a_writers_refusal_returns_to_the_workflow(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    refused = await add(nh, "writer", "p1", "wf_run-1")
    assert lines(refused)[-1] == f"Next: {RETURN_TO_WORKFLOW}" and WRITER_LINE not in text(refused)
    assert pending(nh)["key"] == install_key()  # recorded for the main conversation


# --- the levels, through the gateway ---------------------------------------------------------


@pytest.mark.parametrize(
    ("toml", "outcome"),
    [
        ("", "E122"),
        ('[lint]\nmode = "strict"\n', "E122"),
        ('[lint.rules]\npackage_install = "error"\n', "E120"),
        ('[lint.rules]\npackage_install = "hint"\n', "written"),
        ('[lint.rules]\npackage_install = "off"\n', "written"),
        ('[lint.rules]\npackage_install = "maybe"\n', "E122"),  # falls back to the default
    ],
)
async def test_the_rule_level_decides_what_an_install_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, outcome: str
) -> None:
    project = make_project(tmp_path, toml)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="install seaborn")
        result = await h.call("nh_add_cell", "p1", **INSTALL)
    body = text(result)
    if outcome == "written":
        assert_written(result)
        hinted = "The cell installs `seaborn` into the kernel only" in body
        assert hinted == ("hint" in toml), body  # a hint after the run; off says nothing
    else:
        assert result.is_error and f"nh: {outcome}" in body, body
    if outcome == "E120":
        assert "Don't call again yet: ask the user whether to install the package" in body


SECRET_INSTALL = dict(
    INSTALL,
    code="%pip install git+https://user:s3cretPassw0rdXYZ@github.com/org/lib.git "
    "git+https://ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8@github.com/org/other.git\n"
    "sorted(['b', 'a'])",
)
LEAKS = ("s3cretPassw0rdXYZ", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")


@pytest.mark.parametrize("who", ["main", "writer"])
async def test_no_secret_reaches_the_question(nh: Harness, who: str) -> None:
    nh.turns.prompt("p1", text="install our lib")
    asked = await add(nh, who, "p1", "wf_run-1", **SECRET_INSTALL)
    body = text(asked)
    assert asked.is_error and "nh: E122" in body and "This cell installs" in body, body
    assert "[redacted:" in body and not any(leak in body for leak in LEAKS), body


async def test_no_secret_reaches_the_hint_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path, '[lint.rules]\npackage_install = "hint"\n')
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="install our lib")
        result = await h.call("nh_add_cell", "p1", **SECRET_INSTALL)
    body = text(result)
    assert not result.is_error and "into the kernel only" in body, body
    assert "[redacted:" in body and not any(leak in body for leak in LEAKS), body


async def test_a_quoted_spec_reads_cleanly_inside_the_question(nh: Harness) -> None:
    """Shell quoting in the question uses double quotes: E122 puts it in single quotes."""
    nh.turns.prompt("p1", text="install pandas 2")
    asked = await nh.call("nh_add_cell", "p1", **dict(INSTALL, code="%pip install 'pandas>=2'"))
    assert lines(asked)[-1] == (
        "Next: Ask the user, then stop: 'This cell installs `pandas>=2` into the kernel only, "
        'and the next env sync removes it (`uv add "pandas>=2"` keeps it). Run it as it is?\'. '
        "After a yes, send the same call again."
    )


async def test_a_rule_that_cant_ask_keeps_its_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notebook_write = "ask" is a config problem; the rule stays an error (design §6.4)."""
    project = make_project(tmp_path, '[lint.rules]\nnotebook_write = "ask"\n')
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="save the notebook")
        code = "import nbformat\nnb = nbformat.v4.new_notebook()\nnbformat.write(nb, 'x.ipynb')"
        result = await h.call("nh_add_cell", "p1", **dict(LOAD, code=code))
    body = text(result)
    assert result.is_error and "nh: E120" in body and "L008" in body, body
    assert "E122" not in body and pending(h) is None
    assert config.load(project).problems == ["lint.rules.notebook_write must be off|hint|error"]


# --- locking ---------------------------------------------------------------------------------


async def test_the_grant_is_checked_and_used_inside_the_lock(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical granted calls of the yes message at once, the first holding its write open
    (a slow insert): the turn lock serialises them, so one is written and the other gets E110,
    and no question is left pending. With the grant check moved before ``svc.locks.hold`` (E110
    still checked first), the second call passes E110 while the first is inside its insert,
    finds the grant spent and asks again, leaving a pending question; this test then fails
    (checked in a scratch copy, design §6.4)."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p2", text="yes")
    real = nh.backend.insert_cells

    async def slow(*args: Any, **kwargs: Any) -> Any:  # the first call holds its write open
        await asyncio.sleep(0.1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(nh.backend, "insert_cells", slow)
    one = dict(INSTALL, title="Install seaborn now")
    two = dict(INSTALL, title="Install seaborn for plots")
    results = await asyncio.gather(
        nh.call("nh_add_cell", "p2", **one), nh.call("nh_add_cell", "p2", **two)
    )
    bodies = sorted(text(r) for r in results)
    assert sum(not r.is_error for r in results) == 1, bodies
    assert any("nh: E110" in b for b in bodies) and not any("nh: E122" in b for b in bodies)
    assert len(nh_code_cells(nh)) == 1
    assert pending(nh) is None


async def test_the_edit_grant_is_checked_and_used_inside_the_lock(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit-path twin of the test above: two identical granted ``nh_edit_cell`` calls of
    the yes message at once, the first holding its update open. One is written; the other is
    refused by the edit checks (E133 or E112, never E122), and no question is left pending.
    With edit's grant check moved before ``svc.locks.hold``, the second call finds the grant
    spent and asks again (checked in a scratch copy, design §6.4)."""
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.turns.prompt("p2", text="install seaborn in the load cell")
    code = INSTALL["code"] + "\n" + LOAD["code"]
    assert "nh: E122" in text(await nh.call("nh_edit_cell", "p2", cell_id=uid, code=code))
    nh.turns.prompt("p3", text="yes")
    real = nh.backend.update_cells

    async def slow(*args: Any, **kwargs: Any) -> Any:  # the first call holds its write open
        await asyncio.sleep(0.1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(nh.backend, "update_cells", slow)
    results = await asyncio.gather(
        nh.call("nh_edit_cell", "p3", cell_id=uid, code=code, title="Load and style"),
        nh.call("nh_edit_cell", "p3", cell_id=uid, code=code, title="Load, then style"),
    )
    bodies = sorted(text(r) for r in results)
    assert sum(not r.is_error for r in results) == 1, bodies
    assert not any("nh: E122" in b for b in bodies), bodies
    [refused] = [text(r) for r in results if r.is_error]
    assert "nh: E133" in refused or "nh: E112" in refused, refused  # its run, going or ok
    assert [c["source"] for c in nh_code_cells(nh)] == [code]
    assert pending(nh) is None


# --- L012: a cell that reaches the network (C5b) ---------------------------------------------
#
# A real reader: FakeBackend runs it with pandas, and the module's urlopen serves the file (and
# refuses every other URL), so no test reaches the network.
TRIPS_URL = "https://data.example.org/trips-2023.csv"
TRIPS_CSV = "trip_id,minutes\n1,12\n2,7\n3,31\n"
NETWORK = dict(
    title="Read the 2023 trips file",
    notes=["Reads the 2023 trips file from its URL.", "Shows how many rows and columns it has."],
    intent="load the 2023 trips data",
    code=f'import pandas as pd\n\nTRIPS_URL = "{TRIPS_URL}"\ntrips = pd.read_csv(TRIPS_URL)\ntrips.shape',
)
NETWORK_QUESTION = "This cell connects to `data.example.org` over the network. Run it as it is?"
NETWORK_FINDING = (
    "- L012: The cell connects to `data.example.org` over the network (`pd.read_csv`)."
)
NETWORK_ASKED = [
    FIRST,
    "nh: E122",
    NETWORK_FINDING,
    f"Next: Ask the user, then stop: '{NETWORK_QUESTION}'. After a yes, send the same call again.",
]


@pytest.fixture(autouse=True)
def _trips_served() -> Iterator[None]:
    with serving({TRIPS_URL: TRIPS_CSV}):
        yield


def approve_hosts(h: Harness, content: Any) -> None:
    path = Layout(h.project).approved_hosts
    if isinstance(content, str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    else:
        atomic_write_json(path, content)


@pytest.mark.parametrize("who", ["main", "writer"])
async def test_a_network_cell_asks_and_the_yes_grants_that_call_once(nh: Harness, who: str) -> None:
    nh.turns.prompt("p1", text="load the 2023 trips CSV from data.example.org")
    asked = await add(nh, who, "p1", "wf_run-1", **NETWORK)
    if who == "main":
        assert asked.is_error and lines(asked) == NETWORK_ASKED, text(asked)
    else:
        assert asked.is_error and lines(asked)[-2:] == [
            f"- The main conversation asks the user: '{NETWORK_QUESTION}'",
            f"Next: {RETURN_TO_WORKFLOW}",
        ]
    key = approvals.cell_key(NOTEBOOK, "add", NETWORK["code"])
    assert pending(nh)["key"] == key
    assert [e["rules"] for e in events(nh, "cell_asked")] == [["L012"]]
    if who == "writer":  # the user sees its question with the run's report (C5d2)
        reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **NETWORK))
    assert [c["source"] for c in nh_code_cells(nh)] == [NETWORK["code"]]
    assert "(3, 2)" in str(nh_code_cells(nh)[0]["outputs"])  # it read the served file
    assert pending(nh) is None and [e["rules"] for e in events(nh, "cell_granted")] == [["L012"]]
    again = await nh.call("nh_add_cell", "p2", **dict(NETWORK, title="Read the trips again"))
    assert again.is_error and "nh: E110" in text(again)
    assert Layout(nh.project).approved_hosts.exists() is False  # a yes approves the cell only


async def test_an_approved_host_is_written_with_no_question(nh: Harness) -> None:
    approve_hosts(nh, ["data.example.org"])
    nh.turns.prompt("p1", text="load the 2023 trips CSV from data.example.org")
    written = await nh.call("nh_add_cell", "p1", **NETWORK)
    assert_written(written)
    assert "data.example.org" not in text(written).split("--- next ---")[0].split("--- output")[0]
    assert pending(nh) is None and events(nh, "cell_asked") == []
    [added] = events(nh, "cell_added")
    assert "L012" not in added["hints"]


async def test_the_approved_hosts_are_read_on_every_call(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the trips")
    assert lines(await nh.call("nh_add_cell", "p1", **NETWORK)) == NETWORK_ASKED
    approve_hosts(nh, ["DATA.example.org."])  # the user adds it by hand; any form of the host
    nh.turns.prompt("p2", text="I added the host, load the trips")
    assert_written(await nh.call("nh_add_cell", "p2", **NETWORK))
    Layout(nh.project).approved_hosts.unlink()
    nh.turns.prompt("p3", text="now the 2024 file")
    code = NETWORK["code"].replace("2023", "2024")
    asked = await nh.call("nh_add_cell", "p3", **dict(NETWORK, code=code))
    assert asked.is_error and "- L012: The cell connects to `data.example.org`" in text(asked)


@pytest.mark.parametrize(
    "content",
    ["{", '{"hosts": ["data.example.org"]}', '"data.example.org"', ["api.data.example.org"]],
)
async def test_a_corrupt_list_or_another_host_still_asks(nh: Harness, content: Any) -> None:
    approve_hosts(nh, content)
    nh.turns.prompt("p1", text="load the trips")
    assert lines(await nh.call("nh_add_cell", "p1", **NETWORK)) == NETWORK_ASKED


async def test_the_network_question_names_the_host_never_the_url(nh: Harness) -> None:
    url = "https://analyst:s3cretPassw0rdXYZ@data.example.org:8443/private/t.csv?token=tok_9f8e7d"
    nh.turns.prompt("p1", text="load the trips")
    code = f'import pandas as pd\n\ntrips = pd.read_csv("{url}")'
    asked = await nh.call("nh_add_cell", "p1", **dict(NETWORK, code=code))
    body = text(asked)
    assert asked.is_error and NETWORK_QUESTION in body, body
    for part in ("analyst", "s3cretPassw0rdXYZ", "8443", "private", "tok_9f8e7d"):
        assert part not in body, (part, body)


async def test_an_install_and_a_download_ask_one_question(nh: Harness) -> None:
    code = "%pip install seaborn\n" + NETWORK["code"]
    nh.turns.prompt("p1", text="install seaborn and load the trips")
    asked = await nh.call("nh_add_cell", "p1", **dict(NETWORK, code=code))
    assert asked.is_error and lines(asked)[1:4] == [
        "nh: E122",
        FINDING,
        NETWORK_FINDING,
    ]
    assert lines(asked)[-1] == (
        "Next: Ask the user, then stop: 'This cell installs `seaborn` into the kernel only, and "
        "the next env sync removes it (`uv add seaborn` keeps it); it also connects to "
        "`data.example.org` over the network. Run it as it is?'. After a yes, send the same call "
        "again."
    )
    assert [e["rules"] for e in events(nh, "cell_asked")] == [["L009", "L012"]]


async def test_an_edit_reads_the_approved_hosts_too(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.turns.prompt("p2", text="read the 2023 trips from data.example.org in that cell instead")
    asked = await nh.call("nh_edit_cell", "p2", cell_id=uid, code=NETWORK["code"])
    assert asked.is_error and lines(asked)[:3] == NETWORK_ASKED[:3], text(asked)
    assert pending(nh)["key"] == approvals.cell_key(NOTEBOOK, f"edit:{uid}", NETWORK["code"])
    approve_hosts(nh, ["data.example.org"])
    nh.turns.prompt("p3", text="I approved data.example.org, try again")
    edited = await nh.call("nh_edit_cell", "p3", cell_id=uid, code=NETWORK["code"])
    assert not edited.is_error and "Updated" in text(edited), text(edited)
    assert [c["source"] for c in nh_code_cells(nh)] == [NETWORK["code"]]
    assert [e["rules"] for e in events(nh, "cell_asked")] == [["L012"]]


async def test_a_url_named_in_an_earlier_cell_still_asks(nh: Harness) -> None:
    """The gateway passes the cells above: a URL bound in one (the user approved it, or it
    reached nothing) counts in the next cell that reads it."""
    named = dict(
        title="Name the trips file",
        notes=["Keeps the 2023 trips URL in one place.", "Shows the file name it ends in."],
        intent="name the trips data",
        code=f'TRIPS_URL = "{TRIPS_URL}"\nTRIPS_URL.rsplit("/", 1)[-1]',
    )
    reads = dict(NETWORK, code="import pandas as pd\n\ntrips = pd.read_csv(TRIPS_URL)\ntrips.shape")
    nh.turns.prompt("p1", text="name the trips file")
    assert_written(await nh.call("nh_add_cell", "p1", **named))
    nh.turns.prompt("p2", text="now load it")
    assert lines(await nh.call("nh_add_cell", "p2", **reads)) == NETWORK_ASKED
    nh.turns.prompt("p3", text="yes")
    assert_written(await nh.call("nh_add_cell", "p3", **reads))
    assert "(3, 2)" in str(nh_code_cells(nh)[-1]["outputs"])


async def test_a_list_nh_cant_use_shows_a_config_line(nh: Harness) -> None:
    entry = (
        "`.nh/state/approved_hosts.json` entry 2 is not a host name (write the host only, such "
        "as `data.example.org`): it approves nothing."
    )
    approve_hosts(nh, ["data.example.org", "https://api.example.org/x?token=tok_1"])
    nh.turns.prompt("p1", text="load the trips")
    written = await nh.call("nh_add_cell", "p1", **NETWORK)
    assert_written(written)
    assert f"--- config ---\n{entry}" in text(written), text(written)
    assert "tok_1" not in text(written)
    status = text(await nh.call("nh_inspect", "p1", view="status"))
    assert entry in status, status
    approve_hosts(nh, "{")
    broken = (
        "`.nh/state/approved_hosts.json` is not a JSON list of host names: nh approves no host "
        "from it."
    )
    assert broken in text(await nh.call("nh_inspect", "p1", view="overview"))
    Layout(nh.project).approved_hosts.unlink()
    assert ".nh/state/approved_hosts.json" not in text(
        await nh.call("nh_inspect", "p1", view="status")
    )


@pytest.mark.parametrize(
    ("toml", "outcome"),
    [
        ('[lint]\nmode = "strict"\n', "E122"),
        ('[lint.rules]\nnetwork = "error"\n', "E120"),
        ('[lint.rules]\nnetwork = "hint"\n', "written"),
        ('[lint.rules]\nnetwork = "off"\n', "written"),
    ],
)
async def test_the_rule_level_decides_what_a_network_cell_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, outcome: str
) -> None:
    project = make_project(tmp_path, toml)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="load the trips")
        result = await h.call("nh_add_cell", "p1", **NETWORK)
    body = text(result)
    if outcome == "written":
        assert_written(result)
        hinted = "The cell connects to `data.example.org` over the network" in body
        assert hinted == ("hint" in toml), body
    else:
        assert result.is_error and f"nh: {outcome}" in body, body
    if outcome == "E120":
        assert lines(result)[-1] == (
            "Next: Don't call again yet: this project refuses cells that reach the network. Ask "
            "the user to download what the cell needs into the project (for example data/raw/), "
            "then write the cell to read it from there."
        )


# --- L013: a cell that writes outside the project (C5c) -----------------------------------------
#
# FakeBackend runs a cell in this process, so `~` is the HOME each test points at its tmp_path,
# and a relative path starts from the folder the test moves to. The project itself is under
# /tmp, which L013 exempts (design §6.4 "Exempt"): a test that needs the project's own bounds
# empties writes.EXEMPT.
OUTSIDE = dict(
    title="Save the trips to my home folder",
    notes=["Writes the trips table to the home folder.", "Shows how many rows it wrote."],
    intent="save the trips to ~/trips.csv",
    code='import pandas as pd\n\ntrips = pd.DataFrame({"trip_id": [1, 2], "minutes": [12, 7]})\n'
    'trips.to_csv("~/trips.csv", index=False)\ntrips.shape',
)
OUTSIDE_QUESTION = "This cell writes to `~/trips.csv`, outside the project. Run it as it is?"
OUTSIDE_FINDING = "- L013: The cell writes to `~/trips.csv`, outside the project (`trips.to_csv`)."
OUTSIDE_ASKED = [
    FIRST,
    "nh: E122",
    OUTSIDE_FINDING,
    f"Next: Ask the user, then stop: '{OUTSIDE_QUESTION}'. After a yes, send the same call again.",
]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    return folder


@pytest.mark.parametrize("who", ["main", "writer"])
async def test_an_outside_write_asks_and_the_yes_writes_it_once(
    nh: Harness, home: Path, who: str
) -> None:
    nh.turns.prompt("p1", text="save the trips to ~/trips.csv")
    asked = await add(nh, who, "p1", "wf_run-1", **OUTSIDE)
    if who == "main":
        assert asked.is_error and lines(asked) == OUTSIDE_ASKED, text(asked)
    else:
        assert asked.is_error and lines(asked)[2:] == [
            OUTSIDE_FINDING,
            f"- The main conversation asks the user: '{OUTSIDE_QUESTION}'",
            f"Next: {RETURN_TO_WORKFLOW}",
        ], text(asked)
    assert not (home / "trips.csv").exists() and nh_code_cells(nh) == []
    assert pending(nh)["key"] == approvals.cell_key(NOTEBOOK, "add", OUTSIDE["code"])
    assert [e["rules"] for e in events(nh, "cell_asked")] == [["L013"]]
    if who == "writer":  # the user sees its question with the run's report (C5d2)
        reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **OUTSIDE))
    assert (home / "trips.csv").read_text() == "trip_id,minutes\n1,12\n2,7\n"
    assert [c["source"] for c in nh_code_cells(nh)] == [OUTSIDE["code"]]
    assert pending(nh) is None and [e["rules"] for e in events(nh, "cell_granted")] == [["L013"]]
    again = await nh.call("nh_add_cell", "p2", **dict(OUTSIDE, title="Save the trips again"))
    assert again.is_error and "nh: E110" in text(again)


INSIDE = dict(
    title="Save the trips for later steps",
    notes=["Writes the trips table under data/processed.", "Later cells read it from there."],
    intent="save the trips",
    code='import os\nimport pandas as pd\n\ntrips = pd.DataFrame({"trip_id": [1, 2], "minutes": [12, 7]})\n'
    'os.makedirs("../data/processed", exist_ok=True)\n'
    'trips.to_csv("../data/processed/trips.csv", index=False)\ntrips.shape',
)


async def test_an_inside_write_is_written_with_no_question(
    nh: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway passes the project root and the notebook's folder (`notebooks/`): `../data`
    is inside from there, `../../` is not."""
    from nh_gateway.lint import writes

    monkeypatch.setattr(writes, "EXEMPT", ())
    monkeypatch.chdir(nh.project / "notebooks")  # where the kernel runs
    nh.turns.prompt("p1", text="save the trips for the next steps")
    written = await nh.call("nh_add_cell", "p1", **INSIDE)
    assert_written(written)
    assert (nh.project / "data" / "processed" / "trips.csv").exists()
    assert pending(nh) is None and events(nh, "cell_asked") == []
    [added] = events(nh, "cell_added")
    assert "L013" not in added["hints"]
    climbs = INSIDE["code"].replace("../data/processed", "../../exports")
    nh.turns.prompt("p2", text="save them next to the project instead")
    asked = await nh.call("nh_add_cell", "p2", **dict(INSIDE, code=climbs))
    exports = tmp_path / "exports"
    assert asked.is_error and lines(asked)[:3] == [
        FIRST,
        "nh: E122",
        f"- L013: The cell writes to `{exports}` and `{exports}/trips.csv`, outside the "
        "project (`os.makedirs` (+1 more)).",
    ], text(asked)
    assert not exports.exists()


async def test_an_edit_that_writes_outside_asks_too(nh: Harness, home: Path) -> None:
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.turns.prompt("p2", text="also save it to my home folder in that cell")
    code = LOAD["code"] + '\ndf.to_csv("~/sales.csv")'
    asked = await nh.call("nh_edit_cell", "p2", cell_id=uid, code=code)
    assert asked.is_error and lines(asked)[:3] == [
        FIRST,
        "nh: E122",
        "- L013: The cell writes to `~/sales.csv`, outside the project (`df.to_csv`).",
    ], text(asked)
    assert pending(nh)["key"] == approvals.cell_key(NOTEBOOK, f"edit:{uid}", code)
    assert not (home / "sales.csv").exists()
    nh.turns.prompt("p3", text="yes")
    edited = await nh.call("nh_edit_cell", "p3", cell_id=uid, code=code)
    assert not edited.is_error and "Updated" in text(edited), text(edited)
    assert (home / "sales.csv").exists()
    assert [e["rules"] for e in events(nh, "cell_granted")] == [["L013"]]


async def test_an_edit_reads_paths_from_the_notebooks_folder_too(
    nh: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """nh_edit_cell passes the notebook's folder as nh_add_cell does: from `notebooks/`,
    `../data/processed` is inside and `../../` is not."""
    from nh_gateway.lint import writes

    monkeypatch.setattr(writes, "EXEMPT", ())
    monkeypatch.chdir(nh.project / "notebooks")  # where the kernel runs
    nh.turns.prompt("p1", text="load the data")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    keep = '\nimport os\nos.makedirs("../data/processed", exist_ok=True)\ndf.to_csv("{}")'
    inside = LOAD["code"] + keep.format("../data/processed/sales.csv")
    nh.turns.prompt("p2", text="also keep a copy for the later steps")
    edited = await nh.call("nh_edit_cell", "p2", cell_id=uid, code=inside)
    assert not edited.is_error and "Updated" in text(edited), text(edited)
    assert (nh.project / "data" / "processed" / "sales.csv").exists()
    assert events(nh, "cell_asked") == [] and pending(nh) is None
    outside = LOAD["code"] + keep.format("../../sales.csv")
    nh.turns.prompt("p3", text="keep it next to the project instead")
    asked = await nh.call("nh_edit_cell", "p3", cell_id=uid, code=outside)
    assert asked.is_error and lines(asked)[:3] == [
        FIRST,
        "nh: E122",
        f"- L013: The cell writes to `{tmp_path / 'sales.csv'}`, outside the project (`df.to_csv`).",
    ], text(asked)
    assert not (tmp_path / "sales.csv").exists()


async def test_a_function_an_earlier_cell_defines_asks_where_it_is_called(
    nh: Harness, home: Path
) -> None:
    """The cell that defines `export` writes nothing; the call that gives it a path outside the
    project asks, named by the function (design §6.4 "A function the cell defines")."""
    define = dict(
        title="Define the export helper",
        notes=["Defines export(frame, path), which saves a frame as CSV.", "Nothing is saved yet."],
        intent="define a helper that saves a frame",
        code="def export(frame, path):\n    frame.to_csv(path, index=False)\n\n\nexport",
    )
    nh.turns.prompt("p1", text="add a helper to save frames")
    assert_written(await nh.call("nh_add_cell", "p1", **define))
    call = dict(
        OUTSIDE,
        code='import os\nimport pandas as pd\n\ntrips = pd.DataFrame({"trip_id": [1, 2]})\n'
        'export(trips, os.path.expanduser("~/trips.csv"))',
    )
    nh.turns.prompt("p2", text="save the trips to ~/trips.csv")
    asked = await nh.call("nh_add_cell", "p2", **call)
    assert asked.is_error and lines(asked)[:3] == [
        FIRST,
        "nh: E122",
        "- L013: The cell writes to `~/trips.csv`, outside the project (`export`).",
    ], text(asked)
    assert not (home / "trips.csv").exists() and [e["rules"] for e in events(nh, "cell_asked")] == [
        ["L013"]
    ]


async def test_an_install_a_download_and_an_outside_write_ask_one_question(
    nh: Harness, home: Path
) -> None:
    code = "%pip install seaborn\n" + NETWORK["code"] + '\ntrips.to_csv("~/trips.csv")'
    nh.turns.prompt("p1", text="install seaborn, load the trips and save them to ~/trips.csv")
    asked = await nh.call("nh_add_cell", "p1", **dict(NETWORK, code=code))
    assert asked.is_error and lines(asked)[1:5] == [
        "nh: E122",
        FINDING,
        NETWORK_FINDING,
        OUTSIDE_FINDING,
    ], text(asked)
    assert lines(asked)[-1] == (
        "Next: Ask the user, then stop: 'This cell installs `seaborn` into the kernel only, and "
        "the next env sync removes it (`uv add seaborn` keeps it); it also connects to "
        "`data.example.org` over the network; it also writes to `~/trips.csv`, outside the "
        "project. Run it as it is?'. After a yes, send the same call again."
    )
    assert [e["rules"] for e in events(nh, "cell_asked")] == [["L009", "L012", "L013"]]


async def test_no_secret_in_a_path_reaches_the_question(nh: Harness, home: Path) -> None:
    token = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    code = OUTSIDE["code"].replace("~/trips.csv", f"~/{token}/trips.csv")
    nh.turns.prompt("p1", text="save the trips")
    asked = await nh.call("nh_add_cell", "p1", **dict(OUTSIDE, code=code))
    body = text(asked)
    assert asked.is_error and "- L013: The cell writes to `~/[redacted:" in body, body
    assert "outside the project. Run it as it is?" in body and token not in body, body


@pytest.mark.parametrize(
    ("toml", "outcome"),
    [
        ('[lint]\nmode = "strict"\n', "E122"),
        ('[lint.rules]\noutside_write = "error"\n', "E120"),
        ('[lint.rules]\noutside_write = "hint"\n', "written"),
        ('[lint.rules]\noutside_write = "off"\n', "written"),
    ],
)
async def test_the_rule_level_decides_what_an_outside_write_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path, toml: str, outcome: str
) -> None:
    project = make_project(tmp_path, toml)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="save the trips to ~/trips.csv")
        result = await h.call("nh_add_cell", "p1", **OUTSIDE)
    body = text(result)
    if outcome == "written":
        assert_written(result)
        hinted = "The cell writes to `~/trips.csv`, outside the project" in body
        assert hinted == ("hint" in toml), body
        assert (home / "trips.csv").exists()
    else:
        assert result.is_error and f"nh: {outcome}" in body, body
        assert not (home / "trips.csv").exists()
    if outcome == "E120":
        assert lines(result)[-1] == (
            "Next: Don't call again yet: this project refuses cells that write outside it. Write "
            "the files inside the project instead (for example data/processed/ or reports/), or "
            "tell the user where the cell would write and let them change it themselves."
        )


async def test_l012s_refusal_comes_before_l013s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At `error`, E120 lists both rules; its Next is L012's (design §6.4 L013 "Fix")."""
    project = make_project(tmp_path, '[lint.rules]\nnetwork = "error"\noutside_write = "error"\n')
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    code = NETWORK["code"] + '\ntrips.to_csv("~/trips.csv")'
    async with Client(create_server(project, backend)) as client:
        h = Harness(project, backend, client, Turns(project, tmp_path / "data"))
        h.turns.prompt("p1", text="load the trips and save them to ~/trips.csv")
        result = await h.call("nh_add_cell", "p1", **dict(NETWORK, code=code))
    body = text(result)
    assert result.is_error and "nh: E120" in body, body
    assert "- L012: " in body and "- L013: The cell writes to `~/trips.csv`" in body, body
    assert lines(result)[-1].startswith(
        "Next: Don't call again yet: this project refuses cells that reach the network."
    )


# --- ultracode: the writer asks, the main conversation sends the call (design §6.4, C5d) --------

WITH_INSTALL = "%pip install seaborn\n" + LOAD["code"]


async def as_writer(h: Harness, turn: str, run: str, tool: str, args: dict[str, Any]) -> Any:
    """``tool`` from nh:cell-writer in run ``run``, launched in ``turn`` first if it is new."""
    if not h.turns.transcript_dir(run).exists():
        h.turns.workflow_launched(turn, run, tool_use_id=f"toolu_{run}", task_id=f"task-{run}")
    return await h.turns.writer_call(h.client, f"w-{run}", run, tool, args, turn)


def reported(h: Harness, run: str) -> None:
    """The run's report arrives: a notification, an alias of the message that launched it."""
    h.turns.notification(f"n-{run}", tool_use_id=f"toolu_{run}", task_id=f"task-{run}")


async def writer_asks(h: Harness, shape: str) -> tuple[str, dict[str, Any], str]:
    """nh:cell-writer's call that nh asks about in message p1, in each shape the workflow
    reports: (the tool, the call as the writer sent it, the pending key)."""
    if shape in ("add", "add placed", "add with notebook"):
        args = dict(INSTALL)
        if shape == "add placed":  # between two cells, so the position shows
            nh_ids = []
            for prompt, cell in (("p0", LOAD), ("p00", dict(LOAD, title="Load more sales data"))):
                h.turns.prompt(prompt, text="load the data")
                nh_ids.append((await h.call("nh_add_cell", prompt, **cell)).meta["nh/cell_id"])
            args.update(after_cell_id=nh_ids[0], notebook=NOTEBOOK)
        if shape == "add with notebook":  # not the default notebook, so naming it shows
            args.update(notebook=OTHER)
        h.turns.prompt("p1", text="install seaborn")
        assert_asked(await as_writer(h, "p1", "wf_run-1", "nh_add_cell", args), writer=True)
        if shape == "add with notebook":
            return "nh_add_cell", args, approvals.cell_key(OTHER, "add", INSTALL["code"])
        return "nh_add_cell", args, install_key()
    if shape == "launch edit":  # the launch named an earlier message's cell to change
        h.turns.prompt("p0", text="load the data")
        uid = (await h.call("nh_add_cell", "p0", **LOAD)).meta["nh/cell_id"]
        h.turns.prompt("p1", text="style the plots of that cell with seaborn")
        args = dict(cell_id=uid, code=WITH_INSTALL)
        asked = await as_writer(h, "p1", "wf_run-1", "nh_edit_cell", args)
        assert asked.is_error and lines(asked)[-2:] == WRITER_ASKED[-2:], text(asked)
        return "nh_edit_cell", args, approvals.cell_key(NOTEBOOK, f"edit:{uid}", WITH_INSTALL)
    h.turns.prompt("p1", text="load the data and style the plots with seaborn")
    first = dict(LOAD, code="undefined_name") if shape == "edit own failed cell" else LOAD
    added = await as_writer(h, "p1", "wf_run-1", "nh_add_cell", first)
    assert not added.is_error, text(added)
    uid = added.meta["nh/cell_id"]
    # A failed run's fix (a retry), or a revision of the OK cell from QA's findings.
    args = dict(cell_id=uid, code=WITH_INSTALL)
    asked = await as_writer(h, "p1", "wf_run-1", "nh_edit_cell", args)
    assert asked.is_error and lines(asked)[-2:] == WRITER_ASKED[-2:], text(asked)
    return "nh_edit_cell", args, approvals.cell_key(NOTEBOOK, f"edit:{uid}", WITH_INSTALL)


@pytest.mark.parametrize(
    "shape",
    ["add", "add placed", "add with notebook", "launch edit", "edit own failed cell", "revision"],
)
async def test_a_writers_question_is_granted_to_the_main_conversations_exact_call(
    nh: Harness, shape: str
) -> None:
    """The writer's E122 records the question for the main conversation under the run's turn
    and the call's key; the workflow reports the call (needs_approval), and the main
    conversation's exact call in the user's yes message is granted once."""
    tool, args, key = await writer_asks(nh, shape)
    asked = pending(nh)
    assert asked is not None and asked["turn_id"] == "p1" and asked["key"] == key
    before = [c["source"] for c in nh_code_cells(nh)]
    reported(nh, "wf_run-1")
    # Replying to the report is still the asking message: sending the call there writes
    # nothing (it asks again; a revision of the message's OK cell is E112 for the main
    # conversation) and leaves the question as it was.
    again = await nh.call(tool, "n-wf_run-1", **args)
    if shape == "revision":
        assert again.is_error and "nh: E112" in text(again), text(again)
    else:
        assert again.is_error and "Next: Ask the user, then stop:" in text(again), text(again)
    assert pending(nh) == asked and [c["source"] for c in nh_code_cells(nh)] == before
    nh.turns.prompt("p2", text="yes")
    if shape == "add with notebook":  # the active notebook is the one the writer named
        args = {k: v for k, v in args.items() if k != "notebook"}
    granted = await nh.call(tool, "p2", **args)
    assert not granted.is_error and "nh: E" not in text(granted), text(granted)
    assert pending(nh) is None
    assert [e["turn_id"] for e in events(nh, "cell_granted")] == ["p2"]
    if shape == "add with notebook":
        assert f"in {OTHER}" in text(granted) and nh_code_cells(nh) == [], text(granted)
        [placed] = [c for c in nh.backend.notebook(OTHER)["cells"] if c["cell_type"] == "code"]
        assert placed["source"] == INSTALL["code"] and placed["metadata"]["nh"]["turn_id"] == "p2"
        once = await nh.call(tool, "p2", **args)
        assert once.is_error and "nh: E110" in text(once), text(once)
        return
    sources = [c["source"] for c in nh_code_cells(nh)]
    if shape == "add placed":
        assert sources == [LOAD["code"], INSTALL["code"], LOAD["code"]]
    elif tool == "nh_add_cell":
        assert sources == [INSTALL["code"]]
    else:
        assert sources == [WITH_INSTALL]
    if tool == "nh_add_cell":  # the yes message's cell
        placed = nh_code_cells(nh)[1 if shape == "add placed" else 0]
        assert placed["metadata"]["nh"]["turn_id"] == "p2"
    else:
        [edited] = events(nh, "cell_edited")[-1:]
        assert edited["turn_id"] == "p2" and edited["agent"] is None
    once = await nh.call(tool, "p2", **args)  # used once: the yes message has its cell
    code = "nh: E110" if tool == "nh_add_cell" else "nh: E112"  # E112: its OK cell, revised
    assert once.is_error and code in text(once) and "nh: E122" not in text(once), text(once)


async def test_the_writers_call_and_the_main_conversations_share_a_key_with_or_without_notebook(
    nh: Harness,
) -> None:
    """The writer named no notebook (the active one); the main conversation names it."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="go")
    assert_written(await nh.call("nh_add_cell", "p2", **dict(INSTALL, notebook=NOTEBOOK)))


@pytest.mark.parametrize("run_reported", [False, True])
async def test_the_asking_runs_writer_gets_e107_and_leaves_the_grant(
    nh: Harness, run_reported: bool
) -> None:
    """A run of the asking message never reaches the gate in the yes message (E107, unchanged):
    it neither uses nor drops the grant, so the main conversation's call is still granted once
    the run reported before the yes. A yes typed before the report grants nothing (C5d2): the
    main conversation's call there asks."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    asked = pending(nh)
    if run_reported:
        reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    for prompt in ("p1", "p2"):  # whichever prompt id its calls carry
        late = await nh.turns.writer_call(
            nh.client, "w-wf_run-1", "wf_run-1", "nh_add_cell", INSTALL, prompt
        )
        assert late.is_error and "nh: E107" in text(late), text(late)
    assert pending(nh) == asked and not nh_code_cells(nh)
    assert not events(nh, "cell_granted")
    main = await nh.call("nh_add_cell", "p2", **INSTALL)
    if run_reported:
        assert_written(main)
        assert pending(nh) is None
    else:
        assert_asked(main)
        assert pending(nh)["turn_id"] == "p2" and pending(nh)["run_id"] is None
        assert not nh_code_cells(nh) and not events(nh, "cell_granted")


async def test_a_writer_of_the_yes_messages_run_may_send_the_approved_call_once(
    nh: Harness,
) -> None:
    """Decided in C5d (design §6.4, "Which thread may send the approved call"): the grant
    covers the call, not the thread, so a run launched in the yes message, working for it,
    may send the identical call once; qa-workflow.md still has the main conversation send it."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await as_writer(nh, "p2", "wf_run-2", "nh_add_cell", INSTALL))
    assert pending(nh) is None
    [cell] = nh_code_cells(nh)
    assert cell["metadata"]["nh"]["turn_id"] == "p2"
    again = await as_writer(nh, "p2", "wf_run-2", "nh_add_cell", INSTALL)
    assert again.is_error and "nh: E110" in text(again) and "nh: E122" not in text(again)


async def test_a_writers_different_no_ask_cell_in_the_yes_message_loses_the_yes(
    nh: Harness,
) -> None:
    """A run launched in the yes message whose writer drops what nh asked about writes that
    cell as the yes message's one cell; the approved call then gets E110 (design §6.4, Which
    thread may send the approved call): nothing unasked, but the yes is lost."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_written(await as_writer(nh, "p2", "wf_run-2", "nh_add_cell", LOAD))
    assert pending(nh) is not None and pending(nh)["turn_id"] == "p1"  # not this message's
    reported(nh, "wf_run-2")
    lost = await nh.call("nh_add_cell", "n-wf_run-2", **INSTALL)
    assert lost.is_error and "nh: E110" in text(lost), text(lost)
    assert [c["source"] for c in nh_code_cells(nh)] == [LOAD["code"]]
    assert not events(nh, "cell_granted")


# --- the reply turn: still the asking message (design §6.4, "The reply turn") ----------------


async def test_another_asked_for_cell_in_the_reply_turn_waits(nh: Harness) -> None:
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    asked = pending(nh)
    reported(nh, "wf_run-1")
    waiting = await nh.call("nh_add_cell", "n-wf_run-1", **PLOTLY)
    assert waiting.is_error and lines(waiting)[-2:] == [
        approvals.WAITING_LINE,
        f"Next: {approvals.WAITING_NEXT}",
    ], text(waiting)
    assert pending(nh) == asked and not nh_code_cells(nh)
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))  # the writer's question won


async def test_another_cell_written_in_the_reply_turn_clears_the_question(nh: Harness) -> None:
    """A cell that asks nothing is written as the asking message's cell (QA_REPORT's "unless
    its writer wrote none" is no leave to write here) and supersedes the writer's question, so
    the user's later yes grants nothing: the approved call asks again (fails closed)."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    head = nh.turns.notification(
        "n-wf_run-1", tool_use_id="toolu_wf_run-1", task_id="task-wf_run-1"
    )
    assert head is not None and "unless its writer wrote none" in json.dumps(head)
    assert_written(await nh.call("nh_add_cell", "n-wf_run-1", **LOAD))
    assert pending(nh) is None
    nh.turns.prompt("p2", text="yes")
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert pending(nh)["turn_id"] == "p2" and not events(nh, "cell_granted")
    assert [c["source"] for c in nh_code_cells(nh)] == [LOAD["code"]]


# --- a yes typed during the run (design §6.4, "A yes typed during the run": C5d2) -----------

QA_EARLIER_PART = "change no cell for it"  # prompt_submit.QA_EARLIER


def question_of(h: Harness) -> dict[str, Any]:
    """The pending question without its time."""
    current = pending(h)
    assert current is not None
    return {k: v for k, v in current.items() if k != "ts"}


@pytest.mark.parametrize("where", ["the yes message", "the report's turn"])
@pytest.mark.parametrize("word", ["ok", "yes", "go"])
async def test_a_yes_typed_during_the_run_grants_nothing(
    nh: Harness, word: str, where: str
) -> None:
    """The user's message typed after the writer's E122 and before the report is the next
    message after the asking one, but the user hadn't seen nh's question: the run reported
    after that message opened. The exact call there, or in the report's turn (an alias of it),
    asks anew (no "held"), as the main conversation's own question; a yes to it grants it."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    assert question_of(nh) == {
        "kind": "cell",
        "key": install_key(),
        "turn_id": "p1",
        "run_id": "wf_run-1",
    }
    nh.turns.prompt("p2", text=word)  # typed while the run works in the background
    if where == "the yes message":  # before the report
        sent = await nh.call("nh_add_cell", "p2", **INSTALL)
    head = nh.turns.notification(
        "n-wf_run-1", tool_use_id="toolu_wf_run-1", task_id="task-wf_run-1"
    )
    assert head is not None and QA_EARLIER_PART in json.dumps(head)  # for an earlier message
    if where == "the report's turn":
        sent = await nh.call("nh_add_cell", "n-wf_run-1", **INSTALL)
    assert_asked(sent)  # nh's question, with the main conversation's Next
    mine = {"kind": "cell", "key": install_key(), "turn_id": "p2", "run_id": None}
    assert question_of(nh) == mine
    again = await nh.call("nh_add_cell", "n-wf_run-1", **INSTALL)  # the report's turn: p2's
    assert_asked(again)
    assert question_of(nh) == mine
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "asked", "repeated"]
    assert not events(nh, "cell_granted") and not nh_code_cells(nh)
    nh.turns.prompt("p3", text="yes")  # the user saw nh's question this time
    assert_written(await nh.call("nh_add_cell", "p3", **INSTALL))
    assert [e["turn_id"] for e in events(nh, "cell_granted")] == ["p3"]
    assert pending(nh) is None


async def test_a_clock_stepped_back_doesnt_turn_a_yes_typed_during_the_run_into_an_answer(
    nh: Harness,
) -> None:
    """The report's time can read before the yes message's (a system clock stepped back between
    them), but the report still reached that message's reply, not the asking one's: the hook
    keeps the turn it aliased (``done_turn``), so nothing is granted (design §6.4, C5d3)."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    nh.turns.prompt("p2", text="yes")  # typed while the run works in the background
    nh.turns.notification("n-wf_run-1", tool_use_id="toolu_wf_run-1", task_id="task-wf_run-1")
    layout = Layout(nh.project)
    record = turn_record.read(layout, SESSION)
    assert record is not None and record["turn_id"] == "p2"
    data = json.loads(layout.workflow_file(SESSION).read_text())
    [entry] = [e for e in data["runs"] if e["run_id"] == "wf_run-1"]
    assert entry["done_turn"] == "p2"
    entry["done_ts"] = record["ts"] - 5.0  # what a clock stepped back leaves behind
    atomic_write_json(layout.workflow_file(SESSION), data)
    for prompt_id in ("p2", "n-wf_run-1"):  # the yes message and the report's turn
        assert_asked(await nh.call("nh_add_cell", prompt_id, **INSTALL))
    assert not events(nh, "cell_granted") and not nh_code_cells(nh)
    assert question_of(nh) == {
        "kind": "cell",
        "key": install_key(),
        "turn_id": "p2",
        "run_id": None,
    }


async def test_a_report_whose_time_reads_after_the_yes_grants_and_holds_nothing(
    nh: Harness,
) -> None:
    """The report reached the asking message's reply (``done_turn`` p1), but its time reads
    after the yes message opened (a system clock stepped back between the report and the
    yes): the time check, the rule's second condition, fails closed. The exact call asks anew,
    neither granted nor "held" (design §6.4 Fails closed, C5d3)."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    reported(nh, "wf_run-1")
    nh.turns.prompt("p2", text="yes")
    layout = Layout(nh.project)
    record = turn_record.read(layout, SESSION)
    assert record is not None and record["turn_id"] == "p2"
    data = json.loads(layout.workflow_file(SESSION).read_text())
    [entry] = [e for e in data["runs"] if e["run_id"] == "wf_run-1"]
    assert entry["done_turn"] == "p1"
    entry["done_ts"] = record["ts"] + 5.0  # what a clock stepped back leaves behind
    atomic_write_json(layout.workflow_file(SESSION), data)
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "asked"]  # no "held"
    assert not events(nh, "cell_granted") and not nh_code_cells(nh)
    assert question_of(nh) == {
        "kind": "cell",
        "key": install_key(),
        "turn_id": "p2",
        "run_id": None,
    }


@pytest.mark.parametrize("yes_typed", ["after the report", "during the run"])
async def test_another_asked_for_cell_is_held_only_by_a_yes_to_a_question_the_user_saw(
    nh: Harness, yes_typed: str
) -> None:
    """ "Held" checks grant() with the pending question's own kind and key and the run's report
    too: a yes typed during the run holds nothing, so another cell nh asks about records its
    own question."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    if yes_typed == "after the report":
        reported(nh, "wf_run-1")
        nh.turns.prompt("p2", text="yes")
        turn = "p2"
    else:
        nh.turns.prompt("p2", text="yes")
        reported(nh, "wf_run-1")
        turn = "n-wf_run-1"
    other = await nh.call("nh_add_cell", turn, **PLOTLY)
    assert other.is_error and "nh: E122" in text(other), text(other)
    if yes_typed == "after the report":
        assert lines(other)[-2:] == HELD
        assert question_of(nh)["key"] == install_key() and question_of(nh)["turn_id"] == "p1"
        assert_written(await nh.call("nh_add_cell", turn, **INSTALL))
        return
    assert lines(other)[-1].startswith(
        "Next: Ask the user, then stop: 'This cell installs `plotly`"
    )
    assert question_of(nh) == {
        "kind": "cell",
        "key": install_key(PLOTLY["code"]),
        "turn_id": "p2",
        "run_id": None,
    }
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "asked"]


async def test_a_stopped_runs_question_is_never_granted(nh: Harness) -> None:
    """TaskStop marks the run done, but a stopped run sends no report: its writer's question
    never reached the user, so the next message's yes grants nothing."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    nh.turns.task_stopped("p1", "task-wf_run-1")
    nh.turns.prompt("p2", text="yes")
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert question_of(nh)["turn_id"] == "p2" and question_of(nh)["run_id"] is None
    assert not events(nh, "cell_granted") and not nh_code_cells(nh)


def past_its_hour(h: Harness, run: str) -> None:
    """The run never reported and was launched over an hour ago: it no longer counts as open."""
    layout = Layout(h.project)
    data = json.loads(layout.workflow_file(SESSION).read_text())
    for entry in data["runs"]:
        if entry["run_id"] == run:
            entry["ts"] = time.time() - turn_record.RUN_OPEN_TTL_S - 1
    atomic_write_json(layout.workflow_file(SESSION), data)


@pytest.mark.parametrize("ended", ["stopped", "past its hour"])
async def test_a_question_the_main_conversation_asks_itself_is_granted_once(
    nh: Harness, ended: str
) -> None:
    """The run won't bring its writer's question to the user (stopped, or past its hour with no
    report), so E108 lets the main conversation's identical call through in the asking message:
    its E122 "repeated" has it ask the user itself, so the question becomes its own and the
    yes in the next message grants it once (design §6.4, "The main conversation asks it
    itself")."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    # The writer's own repeat returns to the workflow: its question still waits for the report.
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    assert question_of(nh)["run_id"] == "wf_run-1"
    waits = await nh.call("nh_add_cell", "p1", **INSTALL)
    assert waits.is_error and "nh: E108" in text(waits), text(waits)
    if ended == "stopped":
        nh.turns.task_stopped("p1", "task-wf_run-1")
    else:
        past_its_hour(nh, "wf_run-1")
    assert question_of(nh)["run_id"] == "wf_run-1"
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))  # ASK_NEXT: it asks the user
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "repeated", "repeated"]
    mine = {"kind": "cell", "key": install_key(), "turn_id": "p1", "run_id": None}
    assert question_of(nh) == mine
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert [e["turn_id"] for e in events(nh, "cell_granted")] == ["p2"]
    assert pending(nh) is None
    once = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert once.is_error and "nh: E110" in text(once), text(once)


async def test_a_repeat_after_a_later_message_opened_keeps_the_writers_run(nh: Harness) -> None:
    """A late call that ``canonical()`` maps to the asking message (an earlier notification's
    prompt) after the user's next message opened: that message may have been typed before the
    main conversation asked, so the question keeps its run and the yes there grants nothing."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    nh.turns.notification("n-other", tool_use_id=None, task_id="bash-1")  # another task's
    nh.turns.task_stopped("p1", "task-wf_run-1")
    nh.turns.prompt("p2", text="yes")
    late = await nh.call("nh_add_cell", "n-other", **INSTALL)  # counted against p1
    assert_asked(late)
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "repeated"]
    writers = {"kind": "cell", "key": install_key(), "turn_id": "p1", "run_id": "wf_run-1"}
    assert question_of(nh) == writers
    assert_asked(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert question_of(nh) == dict(writers, turn_id="p2", run_id=None)
    assert not events(nh, "cell_granted") and not nh_code_cells(nh)


class _NoRuns:
    """``policy.turn``'s ``turn_record``, failing the test if it reads the runs file."""

    def __getattr__(self, name: str) -> Any:
        if name == "find_runs":
            pytest.fail("policy.turn read the runs")
        return getattr(turn_record, name)


async def test_the_gate_reads_no_runs_for_the_main_conversations_question(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a writer's question needs its run's report: for the main conversation's own, the
    repeated call, the "held" check and the grant never read the runs (design §6.4)."""
    monkeypatch.setattr(policy_turn, "turn_record", _NoRuns())
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))  # repeated
    nh.turns.prompt("p2", text="yes")
    other = await nh.call("nh_add_cell", "p2", **PLOTLY)
    assert other.is_error and lines(other)[-2:] == HELD, text(other)
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert [e["outcome"] for e in events(nh, "cell_asked")] == ["asked", "repeated", "held"]


async def test_the_main_conversations_question_needs_no_report(nh: Harness) -> None:
    """The main conversation asked the user itself: a run that hasn't reported (one launched
    in the asking message after the question) changes nothing."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.workflow_launched("p1", "wf_run-1", tool_use_id="toolu_wf_run-1")
    assert question_of(nh)["run_id"] is None
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert [e["turn_id"] for e in events(nh, "cell_granted")] == ["p2"]


@pytest.mark.parametrize("meanwhile", ["what does the data look like?", "ok"])
@pytest.mark.parametrize("later", ["yes", "ok, write that cell"])
async def test_after_an_earlier_messages_report_the_call_asks_anew(
    nh: Harness, meanwhile: str, later: str
) -> None:
    """qa-workflow.md: neither ask nor send the call in the reply to a report for an earlier
    message; if the user then asks for the cell, the call sent in that message finds the
    question two messages old (6.1 drops it), so nh asks its own question there."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    nh.turns.prompt("p2", text=meanwhile)
    reported(nh, "wf_run-1")  # QA_EARLIER: reported, nothing sent
    nh.turns.prompt("p3", text=later)
    assert_asked(await nh.call("nh_add_cell", "p3", **INSTALL))
    assert pending(nh)["turn_id"] == "p3" and not nh_code_cells(nh)
    nh.turns.prompt("p4", text="yes")
    assert_written(await nh.call("nh_add_cell", "p4", **INSTALL))


# --- the approved batch (design §6.3, C6a): the batch kind of the matrix, main conversation ----

STEPS = [
    LOAD,
    dict(
        title="Total price by region",
        notes=["Sums the price per region.", "Shows which region sells the most."],
        intent="total price by region",
        code="totals = df.groupby('region')['price'].sum()\ntotals",
    ),
    dict(
        title="Count rows per region",
        notes=["Counts the rows of each region.", "A small region makes its total less sure."],
        intent="rows per region",
        code="counts = df['region'].value_counts()\ncounts",
    ),
    dict(
        title="Largest price",
        notes=["Finds the highest price.", "A very high one may be a typo."],
        intent="largest price",
        code="top_price = df['price'].max()\ntop_price",
    ),
    dict(
        title="Smallest price",
        notes=["Finds the lowest price.", "A zero or negative one may be a typo."],
        intent="smallest price",
        code="low_price = df['price'].min()\nlow_price",
    ),
    dict(
        title="Mean price",
        notes=["Averages the prices.", "Missing prices are left out."],
        intent="mean price",
        code="mean_price = df['price'].mean()\nmean_price",
    ),
]
# A step whose result has a 'check this' section: the filter keeps none of df's 3 rows.
CHECKED = dict(
    title="Keep expensive sales",
    notes=["Keeps sales above 100.", "Expensive sales may behave differently."],
    intent="expensive sales",
    code="expensive = df[df['price'] > 100]\nexpensive.shape",
)
FAILING = dict(
    title="Price per unit",
    notes=["Divides the price by the units.", "Shows what one unit costs."],
    intent="price per unit",
    code="per_unit = 1 / 0\nper_unit",
)


def machine(result: Any) -> str:
    return next(line for line in lines(result) if line.startswith("nh: cell="))


def next_text(result: Any) -> str:
    return text(result).split("--- next ---\n", 1)[1].split("\n---", 1)[0]


def stopped(step: str, verb: str = "written") -> str:
    return (
        f"Not {verb}: the approved batch stopped at step {step}; nh changes nothing more this "
        "message."
    )


def turn_state(h: Harness, turn: str) -> dict[str, Any]:
    return ledger(h)["turns"][turn]


def WAITING(step: str) -> list[str]:  # noqa: N802  (a pinned text, like the constants)
    """E133 for a step sent before step ``step``'s result is in (design §6.3)."""
    return [
        f"The kernel is busy with step {step} of the approved batch, whose result isn't in yet.",
        "nh: E133",
        f"Next: Wait for step {step}'s result; send this call again only if that result says to "
        "go on.",
    ]


async def ask_and_answer(h: Harness, answer: str = "yes", request: str = "run the next 3") -> None:
    """Message p1 asks for the batch (nh writes nothing in it: E109); p2 answers."""
    h.turns.prompt("p1", text=request)
    assert "nh: E109" in text(await h.call("nh_add_cell", "p1", **STEPS[0]))
    h.turns.prompt("p2", text=answer)


@contextlib.asynccontextmanager
async def batch_harness(
    tmp_path: Path, toml: str = "", **backend_kw: Any
) -> AsyncIterator[Harness]:
    project = make_project(tmp_path, toml)
    backend = FakeBackend(project, **backend_kw)
    async with Client(create_server(project, backend)) as client:
        yield Harness(project, backend, client, Turns(project, tmp_path / "data"))


def test_e123_texts_are_pinned() -> None:
    assert CATALOGUE["E123"] == (
        "Not {verb}: the approved {what} stopped at step {step}; nh changes nothing more this "
        "message.",
        "Report the batch to the user: what each step did, then where and why it stopped (the "
        "error, the 'check this' finding or nh's question). No retry and no new cell this "
        "message; wait for the user.",
    )
    assert batch.STOP_LINE.format(step=2, total=3) == (
        "- The approved batch stops here, at step 2 of 3: nh changes nothing more this message."
    )
    assert batch.REFUSED_NEXT == (
        "Don't call again: the approved batch stops here. Tell the user what each step did, "
        "which step nh refused and why, and what you would change; then wait."
    )
    assert batch.FULL_HEAD == (
        "Not written (by design): the approved batch's {total} cells are written; the last is "
        "{cell}."
    )
    assert batch.FULL_LINE == "- The rest of the plan waits for the user's next message."
    assert batch.RUN_HEAD == (
        "Not written (by design): one new cell per nh:qa-cell run, and this run already wrote one."
    )
    assert WAITING("2") == [
        "The kernel is busy with step 2 of the approved batch, whose result isn't in yet.",
        "nh: E133",
        "Next: Wait for step 2's result; send this call again only if that result says to go on.",
    ]
    assert batch.UNDONE_NEXT.format(step=2, total=3) == (
        "The approved batch stops here, at step 2 of 3: say which planned steps did not run."
    )
    assert {"E120", "E121", "E122", "E124", "E125"} == batch.STOP_CODES


@pytest.mark.parametrize("when", ["same message", "next message", "two messages later"])
@pytest.mark.parametrize("answer", ANSWERS)
async def test_only_a_yes_in_the_next_message_grants_the_batch(
    nh: Harness, answer: str, when: str
) -> None:
    """ "run the next 3", then the answer: only a whole-message yes ("go" alone too) in the
    very next message lets that message write 3 cells. Nothing is recorded for the ask."""
    nh.turns.prompt("p1", text="run the next 3")
    if when == "same message":  # typed while Claude works: absorbed, never an answer (6.1)
        nh.turns.prompt("p1", text=answer)
        turn = "p1"
    elif when == "next message":
        nh.turns.prompt("p2", text=answer)
        turn = "p2"
    else:
        nh.turns.prompt("p2", text=answer)
        nh.turns.prompt("p3", text=answer)
        turn = "p3"
    assert pending(nh) is None  # a batch ask records no question (design §6.3)
    first = await nh.call("nh_add_cell", turn, **STEPS[0])
    if when == "same message":  # still the ask message: mode ask holds (E109)
        assert first.is_error and "nh: E109" in text(first), text(first)
        assert not nh_code_cells(nh) and events(nh, "batch_granted") == []
        return
    assert_written(first)
    granted = answer in YES_ANSWERS and when == "next message"
    if granted:
        assert "turn=1/3 batch" in machine(first)
        for k, step in enumerate(STEPS[1:3], start=2):
            result = await nh.call("nh_add_cell", turn, **step)
            assert_written(result)
            assert f"turn={k}/3 batch" in machine(result)
        late = await nh.call("nh_add_cell", turn, **STEPS[3])
        assert late.is_error and lines(late)[:3] == [
            "Not written (by design): the approved batch's 3 cells are written; the last is "
            '"Count rows per region" [3].',
            "nh: E110",
            batch.FULL_LINE,
        ], text(late)
        assert len(nh_code_cells(nh)) == 3
        assert [(e["turn_id"], e["n"], e["total"]) for e in events(nh, "batch_granted")] == [
            (turn, 3, 3)
        ]
        assert turn_state(nh, turn)["batch_total"] == 3
    else:
        assert "turn=1/1 retries" in machine(first)
        late = await nh.call("nh_add_cell", turn, **STEPS[1])
        assert late.is_error and "nh: E110" in text(late) and "batch" not in text(late)
        assert len(nh_code_cells(nh)) == 1 and events(nh, "batch_granted") == []
        assert turn_state(nh, turn)["batch_total"] == 0
    assert pending(nh) is None


@pytest.mark.parametrize("request_text", ["run steps 3-5", "run the next three"])
async def test_both_request_forms_grant_the_batch(nh: Harness, request_text: str) -> None:
    await ask_and_answer(nh, request=request_text)
    for step in STEPS[:3]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    assert "nh: E110" in text(await nh.call("nh_add_cell", "p2", **STEPS[3]))


async def test_a_batch_is_used_once(nh: Harness) -> None:
    """A second yes later grants nothing: the yes message's own request (None) is the next
    message's prev_request."""
    await ask_and_answer(nh)
    for step in STEPS[:3]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    nh.turns.prompt("p3", text="yes")
    assert_written(await nh.call("nh_add_cell", "p3", **STEPS[3]))
    late = await nh.call("nh_add_cell", "p3", **STEPS[4])
    assert late.is_error and "nh: E110" in text(late)
    assert len(nh_code_cells(nh)) == 4
    assert [e["turn_id"] for e in events(nh, "batch_granted")] == ["p2"]


async def test_a_yes_message_with_no_write_yet_keeps_its_batch_for_its_first_call(
    nh: Harness,
) -> None:
    """The batch is decided by the message's first gated call, not by an inspect or a wait."""
    await ask_and_answer(nh)
    assert not (await nh.call("nh_inspect", "p2", view="outline")).is_error
    for step in STEPS[:3]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))


async def test_max_batch_caps_the_batch(nh: Harness) -> None:
    """ "run the next 6" asks for more than [turn] max_batch (5): the yes allows 5."""
    await ask_and_answer(nh, request="run the next 6")
    for k, step in enumerate(STEPS[:5], start=1):
        result = await nh.call("nh_add_cell", "p2", **step)
        assert_written(result)
        assert f"turn={k}/5 batch" in machine(result)
    late = await nh.call("nh_add_cell", "p2", **STEPS[5])
    assert late.is_error and "nh: E110" in text(late)
    assert lines(late)[0].startswith("Not written (by design): the approved batch's 5 cells")
    assert [(e["n"], e["total"]) for e in events(nh, "batch_granted")] == [(6, 5)]


async def test_a_lower_max_batch_in_harness_toml_caps_it(tmp_path: Path) -> None:
    async with batch_harness(tmp_path, "[turn]\nmax_batch = 2\n") as h:
        await ask_and_answer(h)
        for step in STEPS[:2]:
            assert_written(await h.call("nh_add_cell", "p2", **step))
        late = await h.call("nh_add_cell", "p2", **STEPS[2])
        assert late.is_error and lines(late)[1:3] == ["nh: E110", batch.FULL_LINE], text(late)
        assert lines(late)[0].startswith("Not written (by design): the approved batch's 2 cells")


async def test_a_batch_request_typed_mid_turn_is_granted_by_the_next_yes(nh: Harness) -> None:
    """6.1: an absorbed request becomes the turn's, so the next message's prev_request."""
    nh.turns.prompt("p1", text="load the sales data")
    assert_written(await nh.call("nh_add_cell", "p1", **STEPS[0]))
    nh.turns.prompt("p1", text="then run the next 2")  # mode ask for the rest of p1
    assert "nh: E109" in text(await nh.call("nh_add_cell", "p1", **STEPS[1]))
    nh.turns.prompt("p2", text="yes")
    for step in STEPS[1:3]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    assert "nh: E110" in text(await nh.call("nh_add_cell", "p2", **STEPS[3]))


# --- the batch's stops -----------------------------------------------------------------------


async def test_an_error_stops_the_batch(nh: Harness) -> None:
    await ask_and_answer(nh)
    first = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert next_text(first).startswith("Step 1 of 3 of the approved batch ran OK.")
    failed = await nh.call("nh_add_cell", "p2", **FAILING)
    assert failed.meta["nh/status"] == "error" and "turn=2/3 batch" in machine(failed)
    assert next_text(failed).startswith(
        'The approved batch stops at step 2 of 3: "Price per unit" [2] failed.'
    )
    uid = failed.meta["nh/cell_id"]
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"], text(after)
    retry = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="per_unit = 1 / 1\nper_unit")
    assert lines(retry)[:2] == [stopped("2 of 3"), "nh: E123"]  # no retry in a batch
    rerun = await nh.call("nh_run", "p2", cell_id=uid)
    assert lines(rerun)[:2] == [stopped("2 of 3", "run"), "nh: E123"]
    assert lines(rerun)[-1] == f"Next: {CATALOGUE['E123'][1]}"
    assert len(nh_code_cells(nh)) == 2
    assert [(e["step"], e["total"], e["reason"]) for e in events(nh, "batch_stopped")] == [
        (2, 3, "error")
    ]
    assert turn_state(nh, "p2")["batch_stop"] == 2


async def test_a_check_this_stops_the_batch(nh: Harness) -> None:
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    checked = await nh.call("nh_add_cell", "p2", **CHECKED)
    assert_written(checked)
    assert "--- check this ---" in text(checked)
    assert next_text(checked).startswith(
        'The approved batch stops at step 2 of 3: "Keep expensive sales" [2] ran, but its '
        "result needs a look (see 'check this')."
    )
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"]
    uid = checked.meta["nh/cell_id"]
    tidy = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="expensive = df[df['price'] > 2]")
    assert lines(tidy)[:2] == [stopped("2 of 3"), "nh: E123"]
    assert [e["reason"] for e in events(nh, "batch_stopped")] == ["check_this"]


@pytest.mark.parametrize("why", ["running", "queued"])
async def test_a_cell_still_running_or_queued_stops_the_batch(tmp_path: Path, why: str) -> None:
    async with batch_harness(tmp_path, "[exec]\nsoft_timeout_s = 5\n") as h:  # 5 s at least
        await ask_and_answer(h)
        assert_written(await h.call("nh_add_cell", "p2", **STEPS[0]))
        if why == "running":
            h.backend.exec_delay_s = 6
        else:
            h.backend.kernel_busy = True
        slow = await h.call("nh_add_cell", "p2", **STEPS[1])
        assert slow.meta["nh/status"] == why, text(slow)
        assert next_text(slow).endswith(
            "The approved batch stops at step 2 of 3: say which planned steps did not run."
        )
        after = await h.call("nh_add_cell", "p2", **STEPS[2])
        assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"]
        assert [e["reason"] for e in events(h, "batch_stopped")] == [why]
        h.backend.kernel_busy = False
        waited = await h.call("nh_run", "p2", cell_id=slow.meta["nh/cell_id"], mode="wait")
        assert waited.meta["nh/status"] == "ok", text(waited)
        assert next_text(waited).startswith(
            '"Total price by region" [2] ran OK, but the approved batch stopped at step 2 of 3.'
        )
        again = await h.call("nh_add_cell", "p2", **STEPS[2])
        assert lines(again)[:2] == [stopped("2 of 3"), "nh: E123"]  # a stop is for the message
        edit = await h.call(
            "nh_edit_cell", "p2", cell_id=slow.meta["nh/cell_id"], code=STEPS[1]["code"] + "\n"
        )
        assert lines(edit)[:2] == [stopped("2 of 3"), "nh: E123"]


async def test_an_e122_inside_a_batch_stops_it_and_the_next_yes_grants_that_call_alone(
    nh: Harness,
) -> None:
    """A step nh asks about: its question is recorded as any cell's (6.4), and the batch
    stops there. The next message's yes writes that exact call, as its one cell: no batch."""
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    asked = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert lines(asked) == [
        FIRST,
        "nh: E122",
        FINDING,
        batch.STOP_LINE.format(step=2, total=3),
        ASK_NEXT,
    ]
    assert pending(nh)["turn_id"] == "p2" and pending(nh)["key"] == install_key()
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"]
    assert [e["reason"] for e in events(nh, "batch_stopped")] == ["E122"]
    nh.turns.prompt("p3", text="yes")
    granted = await nh.call("nh_add_cell", "p3", **INSTALL)
    assert_written(granted)
    assert "turn=1/1 retries" in machine(granted)
    late = await nh.call("nh_add_cell", "p3", **STEPS[1])
    assert late.is_error and "nh: E110" in text(late) and "batch" not in text(late)
    assert [c["source"] for c in nh_code_cells(nh)] == [STEPS[0]["code"], INSTALL["code"]]


async def test_the_first_step_asking_stops_the_batch_at_step_1(nh: Harness) -> None:
    await ask_and_answer(nh)
    asked = await nh.call("nh_add_cell", "p2", **INSTALL)
    assert batch.STOP_LINE.format(step=1, total=3) in lines(asked)
    after = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert lines(after)[:2] == [stopped("1 of 3"), "nh: E123"]


async def test_a_yes_that_could_answer_a_cell_question_and_a_batch_answers_the_question(
    nh: Harness,
) -> None:
    """First ask wins: the cell was asked about before the batch request was typed mid-turn,
    so the yes grants that call and no batch."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p1", **INSTALL))
    nh.turns.prompt("p1", text="and run the next 3")  # absorbed: request batch, mode ask
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    late = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert late.is_error and "nh: E110" in text(late)
    assert events(nh, "batch_granted") == []
    assert [(e["turn_id"], e["reason"]) for e in events(nh, "batch_not_granted")] == [
        ("p2", "pending")
    ]


async def test_headless_grants_no_batch(nh: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    await ask_and_answer(nh)
    monkeypatch.setenv("NH_HEADLESS", "1")
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    late = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert late.is_error and "nh: E110" in text(late)
    assert [e["reason"] for e in events(nh, "batch_not_granted")] == ["headless"]


# --- each step waits for the last one's result (E133; review C6a) ----------------------------


def _after_probe_held(h: Harness) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the next report's after-probe (its run is over, its 'check this' not known yet):
    (entered, release)."""
    entered, release = asyncio.Event(), asyncio.Event()
    real = h.backend.probe

    async def held(ref: Any, name: str, args: dict[str, Any], timeout: float) -> Any:
        reporting = any(frame.function == "report_run" for frame in inspect.stack(0))
        if name == "vars" and not entered.is_set() and reporting:
            entered.set()
            await release.wait()
        return await real(ref, name, args, timeout)

    h.backend.probe = held  # type: ignore[method-assign]
    return entered, release


async def test_a_parallel_step_gets_e133_until_the_last_result_is_in(nh: Harness) -> None:
    """Two steps sent at once, the first still running when the second takes the lock: the
    second gets E133 (nothing is written and nothing stops), and sent again after the first
    one's OK result it is written as the next step."""
    await ask_and_answer(nh)
    nh.backend.exec_delay_s = 0.3
    results = await asyncio.gather(
        nh.call("nh_add_cell", "p2", **STEPS[0]), nh.call("nh_add_cell", "p2", **STEPS[1])
    )
    nh.backend.exec_delay_s = 0
    bodies = [text(r) for r in results]
    [written] = [r for r in results if not r.is_error]
    [waited] = [r for r in results if r.is_error]
    assert lines(waited) == WAITING("1"), bodies
    assert next_text(written).startswith("Step 1 of 3 of the approved batch ran OK."), bodies
    assert events(nh, "batch_stopped") == [] and len(nh_code_cells(nh)) == 1
    first_was_load = '"Load sales data"' in lines(written)[0]
    again = await nh.call("nh_add_cell", "p2", **(STEPS[1] if first_was_load else STEPS[0]))
    assert_written(again)
    assert "turn=2/3 batch" in machine(again)


async def test_two_quick_steps_sent_at_once_never_skip_a_check(nh: Harness) -> None:
    """No exec delay: the first step's run is over before the second call takes the lock, but
    its report isn't built. Its cell still reads running (record_finish leaves a going batch's
    step to its report), so the second gets E133, and no step is written after a 'check this'
    one."""
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    results = await asyncio.gather(
        nh.call("nh_add_cell", "p2", **CHECKED), nh.call("nh_add_cell", "p2", **STEPS[1])
    )
    bodies = [text(r) for r in results]
    [written] = [r for r in results if not r.is_error]
    [waited] = [r for r in results if r.is_error]
    assert lines(waited) == WAITING("2"), bodies
    assert len(nh_code_cells(nh)) == 2
    if "--- check this ---" in text(written):  # the checked step took the lock first
        assert [(e["step"], e["reason"]) for e in events(nh, "batch_stopped")] == [
            (2, "check_this")
        ]
        again = await nh.call("nh_add_cell", "p2", **STEPS[1])
        assert lines(again)[:2] == [stopped("2 of 3"), "nh: E123"]
        assert len(nh_code_cells(nh)) == 2


@pytest.mark.parametrize("step", ["checked", "ok"])
async def test_the_next_step_waits_for_the_last_steps_check(nh: Harness, step: str) -> None:
    """Held at step 2's after-probe, step 2 reads running in the ledger, so step 3, an edit and
    a re-run of a step all get E133 and nothing stops. Then step 2's result decides: its
    'check this' stops the batch (step 3 sent again: E123), an OK one lets step 3 in."""
    await ask_and_answer(nh)
    first = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert_written(first)
    entered, release = _after_probe_held(nh)
    second = asyncio.create_task(
        nh.call("nh_add_cell", "p2", **(CHECKED if step == "checked" else STEPS[1]))
    )
    await asyncio.wait_for(entered.wait(), 10)
    state = turn_state(nh, "p2")
    assert [state["status"][uid] for uid in state["claims"]] == ["ok", "running"]
    uid = first.meta["nh/cell_id"]
    for call in (
        nh.call("nh_add_cell", "p2", **STEPS[2]),
        nh.call("nh_edit_cell", "p2", cell_id=uid, code=STEPS[0]["code"] + "\n"),
        nh.call("nh_run", "p2", cell_id=uid),
    ):
        assert lines(await call) == WAITING("2")
    assert turn_state(nh, "p2")["batch_stop"] == 0 and events(nh, "batch_stopped") == []
    release.set()
    result = await second
    third = await nh.call("nh_add_cell", "p2", **STEPS[2])
    if step == "checked":
        assert "--- check this ---" in text(result)
        assert next_text(result).startswith("The approved batch stops at step 2 of 3:")
        assert lines(third)[:2] == [stopped("2 of 3"), "nh: E123"]
        assert [(e["step"], e["reason"]) for e in events(nh, "batch_stopped")] == [
            (2, "check_this")
        ]
        assert len(nh_code_cells(nh)) == 2
    else:
        assert next_text(result).startswith("Step 2 of 3 of the approved batch ran OK.")
        assert_written(third)
        assert "turn=3/3 batch" in machine(third)


async def test_a_full_batchs_parallel_add_gets_e110_not_e133(nh: Harness) -> None:
    """With N cells claimed, one more add is E110 at once, even while the last step's result
    isn't in: waiting for it couldn't let the cell in."""
    await ask_and_answer(nh, request="run the next 2")
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    entered, release = _after_probe_held(nh)
    second = asyncio.create_task(nh.call("nh_add_cell", "p2", **STEPS[1]))
    await asyncio.wait_for(entered.wait(), 10)
    third = await nh.call("nh_add_cell", "p2", **STEPS[2])
    assert lines(third)[1:3] == ["nh: E110", batch.FULL_LINE], text(third)
    release.set()
    assert next_text(await second).startswith("That was step 2 of 2")
    assert len(nh_code_cells(nh)) == 2


# --- more of the grant -----------------------------------------------------------------------


async def test_an_older_pending_question_does_not_block_the_batch(nh: Harness) -> None:
    """First ask wins only against a question of the previous message: one left from an
    earlier message (its asking message's writes never reached the ledger again) doesn't."""
    nh.turns.prompt("p0", text="install seaborn")
    assert_asked(await nh.call("nh_add_cell", "p0", **INSTALL))
    await ask_and_answer(nh)
    assert pending(nh) is not None and pending(nh)["turn_id"] == "p0"
    for k, step in enumerate(STEPS[:3], start=1):
        result = await nh.call("nh_add_cell", "p2", **step)
        assert_written(result)
        assert f"turn={k}/3 batch" in machine(result)
    assert [e["turn_id"] for e in events(nh, "batch_granted")] == ["p2"]


@pytest.mark.parametrize("call", ["edit", "re-run"])
async def test_an_edit_or_a_rerun_as_the_first_call_decides_the_batch(
    nh: Harness, call: str
) -> None:
    """The yes message's first gated call decides the batch whatever it is (design §6.3): a
    re-run of an earlier message's failed cell is step 1 and stops the batch on its error; a
    fix of it is step 1 and the batch goes on."""
    nh.turns.prompt("p0", text="per unit")
    failed = await nh.call("nh_add_cell", "p0", **FAILING)
    assert failed.meta["nh/status"] == "error"
    uid = failed.meta["nh/cell_id"]
    await ask_and_answer(nh)
    if call == "re-run":
        rerun = await nh.call("nh_run", "p2", cell_id=uid)
        assert rerun.meta["nh/status"] == "error" and "turn=1/3 batch" in machine(rerun)
        assert next_text(rerun).startswith("The approved batch stops at step 1 of 3:")
        assert [e["reason"] for e in events(nh, "batch_stopped")] == ["error"]
        return
    fixed = await nh.call("nh_edit_cell", "p2", cell_id=uid, code="per_unit = 1 / 1\nper_unit")
    assert_written(fixed)
    assert "turn=1/3 batch" in machine(fixed)
    assert next_text(fixed).startswith("Step 1 of 3 of the approved batch ran OK.")
    assert "turn=2/3 batch" in machine(await nh.call("nh_add_cell", "p2", **STEPS[0]))


async def test_a_batch_caps_at_its_n_below_a_raised_max_code_cells(tmp_path: Path) -> None:
    async with batch_harness(tmp_path, "[turn]\nmax_code_cells = 5\n") as h:
        await ask_and_answer(h, request="run the next 2")
        for step in STEPS[:2]:
            assert_written(await h.call("nh_add_cell", "p2", **step))
        late = await h.call("nh_add_cell", "p2", **STEPS[2])
        assert late.is_error and lines(late)[1:3] == ["nh: E110", batch.FULL_LINE], text(late)
        assert len(nh_code_cells(h)) == 2


async def test_a_stop_on_the_last_step_gives_e123_not_e110(nh: Harness) -> None:
    """E123 comes before E110's cap; at the last step every planned step ran, so its block
    doesn't ask which did not."""
    await ask_and_answer(nh, request="run the next 2")
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    failed = await nh.call("nh_add_cell", "p2", **FAILING)
    assert next_text(failed) == (
        'The approved batch stops at step 2 of 2: "Price per unit" [2] failed. Don\'t fix it in '
        "this message: a batch has no retries. Explain in plain words: quote the failing code, "
        "what Python said, the likely cause and one fix; then wait."
    )
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("2 of 2"), "nh: E123"], text(after)


# --- more of the stops -----------------------------------------------------------------------

MARKED = "key = '[redacted:API_KEY]'"


async def test_e125_on_an_edit_stops_the_batch(nh: Harness) -> None:
    await ask_and_answer(nh)
    first = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert_written(first)
    marked = await nh.call("nh_edit_cell", "p2", cell_id=first.meta["nh/cell_id"], code=MARKED)
    assert lines(marked)[1] == "nh: E125"
    assert lines(marked)[-2:] == [
        batch.STOP_LINE.format(step=1, total=3),
        f"Next: {batch.REFUSED_NEXT}",
    ]
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("1 of 3"), "nh: E123"]


async def test_an_e122_on_an_edit_stops_the_batch(nh: Harness) -> None:
    """The yes message's first call edits an earlier cell into one nh asks about."""
    nh.turns.prompt("p0", text="load the sales data")
    loaded = await nh.call("nh_add_cell", "p0", **LOAD)
    assert_written(loaded)
    await ask_and_answer(nh)
    asked = await nh.call(
        "nh_edit_cell", "p2", cell_id=loaded.meta["nh/cell_id"], code=INSTALL["code"]
    )
    assert lines(asked)[1] == "nh: E122"
    assert batch.STOP_LINE.format(step=1, total=3) in lines(asked)
    assert pending(nh)["turn_id"] == "p2"
    after = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert lines(after)[:2] == [stopped("1 of 3"), "nh: E123"]


async def test_e121_stops_the_batch(tmp_path: Path) -> None:
    async with batch_harness(tmp_path, "[turn]\nmax_lint_rejects = 0\n") as h:
        await ask_and_answer(h)
        refused = await h.call("nh_add_cell", "p2", **STEPS[0])
        assert lines(refused)[1] == "nh: E121"
        assert lines(refused)[-2:] == [
            batch.STOP_LINE.format(step=1, total=3),
            f"Next: {batch.REFUSED_NEXT}",
        ]
        assert [e["reason"] for e in events(h, "batch_stopped")] == ["E121"]


async def test_a_non_e12x_refusal_does_not_stop_the_batch(nh: Harness) -> None:
    await ask_and_answer(nh)
    first = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert_written(first)
    edit = await nh.call(
        "nh_edit_cell", "p2", cell_id=first.meta["nh/cell_id"], code=STEPS[0]["code"] + "\n"
    )
    assert lines(edit)[1] == "nh: E112"
    assert "batch" not in text(edit)
    second = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert_written(second)
    assert "turn=2/3 batch" in machine(second)
    assert events(nh, "batch_stopped") == []


async def test_e125_after_a_full_batch_names_step_n(nh: Harness) -> None:
    await ask_and_answer(nh, request="run the next 2")
    for step in STEPS[:2]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    marked = await nh.call("nh_add_cell", "p2", **dict(LOAD, code=MARKED))
    assert lines(marked)[1] == "nh: E125"
    assert batch.STOP_LINE.format(step=2, total=2) in lines(marked), text(marked)


async def test_e125_after_a_stop_is_e123(nh: Harness) -> None:
    """E125 comes before the lock, but once the batch has stopped it is E123 like every other
    call: its own Next (rewrite and call again) would only meet E123."""
    await ask_and_answer(nh)
    failed = await nh.call("nh_add_cell", "p2", **FAILING)
    assert failed.meta["nh/status"] == "error"
    for call in (
        nh.call("nh_add_cell", "p2", **dict(LOAD, code=MARKED)),
        nh.call("nh_edit_cell", "p2", cell_id=failed.meta["nh/cell_id"], code=MARKED),
    ):
        body = lines(await call)
        assert body[:2] == [stopped("1 of 3"), "nh: E123"] and body[-1] == (
            f"Next: {CATALOGUE['E123'][1]}"
        )
    assert turn_state(nh, "p2")["batch_stop"] == 1
    assert [e["reason"] for e in events(nh, "batch_stopped")] == ["error"]


@pytest.mark.parametrize("earlier", ["ok", "error"])
async def test_a_wait_on_an_earlier_messages_cell_in_a_batch(tmp_path: Path, earlier: str) -> None:
    """An earlier message's cell isn't a step: its OK result gets the plain block and the
    batch goes on; its error stops the batch at the steps written so far, and its block offers
    no retry (E123 would refuse one)."""
    async with batch_harness(tmp_path, "[exec]\nsoft_timeout_s = 5\n") as h:
        h.turns.prompt("p0", text="load it")
        h.backend.exec_delay_s = 8  # still running after the soft timeout and the ask message
        slow = await h.call("nh_add_cell", "p0", **(LOAD if earlier == "ok" else FAILING))
        assert slow.meta["nh/status"] == "running", text(slow)
        h.backend.exec_delay_s = 0
        uid = slow.meta["nh/cell_id"]
        await ask_and_answer(h)
        busy = await h.call("nh_add_cell", "p2", **STEPS[1])  # decides the batch; no stop
        assert lines(busy)[1] == "nh: E133", text(busy)
        if earlier == "ok":
            waited = await h.call("nh_run", "p2", cell_id=uid, mode="wait")
            assert waited.meta["nh/status"] == "ok", text(waited)
            assert "approved batch" not in next_text(waited)
            step = await h.call("nh_add_cell", "p2", **STEPS[1])
            assert_written(step)
            assert "turn=1/3 batch" in machine(step) and events(h, "batch_stopped") == []
            return
        deadline = time.monotonic() + 15
        while turn_state(h, "p0")["status"].get(uid) != "error" and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert_written(await h.call("nh_add_cell", "p2", **LOAD))
        waited = await h.call("nh_run", "p2", cell_id=uid, mode="wait")
        assert waited.meta["nh/status"] == "error", text(waited)
        assert next_text(waited).startswith(
            '"Price per unit" [1] failed, and the approved batch stopped at step 1 of 3. '
            "Don't fix it in this message: a batch has no retries."
        )
        assert [(e["step"], e["reason"]) for e in events(h, "batch_stopped")] == [(1, "error")]
        after = await h.call("nh_add_cell", "p2", **STEPS[1])
        assert lines(after)[:2] == [stopped("1 of 3"), "nh: E123"]


# --- an undo in a batch ----------------------------------------------------------------------


async def test_an_undo_stops_the_batch_at_the_undone_step(nh: Harness) -> None:
    """The steps after an undone cell would build on a notebook without it: the undo goes
    through and stops the batch there; every later call, an undo too, gets E123."""
    await ask_and_answer(nh)
    for step in STEPS[:2]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    undone = await nh.call("nh_undo", "p2")
    assert not undone.is_error
    assert any(line.startswith('Removed "Total price by region" [2]') for line in lines(undone))
    assert next_text(undone).endswith(" " + batch.UNDONE_NEXT.format(step=2, total=3))
    after = await nh.call("nh_add_cell", "p2", **STEPS[2])
    assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"]
    again = await nh.call("nh_undo", "p2")
    assert lines(again)[:2] == [stopped("2 of 3", "undone"), "nh: E123"]
    assert [(e["step"], e["reason"]) for e in events(nh, "batch_stopped")] == [(2, "undo")]
    assert len(nh_code_cells(nh)) == 1


async def test_an_undo_after_a_stop_is_e123(nh: Harness) -> None:
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    assert (await nh.call("nh_add_cell", "p2", **FAILING)).meta["nh/status"] == "error"
    refused = await nh.call("nh_undo", "p2")
    assert lines(refused)[:2] == [stopped("2 of 3", "undone"), "nh: E123"]
    assert len(nh_code_cells(nh)) == 2


async def test_an_undo_of_a_step_already_deleted_stops_the_batch(nh: Harness) -> None:
    """E142's path (the user deleted nh's cell in JupyterLab): nothing is undone, but the steps
    after it would still build on a notebook without it, so the batch stops there."""
    await ask_and_answer(nh)
    for step in STEPS[:2]:
        assert_written(await nh.call("nh_add_cell", "p2", **step))
    nh.backend.user_delete(NOTEBOOK, nh_code_cells(nh)[1]["id"])
    undone = await nh.call("nh_undo", "p2")
    assert "Nothing undone" in text(undone)
    assert batch.UNDONE_NEXT.format(step=2, total=3) in text(undone)
    after = await nh.call("nh_add_cell", "p2", **STEPS[2])
    assert lines(after)[:2] == [stopped("2 of 3"), "nh: E123"]
    assert [(e["step"], e["reason"]) for e in events(nh, "batch_stopped")] == [(2, "undo")]


async def test_an_undo_restoring_an_edited_step_stops_the_batch(nh: Harness) -> None:
    """The yes message's first step fixes an earlier message's cell; undoing that fix restores
    the old code and stops the batch at step 1."""
    nh.turns.prompt("p0", text="per unit")
    failed = await nh.call("nh_add_cell", "p0", **FAILING)
    uid = failed.meta["nh/cell_id"]
    await ask_and_answer(nh)
    assert_written(
        await nh.call("nh_edit_cell", "p2", cell_id=uid, code="per_unit = 1 / 1\nper_unit")
    )
    undone = await nh.call("nh_undo", "p2")
    assert not undone.is_error
    assert next_text(undone).endswith(" " + batch.UNDONE_NEXT.format(step=1, total=3))
    after = await nh.call("nh_add_cell", "p2", **STEPS[0])
    assert lines(after)[:2] == [stopped("1 of 3"), "nh: E123"]
    assert [(e["step"], e["reason"]) for e in events(nh, "batch_stopped")] == [(1, "undo")]


async def test_an_undo_outside_a_batch_is_unchanged(nh: Harness) -> None:
    nh.turns.prompt("p1", text="load it")
    assert_written(await nh.call("nh_add_cell", "p1", **LOAD))
    undone = await nh.call("nh_undo", "p1")
    assert not undone.is_error and "batch" not in text(undone)


# --- nh:cell-writer in a batch (C6a; the writer column comes in C6b) --------------------------


async def test_one_qa_cell_run_writes_one_cell_of_a_batch(nh: Harness) -> None:
    """Before C6a the cap of 1 kept a run to one cell; a batch's cap of N doesn't let one run
    (one QA pass) write N cells. C6b's per-slot runs give each step its own run."""
    await ask_and_answer(nh)
    first = await add(nh, "writer", "p2", "wf_run-1", **STEPS[0])
    assert_written(first)
    assert "turn=1/3 batch" in machine(first)
    second = await add(nh, "writer", "p2", "wf_run-1", **STEPS[1])
    assert lines(second) == [batch.RUN_HEAD, "nh: E110", f"Next: {RETURN_TO_WORKFLOW}"]
    other = await add(nh, "writer", "p2", "wf_run-2", **STEPS[1])
    assert lines(other)[1:3] == ["nh: E110", "- Another nh:qa-cell run owns this message's cell."]
    assert len(nh_code_cells(nh)) == 1 and events(nh, "batch_stopped") == []


async def test_a_writers_failed_step_gets_no_retry(nh: Harness) -> None:
    await ask_and_answer(nh)
    failed = await add(nh, "writer", "p2", "wf_run-1", **FAILING)
    assert failed.meta["nh/status"] == "error" and "turn=1/3 batch" in machine(failed)
    assert next_text(failed) == (
        '"Price per unit" [1] failed, so the approved batch stops here, at step 1 of 3: a batch '
        "has no retries. Return this whole result to the workflow as your final answer (status "
        "error). Don't change it again."
    )
    fix = await as_writer(
        nh, "p2", "wf_run-1", "nh_edit_cell", dict(cell_id=failed.meta["nh/cell_id"], code="1")
    )
    assert lines(fix)[:2] == [stopped("1 of 3"), "nh: E123"]
    assert lines(fix)[-1] == f"Next: {RETURN_TO_WORKFLOW}"


async def test_a_writers_step_sent_too_early_returns_to_the_workflow(nh: Harness) -> None:
    """A writer's E133 for a step whose result isn't in has RETURN_TO_WORKFLOW, not the main
    conversation's wait-and-resend; nothing stops."""
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    entered, release = _after_probe_held(nh)
    second = asyncio.create_task(nh.call("nh_add_cell", "p2", **STEPS[1]))
    await asyncio.wait_for(entered.wait(), 10)
    waited = await add(nh, "writer", "p2", "wf_run-9", **STEPS[2])
    assert lines(waited) == [WAITING("2")[0], "nh: E133", f"Next: {RETURN_TO_WORKFLOW}"]
    release.set()
    assert_written(await second)
    assert turn_state(nh, "p2")["batch_stop"] == 0


async def test_after_the_runs_step_the_main_conversation_writes_the_next(nh: Harness) -> None:
    """§6.3 Known gaps, "Ultracode until C6b": one run writes step 1, and once its report is
    in the main conversation may write the next step itself."""
    await ask_and_answer(nh)
    assert_written(await add(nh, "writer", "p2", "wf_run-1", **STEPS[0]))
    reported(nh, "wf_run-1")
    second = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert_written(second)
    assert "turn=2/3 batch" in machine(second)


# --- C1's rule in a batch (design §6.3, Known gaps) --------------------------------------------


async def test_a_stop_typed_mid_batch_leaves_the_gateways_batch_going(nh: Harness) -> None:
    """An absorbed message only tightens and its no is never an answer (6.1), so the gateway
    goes on; C6b's reminder part tells Claude to stop. Changing this is the user's call."""
    await ask_and_answer(nh)
    assert_written(await nh.call("nh_add_cell", "p2", **STEPS[0]))
    nh.turns.prompt("p2", text="stop")
    second = await nh.call("nh_add_cell", "p2", **STEPS[1])
    assert_written(second)
    assert "turn=2/3 batch" in machine(second)
