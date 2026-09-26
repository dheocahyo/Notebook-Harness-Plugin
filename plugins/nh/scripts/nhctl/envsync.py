"""nhctl env sync|wait (the project environment) and nhctl runtime sync|status (nh's own venv).

The project env lives inside the project: ``<proj>/.venv`` (uv) or ``<proj>/.conda``
(conda). A background sync runs as a detached ``nhctl env sync --_worker`` that holds
an flock for its whole run, writes ``.nh/state/env-sync.json`` and logs to
``.nh/logs/env-sync.log``. Success writes ``.nh/state/env.json``.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import common
from common import NhctlError, Result

from nh_gateway._shared import paths
from nh_gateway._shared.scaffold import core

STATUS_FILE = "env-sync.json"
LOCK_FILE = "env-sync.lock"
LOG_FILE = "env-sync.log"
STARTUP_GRACE_S = 30

_PROBE = """
import importlib.metadata as md, json, platform, sys
def version(*names):
    for name in names:
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            pass
    return None
print(json.dumps({"python": platform.python_version(), "prefix": sys.prefix,
                  "jupyterlab": version("jupyterlab"),
                  "jupyter_collaboration": version("jupyter-collaboration", "jupyter_collaboration")}))
"""


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    env = sub.add_parser("env", parents=[common_opts], help="build the project environment")
    env_sub = env.add_subparsers(dest="action", required=True)
    sync = env_sub.add_parser("sync", parents=[common_opts], help="uv sync / conda env create")
    sync.add_argument("--background", action="store_true", help="return at once; see env wait")
    sync.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    sync.set_defaults(func=cmd_env_sync)
    wait = env_sub.add_parser("wait", parents=[common_opts], help="wait for a background sync")
    wait.add_argument("--timeout", type=float, default=540, help="seconds (default 540)")
    wait.set_defaults(func=cmd_env_wait)

    runtime = sub.add_parser("runtime", parents=[common_opts], help="nh's own Python runtime")
    rt_sub = runtime.add_subparsers(dest="action", required=True)
    rt_sync = rt_sub.add_parser("sync", parents=[common_opts], help="build it via libexec/nh-sync")
    rt_sync.add_argument("--background", action="store_true")
    rt_sync.set_defaults(func=cmd_runtime_sync)
    rt_status = rt_sub.add_parser("status", parents=[common_opts], help="is it ready?")
    rt_status.set_defaults(func=cmd_runtime_status)


# ------------------------------------------------------------------ project env


class EnvLock:
    """An exclusive flock held for a whole sync; the kernel drops it if the process dies."""

    def __init__(self, layout: paths.Layout) -> None:
        layout.locks.mkdir(parents=True, exist_ok=True)
        self.path = layout.locks / LOCK_FILE
        self.fd: int | None = None

    def acquire(self, wait_s: float = 0.0) -> bool:
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def held_elsewhere(self) -> bool:
        if not self.acquire():
            return True
        self.release()
        return False


def manager_for(project: Path) -> tuple[str, str]:
    """(manager, env file) from harness.toml, else from the files present."""
    manager = common.harness_project(project).get("env_manager")
    yml = core.existing_environment_yml(project)
    if manager == "conda" or (manager not in ("uv", "conda") and yml):
        return "conda", yml or "environment.yml"
    if manager == "uv" or (project / "pyproject.toml").is_file():
        return "uv", "pyproject.toml"
    raise NhctlError(
        "D141",
        "This project has no pyproject.toml or environment.yml.",
        "Run nhctl scaffold (or /nh:init) first.",
    )


def env_prefix_for(project: Path, manager: str) -> Path:
    return project / (".venv" if manager == "uv" else ".conda")


def sync_command(project: Path, manager: str, env_file: str) -> tuple[list[str], dict]:
    if not (project / env_file).is_file():
        raise NhctlError("D141", f"{env_file} is missing.", "Run nhctl scaffold first.")
    if manager == "uv":
        uv = core.find_tool("uv")
        if not uv:
            raise NhctlError(
                "D110",
                "uv is not installed.",
                "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            )
        # The env must be <proj>/.venv, whatever venv or override the shell carries.
        env = common.env_with(drop=("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"))
        return [uv, "sync"], env
    prefix = env_prefix_for(project, manager)
    tools = core.Tools.find()
    tool = tools.conda_like
    if not tool:
        raise NhctlError(
            "D111",
            f"{env_file} needs conda or mamba, and neither is installed.",
            "Install Miniforge (https://conda-forge.org/download/).",
        )
    exists = (prefix / "conda-meta").is_dir()
    verb = ["env", "update", "-p", str(prefix), "-f", env_file, "--prune"] if exists else [
        "env", "create", "-p", str(prefix), "-f", env_file,
    ]  # fmt: skip
    cmd = [tool, *verb]
    if tool == tools.conda and needs_libmamba_flag(tool):
        cmd.append("--solver=libmamba")
    env = common.env_with(CONDA_ALWAYS_YES="true", MAMBA_ALWAYS_YES="true")
    return cmd, env


def needs_libmamba_flag(conda: str) -> bool:
    """conda < 23.10 still defaults to the classic solver; use libmamba if it is installed."""
    code, out = common.run([conda, "--version"], timeout=20)
    version = common.version_tuple(out) if code == 0 else ()
    if not version or version >= (23, 10):
        return False
    base = Path(os.path.realpath(conda)).parent.parent
    return bool(glob.glob(str(base / "conda-meta" / "conda-libmamba-solver-*.json")))


def probe_env(prefix: Path) -> dict:
    python = prefix / "bin" / "python"
    code, out = common.run([str(python), "-I", "-c", _PROBE], timeout=60)
    if code != 0:
        raise NhctlError("D142", f"The new environment's Python didn't run: {out.strip()[-300:]}")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise NhctlError("D142", f"Unexpected output from {python}: {out[-300:]}") from exc


def env_problem(env: dict) -> str | None:
    lab = common.version_tuple(env.get("jupyterlab"))
    collab = common.version_tuple(env.get("jupyter_collaboration"))
    if not lab or lab < common.MIN_JUPYTERLAB:
        return f"JupyterLab {env.get('jupyterlab') or 'is missing'}; nh needs 4.6 or newer."
    if not collab or collab < common.MIN_COLLABORATION:
        return (
            f"jupyter-collaboration {env.get('jupyter_collaboration') or 'is missing'}; "
            "nh needs 5 or newer."
        )
    return None


def _write_status(layout: paths.Layout, **fields: object) -> dict:
    status = {"v": 1, "log": f".nh/logs/{LOG_FILE}", **fields}
    paths.atomic_write_json(layout.state / STATUS_FILE, status)
    return status


def run_sync(project: Path, lock_wait_s: float = 0.0) -> dict:
    """Build the env while holding the lock; returns the final status dict."""
    layout = paths.Layout(project)
    lock = EnvLock(layout)
    if not lock.acquire(lock_wait_s):
        raise NhctlError(
            "D143", "An environment sync is already running.", "Run nhctl env wait to follow it."
        )
    base: dict = {"manager": None, "pid": os.getpid(), "started_at": time.time()}
    try:
        manager, env_file = manager_for(project)
        base["manager"] = manager
        cmd, env = sync_command(project, manager, env_file)
        _write_status(layout, status="running", message="Syncing.", **base)
        layout.logs.mkdir(parents=True, exist_ok=True)
        log_path = layout.logs / LOG_FILE
        with open(log_path, "ab") as log:
            header = f"\n== nhctl env sync {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(cmd)}\n"
            log.write(header.encode("utf-8"))
            log.flush()
            try:
                code = subprocess.call(
                    cmd, cwd=str(project), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log
                )
            except OSError as exc:
                log.write(f"{exc}\n".encode())
                code = 127
        if code != 0:
            return _write_status(
                layout, status="error", exit_code=code, finished_at=time.time(),
                message=f"{' '.join([Path(cmd[0]).name, *cmd[1:3]])} failed (exit {code}).",
                log_tail=common.tail(log_path), **base,
            )  # fmt: skip
        prefix = env_prefix_for(project, manager)
        probe = probe_env(prefix)
        info = {
            "manager": manager,
            "prefix": str(prefix),
            "python": probe.get("python"),
            "jupyterlab": probe.get("jupyterlab"),
            "jupyter_collaboration": probe.get("jupyter_collaboration"),
            "synced_at": time.time(),
        }
        paths.atomic_write_json(layout.env_json, info)
        problem = env_problem(info)
        return _write_status(
            layout, status="error" if problem else "ok", exit_code=0, finished_at=time.time(),
            message=problem or "Environment ready.", env=info, **base,
        )  # fmt: skip
    except NhctlError as exc:
        return _write_status(
            layout, status="error", finished_at=time.time(), message=exc.message, fix=exc.fix,
            **base,
        )  # fmt: skip
    finally:
        lock.release()


def read_status(layout: paths.Layout) -> dict:
    """The recorded status, corrected when a 'running' worker has died."""
    status = paths.read_json(layout.state / STATUS_FILE, None)
    if not isinstance(status, dict):
        return {"status": "none", "message": "No environment sync has started."}
    if status.get("status") == "running" and not _still_running(layout, status):
        status = dict(status, status="error", message="The sync stopped unexpectedly.")
        status["log_tail"] = common.tail(layout.logs / LOG_FILE)
    return status


def _still_running(layout: paths.Layout, status: dict) -> bool:
    """The lock is the evidence; a worker that just started may not hold it yet."""
    if EnvLock(layout).held_elsewhere():
        return True
    started = status.get("started_at")
    fresh = isinstance(started, (int, float)) and time.time() - started < STARTUP_GRACE_S
    pid = status.get("pid")
    return fresh and (pid is None or common.pid_alive(pid))


def _status_result(status: dict) -> Result:
    state = status.get("status")
    text = f"env sync: {state}. {status.get('message', '')}".rstrip()
    env = status.get("env") or {}
    if state == "ok":
        text += (
            f"\n{env.get('manager')} env at {env.get('prefix')}: Python {env.get('python')}, "
            f"JupyterLab {env.get('jupyterlab')}, jupyter-collaboration {env.get('jupyter_collaboration')}"
        )
    elif state == "running":
        text += f"\nFollow along in {status.get('log')}; run nhctl env wait again to keep waiting."
    elif status.get("log_tail"):
        text += "\n" + status["log_tail"]
    data = dict(status, ok=state in ("ok", "running"))
    return Result(data, text, 0 if state in ("ok", "running") else 1)


def cmd_env_sync(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    if args._worker:
        # The parent already wrote "running"; a racing `env wait` may probe the lock briefly.
        return _status_result(run_sync(project, lock_wait_s=5.0))
    if not args.background:
        return _status_result(run_sync(project))

    manager, env_file = manager_for(project)
    sync_command(project, manager, env_file)  # fail fast on a missing tool or file
    current = read_status(layout)
    if current.get("status") == "running" or EnvLock(layout).held_elsewhere():
        return _status_result(dict(current, message="A sync is already running."))
    layout.logs.mkdir(parents=True, exist_ok=True)
    # Written before the spawn, so the worker's own status can never be overwritten by it.
    status = _write_status(
        layout, status="running", manager=manager, pid=None, started_at=time.time(),
        message="Started in the background.",
    )  # fmt: skip
    cmd = [sys.executable, "-I", "-S", "-B", str(common.MAIN), "env", "sync", "--_worker"]
    cmd += ["--project", str(project)]
    with open(layout.logs / LOG_FILE, "ab") as log:
        proc = subprocess.Popen(
            cmd, cwd=str(project), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    return _status_result(dict(status, pid=proc.pid))


def cmd_env_wait(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    started = time.monotonic()
    deadline = started + max(0.0, args.timeout)
    status = read_status(layout)
    while status.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        status = read_status(layout)
    status: dict = dict(status, waited_s=round(time.monotonic() - started, 1))
    if status.get("status") == "none":
        status["message"] = "No environment sync has started; run nhctl env sync --background."
        return Result(dict(status, ok=False), status["message"], 1)
    return _status_result(status)


# ---------------------------------------------------------------- plugin runtime


def runtime_status(data: Path) -> dict:
    venv = common.runtime_venv(data)
    ready = bool(venv and (venv / ".nh-ready").is_file())
    others = sorted(
        str(p) for p in data.glob("venv-*") if p.is_dir() and (venv is None or p != venv)
    )
    log = data / "logs" / "sync.log"
    return {
        "data_dir": str(data),
        "venv": str(venv) if venv else None,
        "ready": ready,
        "other_venvs": others,
        "log": str(log) if log.is_file() else None,
    }


def _runtime_text(status: dict) -> str:
    state = "ready" if status["ready"] else "not ready"
    return f"nh runtime: {state} ({status['venv'] or 'no uv.lock found'})"


def cmd_runtime_status(args: argparse.Namespace) -> Result:
    data = common.plugin_data_dir(getattr(args, "plugin_data", None))
    status = runtime_status(data)
    return Result(dict(status, ok=status["ready"]), _runtime_text(status))


def cmd_runtime_sync(args: argparse.Namespace) -> Result:
    data = common.plugin_data_dir(getattr(args, "plugin_data", None))
    script = common.ROOT / "libexec" / "nh-sync"
    if not script.is_file():
        raise NhctlError(
            "D122",
            f"nh-sync is missing from the plugin ({script}).",
            "Reinstall or update the nh plugin.",
        )
    data.mkdir(parents=True, exist_ok=True)
    env = common.env_with(CLAUDE_PLUGIN_ROOT=str(common.ROOT), CLAUDE_PLUGIN_DATA=str(data))
    mode = "--ensure-background" if args.background else "--foreground"
    # Output goes to a file, not a pipe: a detached child that keeps our stdout open would
    # otherwise hold a pipe read until it finishes.
    with tempfile.TemporaryFile() as output:
        try:
            code = subprocess.call(
                ["/bin/sh", str(script), mode], env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, timeout=60 if args.background else 900,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            code = 124
        output.seek(0)
        out = output.read().decode("utf-8", "replace")
    status = runtime_status(data)
    if code != 0:
        tail = common.scrub("\n".join(out.strip().splitlines()[-20:]))
        raise NhctlError(
            "D123",
            f"nh-sync {mode} failed (exit {code}).",
            f"See {status['log'] or data / 'logs'}; check that uv is installed.",
            data={"runtime": status, "output_tail": tail},
        )
    state = "started in the background" if args.background and not status["ready"] else None
    text = _runtime_text(status) + (f"; sync {state}" if state else "")
    return Result(dict(status, ok=True, background=args.background), text)
