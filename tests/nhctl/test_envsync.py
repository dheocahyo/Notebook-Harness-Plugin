"""nhctl env sync|wait with fake uv/conda, and nhctl runtime sync|status with a fake nh-sync."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time

import pytest
from nhctl_testlib import SERVER, env_builder, fake_conda, fake_uv, plugin_copy


def scaffolded(env, project, *tools):
    for name in tools or ("uv",):
        if not (env.bin / name).exists():
            env.script(name, "exit 0")
    env.json("scaffold", cwd=project)
    return project


# ------------------------------------------------------------------------- uv


def test_uv_sync_foreground(env, project):
    scaffolded(env, project)
    fake_uv(env)
    result = env.json(
        "env",
        "sync",
        cwd=project,
        extra={"VIRTUAL_ENV": "/elsewhere", "UV_PROJECT_ENVIRONMENT": "/x"},
    )
    assert result["status"] == "ok" and result["ok"] is True
    calls = (project / "uv-calls.log").read_text()
    assert "uv-args: sync" in calls
    assert "VIRTUAL_ENV=unset UV_PROJECT_ENVIRONMENT=unset" in calls
    env_json = json.loads((project / ".nh/state/env.json").read_text())
    assert {
        k: env_json[k]
        for k in ("manager", "prefix", "python", "jupyterlab", "jupyter_collaboration")
    } == {
        "manager": "uv",
        "prefix": str(project.resolve() / ".venv"),
        "python": "3.13.8",
        "jupyterlab": "4.6.4",
        "jupyter_collaboration": "5.0.4",
    }
    status = json.loads((project / ".nh/state/env-sync.json").read_text())
    assert status["status"] == "ok"
    assert "Installed 42 packages" in (project / ".nh/logs/env-sync.log").read_text()


def test_uv_sync_failure_reports_log_tail(env, project):
    scaffolded(env, project)
    fake_uv(env, exit_code=2)
    proc = env.run("env", "sync", "--json", cwd=project)
    result = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert result["status"] == "error" and "exit 2" in result["message"]
    assert "resolution failed" in result["log_tail"]
    assert not (project / ".nh/state/env.json").exists()


def test_a_failed_syncs_log_tail_is_redacted_on_disk_too(env, project):
    """envsync writes common.tail into .nh/state/env-sync.json, which print_result never sees
    (design §6.8; review of C3)."""
    scaffolded(env, project)
    secret = "Fk7Qm2Wz" + "9Lp4Xv8R"  # fake
    with open(project / ".env", "a") as handle:
        handle.write(f"\nPRIVATE_INDEX_TOKEN={secret}\n")
    env.script("uv", f"""
        if [ "$1" = "--version" ]; then echo "uv 0.10.6 (fake)"; exit 0; fi
        echo "error: 401 from https://pypi.example/simple with token {secret}" >&2
        exit 2
        """)  # fmt: skip
    proc = env.run("env", "sync", "--json", cwd=project)
    assert proc.returncode == 1, proc.stderr
    status = (project / ".nh/state/env-sync.json").read_text()
    assert "with token [redacted:PRIVATE_INDEX_TOKEN]" in json.loads(status)["log_tail"]
    assert secret not in status + proc.stdout + proc.stderr


def test_too_old_jupyterlab_is_an_error(env, project):
    scaffolded(env, project)
    fake_uv(env, {"python": "3.12.1", "jupyterlab": "4.2.0", "jupyter_collaboration": None})
    result = env.json("env", "sync", cwd=project, expect=1)
    assert result["status"] == "error" and "JupyterLab 4.2.0" in result["message"]


def test_background_then_wait(env, project):
    scaffolded(env, project)
    fake_uv(env, delay=1)
    started = env.json("env", "sync", "--background", cwd=project)
    assert started["status"] == "running" and started["pid"]
    again = env.json("env", "sync", "--background", cwd=project)
    assert again["status"] == "running" and "already running" in again["message"]
    waited = env.json("env", "wait", "--timeout", "60", cwd=project)
    assert waited["status"] == "ok", waited
    assert (project / ".nh/state/env.json").is_file()
    assert (project / "uv-calls.log").read_text().count("uv-args: sync") == 1


def test_wait_times_out_then_sees_a_dead_worker(env, project):
    scaffolded(env, project)
    fake_uv(env, delay=30)
    env.json("env", "sync", "--background", cwd=project)
    waited = env.json("env", "wait", "--timeout", "1", cwd=project)
    assert waited["status"] == "running" and waited["waited_s"] >= 1
    status = json.loads((project / ".nh/state/env-sync.json").read_text())
    assert status["status"] == "running" and isinstance(status["pid"], int)
    os.killpg(status["pid"], signal.SIGKILL)  # the worker and its fake uv
    deadline = time.time() + 10
    while time.time() < deadline:
        waited = env.json("env", "wait", "--timeout", "0", cwd=project, expect=None)
        if waited["status"] != "running":
            break
        time.sleep(0.2)
    assert waited["status"] == "error" and "unexpectedly" in waited["message"]


def test_wait_without_sync(env, project):
    scaffolded(env, project)
    result = env.json("env", "wait", "--timeout", "0", cwd=project, expect=1)
    assert result["status"] == "none"


# ---------------------------------------------------------------------- conda


@pytest.mark.parametrize(
    ("version", "libmamba", "flag"),
    [("23.7.4", True, True), ("23.7.4", False, False), ("24.1.0", True, False)],
)
def test_conda_create_then_update(env, project, tmp_path, version, libmamba, flag):
    fake_conda(env, tmp_path, version, libmamba)
    env.json("scaffold", cwd=project)
    assert (project / "environment.yml").is_file()
    result = env.json("env", "sync", cwd=project)
    assert result["status"] == "ok"
    prefix = str(project.resolve() / ".conda")
    calls = (project / "conda-calls.log").read_text().splitlines()
    expected = f"conda-args: env create -p {prefix} -f environment.yml"
    assert calls[0] == expected + (" --solver=libmamba" if flag else "")
    assert calls[1].endswith("CONDA_ALWAYS_YES=true")
    env.json("env", "sync", cwd=project)
    calls = (project / "conda-calls.log").read_text().splitlines()
    assert calls[2].startswith(f"conda-args: env update -p {prefix} -f environment.yml --prune")
    env_json = json.loads((project / ".nh/state/env.json").read_text())
    assert env_json["manager"] == "conda" and env_json["prefix"] == prefix


def test_mamba_is_preferred(env, project, tmp_path):
    fake_conda(env, tmp_path)
    env.script("mamba", env_builder("mamba", ".conda", None, 0, 0))
    env.json("scaffold", cwd=project)
    env.json("env", "sync", cwd=project)
    first = (project / "mamba-calls.log").read_text().splitlines()[0]
    assert first.startswith("mamba-args: env create -p") and "--solver" not in first
    assert not (project / "conda-calls.log").exists()


# -------------------------------------------------------------------- runtime


def runtime_env(env, tmp_path, script: str | None):
    root = plugin_copy(tmp_path / "plugin")
    if script is not None:
        (root / "libexec/nh-sync").write_text(script)
    return root


def venv_name():
    return "venv-" + hashlib.sha256((SERVER / "uv.lock").read_bytes()).hexdigest()[:12]


FAKE_SYNC = """#!/bin/sh
echo "args=$* root=$CLAUDE_PLUGIN_ROOT data=$CLAUDE_PLUGIN_DATA" >> "$CLAUDE_PLUGIN_DATA/calls.log"
mkdir -p "$CLAUDE_PLUGIN_DATA/{venv}" && touch "$CLAUDE_PLUGIN_DATA/{venv}/.nh-ready"
"""


def test_runtime_sync_and_status(env, tmp_path, project):
    root = runtime_env(env, tmp_path, FAKE_SYNC.format(venv=venv_name()))
    nhctl = root / "bin/nhctl"
    data = tmp_path / "data"
    before = env.json("runtime", "status", "--plugin-data", str(data), cwd=project, nhctl=nhctl)
    assert before["ready"] is False and before["venv"] == str(data / venv_name())
    synced = env.json("runtime", "sync", "--plugin-data", str(data), cwd=project, nhctl=nhctl)
    assert synced["ready"] is True
    calls = (data / "calls.log").read_text()
    assert f"args=--foreground root={root.resolve()} data={data}" in calls
    env.json(
        "runtime", "sync", "--background", "--plugin-data", str(data), cwd=project, nhctl=nhctl
    )
    assert "args=--ensure-background" in (data / "calls.log").read_text()


def test_runtime_background_sync_returns_at_once(env, tmp_path, project):
    # nh-sync's detached child keeps the inherited stdout open for a long time.
    script = "#!/bin/sh\n(sleep 20; echo late) &\necho started\n"
    root = runtime_env(env, tmp_path, script)
    started = time.monotonic()
    result = env.json(
        "runtime", "sync", "--background", "--plugin-data", str(tmp_path / "d"), cwd=project,
        nhctl=root / "bin/nhctl",
    )  # fmt: skip
    assert time.monotonic() - started < 10
    assert result["ok"] is True and result["ready"] is False


def test_runtime_sync_without_nh_sync(env, tmp_path, project):
    root = runtime_env(env, tmp_path, None)
    result = env.json(
        "runtime", "sync", "--plugin-data", str(tmp_path / "d"), cwd=project,
        nhctl=root / "bin/nhctl", expect=1,
    )  # fmt: skip
    assert result["error"]["code"] == "D122"


def test_runtime_sync_failure(env, tmp_path, project):
    root = runtime_env(env, tmp_path, "#!/bin/sh\necho 'uv: not found' >&2\nexit 3\n")
    result = env.json(
        "runtime", "sync", "--plugin-data", str(tmp_path / "d"), cwd=project,
        nhctl=root / "bin/nhctl", expect=1,
    )  # fmt: skip
    assert result["error"]["code"] == "D123" and "uv: not found" in result["output_tail"]


def test_plugin_data_derived_from_cache_path(env, tmp_path, project):
    cache = tmp_path / "cfg/plugins/cache/notebook-harness/nh"
    root = plugin_copy(cache)
    root.rename(cache / "0.1.0")
    status = env.json("runtime", "status", cwd=project, nhctl=cache / "0.1.0/bin/nhctl")
    assert status["data_dir"] == str((tmp_path / "cfg/plugins/data/nh-notebook-harness").resolve())


def test_plugin_dir_load_needs_plugin_data(env, tmp_path, project):
    root = plugin_copy(tmp_path / "checkout")
    result = env.json("runtime", "status", cwd=project, nhctl=root / "bin/nhctl", expect=1)
    assert result["error"]["code"] == "D121" and "--plugin-data" in result["error"]["fix"]
