"""L011 and L014 through the gateway (design §6.7): a cell that would show an env var's value is
refused as a hard-rule failure and counts as a lint reject, in the main thread and the writer's;
a shown secret name is a hint on a written cell."""

from __future__ import annotations

import json

import pytest

from nh_gateway._shared.paths import Layout
from nh_gateway.policy.errors import CATALOGUE, WRITER_LINE
from tests.fakes.turns import text
from tests.gateway.conftest import Harness

VALUE = "sk-" + "fake0nlyForTests-9Qz7Lw2Xv8Rk"  # fake; set in the gateway's environment
RUN = "wf_run-1"
E120_FIRST = CATALOGUE["E120"][0]
E121_FIRST = CATALOGUE["E121"][0]
L011 = (
    '- L011: `print(os.environ["OPENAI_API_KEY"])` would show the value of env var '
    "`OPENAI_API_KEY`. Fix: Check it without showing the value, e.g. "
    '`print("OPENAI_API_KEY" in os.environ)` or `print(bool(os.getenv("OPENAI_API_KEY")))`.'
)

PRINTS = dict(
    title="Check the API key",
    notes=["Checks that the OpenAI key is set.", "Shows only whether it is there."],
    intent="check the API key is set",
    code='import os\n\nprint(os.environ["OPENAI_API_KEY"])',
)
CHECKS = dict(PRINTS, code='import os\n\nprint("OPENAI_API_KEY" in os.environ)')


def code_cells(h: Harness) -> list[dict]:
    return [c for c in h.cells() if c["cell_type"] == "code"]


def lint_rejects(h: Harness, prompt_id: str) -> int:
    ledger = json.loads(Layout(h.project).ledger_file("sess-1").read_text())
    return ledger["turns"][prompt_id]["lint_rejects"]


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", VALUE)


def assert_l011_refusal(body: str) -> None:
    lines = body.splitlines()
    assert lines[:2] == [E120_FIRST, "nh: E120"], lines
    assert L011 in lines, lines
    assert VALUE not in body and VALUE[3:12] not in body


async def test_a_main_add_that_prints_an_env_var_is_refused_and_counts(nh: Harness) -> None:
    nh.turns.prompt("p1")
    for count in range(1, 4):
        result = await nh.call("nh_add_cell", "p1", **PRINTS)
        assert result.is_error
        assert_l011_refusal(text(result))
        assert lint_rejects(nh, "p1") == count
    assert nh.cells() == []
    # max_lint_rejects is 3: the fourth attempt is E121, even with the fixed code
    fourth = await nh.call("nh_add_cell", "p1", **CHECKS)
    assert fourth.is_error and text(fourth).splitlines()[:2] == [E121_FIRST, "nh: E121"]
    assert nh.cells() == []


async def test_the_fixed_check_is_written_and_shows_no_value(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert (await nh.call("nh_add_cell", "p1", **PRINTS)).is_error
    added = await nh.call("nh_add_cell", "p1", **CHECKS)
    body = text(added)
    assert not added.is_error, body
    assert "True" in body and VALUE not in body and "L011" not in body and "L014" not in body
    [cell] = code_cells(nh)
    assert cell["source"] == CHECKS["code"]
    assert VALUE not in json.dumps(cell["outputs"])
    assert lint_rejects(nh, "p1") == 1


async def test_a_main_edit_that_prints_an_env_var_is_refused_and_counts(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **CHECKS)).is_error
    [cell] = code_cells(nh)
    before = json.dumps(nh.cells())
    nh.turns.prompt("p2")
    result = await nh.call("nh_edit_cell", "p2", cell_id=cell["id"], code=PRINTS["code"])
    assert result.is_error
    assert_l011_refusal(text(result))
    assert json.dumps(nh.cells()) == before
    assert lint_rejects(nh, "p2") == 1


