"""Redaction through the gateway (design §6.8) and E125, with the fake backend: what Claude
reads holds the marker; the notebook, the history and every sha keep the raw value."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from nh_gateway import app, meta
from nh_gateway._shared import secrets
from nh_gateway._shared.paths import Layout
from nh_gateway.backend import discovery, rest
from nh_gateway.backend.base import ServerInfo
from nh_gateway.backend.fake import FakeBackend
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.policy.errors import CATALOGUE, WRITER_LINE
from nh_gateway.tools import inspect as inspect_tool
from nh_gateway.tools import write as write_tool
from nh_gateway.tools.common import text_result
from tests.fakes.turns import text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project

PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
MARK = "[redacted:DB_PASSWORD]"
RUN = "wf_run-1"
E125_FIRST = CATALOGUE["E125"][0]

LOAD = dict(
    title="Load sales data",
    notes=["Builds a small frame of prices by region.", "Keeps the missing price for later."],
    intent="load the sales data",
    code="import pandas as pd\n\ndf = pd.DataFrame({'region': ['a', 'b'], 'price': [1.0, None]})\ndf.shape",
)
# What Claude would send after copying code from a cell view: the marker, not the value.
MARKED = dict(LOAD, code=LOAD["code"] + f"\npassword = '{MARK}'")


def with_env(h: Harness) -> Path:
    path = h.project / ".env"
    path.write_text(f"DB_PASSWORD={PASSWORD}\n")
    return path


def code_cells(h: Harness) -> list[dict]:
    return [c for c in h.cells() if c["cell_type"] == "code"]


def lint_rejects(h: Harness, prompt_id: str) -> int:
    ledger = json.loads(Layout(h.project).ledger_file("sess-1").read_text())
    return ledger["turns"][prompt_id]["lint_rejects"]


def assert_clean(body: str) -> None:
    """No piece of the password: the whole value, its head or its tail."""
    assert PASSWORD not in body
    assert PASSWORD[:6] not in body and PASSWORD[-6:] not in body


# --- E125: code holding the marker is never written --------------------------------------


async def test_e125_refuses_a_main_add_and_counts_no_lint_reject(nh: Harness) -> None:
    nh.turns.prompt("p1")
    for _ in range(3):  # max_lint_rejects is 3: three more would be E121 if they counted
        result = await nh.call("nh_add_cell", "p1", **MARKED)
        lines = text(result).splitlines()
        assert result.is_error and lines[:2] == [E125_FIRST, "nh: E125"], lines
        assert lines[-1].startswith("Next: Read the value from the environment")
        assert WRITER_LINE not in lines
    assert nh.cells() == []  # nothing written
    added = await nh.call("nh_add_cell", "p1", **LOAD)
    assert not added.is_error, text(added)
    assert lint_rejects(nh, "p1") == 0


async def test_e125_refuses_a_main_edit(nh: Harness) -> None:
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    [cell] = code_cells(nh)
    before = json.dumps(nh.cells())
    nh.turns.prompt("p2")
    for _ in range(3):
        result = await nh.call("nh_edit_cell", "p2", cell_id=cell["id"], code=MARKED["code"])
        assert result.is_error and text(result).splitlines()[:2] == [E125_FIRST, "nh: E125"]
    assert json.dumps(nh.cells()) == before
    edited = await nh.call("nh_edit_cell", "p2", cell_id=cell["id"], code=LOAD["code"] + "\n1")
    assert not edited.is_error, text(edited)
    assert lint_rejects(nh, "p2") == 0


async def test_e125_refuses_the_writers_add_with_the_writer_line(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    for _ in range(3):
        result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", MARKED, "p1")
        lines = text(result).splitlines()
        assert result.is_error and lines[:2] == [E125_FIRST, "nh: E125"], lines
        assert lines[-2].startswith("Next: Read the value") and lines[-1] == WRITER_LINE
    assert nh.cells() == []
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not added.is_error, text(added)
    assert lint_rejects(nh, "p1") == 0


async def test_e125_refuses_the_writers_edit_with_the_writer_line(nh: Harness) -> None:
    nh.turns.prompt("p1")
    nh.turns.workflow_launched("p1")
    added = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_add_cell", LOAD, "p1")
    assert not added.is_error, text(added)
    [cell] = code_cells(nh)
    before = json.dumps(nh.cells())
    edit = {"cell_id": cell["id"], "code": MARKED["code"]}
    result = await nh.turns.writer_call(nh.client, "w-1", RUN, "nh_edit_cell", edit, "p1")
    lines = text(result).splitlines()
    assert result.is_error and lines[:2] == [E125_FIRST, "nh: E125"], lines
    assert lines[-1] == WRITER_LINE
    assert json.dumps(nh.cells()) == before
    assert lint_rejects(nh, "p1") == 0


# --- raw vs redacted ------------------------------------------------------------------------

DSN = dict(
    title="Connect to the warehouse",
    notes=["Builds the warehouse connection string.", "Prints it to check the host."],
    intent="connect to the warehouse",
    code=(
        f'dsn = "postgresql://app:{PASSWORD}@db/prod"\n'
        "print(dsn)\n"
        'print("row " * 800)\n'  # past output max_chars (2000): the full copy is written
        "print(dsn)"
    ),
)


async def test_what_claude_reads_is_redacted_and_the_notebook_stays_raw(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    added = await nh.call("nh_add_cell", "p1", **DSN)
    body = text(added)
    assert not added.is_error, body
    assert MARK in body
    assert_clean(body)
    # The notebook keeps the code and the output as they are.
    [cell] = code_cells(nh)
    assert cell["source"] == DSN["code"]
    assert PASSWORD in json.dumps(cell["outputs"])
    # The history keeps the exact code, for undo.
    layout = Layout(nh.project)
    history = "".join(p.read_text() for p in layout.history.rglob("*") if p.is_file())
    assert PASSWORD in history
    # The full copy under .nh/outputs holds the marker, never the value.
    copies = [p for p in layout.outputs.rglob("*") if p.is_file() and p.suffix != ".png"]
    assert copies
    for copy in copies:
        saved = copy.read_text()
        assert MARK in saved
        assert_clean(saved)
    # The ledger (turn state and sha256 keys) and the event log hold no raw text.
    for path in [*layout.ledger.glob("*.json"), layout.log_file]:
        assert PASSWORD not in path.read_text(), path


async def test_the_cell_view_is_redacted_and_its_sha_is_the_raw_sources(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **DSN)).is_error
    [cell] = code_cells(nh)
    view = text(await nh.call("nh_inspect", "p2", view="cell", cell_id=cell["id"]))
    assert 'dsn = "postgresql://' + MARK + '@db/prod"' in view  # the source
    assert "--- outputs ---" in view and view.count(MARK) >= 2
    assert_clean(view)
    raw_sha = meta.source_sha(DSN["code"])
    assert f"sha={raw_sha}" in view
    assert cell["metadata"]["nh"]["source_sha"] == raw_sha


# --- one test per gateway site ------------------------------------------------------------


def install(root: Path) -> None:
    (root / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(root, {}))


def test_the_first_services_call_installs_the_projects_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also when .nh/ appears after the gateway started (/nh:init): the first call that finds
    the project installs its redactor, before any tool builds a text."""
    project = make_project(tmp_path)
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    monkeypatch.setattr(app, "find_project", lambda: None)
    runtime = app._Runtime(None, FakeBackend(project))
    assert runtime.services() is None and secrets.current() is secrets.PATTERNS_ONLY
    monkeypatch.setattr(app, "find_project", lambda: project)
    assert runtime.services() is not None
    assert secrets.current().root == project
    assert secrets.current().redact(PASSWORD) == MARK


