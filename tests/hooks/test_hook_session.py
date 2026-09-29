"""SessionStart context and runtime pre-sync; the Setup pre-warm."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from hookenv import Sandbox, command_line, lock_hash, needs_system_python

pytestmark = needs_system_python

HARNESS = """version = 1
[project]
name = "sales"
goal = "Find what drives late deliveries"   # a comment
notebook = "notebooks/01_eda.ipynb"
[guard]
foreign_mcp = "deny"
"""


def start(sandbox: Sandbox, env: dict[str, str] | None = None, **fields):
    payload = sandbox.payload("SessionStart", source="startup", **fields)
    return sandbox.run("SessionStart", payload, env=env)


def write_lab(sandbox: Sandbox, **lab) -> None:
    path = sandbox.nh / "state" / "lab.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lab))


def test_context_describes_the_project(sandbox: Sandbox) -> None:
    (sandbox.project / "harness.toml").write_text(HARNESS)
    sandbox.ready_runtime()
    run = start(sandbox)
    assert run.returncode == 0
    assert run.output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    text = run.context
    assert "Goal: Find what drives late deliveries." in text
    assert "Main notebook: notebooks/01_eda.ipynb." in text
    assert "nh:notebook" in text and "mcp__plugin_nh_nh__nh_inspect" in text
    assert "Never Read, Write, Edit or NotebookEdit .ipynb" in text
    assert "JupyterLab: no running server found for this project." in text
    assert 'nh_inspect(view="status")' in text and "(E130)" in text
    assert "nhctl lab start" in text
    assert "nh runtime: ready." in text
    assert sandbox.uv_calls() == []


def test_running_lab_is_reported_without_its_token(sandbox: Sandbox) -> None:
    runtime_file = sandbox.tmp / "jpserver-123.json"
    runtime_file.write_text("{}")
    live = subprocess.Popen(["sleep", "30"])
    try:
        write_lab(
            sandbox,
            pid=live.pid,
            url="http://127.0.0.1:8888/lab?token=abc123",
            runtime_file=str(runtime_file),
            env_prefix=str(sandbox.project / ".venv"),
        )
        text = start(sandbox).context
    finally:
        live.kill()
        live.wait()
    assert f"JupyterLab: running at http://127.0.0.1:8888/lab (pid {live.pid})." in text
    assert "abc123" not in text


def test_dead_lab_pid_or_missing_runtime_file_means_not_running(sandbox: Sandbox) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    write_lab(sandbox, pid=gone.pid, url="http://127.0.0.1:8888/lab")
    assert "JupyterLab: no running server found" in start(sandbox).context
    live = subprocess.Popen(["sleep", "30"])
    try:
        write_lab(sandbox, pid=live.pid, url="http://x/lab", runtime_file=str(sandbox.tmp / "no"))
        assert "JupyterLab: no running server found" in start(sandbox).context
    finally:
        live.kill()
        live.wait()


@contextmanager
def live_pid() -> Iterator[int]:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def write_server(folder: Path, pid: int, root: Path, port: int, version: str = "2.14.2") -> Path:
    """A jpserver-<pid>.json as jupyter_server writes it."""
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "base_url": "/",
        "hostname": "127.0.0.1",
        "pid": pid,
        "port": port,
        "root_dir": str(root),
        "secure": False,
        "sock": "",
        "token": "s3cr3t-token",
        "url": f"http://127.0.0.1:{port}/",
        "version": version,
    }
    path = folder / f"jpserver-{pid}.json"
    path.write_text(json.dumps(record))
    (folder / f"jpserver-{pid}-open.html").write_text("<html>token</html>")
    return path


def test_lab_the_user_started_is_reported_running(sandbox: Sandbox) -> None:
    """Review finding 10: no lab.json, but a live project server in JUPYTER_RUNTIME_DIR."""
    runtime = sandbox.tmp / "runtime"
    with live_pid() as pid:
        write_server(runtime, pid, sandbox.project, 64526)
        text = start(sandbox, env=dict(sandbox.env, JUPYTER_RUNTIME_DIR=str(runtime))).context
    assert f"JupyterLab: running at http://127.0.0.1:64526/ (pid {pid})." in text
    assert "no running server" not in text and "s3cr3t" not in text


def test_stale_lab_json_falls_back_to_the_default_runtime_dirs(sandbox: Sandbox) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    write_lab(sandbox, pid=gone.pid, url="http://127.0.0.1:8888/lab")
    with live_pid() as pid:
        # ~/Library/Jupyter/runtime (macOS) and ~/.local/share/jupyter/runtime (Linux)
        write_server(sandbox.home / "Library" / "Jupyter" / "runtime", pid, sandbox.project, 9001)
        assert "running at http://127.0.0.1:9001/" in start(sandbox).context
    with live_pid() as pid:
        folder = sandbox.home / ".local" / "share" / "jupyter" / "runtime"
        write_server(folder, pid, sandbox.tmp, 9002)  # a parent folder of the project
        assert "running at http://127.0.0.1:9002/" in start(sandbox).context


def test_prefers_the_server_rooted_at_the_project(sandbox: Sandbox) -> None:
    runtime = sandbox.tmp / "runtime"
    env = dict(sandbox.env, JUPYTER_RUNTIME_DIR=str(runtime))
    with live_pid() as outer, live_pid() as inner:
        write_server(runtime, outer, sandbox.tmp, 9101)
        write_server(runtime, inner, sandbox.project, 9102)
        os.utime(runtime / f"jpserver-{outer}.json", (2e9, 2e9))  # the newer file
        assert "running at http://127.0.0.1:9102/" in start(sandbox, env=env).context


@pytest.mark.parametrize("case", ["other-root", "dead-pid", "old-server", "notebook-server"])
def test_servers_the_gateway_would_not_use_are_ignored(sandbox: Sandbox, case: str) -> None:
    runtime = sandbox.tmp / "runtime"
    env = dict(sandbox.env, JUPYTER_RUNTIME_DIR=str(runtime))
    elsewhere = sandbox.tmp / "elsewhere"
    elsewhere.mkdir()
    with live_pid() as pid:
        if case == "other-root":
            write_server(runtime, pid, elsewhere, 9201)
        elif case == "dead-pid":
            gone = subprocess.Popen(["true"])
            gone.wait()
            write_server(runtime, gone.pid, sandbox.project, 9201)
        elif case == "old-server":
            write_server(runtime, pid, sandbox.project, 9201, version="1.24.0")
        else:
            path = write_server(runtime, pid, sandbox.project, 9201)
            path.rename(runtime / f"nbserver-{pid}.json")
        text = start(sandbox, env=env).context
    assert "JupyterLab: no running server found" in text, case


def test_eval_runs_skip_the_runtime_sync(sandbox: Sandbox) -> None:
    """`claude plugin eval` sets CLAUDE_CODE_EVAL_CONFINED=1; its nh tools are mocked."""
    env = dict(sandbox.env, CLAUDE_CODE_EVAL_CONFINED="1")
    run = start(sandbox, env=env)
    assert "Notebook Harness (nh) is active" in run.context
    assert "nh runtime" not in run.context and "/mcp" not in run.context
    setup = sandbox.run("Setup", sandbox.payload("Setup", trigger="init"), env=env)
    assert (setup.returncode, setup.stdout) == (0, "")
    assert not (sandbox.data / "logs" / "sync.log").exists()
    assert not (sandbox.data / f"venv-{lock_hash()}").exists()
    assert sandbox.uv_calls() == []


def test_eval_flag_set_to_zero_still_syncs(sandbox: Sandbox) -> None:
    env = dict(sandbox.env, CLAUDE_CODE_EVAL_CONFINED="0", FAKE_UV_SLEEP="1")
    run = start(sandbox, env=env)
    assert "nh runtime: installing in the background" in run.context
    assert sandbox.wait_for(sandbox.data / f"venv-{lock_hash()}" / ".nh-ready")


def test_missing_runtime_starts_a_background_sync(sandbox: Sandbox) -> None:
    run = start(sandbox, env=dict(sandbox.env, FAKE_UV_SLEEP="1"))
    assert "nh runtime: installing in the background" in run.context
    assert str(sandbox.data / "logs" / "sync.log") in run.context
    venv = sandbox.data / f"venv-{lock_hash()}"
    assert sandbox.wait_for(venv / ".nh-ready")
    [call] = sandbox.uv_calls()
    assert call[0].startswith("sync --project ") and "--frozen --no-dev" in call[0]
    assert call[1] == str(venv)


def test_sync_starts_even_outside_nh_projects(sandbox: Sandbox) -> None:
    plain = sandbox.tmp / "plain"
    plain.mkdir()
    env = dict(sandbox.env, CLAUDE_PROJECT_DIR=str(plain))
    handler = sandbox.handlers("SessionStart")[0]
    run = sandbox.run_argv(command_line(handler), sandbox.payload("SessionStart"), env)
    assert run.returncode == 0 and run.stdout == ""
    assert sandbox.wait_for(sandbox.data / f"venv-{lock_hash()}" / ".nh-ready")
    assert not (plain / ".nh").exists()


def test_context_without_harness_toml(sandbox: Sandbox) -> None:
    text = start(sandbox).context
    assert "Goal:" not in text
    assert text.startswith("Notebook Harness (nh) is active in this project")


def test_unparsable_harness_toml_still_gives_context(sandbox: Sandbox) -> None:
    (sandbox.project / "harness.toml").write_text("[project\ngoal = \n")
    assert "Notebook Harness (nh) is active" in start(sandbox).context


# --- redaction (design §6.8): the context never carries a secret ---------------------------

PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; under DB_PASSWORD in the project's .env


def goal_toml(goal: str, notebook: str = "notebooks/01_eda.ipynb") -> str:
    return f'[project]\ngoal = "{goal}"\nnotebook = "{notebook}"\n'


def test_the_goal_is_redacted_before_its_cut(sandbox: Sandbox) -> None:
    (sandbox.project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    (sandbox.project / "harness.toml").write_text(goal_toml(f"Load sales as app with {PASSWORD}"))
    assert "Goal: Load sales as app with [redacted:DB_PASSWORD]." in start(sandbox).context
    prefix = "Load the sales tables " + "and their returns " * 9 + "with pw "  # 192 chars
    assert len(prefix) == 192  # the password straddles the 200-char cut
    (sandbox.project / "harness.toml").write_text(goal_toml(prefix + PASSWORD))
    text = start(sandbox).context
    assert f"Goal: {prefix}[redact…." in text
    assert PASSWORD[:3] not in text


def test_the_whole_context_is_redacted(sandbox: Sandbox) -> None:
    token = "wh-" + "tok-8c1f0e2d9a"
    env = dict(sandbox.env, WAREHOUSE_TOKEN=token)
    toml = goal_toml(f"Query with {token}", notebook=f"notebooks/{token}.ipynb")
    (sandbox.project / "harness.toml").write_text(toml)
    text = start(sandbox, env=env).context
    assert "Goal: Query with [redacted:WAREHOUSE_TOKEN]." in text
    assert "Main notebook: notebooks/[redacted:WAREHOUSE_TOKEN].ipynb." in text
    assert token not in text


def test_setup_builds_the_runtime_in_the_foreground(sandbox: Sandbox) -> None:
    run = sandbox.run("Setup", sandbox.payload("Setup", trigger="init"))
    assert run.returncode == 0 and run.stdout == ""
    assert (sandbox.data / f"venv-{lock_hash()}" / ".nh-ready").exists()
