"""RtcBackend runs cells in the notebook's session kernel of a real JupyterLab: attach, live outputs,
classification, silent probes, interrupts, restarts, timeouts, queueing and reconnects."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import uuid

import nbformat
import pytest

from nh_gateway import meta
from nh_gateway.backend import kernel
from nh_gateway.backend.base import NewCell
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.policy.errors import NhError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

VARS = {"names": None, "max_vars": 40, "budget_s": 0.8, "max_cells": 20_000_000}


async def add(backend: RtcBackend, ref, uid: str, code: str) -> None:
    code_md = {
        "tags": [meta.TAG],
        "nh": meta.code_metadata(
            uid=uid, intent="run", bullets=["a", "b"], turn_id="t", source=code
        ),
    }
    await backend.insert_cells(
        ref, -1, [NewCell(id=uid, cell_type="code", source=code, metadata=code_md)]
    )


def sessions_for(lab, api_path: str) -> list[dict]:
    return [s for s in lab.api("GET", "/api/sessions").json() if s["path"] == api_path]


class UserKernel:
    """Another client of the same kernel, standing in for the user running cells in JupyterLab."""

    def __init__(self, lab, kernel_id: str) -> None:
        info = kernel.ServerInfo(url=lab.url, token=lab.token, root_dir=lab.root)
        self.kc = kernel.open_client(info, kernel_id)
        self.client = self.kc._manager.client

    def send(self, code: str, *, stop_on_error: bool = True) -> str:
        return self.client.execute(code, allow_stdin=False, stop_on_error=stop_on_error)

    def reply(self, msg_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.client.shell_channel.get_msg(timeout=0.25)
            except queue.Empty:
                continue
            if msg["parent_header"].get("msg_id") == msg_id:
                return msg["content"]
        raise AssertionError("no reply")

    def close(self) -> None:
        kernel.close_client(self.kc)


async def started(execution, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not execution.started():
        assert time.monotonic() < deadline, "the cell never started"
        await asyncio.sleep(0.05)


async def test_attaches_to_the_existing_session_kernel(backend: RtcBackend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    created = lab.api(
        "POST",
        "/api/sessions",
        json={
            "path": ref.api_path,
            "type": "notebook",
            "name": "01_eda.ipynb",
            "kernel": {"name": "python3"},
        },
    ).json()
    status = await backend.kernel_status(ref)
    assert status.kernel_id == created["kernel"]["id"]
    assert status.python_version == tuple(sys.version_info[:2])
    assert status.prefix == sys.prefix and status.language == "python"
    assert len(sessions_for(lab, ref.api_path)) == 1
    assert not any("started a kernel" in note for note in backend.take_notices(ref))


async def test_run_streams_outputs_into_the_notebook(backend: RtcBackend, project, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    code = "import time\nfor i in range(3):\n    print(i, flush=True)\n    time.sleep(0.7)\n'done'"
    await add(backend, ref, "nh-0000000010", code)
    async with helpers.observer(ref.api_path) as user:

        def cell():
            return next(c for c in helpers.cells_of(user) if c["id"] == "nh-0000000010")

        execution = await backend.start_execution(ref, "nh-0000000010", hard_timeout=60)
        partial = await helpers.eventually(lambda: cell()["outputs"] and cell(), timeout=15)
        assert not execution.future.done(), "outputs must appear while the cell still runs"
        assert partial["execution_state"] == "running" and partial["outputs"][0]["text"].startswith(
            "0"
        )
        assert execution.partial_outputs()
        result = await asyncio.wait_for(execution.future, 30)
        assert result.status == "ok" and result.error is None and result.execution_count == 1
        assert [o["output_type"] for o in result.outputs] == ["stream", "execute_result"]
        assert (
            result.outputs[0]["text"] == "0\n1\n2\n"
            and result.outputs[1]["data"]["text/plain"] == "'done'"
        )
        final = await helpers.eventually(lambda: cell()["execution_state"] == "idle" and cell())
        assert final["execution_count"] == 1
        assert [o.get("text") for o in final["outputs"]][0] == "0\n1\n2\n"
    disk = await helpers.eventually(
        lambda: (
            lambda d: (
                d
                if d and d["cells"][-1].get("outputs") and d["cells"][-1]["execution_count"] == 1
                else None
            )
        )(helpers.read_disk(project / helpers.NB))
    )
    nbformat.validate(nbformat.from_dict(disk))
    assert not (project / ".nh" / "state" / "running" / "nh-0000000010.json").exists()


async def test_error_cell_is_classified_with_error_info(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000011", "prices = {}\n\nprices['prce']")
    result = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000011", hard_timeout=60)).future, 60
    )
    assert result.status == "error"
    assert (
        result.error.ename == "KeyError"
        and result.error.evalue == "'prce'"
        and result.error.line == 3
    )
    assert "\x1b[" not in result.error.traceback


async def test_silent_probes_leave_no_trace(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    show = "import pandas as pd\nframe = pd.DataFrame({'a': [1, None]})\nprint(sorted(k for k in globals() if not k.startswith('_')))"
    await add(backend, ref, "nh-0000000012", show)
    first = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000012", hard_timeout=60)).future, 60
    )
    assert first.status == "ok"
    payload = await backend.probe(ref, "vars", VARS, 5.0)
    assert payload["vars"]["frame"]["shape"] == [2, 1] and payload["vars"]["frame"]["nulls"] == {
        "a": 1
    }
    assert payload["packages"]["pandas"]
    assert (await backend.probe(ref, "var", {"name": "frame", "rows": 1}, 5.0))["head"]
    attach = await backend.probe(ref, "attach", {}, 5.0)
    assert attach["exec_count"] == first.execution_count + 1  # IPython's count for the next cell
    second = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000012", hard_timeout=60)).future, 60
    )
    assert second.execution_count == first.execution_count + 1, (
        "probes must not use execution counts"
    )
    assert second.outputs[0]["text"] == first.outputs[0]["text"], "probes must not bind names"


async def test_interrupt_a_sleeping_cell(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000013", "import time\ntime.sleep(60)")
    execution = await backend.start_execution(ref, "nh-0000000013", hard_timeout=120)
    await started(execution)
    assert (await backend.probe(ref, "vars", VARS, 2.0))["error"].startswith("KernelBusy")
    with pytest.raises(NhError) as busy:
        await backend.start_execution(ref, "nh-0000000013", hard_timeout=120)
    assert busy.value.code == "E133"
    await backend.interrupt(ref)
    result = await asyncio.wait_for(execution.future, 15)
    assert result.status == "interrupted" and result.error.ename == "KeyboardInterrupt"


async def test_restart_mid_run_is_lost_quickly_and_the_next_run_works(
    backend: RtcBackend, lab, helpers
):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000014", "import time\ntime.sleep(60)")
    execution = await backend.start_execution(ref, "nh-0000000014", hard_timeout=120)
    await started(execution)
    kernel_id = (await backend.kernel_status(ref)).kernel_id
    began = time.monotonic()
    await asyncio.to_thread(lab.api, "POST", f"/api/kernels/{kernel_id}/restart")
    result = await asyncio.wait_for(execution.future, 15)
    assert time.monotonic() - began < 6.0
    assert result.status == "lost" and "restarted" in result.note
    cell = next(c for c in await backend.snapshot(ref, outputs="full") if c.id == "nh-0000000014")
    assert not cell.running and "restarted" in cell.outputs[-1]["text"]
    await add(backend, ref, "nh-0000000015", "40 + 2")
    again = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000015", hard_timeout=60)).future, 60
    )
    assert again.status == "ok" and again.outputs[0]["data"]["text/plain"] == "42"


async def test_hard_timeout_interrupts_a_started_run(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000016", "import time\ntime.sleep(60)")
    result = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000016", hard_timeout=2)).future, 30
    )
    assert result.status == "timeout" and "time limit" in result.note
    assert result.error is not None and result.error.ename == "KeyboardInterrupt"


async def test_queued_behind_the_users_cell_is_never_interrupted(backend: RtcBackend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000017", "mine = 1")
    status = await backend.kernel_status(ref)
    user = await asyncio.to_thread(UserKernel, lab, status.kernel_id)
    try:
        theirs = user.send("import time\ntime.sleep(6)\nuser_done = True")
        await asyncio.sleep(0.5)
        execution = await backend.start_execution(ref, "nh-0000000017", hard_timeout=2)
        result = await asyncio.wait_for(execution.future, 30)
        assert result.status == "timeout" and "never started" in result.note
        assert not execution.started()
        reply = await asyncio.to_thread(user.reply, theirs)
        assert reply["status"] == "ok", "nh must not interrupt the user's cell"
        cell = next(
            c for c in await backend.snapshot(ref, outputs="full") if c.id == "nh-0000000017"
        )
        assert "never started" in cell.outputs[-1]["text"]
    finally:
        await asyncio.to_thread(user.close)


async def test_aborted_by_a_failing_cell_ahead(backend: RtcBackend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000018", "mine = 1")
    status = await backend.kernel_status(ref)
    user = await asyncio.to_thread(UserKernel, lab, status.kernel_id)
    try:
        user.send("import time\ntime.sleep(1.5)\n1 / 0", stop_on_error=True)
        await asyncio.sleep(0.3)
        result = await asyncio.wait_for(
            (await backend.start_execution(ref, "nh-0000000018", hard_timeout=60)).future, 30
        )
        assert result.status == "aborted" and result.error is None
    finally:
        await asyncio.to_thread(user.close)


async def test_kernel_websocket_drop_reconnects(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000019", "1 + 1")
    first = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000019", hard_timeout=60)).future, 60
    )
    assert first.status == "ok"
    handle = next(iter(backend._kernels.values()))
    await asyncio.to_thread(handle.exec_kc._manager.client.stop_channels)  # the socket drops
    assert not handle.exec_kc._manager.client.channels_running
    second = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000019", hard_timeout=60)).future, 60
    )
    assert second.status == "ok" and second.execution_count == first.execution_count + 1


async def test_waits_for_jupyterlab_to_rename_its_uuid_session(backend: RtcBackend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    folder = ref.api_path.rsplit("/", 1)[0]
    pending = lab.api(
        "POST",
        "/api/sessions",
        json={
            "path": f"{folder}/{uuid.uuid4()}",
            "type": "notebook",
            "name": "01_eda.ipynb",
            "kernel": {"name": "python3"},
        },
    ).json()

    def rename():
        time.sleep(1.5)
        lab.api("PATCH", f"/api/sessions/{pending['id']}", json={"path": ref.api_path})

    threading.Thread(target=rename, daemon=True).start()
    status = await backend.kernel_status(ref)
    assert status.kernel_id == pending["kernel"]["id"], "nh must not start a second kernel"
    assert len(sessions_for(lab, ref.api_path)) == 1


async def test_unknown_kernelspec_falls_back_to_python3(backend: RtcBackend, project, lab, helpers):
    helpers.new_notebook(
        project / helpers.NB,
        [nbformat.v4.new_markdown_cell("# t", id="title")],
        kernel="no-such-kernel",
    )
    ref = await backend.resolve_notebook(helpers.NB)
    status = await backend.kernel_status(ref)
    assert status.name == "python3"
    assert [s["kernel"]["name"] for s in sessions_for(lab, ref.api_path)] == ["python3"]
    assert any("started a kernel" in note for note in backend.take_notices(ref))


async def test_large_output_is_capped_in_the_notebook_only(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000020", "print('x' * 1_500_000)")
    result = await asyncio.wait_for(
        (await backend.start_execution(ref, "nh-0000000020", hard_timeout=60)).future, 60
    )
    assert result.status == "ok" and len(result.outputs[0]["text"]) == 1_500_001
    cell = next(c for c in await backend.snapshot(ref, outputs="full") if c.id == "nh-0000000020")
    stored = cell.outputs[0]["text"]
    assert stored.startswith("[nh: ") and len(stored) < 1_100_000


async def test_outputs_survive_a_room_reconnect_mid_run(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    code = "import time\nfor i in range(4):\n    print(i, flush=True)\n    time.sleep(0.5)"
    await add(backend, ref, "nh-0000000021", code)
    execution = await backend.start_execution(ref, "nh-0000000021", hard_timeout=60)
    await helpers.eventually(execution.partial_outputs)
    doc = backend._docs[str(ref.abs_path)]
    generation = doc.generation
    doc._task.cancel()  # the room websocket drops while the cell prints
    result = await asyncio.wait_for(execution.future, 30)
    assert result.status == "ok" and doc.generation > generation
    async with helpers.observer(ref.api_path) as user:
        cell = await helpers.eventually(
            lambda: next(
                (
                    c
                    for c in helpers.cells_of(user)
                    if c["id"] == "nh-0000000021"
                    and c["execution_state"] == "idle"
                    and c["outputs"]
                    and c["outputs"][0]["text"] == "0\n1\n2\n3\n"
                ),
                None,
            )
        )
        assert cell["execution_count"] == result.execution_count


async def test_shutdown_mid_run_marks_the_cell(project, helpers):
    from nh_gateway._shared.paths import Layout
    from nh_gateway.config import ConfigCache

    backend = RtcBackend(Layout(project), ConfigCache(project))
    ref = await backend.resolve_notebook(helpers.NB)
    await add(backend, ref, "nh-0000000022", "import time\ntime.sleep(60)")
    execution = await backend.start_execution(ref, "nh-0000000022", hard_timeout=120)
    await started(execution)
    await backend.aclose()
    result = execution.future.result()
    assert result.status == "lost" and "nh stopped" in result.note
    async with helpers.observer(ref.api_path) as user:
        cell = await helpers.eventually(
            lambda: next(
                (
                    c
                    for c in helpers.cells_of(user)
                    if c["id"] == "nh-0000000022" and c["execution_state"] == "idle"
                ),
                None,
            )
        )
        assert "nh stopped" in cell["outputs"][-1]["text"]


async def test_a_probe_never_starts_a_kernel(backend: RtcBackend, lab, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    with pytest.raises(NhError) as info:
        await backend.probe(ref, "vars", VARS, 2.0)
    assert info.value.code == "E134"
    assert sessions_for(lab, ref.api_path) == []
