"""RtcBackend regressions from the v0.1 review, against a real JupyterLab: restarts of JupyterLab
(17) and of the kernel (incarnation), a gateway that died mid-run (18), the user's stop button (19),
stream output volume (20), huge results (21), probes that must not run user code (67), a renamed
notebook (68) and typing into nh's cell before it runs (69)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import nbformat
import pytest

from nh_gateway import meta
from nh_gateway._shared.paths import Layout
from nh_gateway.backend.base import CellConflict, NewCell
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SERVER_SRC = Path(__file__).resolve().parents[2] / "plugins" / "nh" / "server" / "src"
VARS = {"names": None, "max_vars": 40, "budget_s": 0.8, "max_cells": 20_000_000}


def code_cell(uid: str, code: str) -> NewCell:
    md = {
        "tags": [meta.TAG],
        "nh": meta.code_metadata(
            uid=uid, intent="run", bullets=["a", "b"], turn_id="t", source=code
        ),
    }
    return NewCell(id=uid, cell_type="code", source=code, metadata=md)


async def run(backend: RtcBackend, ref, uid: str, code: str, timeout: float = 60.0):
    await backend.insert_cells(ref, -1, [code_cell(uid, code)])
    execution = await backend.start_execution(ref, uid, hard_timeout=120, expected_source=code)
    return await asyncio.wait_for(execution.future, timeout)


async def started(execution, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not execution.started():
        assert time.monotonic() < deadline, "the cell never started"
        await asyncio.sleep(0.05)


def kernel_id_for(lab, api_path: str) -> str | None:
    for session in lab.api("GET", "/api/sessions").json():
        if session["path"] == api_path:
            return session["kernel"]["id"]
    return None


# ---------------------------------------------------------------------------- 17: JupyterLab restarts


@pytest.mark.parametrize("same_port", [True, False], ids=["same_port_new_token", "new_port"])
async def test_jupyterlab_restart_is_rediscovered_within_the_call(
    tmp_path, monkeypatch, helpers, same_port
):
    base = Path(os.path.realpath(tmp_path))
    first = helpers.start_lab(base)
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(first.runtime))
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN", "JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    project = first.root / "proj"
    (project / ".nh" / "state").mkdir(parents=True)
    helpers.new_notebook(project / helpers.NB, [nbformat.v4.new_markdown_cell("# t", id="title")])
    backend = RtcBackend(Layout(project), ConfigCache(project))
    second = None
    try:
        ref = await backend.resolve_notebook(helpers.NB)
        assert (await run(backend, ref, "nh-0000000a01", "a = 1\na")).status == "ok"
        await asyncio.sleep(1.0)  # saved (save delay 0.5 s)
        helpers.stop(first.proc)
        port = int(first.url.rsplit(":", 1)[1].strip("/")) if same_port else None
        second = helpers.start_lab(base, root=first.root, runtime=first.runtime, port=port)
        assert second.token != first.token
        # the very next call recovers: no E130, no second call needed
        result = await run(backend, ref, "nh-0000000a02", "b = 2\nb")
        assert result.status == "ok" and result.outputs[0]["data"]["text/plain"] == "2"
        assert backend._api is not None and backend._api.server.token == second.token
    finally:
        await backend.aclose()
        helpers.stop(first.proc)
        if second is not None:
            helpers.stop(second.proc)


# ---------------------------------------------------------------------------- kernel incarnation, live_run


async def test_kernel_restart_changes_the_incarnation_not_the_id(backend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    before = await backend.kernel_status(ref)
    assert (
        before.incarnation and before.incarnation == (await backend.kernel_status(ref)).incarnation
    )
    await asyncio.to_thread(lab.api, "POST", f"/api/kernels/{before.kernel_id}/restart")
    deadline = time.monotonic() + 20
    while True:
        status = await backend.kernel_status(ref)
        if status.execution_state == "idle" and status.incarnation != before.incarnation:
            break
        assert time.monotonic() < deadline, "the restart never showed in kernel_status"
        await asyncio.sleep(0.3)
    assert status.kernel_id == before.kernel_id and status.incarnation


async def test_live_run_names_nh_s_running_cell(backend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, [code_cell("nh-0000000a10", "import time\ntime.sleep(2)")])
    assert backend.live_run(ref) is None
    execution = await backend.start_execution(ref, "nh-0000000a10", hard_timeout=60)
    assert backend.live_run(ref) == "nh-0000000a10"
    await asyncio.wait_for(execution.future, 30)
    assert backend.live_run(ref) is None


# ---------------------------------------------------------------------------- 18: nh died mid-run

CRASH = """
import asyncio, os, sys
from pathlib import Path
from nh_gateway import meta
from nh_gateway._shared.paths import Layout
from nh_gateway.backend.base import NewCell
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache

