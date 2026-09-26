"""libexec/nh-sync: the immutable, versioned runtime venv; locking; detached background sync.

Most tests use the fake uv from hookenv; ``test_real_uv_sync_end_to_end`` runs the real
``uv sync --frozen --no-dev`` into a temp plugin data dir.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from hookenv import LIBEXEC, PLUGIN, Sandbox, lock_hash, needs_system_python

pytestmark = needs_system_python


def nh_sync(sandbox: Sandbox, *args: str, env: dict[str, str] | None = None, **kw):
    return subprocess.run(
        ["/bin/sh", str(LIBEXEC / "nh-sync"), *args],
        capture_output=True,
        text=True,
        env=env or sandbox.env,
        timeout=kw.pop("timeout", 60),
        **kw,
    )


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def venv_of(sandbox: Sandbox) -> Path:
    return sandbox.data / f"venv-{lock_hash()}"


def test_path_is_keyed_by_the_lockfile_hash(sandbox: Sandbox) -> None:
    proc = nh_sync(sandbox, "--path")
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(venv_of(sandbox))


def test_foreground_builds_the_runtime(sandbox: Sandbox) -> None:
    assert nh_sync(sandbox, "--check").returncode == 1
    env = dict(sandbox.env, UV_PYTHON="3.12", VIRTUAL_ENV="/some/venv")
    proc = nh_sync(sandbox, "--foreground", env=env)
    assert proc.returncode == 0, proc.stderr
    [call] = sandbox.uv_calls()
    args, project_env, uv_python, virtual_env = call
    assert args == f"sync --project {PLUGIN}/server --frozen --no-dev --no-progress"
    assert project_env == str(venv_of(sandbox))
    assert (uv_python, virtual_env) == ("unset", "unset")
    assert (venv_of(sandbox) / ".nh-ready").read_text().strip() == lock_hash()
    assert not (sandbox.data / "sync.lock").exists()
    assert "ready:" in (sandbox.data / "logs" / "sync.log").read_text()
    assert nh_sync(sandbox, "--check").returncode == 0
    assert nh_sync(sandbox, "--foreground").returncode == 0
    assert len(sandbox.uv_calls()) == 1  # ready: nothing to do


def test_failed_sync_is_not_ready_and_releases_the_lock(sandbox: Sandbox) -> None:
    proc = nh_sync(sandbox, "--foreground", env=dict(sandbox.env, FAKE_UV_FAIL="1"))
    assert proc.returncode == 1
    assert "sync.log" in proc.stderr
    assert not (venv_of(sandbox) / ".nh-ready").exists()
    assert not (sandbox.data / "sync.lock").exists()
    assert "sync failed" in (sandbox.data / "logs" / "sync.log").read_text()


def test_missing_uv_is_reported(sandbox: Sandbox) -> None:
    fallbacks = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
    if any(os.path.exists(path) for path in fallbacks):
        pytest.skip("a uv in a fallback location would be found")
    (sandbox.bin / "uv").unlink()
    proc = nh_sync(sandbox, "--foreground")
    assert proc.returncode == 3
    assert "uv not found" in proc.stderr
    assert "uv not found" in (sandbox.data / "logs" / "sync.log").read_text()


def test_uv_is_found_in_the_home_fallbacks(sandbox: Sandbox) -> None:
    local_bin = sandbox.home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (sandbox.bin / "uv").rename(local_bin / "uv")
    assert nh_sync(sandbox, "--foreground").returncode == 0
    assert len(sandbox.uv_calls()) == 1


def test_keeps_the_newest_two_runtimes(sandbox: Sandbox) -> None:
    now = time.time()
    for age, name in enumerate(["venv-aaaaaaaaaaaa", "venv-bbbbbbbbbbbb", "venv-cccccccccccc"]):
        old = sandbox.data / name
        (old / "bin").mkdir(parents=True)
        (old / ".nh-ready").write_text("x")
        os.utime(old, (now - 100 * (age + 1), now - 100 * (age + 1)))
    assert nh_sync(sandbox, "--foreground").returncode == 0
    left = sorted(path.name for path in sandbox.data.glob("venv-*"))
    assert left == sorted(["venv-aaaaaaaaaaaa", venv_of(sandbox).name])


def test_stale_lock_is_recovered(sandbox: Sandbox) -> None:
    lock = sandbox.data / "sync.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{dead_pid()}\n")
    assert nh_sync(sandbox, "--foreground").returncode == 0
    assert not lock.exists()
    assert "recovered a stale lock" in (sandbox.data / "logs" / "sync.log").read_text()


def test_live_lock_blocks_background_start(sandbox: Sandbox) -> None:
    lock = sandbox.data / "sync.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    assert nh_sync(sandbox, "--ensure-background").returncode == 0
    time.sleep(1)
    assert sandbox.uv_calls() == []
    shutil.rmtree(lock)


def test_foreground_waits_for_the_lock_holder(sandbox: Sandbox) -> None:
    lock = sandbox.data / "sync.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    waiter = subprocess.Popen(
        ["/bin/sh", str(LIBEXEC / "nh-sync"), "--foreground"], env=sandbox.env
    )
    time.sleep(1.5)
    assert waiter.poll() is None
    assert sandbox.uv_calls() == []
    shutil.rmtree(lock)  # the holder "finished" without building: the waiter takes over
    assert waiter.wait(timeout=20) == 0
    assert (venv_of(sandbox) / ".nh-ready").exists()


def test_concurrent_foreground_syncs_run_uv_once(sandbox: Sandbox) -> None:
    env = dict(sandbox.env, FAKE_UV_SLEEP="1")
    procs = [
        subprocess.Popen(["/bin/sh", str(LIBEXEC / "nh-sync"), "--foreground"], env=env)
        for _ in range(4)
    ]
    assert [proc.wait(timeout=30) for proc in procs] == [0, 0, 0, 0]
    assert len(sandbox.uv_calls()) == 1


def test_detached_sync_survives_its_parent_and_sighup(sandbox: Sandbox) -> None:
    """S4: the SessionStart shim exits at once; the nohup'd sync must carry on.

    The parent runs in its own process group, which gets SIGHUP after the parent exits
    (what a closing terminal sends). The background sync is still in that group.
    """
    pidfile = sandbox.tmp / "uv.pid"
    env = dict(sandbox.env, FAKE_UV_SLEEP="2", FAKE_UV_PIDFILE=str(pidfile))
    started = time.monotonic()
    parent = subprocess.Popen(
        ["/bin/sh", "-c", f'/bin/sh "{LIBEXEC / "nh-sync"}" --ensure-background; exit 0'],
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = parent.communicate(timeout=10)
    assert parent.returncode == 0
    assert time.monotonic() - started < 1.5, "the hook must not wait for the sync"
    assert (out, err) == (b"", b""), "the detached sync must not hold the hook's pipes"
    assert sandbox.wait_for(pidfile, timeout=10)
    os.killpg(parent.pid, signal.SIGHUP)
    uv_pid = int(pidfile.read_text())
    os.kill(uv_pid, 0)  # still running after the parent exited and the group got SIGHUP
    assert sandbox.wait_for(venv_of(sandbox) / ".nh-ready", timeout=15)


def test_ensure_background_is_a_no_op_when_ready(sandbox: Sandbox) -> None:
    sandbox.ready_runtime()
    assert nh_sync(sandbox, "--ensure-background").returncode == 0
    time.sleep(0.5)
    assert sandbox.uv_calls() == []


def test_bad_mode_and_missing_data_dir(sandbox: Sandbox) -> None:
    assert nh_sync(sandbox, "--bogus").returncode == 2
    env = {k: v for k, v in sandbox.env.items() if k != "CLAUDE_PLUGIN_DATA"}
    proc = nh_sync(sandbox, "--check", env=env)
    assert proc.returncode == 2 and "CLAUDE_PLUGIN_DATA" in proc.stderr


def plugin_copy(tmp: Path) -> Path:
    """A plugin root with the real libexec, pyproject and lockfile, and a stub gateway."""
    root = tmp / "plugin"
    shutil.copytree(LIBEXEC, root / "libexec")
    (root / "server" / "src" / "nh_gateway").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(PLUGIN / "server" / name, root / "server" / name)
    return root


@pytest.mark.slow
def test_real_uv_sync_end_to_end(sandbox: Sandbox) -> None:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not os.access(uv, os.X_OK):
        pytest.skip("needs uv")
    root = plugin_copy(sandbox.tmp)
    env = dict(sandbox.env, PATH=f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin")
    env["HOME"] = str(Path.home())  # reuse the user's uv cache and managed Pythons
    script = str(root / "libexec" / "nh-sync")
    proc = subprocess.run(
        ["/bin/sh", script, "--foreground"], capture_output=True, text=True, env=env, timeout=600
    )
    log = sandbox.data / "logs" / "sync.log"
    assert proc.returncode == 0, log.read_text() if log.exists() else proc.stderr
    venv = sandbox.data / f"venv-{lock_hash(root)}"
    assert (venv / ".nh-ready").exists()
    assert subprocess.run(["/bin/sh", script, "--check"], env=env).returncode == 0
    check = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import fastmcp, jupyter_nbmodel_client, pytest"],
        capture_output=True,
        text=True,
    )
    assert "No module named 'pytest'" in check.stderr, "dev dependencies must not be installed"
    imports = "import fastmcp, jupyter_nbmodel_client, jupyter_kernel_client, PIL, filelock"
    assert subprocess.run([str(venv / "bin" / "python"), "-c", imports]).returncode == 0
    assert not (root / "server" / ".venv").exists()