async def test_services_installs_the_projects_redactor(nh: Harness) -> None:
    await nh.call("nh_inspect", "p1", view="status")
    assert secrets.current().root == nh.project
    assert secrets.current().redact(PASSWORD) == PASSWORD  # no .env yet
    with_env(nh)  # the next call picks it up
    await nh.call("nh_inspect", "p1", view="status")
    assert secrets.current().redact(f"pw={PASSWORD}") == f"pw={MARK}"


async def test_the_var_views_are_redacted(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **DSN)).is_error
    one = text(await nh.call("nh_inspect", "p2", view="var", name="dsn"))
    assert "dsn: str" in one and MARK in one
    assert_clean(one)
    every = text(await nh.call("nh_inspect", "p2", view="vars"))
    assert "dsn: str" in every and MARK in every
    assert_clean(every)


async def test_outline_rows_are_redacted_before_their_cut(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    prefix = 'conn = connect(host="warehouse.internal", user="reporting", password="'
    assert len(prefix) == 70  # the password straddles the row's 80-char cut
    nh.backend.user_insert(NOTEBOOK, len(nh.cells()), prefix + PASSWORD + '")')
    nh.backend.user_insert(NOTEBOOK, len(nh.cells()), f"Log in with {PASSWORD}", "markdown")
    outline = text(await nh.call("nh_inspect", "p2", view="outline"))
    assert f"{prefix}[redacted…" in outline
    assert f" md          Log in with {MARK}" in outline
    assert_clean(outline)


async def test_status_lines_are_redacted(nh: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    with_env(nh)
    lab_token = "c0ffee" + "b4d5eed1234567890abcdef"

    async def describe(ref: object) -> dict[str, str]:
        return {"server": f"http://127.0.0.1:8888/lab?token={lab_token}"}

    monkeypatch.setattr(nh.backend, "describe", describe)
    env_json = Layout(nh.project).env_json
    env_json.parent.mkdir(parents=True, exist_ok=True)
    env_json.write_text(json.dumps({"manager": "uv", "db_url": f"postgresql://app:{PASSWORD}@db"}))
    status = text(await nh.call("nh_inspect", "p1", view="status"))
    assert "server: http://127.0.0.1:8888/lab?token=[redacted:token]" in status
    assert "db_url=postgresql://" in status and MARK in status
    assert lab_token not in status
    assert_clean(status)


async def test_a_failing_code_line_is_redacted(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    failing = dict(LOAD, code=f'checked = 1 / 0 if "{PASSWORD}" else 0')
    result = await nh.call("nh_add_cell", "p1", **failing)
    body = text(result)
    assert f'failing code: checked = 1 / 0 if "{MARK}" else 0' in body
    assert_clean(body)


async def test_nothing_is_running_reports_a_redacted_error(nh: Harness) -> None:
    env = with_env(nh)
    nh.turns.prompt("p1")
    reads = dict(  # the value comes from the file: the code holds no secret
        LOAD,
        code=(
            f'password = open({str(env)!r}).read().split("=", 1)[1].strip()\n'
            'raise ValueError("login refused for app:" + password)'
        ),
    )
    added = await nh.call("nh_add_cell", "p1", **reads)
    assert not added.is_error and MARK in text(added), text(added)
    assert_clean(text(added))
    [cell] = code_cells(nh)
    waited = text(await nh.call("nh_run", "p1", cell_id=cell["id"], mode="wait"))
    assert waited.startswith("Nothing is running: ")
    assert f"failed with ValueError: login refused for app:{MARK}" in waited
    assert_clean(waited)


async def test_a_user_change_shown_by_e141_is_redacted(nh: Harness) -> None:
    with_env(nh)
    nh.turns.prompt("p1")
    assert not (await nh.call("nh_add_cell", "p1", **LOAD)).is_error
    [cell] = code_cells(nh)
    nh.backend.user_edit(NOTEBOOK, cell["id"], LOAD["code"] + f"\npassword = '{PASSWORD}'")
    nh.turns.prompt("p2")
    result = await nh.call("nh_edit_cell", "p2", cell_id=cell["id"], code=LOAD["code"] + "\n1")
    body = text(result)
    assert result.is_error and "nh: E141" in body
    assert f"+password = '{MARK}'" in body
    assert_clean(body)


async def test_an_internal_error_is_redacted_before_its_cut(
    nh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_env(nh)

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("x" * 270 + PASSWORD)  # "RuntimeError: " + 270: the cut is at 300

    monkeypatch.setattr("nh_gateway.tools.write.lint_cell", crash)
    nh.turns.prompt("p1")
    result = await nh.call("nh_add_cell", "p1", **LOAD)
    body = text(result)
    assert result.is_error and "nh: E199" in body
    assert "x" * 270 + "[redacted:DB_PA" in body
    assert_clean(body)


@pytest.mark.parametrize("inside", [4, 8, 11])
def test_a_view_is_redacted_before_its_cut(tmp_path: Path, inside: int) -> None:
    """``inside`` chars of the password fall before the view's cut: fewer than the 12 the
    cut-piece rule needs, so only redacting first hides them (review of C3)."""
    install(tmp_path)
    limit = 300
    view = inspect_tool._clip("x" * (limit - 60 - inside) + PASSWORD + "y" * 400, limit)
    assert view.startswith("x" * (limit - 60 - inside) + "[redacted:DB_PASSWORD]"[:inside])
    assert view.endswith("more chars; narrow the view]")
    assert_clean(view) and PASSWORD[:4] not in view


def test_a_failing_code_line_is_redacted_where_it_is_built(tmp_path: Path) -> None:
    install(tmp_path)
    err = SimpleNamespace(ename="ZeroDivisionError", evalue="division by zero", line=2)
    lines = write_tool._error_lines(err, f'x = 1\nchecked = 1 / 0 if "{PASSWORD}" else 0')
    assert lines == [
        "ZeroDivisionError: division by zero",
        f'failing code: checked = 1 / 0 if "{MARK}" else 0',
    ]


def test_the_log_filter_redacts_a_stack(tmp_path: Path) -> None:
    install(tmp_path)
    record = logging.LogRecord("nh", logging.INFO, __file__, 1, "probe", None, None)
    record.stack_info = f'Stack (most recent call last):\n  connect("{PASSWORD}")'
    assert app._TokenScrub().filter(record)
    assert record.stack_info == f'Stack (most recent call last):\n  connect("{MARK}")'


def test_every_tool_result_passes_the_redactor_last(tmp_path: Path) -> None:
    """text_result is the safety net for any text a site left unredacted."""
    (tmp_path / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path))
    result = text_result(f"line one\npw={PASSWORD}", status="ok")
    assert [part.text for part in result.content] == [f"line one\npw={MARK}"]


def test_the_log_filter_redacts_messages_and_tracebacks(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(app._TokenScrub())
    logger = logging.getLogger("nh.test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise RuntimeError(f"connect failed: {PASSWORD}")
        except RuntimeError:
            logger.exception("internal error with %s", PASSWORD)
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert f"internal error with {MARK}" in out
    assert f"RuntimeError: connect failed: {MARK}" in out
    assert_clean(out)


async def test_the_discovered_jupyter_token_is_redacted_from_then_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    (project / ".nh").mkdir(parents=True)
    token = "5e2b" + "9c7d1a3f4e6b8c0d2e4f6a8b"
    secrets.install(secrets.Redactor.for_project(project))
    assert secrets.current().redact(f"t {token}") == f"t {token}"
    info = ServerInfo(url="http://127.0.0.1:8888/", token=token, root_dir=project)
    monkeypatch.setattr(discovery, "discover", lambda project, cfg: info)
    monkeypatch.setattr(rest, "Rest", lambda info, pool: object())

    async def idle(self: RtcBackend) -> None:
        return None

    monkeypatch.setattr(RtcBackend, "_janitor_loop", idle)
    backend = RtcBackend(Layout(project), ConfigCache(project))
    try:
        await backend._server()
    finally:
        await backend.aclose()
    assert secrets.current().root == project  # the project's redactor, rebuilt with it
    assert secrets.current().redact(f"t {token}") == "t [redacted:JUPYTER_TOKEN]"
