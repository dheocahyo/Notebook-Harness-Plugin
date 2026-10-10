"""/nh:review's deadline through a real kernel (design §6.10, spike V7): ``nhctl fresh-run
--review --timeout`` stops a run that hangs in a cell, reports the cells that finished (D153,
partial), and leaves no runner, kernel or process a cell started behind (in the kernel's group or
in a session of its own), while a live kernel (the user's) keeps running with its state."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jupyter_client.manager import KernelManager

pytestmark = pytest.mark.integration

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"
SERVER = PLUGIN / "server"
VENV_PYTHON = SERVER / ".venv" / "bin" / "python"
SYSTEM_PYTHON = "/usr/bin/python3"
TIMEOUT_S = 12


def cell(source: str, count: int) -> dict:
    return {"cell_type": "code", "execution_count": count, "outputs": [], "metadata": {},
            "source": source}  # fmt: skip


CELLS = [
    cell("import os\nopen('kernel.pid', 'w').write(str(os.getpid()))", 1),
    cell("rows = [1, 2, 3]", 2),
    cell("prices = {}\nprices['price']", 3),
    cell(  # processes the notebook starts: one in the kernel's group, one in its own session
        "import subprocess, time\nchild = subprocess.Popen(['sleep', '300'])\n"
        "open('child.pid', 'w').write(str(child.pid))\n"
        "esc = subprocess.Popen(['sleep', '301'], start_new_session=True)\n"
        "open('esc.pid', 'w').write(str(esc.pid))\ntime.sleep(600)",
        4,
    ),
    cell("after = 1", 5),
]


def ready_runtime(tmp_path: Path) -> Path:
    data = tmp_path / "plugin-data"
    venv = data / ("venv-" + hashlib.sha256((SERVER / "uv.lock").read_bytes()).hexdigest()[:12])
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(VENV_PYTHON)
    (venv / ".nh-ready").write_text("")
    return data


def processes() -> list[tuple[int, str, str]]:
    """(pid, state, command line) of every process, zombies included (``ps``: Linux and macOS)."""
    out = subprocess.run(
        ["ps", "-eo", "pid=,stat=,args="], capture_output=True, text=True, check=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            rows.append((int(parts[0]), parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


def run_in(client, code: str) -> str:
    """Run ``code`` in a live kernel; its stdout."""
    seen: list[str] = []

    def hook(msg: dict) -> None:
        if msg["msg_type"] == "stream":
            seen.append(msg["content"]["text"])

    reply = client.execute_interactive(code, output_hook=hook, timeout=60)
    assert reply["content"]["status"] == "ok", reply["content"]
    return "".join(seen)


def running(pid: int) -> bool:
    """Alive and not a zombie (a zombie whose parent hasn't reaped it yet runs nothing)."""
    return any(p == pid and not state.startswith("Z") for p, state, _ in processes())


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="server venv not synced")
def test_a_hanging_cell_gives_a_partial_review_and_leaves_nothing_running(tmp_path: Path):
    project = tmp_path / "review_timeout_project"
    (project / ".nh" / "state").mkdir(parents=True)
    (project / ".nh" / "state" / "env.json").write_text(
        json.dumps({"manager": "uv", "prefix": str(SERVER / ".venv")})
    )
    (project / "notebooks").mkdir()
    nb = {"cells": CELLS, "nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                      "language": "python"}}}  # fmt: skip
    (project / "notebooks" / "01_eda.ipynb").write_text(json.dumps(nb))
    data = ready_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "jupyter-runtime"  # the live kernel's: the review must not use it
    runtime.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "NH_PYTHON": SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable,
        "JUPYTER_RUNTIME_DIR": str(runtime),
        "JUPYTER_DATA_DIR": str(tmp_path / "jupyter-data"),
        "JUPYTER_CONFIG_DIR": str(tmp_path / "jupyter-config"),
        "LANG": "en_US.UTF-8",
    }
    live = KernelManager(kernel_name="python3", connection_file=str(runtime / "kernel-live.json"))
    live.start_kernel()
    client = live.client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=60)
        run_in(client, "rows = ['live']")
        argv = ["/bin/sh", str(PLUGIN / "bin" / "nhctl"), "fresh-run", "notebooks/01_eda.ipynb"]
        argv += ["--review", "--timeout", str(TIMEOUT_S), "--plugin-data", str(data), "--json"]
        started = time.monotonic()
        proc = subprocess.run(
            argv, cwd=project, env=env, capture_output=True, text=True, timeout=240, check=False
        )
        took = time.monotonic() - started
        # The live kernel: still running, its state and its connection file as they were.
        assert live.is_alive()
        assert run_in(client, "print(rows)") == "['live']\n"
        assert sorted(p.name for p in runtime.iterdir()) == ["kernel-live.json"]
    finally:
        client.stop_channels()
        live.shutdown_kernel(now=True)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    result = json.loads(proc.stdout)

    assert result["error"]["code"] == "D153"
    assert "while the cell `import subprocess, time` [4] was running" in result["error"]["message"]
    assert (result["ok"], result["complete"], result["timeout_s"]) == (False, False, TIMEOUT_S)
    assert result["ran"] == {"ok": 2, "error": 1, "not_run": 2}
    assert [(c["index"], c["ename"], c["evalue"]) for c in result["failing"]] == [
        (2, "KeyError", "'price'")
    ]
    assert result["stopped_at"] == {"index": 3, "label": "the cell `import subprocess, time` [4]"}
    assert [(c["index"], c["reason"]) for c in result["not_run"]] == [
        (3, "stopped"),
        (4, "timeout"),
    ]
    assert TIMEOUT_S <= took < TIMEOUT_S + 30, took
    report = (project / result["report"]).read_text()
    assert f"**Partial:** the review stopped after {TIMEOUT_S} s" in report
    assert list((project / ".nh" / "tmp").glob("rt-*")) == []  # the private runtime dir is gone

    kernel = int((project / "notebooks" / "kernel.pid").read_text())
    child = int((project / "notebooks" / "child.pid").read_text())
    esc = int((project / "notebooks" / "esc.pid").read_text())
    end = time.monotonic() + 10  # SIGKILLed processes may take a moment to be reaped
    while time.monotonic() < end and any(running(pid) for pid in (kernel, child, esc)):
        time.sleep(0.2)
    try:
        assert not running(kernel), "the kernel is still running"
        assert not running(child), "a process the notebook started is still running"
        assert not running(esc), "a process the notebook started in its own session is running"
    finally:  # the test's own leftovers, if any
        for pid in (child, esc):
            if running(pid):
                os.kill(pid, 9)
    left = [
        (pid, args)
        for pid, state, args in processes()
        if "review_timeout_project" in args and not state.startswith("Z")
    ]
    assert left == [], left  # the runner (its argv names the copy) and the kernel are gone
