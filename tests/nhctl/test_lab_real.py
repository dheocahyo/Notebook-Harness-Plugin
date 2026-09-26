"""nhctl lab start against a real JupyterLab 4.6 + collaboration 5 (the server venv's).

Run with -m integration. Covers the token staying out of jupyter's log (finding 11) and
adopting a JupyterLab the user started on the project instead of starting a second one
(finding 10, the review's two_labs.sh).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from nhctl_testlib import SERVER

pytestmark = pytest.mark.integration

VENV = SERVER / ".venv"


@pytest.fixture
def python() -> str:
    """One interpreter is enough here: each test starts a real JupyterLab."""
    return sys.executable


@pytest.fixture
def real_project(env, project):
    if not (VENV / "bin" / "jupyter-lab").exists():
        pytest.skip("the server venv has no JupyterLab (uv sync --all-groups)")
    env.script("uv", "exit 0")
    env.json("scaffold", cwd=project)
    env_json = {"manager": "uv", "prefix": str(VENV), "python": "3.13", "jupyterlab": "4.6.4",
                "jupyter_collaboration": "5.0.4", "synced_at": 0}  # fmt: skip
    (project / ".nh/state").mkdir(parents=True, exist_ok=True)
    (project / ".nh/state/env.json").write_text(json.dumps(env_json))
    pids: list[int] = []
    yield project, pids
    lab = project / ".nh/state/lab.json"
    if lab.exists():
        pids.append(json.loads(lab.read_text())["pid"])
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def live_servers(runtime: Path) -> dict[int, dict]:
    out = {}
    for path in runtime.glob("jpserver-*.json"):
        if path.name.count("-") == 1:
            info = json.loads(path.read_text())
            out[info["pid"]] = info
    return out


def test_token_never_reaches_the_log(env, real_project):
    project, _ = real_project
    started = env.json("lab", "start", "--no-browser", "--timeout", "90", cwd=project, timeout=180)
    assert started["status"] == "started", started
    assert started["session"]["created"] is True
    token = live_servers(env.runtime)[started["pid"]]["token"]
    assert len(token) == 48
    log = project / ".nh/logs/jupyterlab.log"
    text = log.read_text()
    assert "Jupyter Server" in text and token not in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert token not in (project / ".nh/state/lab.json").read_text()
    assert env.json("lab", "stop", cwd=project)["stopped"] is True


def test_adopts_the_users_own_jupyterlab(env, real_project):
    project, pids = real_project
    root = project.resolve()
    cmd = (
        f'"{VENV}/bin/jupyter" lab --no-browser --ip=127.0.0.1 --port=0 '
        f'"--ServerApp.root_dir={root}" >"{env.tmp}/user-lab.log" 2>&1 & echo $!'
    )
    launched = subprocess.run(
        ["/bin/sh", "-c", cmd], env=dict(env.vars, PATH=f"{VENV}/bin:/usr/bin:/bin"),
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    user = int(launched.stdout.strip())
    pids.append(user)
    deadline = time.time() + 90
    while user not in live_servers(env.runtime) and time.time() < deadline:
        time.sleep(0.3)
    assert user in live_servers(env.runtime), (env.tmp / "user-lab.log").read_text()

    assert env.json("lab", "status", cwd=project)["registered"] is False
    started = env.json("lab", "start", "--no-browser", cwd=project, timeout=180)
    assert started["status"] == "adopted" and started["pid"] == user
    assert started["session"]["created"] is True
    assert list(live_servers(env.runtime)) == [user]  # no second server on the project
    lab = json.loads((project / ".nh/state/lab.json").read_text())
    assert lab["pid"] == user and lab["adopted"] is True
    assert env.json("lab", "start", "--no-browser", cwd=project)["status"] == "running"