async def test_a_magic_that_shows_the_environment_is_refused(nh: Harness) -> None:
    nh.turns.prompt("p1")
    for code in ["%env", "!printenv OPENAI_API_KEY", "!echo $OPENAI_API_KEY"]:
        result = await nh.call("nh_add_cell", "p1", **dict(PRINTS, code=code))
        body = text(result)
        assert result.is_error and body.splitlines()[:2] == [E120_FIRST, "nh: E120"], body
        assert "- L011: " in body and VALUE not in body
    assert nh.cells() == []
    assert lint_rejects(nh, "p1") == 3


async def test_showing_the_environs_keys_is_refused(nh: Harness) -> None:
    """os.environ.keys() is a view whose repr holds environ({...}): every value."""
    nh.turns.prompt("p1")
    for code in ["import os\n\nos.environ.keys()", "import os\n\nprint(os.environ.keys())"]:
        result = await nh.call("nh_add_cell", "p1", **dict(PRINTS, code=code))
        body = text(result)
        assert result.is_error and body.splitlines()[:2] == [E120_FIRST, "nh: E120"], body
        assert "would show every env var's value" in body and VALUE not in body
    names = await nh.call(
        "nh_add_cell", "p1", **dict(PRINTS, code="import os\n\nsorted(os.environ.keys())")
    )
    assert not names.is_error, text(names)
    assert VALUE not in json.dumps(code_cells(nh)[0]["outputs"])


async def test_the_writers_add_is_refused_for_it_to_fix(nh: Harness) -> None:
    """E120 has no writer line: the writer fixes the code and calls again."""
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", PRINTS, "p1")
    body = text(result)
    assert result.is_error
    assert_l011_refusal(body)
    assert WRITER_LINE not in body
    assert nh.cells() == []
    assert lint_rejects(nh, "p1") == 1
    fixed = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", CHECKS, "p1")
    assert not fixed.is_error, text(fixed)
    [cell] = code_cells(nh)
    assert cell["source"] == CHECKS["code"]


async def test_the_writers_edit_is_refused_for_it_to_fix(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", CHECKS, "p1")
    assert not added.is_error, text(added)
    [cell] = code_cells(nh)
    before = json.dumps(nh.cells())
    edit = {"cell_id": cell["id"], "code": PRINTS["code"]}
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_edit_cell", edit, "p1")
    body = text(result)
    assert result.is_error
    assert_l011_refusal(body)
    assert WRITER_LINE not in body
    assert json.dumps(nh.cells()) == before
    assert lint_rejects(nh, "p1") == 1


async def test_with_secret_print_as_a_hint_the_cell_runs_and_claude_reads_the_marker(
    nh: Harness,
) -> None:
    """What harness-toml.md promises: at "hint" the cell is written and prints the value into
    the notebook, while what Claude reads holds nh's marker."""
    toml = nh.project / "harness.toml"
    toml.write_text(toml.read_text() + '[lint.rules]\nsecret_print = "hint"\n')
    nh.turns.prompt("p1")
    added = await nh.call("nh_add_cell", "p1", **PRINTS)
    body = text(added)
    assert not added.is_error, body
    assert "L011" not in body  # the hint's rule id is not shown, its message is
    assert "would show the value of env var `OPENAI_API_KEY`" in body
    assert "[redacted:OPENAI_API_KEY]" in body and VALUE not in body
    [cell] = code_cells(nh)
    assert VALUE in json.dumps(cell["outputs"])
    assert lint_rejects(nh, "p1") == 0


async def test_a_shown_secret_name_is_a_hint_on_a_written_cell(nh: Harness) -> None:
    nh.turns.prompt("p1")
    code = 'api_key = "placeholder-for-the-test"\nprint(api_key)'
    added = await nh.call("nh_add_cell", "p1", **dict(PRINTS, code=code))
    body = text(added)
    assert not added.is_error, body
    assert (
        "- `print(api_key)` shows `api_key`, whose name says it holds a secret. Show whether it "
        "is set instead, e.g. `print(bool(api_key))`, or leave it out of the output."
    ) in body.splitlines()
    assert lint_rejects(nh, "p1") == 0
