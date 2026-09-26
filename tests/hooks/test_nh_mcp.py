"""libexec/nh-mcp: the gateway launcher, with a stub ``nh_gateway`` package in a plugin copy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from hookenv import LIBEXEC, PLUGIN, Sandbox, lock_hash, needs_system_python

pytestmark = needs_system_python

STUB_MAIN = """import json, os, sys
keys = ("NH_CC_PID", "PYTHONPATH", "PYTHONPYCACHEPREFIX", "UV_PYTHON", "VIRTUAL_ENV", "PYTHONHOME")
print(json.dumps({
    "ppid": os.getppid(),
    "env": {key: os.environ.get(key) for key in keys},
    "executable": sys.executable,
    "no_user_site": sys.flags.no_user_site,
    "stdin": sys.stdin.read(),
}))
"""


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """A plugin root with the real libexec scripts and lockfile and a stub gateway."""
    root = tmp_path / "plugin"
    shutil.copytree(LIBEXEC, root / "libexec")
    package = root / "server" / "src" / "nh_gateway"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(STUB_MAIN)
    shutil.copy(PLUGIN / "server" / "uv.lock", root / "server" / "uv.lock")
    return root


def launch(plugin: Path, env: dict[str, str], stdin: str = "", timeout: float = 60):
    started = time.monotonic()
    proc = subprocess.run(
        ["/bin/sh", str(plugin / "libexec" / "nh-mcp")],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc, time.monotonic() - started


def test_ready_runtime_execs_the_gateway(sandbox: Sandbox, plugin: Path) -> None:
    sandbox.ready_runtime()
    env = dict(sandbox.env, UV_PYTHON="3.12", VIRTUAL_ENV="/x", PYTHONHOME="/broken")
    proc, _ = launch(plugin, env, stdin='{"jsonrpc":"2.0"}\n')
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["ppid"] == os.getpid(), "exec must keep Claude Code as the gateway's parent"
    assert report["env"] == {
        "NH_CC_PID": str(os.getpid()),
        "PYTHONPATH": str(plugin / "server" / "src"),
        "PYTHONPYCACHEPREFIX": str(sandbox.data / "pycache"),
        "UV_PYTHON": None,
        "VIRTUAL_ENV": None,
        "PYTHONHOME": None,
    }
    assert report["executable"] == str(sandbox.data / f"venv-{lock_hash()}" / "bin" / "python")
    assert report["no_user_site"] == 1
    assert report["stdin"] == '{"jsonrpc":"2.0"}\n'
    assert sandbox.uv_calls() == []


def test_missing_runtime_is_synced_then_launched(sandbox: Sandbox, plugin: Path) -> None:
    proc, _ = launch(plugin, dict(sandbox.env, FAKE_UV_SLEEP="1"))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ppid"] == os.getpid()
    [call] = sandbox.uv_calls()
    assert call[0].startswith(f"sync --project {plugin}/server --frozen --no-dev")


@pytest.mark.slow
def test_runtime_not_ready_after_25s_exits_1(sandbox: Sandbox, plugin: Path) -> None:
    proc, seconds = launch(plugin, dict(sandbox.env, FAKE_UV_FAIL="1"))
    assert proc.returncode == 1
    assert proc.stdout == "", "stdout is reserved for MCP frames"
    assert "not ready yet" in proc.stderr
    assert "/mcp" in proc.stderr and "Reconnect" in proc.stderr
    assert str(sandbox.data / "logs" / "sync.log") in proc.stderr
    assert 24 <= seconds < 40
    assert len(sandbox.uv_calls()) == 1  # it started a sync


@pytest.mark.slow
def test_waits_for_a_sync_someone_else_holds(sandbox: Sandbox, plugin: Path) -> None:
    """A SessionStart sync holds the lock: nh-mcp starts no second sync and just polls."""
    lock = sandbox.data / "sync.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    venv = sandbox.data / f"venv-{lock_hash(plugin)}"

    def finish_in_background() -> subprocess.Popen:
        script = (
            f'sleep 2; mkdir -p "{venv}/bin"; ln -s "{sandbox.env["FAKE_UV_TARGET"]}" '
            f'"{venv}/bin/python"; : > "{venv}/.nh-ready"; rm -rf "{lock}"'
        )
        return subprocess.Popen(["/bin/sh", "-c", script])

    holder = finish_in_background()
    try:
        proc, seconds = launch(plugin, sandbox.env)
    finally:
        holder.wait()
    assert proc.returncode == 0, proc.stderr
    assert 1.5 < seconds < 10
    assert sandbox.uv_calls() == []


def test_windows_shells_are_refused(sandbox: Sandbox, plugin: Path) -> None:
    fake_uname = sandbox.bin / "uname"
    fake_uname.write_text("#!/bin/sh\necho MINGW64_NT-10.0-19045\n")
    fake_uname.chmod(0o755)
    proc, _ = launch(plugin, sandbox.env)
    assert proc.returncode == 1
    assert "use WSL" in proc.stderr
    assert proc.stdout == ""


def test_missing_plugin_data_is_refused(sandbox: Sandbox, plugin: Path) -> None:
    env = {k: v for k, v in sandbox.env.items() if k != "CLAUDE_PLUGIN_DATA"}
    proc, _ = launch(plugin, env)
    assert proc.returncode == 1
    assert "CLAUDE_PLUGIN_DATA" in proc.stderr
