"""A real JupyterLab 4.6 + jupyter-collaboration 5 for integration tests (``-m integration``).

One server per session, started from this venv as a subprocess: free port, random token, temp
root, runtime and config dirs, short save and cleanup delays, cwd in the temp dir so its
``.jupyter_ystore.db`` stays isolated. Every test gets its own project folder under the root and
its sessions (kernels) are deleted afterwards; the server's process group is killed at the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import nbformat
import pytest
import pytest_asyncio
import requests

from nh_gateway._shared.paths import Layout
from nh_gateway.backend.rtc import NhNbModelClient
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache

NB = "notebooks/01_eda.ipynb"


def pytest_collection_modifyitems(config, items):
    """Integration tests start JupyterLab: run them only when asked for (``-m integration``)."""
    if "integration" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="integration test: run with -m integration")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _collect_on_main_thread():
    """Collect each test's leftovers (its closed event loop and clients) here, on the main thread.

    pycrdt objects are unsendable: if the cyclic GC happens to run them down on a REST worker
    thread, pyo3 refuses to drop them and reports an unraisable RuntimeError.
    """
    yield
    gc.collect()


@dataclass
class Lab:
    url: str
    token: str
    root: Path
    runtime: Path
    log: Path
    proc: subprocess.Popen

    def api(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {"Authorization": f"token {self.token}"}
        return requests.request(
            method, self.url.rstrip("/") + path, headers=headers, timeout=30, **kwargs
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_lab(
    base: Path,
    *,
    root: Path | None = None,
    runtime: Path | None = None,
    port: int | None = None,
    token: str | None = None,
    args: tuple[str, ...] = (),
) -> Lab:
    """Start JupyterLab from this venv (cwd ``base``, where its YStore lives) and wait for its
    runtime file. Pass ``root``/``runtime``/``port`` (and ``token``) of an earlier lab to restart
    "the same" one; ``args`` are extra command-line options."""
    root, runtime, config_dir = root or base / "root", runtime or base / "runtime", base / "config"
    for folder in (root, runtime, config_dir):
        folder.mkdir(parents=True, exist_ok=True)
    port, token = port or _free_port(), token or secrets.token_hex(16)
    env = {
        **os.environ,
        "JUPYTER_RUNTIME_DIR": str(runtime),
        "JUPYTER_CONFIG_DIR": str(config_dir),
        "JUPYTER_PREFER_ENV_PATH": "1",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    jupyter = Path(sys.executable).parent / "jupyter"
    log = base / f"jupyterlab-{port}-{token[:6]}.log"
    command = [
        str(jupyter),
        "lab",
        "--no-browser",
        "--ip",
        "127.0.0.1",
        "--port",
        str(port),
        "--IdentityProvider.token",
        token,
        "--ServerApp.root_dir",
        str(root),
        "--YDocExtension.document_save_delay",
        "0.5",
        "--YDocExtension.document_cleanup_delay",
        "3",
        *args,
    ]
    with open(log, "ab") as handle:  # a restart of the same lab appends to its log
        proc = subprocess.Popen(
            command,
            cwd=base,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    server = Lab(
        url=f"http://127.0.0.1:{port}/", token=token, root=root, runtime=runtime, log=log, proc=proc
    )
    deadline = time.monotonic() + 90
    while True:
        if proc.poll() is not None:
            pytest.fail(f"JupyterLab exited early:\n{log.read_text()[-3000:]}")
        with contextlib.suppress(requests.RequestException):
            if server.api("GET", "/api").status_code == 200 and list(
                runtime.glob(f"jpserver-{proc.pid}.json")
            ):
                break
        if time.monotonic() > deadline:
            _stop(proc)
            pytest.fail(f"JupyterLab did not start:\n{log.read_text()[-3000:]}")
        time.sleep(0.3)
    return server


@pytest.fixture(scope="session")
def lab(tmp_path_factory) -> Lab:
    base = Path(os.path.realpath(tmp_path_factory.mktemp("jupyterlab")))
    server = start_lab(base)
    try:
        yield server
    finally:
        _stop(server.proc)


def _stop(proc: subprocess.Popen, *, runtime: Path | None = None) -> None:
    """SIGTERM the server's process group, give it 15 s to exit, then SIGKILL the group.

    On SIGTERM the server deletes its rooms and shuts its kernels down; jupyter_client starts each
    kernel in a session of its own, outside the group, and removes its connection file once the
    kernel exited or was killed. A server can then hang instead of exiting (a8: nh connected and
    a cell running); with ``runtime`` the wait ends as soon as no kernel connection file is left
    there.
    """
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            if runtime is not None and not list(runtime.glob("kernel-*.json")):
                break
            time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # the server and anything else left in its group
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(5)


def new_notebook(
    path: Path, cells: list | None = None, *, minor: int = 5, kernel: str = "python3"
) -> None:
    notebook = nbformat.v4.new_notebook(cells=cells or [])
    notebook.metadata["kernelspec"] = {
        "name": kernel,
        "display_name": "Python 3",
        "language": "python",
    }
    notebook.nbformat_minor = minor
    if minor < 5:
        for cell in notebook.cells:
            cell.pop("id", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(nbformat.writes(notebook, version=4))


@pytest.fixture
def project(lab: Lab, request, monkeypatch) -> Path:
    """A fresh nh project under the server root; NB is a 4.5 notebook holding one human markdown cell.

    (A notebook with no cells at all gets an empty code cell from jupyter_ydoc when the room loads.)
    """
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(lab.runtime))
    folder = (
        lab.root
        / f"{request.node.name[:40].replace('[', '_').replace(']', '_')}-{secrets.token_hex(3)}"
    )
    (folder / ".nh" / "state").mkdir(parents=True)
    new_notebook(folder / NB, [nbformat.v4.new_markdown_cell("# Sales EDA", id="title")])
    yield folder
    prefix = folder.relative_to(lab.root).as_posix() + "/"
    with contextlib.suppress(requests.RequestException):
        for session in lab.api("GET", "/api/sessions").json():
            if str(session.get("path", "")).startswith(prefix):
                lab.api("DELETE", f"/api/sessions/{session['id']}")


@pytest_asyncio.fixture
async def backend(project: Path) -> AsyncIterator[RtcBackend]:
    rtc = RtcBackend(Layout(project), ConfigCache(project))
    yield rtc
    await rtc.aclose()


@contextlib.asynccontextmanager
async def observer(lab: Lab, api_path: str) -> AsyncIterator[NhNbModelClient]:
    """A second collaborator in the room, standing in for the user's JupyterLab tab."""
    room = lab.api(
        "PUT", f"/api/collaboration/session/{api_path}", json={"format": "json", "type": "notebook"}
    )
    room.raise_for_status()
    data = room.json()
    url = (
        lab.url.replace("http", "ws", 1).rstrip("/")
        + f"/api/collaboration/room/{data['format']}:{data['type']}:"
        f"{data['fileId']}?sessionId={data['sessionId']}&token={lab.token}"
    )
    client = NhNbModelClient(url, username="user")
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.wait_until_synced(), 10)
    try:
        yield client
    finally:
        task.cancel()
        await asyncio.wait({task})


def cells_of(client: NhNbModelClient) -> list[dict]:
    with client._lock:
        return [ycell.to_py() for ycell in client._doc.ycells]


async def eventually(check, timeout: float = 10.0, interval: float = 0.1):
    """Poll ``check()`` until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


def read_disk(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@pytest.fixture
def helpers(lab: Lab) -> SimpleNamespace:
    """Helpers for the test modules (they can't import this conftest by name)."""
    return SimpleNamespace(
        observer=partial(observer, lab),
        cells_of=cells_of,
        eventually=eventually,
        read_disk=read_disk,
        new_notebook=new_notebook,
        start_lab=start_lab,
        stop=_stop,
        observe=observer,  # observe(lab, api_path) for a lab other than the session's
        NB=NB,
    )