async def main(project, nb, uid, seconds):
    backend = RtcBackend(Layout(Path(project)), ConfigCache(Path(project)))
    ref = await backend.resolve_notebook(nb)
    code = f"import time\\ntime.sleep({seconds})\\n'finished'"
    md = {"tags": [meta.TAG], "nh": meta.code_metadata(uid=uid, intent="r", bullets=["a", "b"], turn_id="t0", source=code)}
    await backend.insert_cells(ref, -1, [NewCell(id=uid, cell_type="code", source=code, metadata=md)])
    execution = await backend.start_execution(ref, uid, hard_timeout=120)
    while not execution.started():
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.0)  # the running state reaches the server
    print("STARTED", flush=True)
    os._exit(9)

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])))
"""


async def test_cell_left_running_by_a_dead_gateway_is_reset_once_the_kernel_is_idle(
    backend, project, lab, helpers
):
    uid = "nh-00000000ff"
    env = {**os.environ, "PYTHONPATH": str(SERVER_SRC), "JUPYTER_RUNTIME_DIR": str(lab.runtime)}
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", CRASH, str(project), helpers.NB, uid, "5"],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert "STARTED" in proc.stdout, proc.stderr[-2000:]
    ref = await backend.resolve_notebook(helpers.NB)
    cell = next(c for c in await backend.snapshot(ref, outputs="none") if c.id == uid)
    assert cell.running  # the kernel is still busy with it: leave it alone
    assert backend.live_run(ref) is None  # the process that ran it is gone

    cell = None
    deadline = time.monotonic() + 30
    while cell is None:
        cell = next(
            (
                c
                for c in await backend.snapshot(ref, outputs="full")
                if c.id == uid and not c.running
            ),
            None,
        )
        assert time.monotonic() < deadline, "the stuck cell was never reset"
        await asyncio.sleep(0.5)
    assert "nh restarted" in cell.outputs[-1]["text"]
    assert any("still marked running" in note for note in backend.take_notices(ref))
    execution = await backend.start_execution(ref, uid, hard_timeout=60)
    assert (await asyncio.wait_for(execution.future, 30)).status == "ok"


# ---------------------------------------------------------------------------- 19: the stop button


async def test_user_interrupt_from_jupyterlab_is_interrupted(backend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, [code_cell("nh-0000000a19", "import time\ntime.sleep(40)")])
    execution = await backend.start_execution(ref, "nh-0000000a19", hard_timeout=120)
    await started(execution)
    await asyncio.sleep(0.5)
    kernel_id = kernel_id_for(lab, ref.api_path)
    await asyncio.to_thread(lab.api, "POST", f"/api/kernels/{kernel_id}/interrupt")
    result = await asyncio.wait_for(execution.future, 20)
    assert result.status == "interrupted" and result.note == "interrupted from JupyterLab"


# ---------------------------------------------------------------------------- 20: stream volume


def ystore_bytes(lab) -> int:
    db = lab.root.parent / ".jupyter_ystore.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("select coalesce(sum(length(yupdate)), 0) from yupdates").fetchone()[0]
    finally:
        con.close()


async def test_streaming_output_is_appended_not_rewritten(backend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    code = (
        "import time\n"
        "for step in range(40):\n"
        "    print(f'step {step:05d} ' + 'loss=0.123456 ' * 140, flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    await backend.insert_cells(ref, -1, [code_cell("nh-0000000a20", code)])
    await asyncio.sleep(1.5)
    before = ystore_bytes(lab)
    execution = await backend.start_execution(ref, "nh-0000000a20", hard_timeout=120)
    async with helpers.observer(ref.api_path) as user:
        result = await asyncio.wait_for(execution.future, 60)
        await asyncio.sleep(2.0)
        grown = ystore_bytes(lab) - before
        output = sum(len(o.get("text", "")) for o in result.outputs)
        assert result.status == "ok"
        assert grown < 2 * output, f"the room stored {grown} bytes for {output} of output"
        cell = next(c for c in helpers.cells_of(user) if c["id"] == "nh-0000000a20")
        assert cell["outputs"][0]["text"] == result.outputs[0]["text"]


# ---------------------------------------------------------------------------- 21: huge results


async def test_a_huge_result_is_ok_not_lost(backend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.kernel_status(ref)  # as nh_add_cell does: connects the probe client too
    result = await run(backend, ref, "nh-0000000a21", "'x' * 25_000_000", timeout=120)
    assert result.status == "ok", result.note
    assert len(result.outputs[0]["data"]["text/plain"]) == 25_000_002


# ---------------------------------------------------------------------------- 67: a look runs no user code


async def test_vars_probe_never_calls_a_user_len(backend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    code = (
        "import time\n\n"
        "class LazyFrame:\n"
        "    def __len__(self):\n"
        "        time.sleep(15)\n"
        "        return 3\n\n"
        "    def __repr__(self):\n"
        "        time.sleep(15)\n"
        "        return 'lazy'\n\n"
        "lazy = LazyFrame()"
    )
    assert (await run(backend, ref, "nh-0000000a67", code)).status == "ok"
    began = time.monotonic()
    payload = await backend.probe(ref, "vars", VARS, 2.0)
    assert payload["vars"]["lazy"] == {"kind": "object", "type": "__main__.LazyFrame"}
    detail = await backend.probe(ref, "var", {"name": "lazy", "rows": 5}, 2.0)
    assert detail["text"] == "<__main__.LazyFrame object>"
    assert time.monotonic() - began < 3.0
    model = lab.api("GET", f"/api/kernels/{kernel_id_for(lab, ref.api_path)}").json()
    assert model["execution_state"] == "idle", "a look left the user's kernel busy"


# ---------------------------------------------------------------------------- 68: renamed notebook


async def test_renamed_notebook_then_a_new_file_at_its_name(backend, project, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    assert (await run(backend, ref, "nh-0000000a68", "a = 1\na")).status == "ok"
    old = ref.api_path
    new = old.replace("01_eda.ipynb", "01_eda_old.ipynb")
    response = await asyncio.to_thread(lab.api, "PATCH", f"/api/contents/{old}", json={"path": new})
    assert response.status_code == 200, response.text
    for session in lab.api(
        "GET", "/api/sessions"
    ).json():  # JupyterLab moves the kernel session too
        if session["path"] == old:
            lab.api("PATCH", f"/api/sessions/{session['id']}", json={"path": new})
    renamed_kernel = kernel_id_for(lab, new)
    helpers.new_notebook(
        project / helpers.NB, [nbformat.v4.new_markdown_cell("# fresh", id="fresh")]
    )
    await asyncio.sleep(2.5)  # past the room re-check interval
    assert (await run(backend, ref, "nh-0000000a69", "b = 2\nb")).status == "ok"
    assert any("is now a different file" in note for note in backend.take_notices(ref))
    assert kernel_id_for(lab, old) not in (None, renamed_kernel), "the new file got its own kernel"
    fresh = await helpers.eventually(
        lambda: (
            lambda d: d if d and any(c["id"] == "nh-0000000a69" for c in d["cells"]) else None
        )(helpers.read_disk(project / helpers.NB))
    )
    assert [c["id"] for c in fresh["cells"]] == ["fresh", "nh-0000000a69"]
    renamed = helpers.read_disk(project / "notebooks" / "01_eda_old.ipynb")
    assert "nh-0000000a69" not in [c["id"] for c in renamed["cells"]]


# ---------------------------------------------------------------------------- 69: typing before the run


async def test_user_typing_into_nh_s_cell_before_it_runs_is_a_conflict(backend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.kernel_status(ref)
    code = "value = 40 + 2\nvalue"
    await backend.insert_cells(ref, -1, [code_cell("nh-0000000a70", code)])
    async with helpers.observer(ref.api_path) as user:
        await helpers.eventually(
            lambda: any(c["id"] == "nh-0000000a70" for c in helpers.cells_of(user))
        )
        with user._lock:
            ycell = next(y for y in user._doc.ycells if y["id"] == "nh-0000000a70")
            ycell["source"].insert(0, "user_was_here = 1\n")
        await helpers.eventually(
            lambda: any(
                c.source.startswith("user_was_here")
                for c in backend._docs[str(ref.abs_path)].cells("none")
            )
        )
        with pytest.raises(CellConflict) as conflict:
            await backend.start_execution(
                ref, "nh-0000000a70", hard_timeout=60, expected_source=code
            )
    assert conflict.value.current.startswith("user_was_here = 1\n")
    cell = next(c for c in await backend.snapshot(ref, outputs="none") if c.id == "nh-0000000a70")
    assert not cell.running and cell.execution_count is None
    payload = await backend.probe(ref, "vars", VARS, 2.0)
    assert "user_was_here" not in payload["vars"], "the kernel ran code nh never checked"
