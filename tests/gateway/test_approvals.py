"""FR-12 cell approvals (design §6.4): a cell the lint asks about (L009, L012 and L013 by
default) waits for the user's yes (E122), and the yes in the next message lets exactly that call
through once; a host in the project's approved list needs none. Through the real hooks and the
gateway (FakeBackend)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from nh_gateway import config
from nh_gateway._shared.paths import Layout, atomic_write_json
from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from nh_gateway.lint.lint import Issue
from nh_gateway.policy.errors import CATALOGUE, RETURN_TO_WORKFLOW, WRITER_LINE, NhError
from nh_gateway.policy.turn import TurnState
from nh_gateway.tools import approvals
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
    assert set(json.loads(stored)["pending"]) == {"kind", "key", "turn_id", "ts"}
    nh.turns.prompt("p2", text="yes")
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    log = Layout(nh.project).log_file.read_text()
    for raw in (stored, Layout(nh.project).ledger_file(SESSION).read_text(), log):
        for word in ("seaborn", "plotly", "pip install", "Run it as it is", "env sync"):
            assert word not in raw, word
    assert install_key() in stored
    asked = events(nh, "cell_asked")
    assert asked and all(set(e) >= {"rules", "outcome"} and e["rules"] == ["L009"] for e in asked)


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
    it neither uses nor drops the grant, so the main conversation's call is still granted."""
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
    assert_written(await nh.call("nh_add_cell", "p2", **INSTALL))
    assert pending(nh) is None


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


# --- a yes typed during the run (design §6.4, A report for an earlier message; Known gaps) ---


@pytest.mark.parametrize("word", ["ok", "yes", "go"])
async def test_a_yes_typed_during_the_run_counts_for_the_writers_question(
    nh: Harness, word: str
) -> None:
    """Pinned as it is: the user's message typed after the writer's E122 and before the report
    is the next message after the asking one, so 6.0 b grants the exact call in it and in the
    report's turn (an alias of it), though nh's question never reached the user. QA_EARLIER and
    qa-workflow.md ("don't send the call in this reply") are the guard."""
    nh.turns.prompt("p1", text="install seaborn")
    assert_asked(await as_writer(nh, "p1", "wf_run-1", "nh_add_cell", INSTALL), writer=True)
    nh.turns.prompt("p2", text=word)  # typed while the run works in the background
    head = nh.turns.notification(
        "n-wf_run-1", tool_use_id="toolu_wf_run-1", task_id="task-wf_run-1"
    )
    assert head is not None and "change no cell for it" in json.dumps(head)  # QA_EARLIER
    assert_written(await nh.call("nh_add_cell", "n-wf_run-1", **INSTALL))
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
