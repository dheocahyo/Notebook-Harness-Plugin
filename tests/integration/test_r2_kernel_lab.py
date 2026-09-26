"""Round-2 regressions for the kernel group on a real JupyterLab (``-m integration``).

A fresh gateway whose first call meets a busy kernel reports no NEW kernel and keeps
'Kernel ≠ notebook' (V7/V13/V27); a restart found by an outline clears it, once (V1); an old
running record whose pid now belongs to another program doesn't block writes (V33).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway._shared.paths import Layout
from nh_gateway.app import create_server
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from tests.fakes.turns import Turns, text
from tests.integration.test_kernel_exec import UserKernel

pytestmark = [pytest.mark.integration]

NB = "notebooks/01_eda.ipynb"
Call = Callable[..., Awaitable[str]]


def cell(code: str, title: str) -> dict:
    return dict(
        title=title,
        notes=["Does the thing for the user.", "Shows the result so we can check it."],
        intent="do the thing",
        code=code,
    )


@contextlib.asynccontextmanager
async def gateway(
    project: Path, backend: RtcBackend, data: Path
) -> AsyncIterator[tuple[Turns, Call]]:
    (project / "harness.toml").write_text(
        f'version = 1\n[project]\nnotebook = "{NB}"\n[exec]\nsoft_timeout_s = 5\n'
    )
    turns = Turns(project, data)
    async with Client(create_server(project, backend)) as client:

        async def call(tool: str, prompt_id: str, **args) -> str:
            return text(await turns.call(client, tool, args, prompt_id))

        yield turns, call


def state_json(project: Path, name: str) -> dict:
    path = project / ".nh" / "state" / name
    return json.loads(path.read_text()) if path.exists() else {}


def kernel_id(lab, project: Path) -> str:
    api_path = (project / NB).relative_to(lab.root).as_posix()
    return next(
        s["kernel"]["id"] for s in lab.api("GET", "/api/sessions").json() if s["path"] == api_path
    )


async def wait_state(lab, kid: str, state: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while lab.api("GET", f"/api/kernels/{kid}").json().get("execution_state") != state:
        assert time.monotonic() < deadline, f"the kernel never became {state}"
        await asyncio.sleep(0.1)


async def test_a_fresh_gateway_meeting_a_busy_kernel_reports_no_new_kernel(
    project: Path, lab, tmp_path: Path
) -> None:
    first = RtcBackend(Layout(project), ConfigCache(project))
    try:
        async with gateway(project, first, tmp_path / "a") as (turns, call):
            turns.prompt("p1")
            assert "ran ok" in await call(
                "nh_add_cell", "p1", **cell("total = 10\ntotal", "Set a total")
            )
            turns.prompt("p2")
            assert (await call("nh_undo", "p2")).startswith("Kernel ≠ notebook: `total`")
    finally:
        await first.aclose()
    known = state_json(project, "kernels.json")
    kid = kernel_id(lab, project)
    user = await asyncio.to_thread(UserKernel, lab, kid)
    second = RtcBackend(Layout(project), ConfigCache(project))  # a new Claude Code session
    try:
        user.send("import time\ntime.sleep(6)")
        await wait_state(lab, kid, "busy")
        async with gateway(project, second, tmp_path / "b") as (turns, call):
            turns.prompt("p3")
            body = await call("nh_add_cell", "p3", **cell("count = 3\ncount", "Set a count"))
            assert "NEW kernel" not in body and body.startswith("Kernel ≠ notebook: `total`"), body
            assert state_json(project, "kernels.json") == known
            await wait_state(lab, kid, "idle")
            body = await call("nh_inspect", "p3", view="outline")
            assert "NEW kernel" not in body and body.startswith("Kernel ≠ notebook: `total`"), body
            assert state_json(project, "kernels.json") == known
    finally:
        await asyncio.to_thread(user.close)
        await second.aclose()


async def test_a_restart_found_by_an_outline_clears_drift_once(
    project: Path, backend: RtcBackend, lab, tmp_path: Path
) -> None:
    async with gateway(project, backend, tmp_path) as (turns, call):
        turns.prompt("p1")
        await call("nh_add_cell", "p1", **cell("total = 10\ntotal", "Set a total"))
        turns.prompt("p2")
        assert (await call("nh_undo", "p2")).startswith("Kernel ≠ notebook: `total`")
        kid = kernel_id(lab, project)
        lab.api("POST", f"/api/kernels/{kid}/restart")
        await asyncio.sleep(1.0)
        await wait_state(lab, kid, "idle")
        body = await call("nh_inspect", "p2", view="outline")
        assert body.startswith("NEW kernel: earlier variables are gone"), body
        assert "Kernel ≠ notebook" not in body
        assert state_json(project, "kernel_drift.json") == {}
        body = await call("nh_inspect", "p2", view="outline")
        assert "NEW kernel" not in body and "Kernel ≠ notebook" not in body


async def test_an_old_record_whose_pid_was_reused_does_not_block_writes(
    project: Path, backend: RtcBackend, tmp_path: Path
) -> None:
    async with gateway(project, backend, tmp_path) as (turns, call):
        turns.prompt("p1")
        body = await call("nh_add_cell", "p1", **cell("a = 1\na", "First"))
        uid = body.split("nh: cell=")[1].split()[0]
        unrelated = subprocess.Popen(["/bin/sleep", "60"])  # the crashed gateway's pid, reused
        record = project / ".nh" / "state" / "running" / f"{uid}.json"
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(
                json.dumps(
                    {
                        "cell_id": uid,
                        "notebook": NB,
                        "kernel_id": "gone",
                        "started": time.time() - 3600,
                        "pid": unrelated.pid,
                    }
                )
            )
            turns.prompt("p2")
            body = await call("nh_add_cell", "p2", **cell("b = 2\nb", "Second"))
            assert "ran ok" in body and "E133" not in body, body
            assert not record.exists()
        finally:
            unrelated.kill()
            unrelated.wait()
