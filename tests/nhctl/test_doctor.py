"""nhctl doctor on synthetic machines and projects (fake claude/uv/git on PATH)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

from nhctl_testlib import SERVER


def codes(report: dict) -> dict[str, bool]:
    return {p["code"]: p["blocking"] for p in report["problems"]}


def ready_runtime(tmp_path):
    data = tmp_path / "plugin-data"
    venv = "venv-" + hashlib.sha256((SERVER / "uv.lock").read_bytes()).hexdigest()[:12]
    (data / venv).mkdir(parents=True)
    (data / venv / ".nh-ready").write_text("")
    return data


def machine(env, claude="2.1.290"):
    if claude:
        env.script("claude", f'echo "{claude} (Claude Code)"')
    env.script("uv", 'echo "uv 0.10.6 (fake)"')


def test_fresh_folder_before_init(env, project, tmp_path):
    machine(env)
    (project / "raw").mkdir()
    (project / "raw/sales.csv").write_text("a\n1\n")
    (project / "old.ipynb").write_text("{}")
    data = ready_runtime(tmp_path)
    report = env.json("doctor", "--plugin-data", str(data), cwd=project)
    assert report["ok"] is True
    assert report["claude_code"] == {
        "path": str(env.bin / "claude"), "version": "2.1.290", "min": "2.1.282", "ok": True,
    }  # fmt: skip
    assert report["uv"]["version"] == "0.10.6"
    assert report["runtime"]["ready"] is True
    project_info = report["project"]
    assert project_info["nh_enabled"] is False and project_info["env_manager"] == "uv"
    assert project_info["data_candidates"] == [{"path": "raw/sales.csv", "bytes": 4}]
    assert project_info["notebooks"] == ["old.ipynb"]
    assert report["env"] is None and report["lab"] is None
    assert codes(report) == {}


def test_blocking_problems(env, project):
    machine(env, claude="2.1.261")
    (env.bin / "uv").unlink()
    report = env.json("doctor", "--plugin-data", str(project / "none"), cwd=project, expect=1)
    assert report["ok"] is False
    found = codes(report)
    assert found["D101"] is True  # Claude Code too old
    assert found["D110"] is True  # neither uv nor conda
    assert found["D120"] is False  # runtime not synced yet
    human = env.run("doctor", "--plugin-data", str(project / "none"), cwd=project)
    assert human.stdout.startswith("nh doctor: BLOCKED")
    assert "fix: Run: claude update" in human.stdout


def test_missing_claude_is_a_warning(env, project, tmp_path):
    machine(env, claude=None)
    report = env.json("doctor", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project)
    assert codes(report) == {"D102": False}


def test_nh_project_checks(env, project, tmp_path):
    machine(env)
    env.json("scaffold", cwd=project)
    state = project / ".nh/state"
    state.mkdir(parents=True, exist_ok=True)
    prefix = project / ".venv"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").write_text("")
    (state / "env.json").write_text(json.dumps({
        "manager": "uv", "prefix": str(prefix), "python": "3.12.1",
        "jupyterlab": "4.6.4", "jupyter_collaboration": "4.1.0",
    }))  # fmt: skip
    with open(project / "harness.toml", "a") as handle:
        handle.write("\n[preset]\nname = 'x'\n\n[turnz]\nmax_cells = 2\n")
    (project / ".pre-commit-config.yaml").write_text(
        "repos:\n- repo: https://github.com/kynan/nbstripout\n  rev: 0.8.1\n  hooks:\n"
        "    - id: nbstripout\n- repo: local\n  hooks:\n    - id: other\n      args: [--keep-id]\n"
    )
    report = env.json(
        "doctor", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project / "notebooks", expect=1
    )
    assert report["project"]["nh_enabled"] is True
    assert report["project"]["dir"] == str(project.resolve())
    found = codes(report)
    assert found["D141"] is True  # collaboration 4 is too old
    assert found["D148"] is False  # lab not running
    assert found["D160"] is False  # optional deny rules
    assert found["D170"] is False  # nbstripout without --keep-id
    assert found["D131"] is False
    problem = next(p for p in report["problems"] if p["code"] == "D131")
    assert "unknown key turnz" in problem["message"] and "preset" not in problem["message"]
    assert report["lab"] == {"running": False}
    assert report["settings"] == {"deny_rules": False}


def test_nbstripout_git_filter(env, project, tmp_path):
    machine(env)
    env.json("scaffold", cwd=project)
    git_env = dict(env.vars, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "-q"], cwd=project, env=git_env, check=True)
    (project / ".gitattributes").write_text("*.ipynb filter=nbstripout\n")
    subprocess.run(
        ["git", "config", "filter.nbstripout.clean", "python -m nbstripout"],
        cwd=project, env=git_env, check=True,
    )  # fmt: skip
    report = env.json("doctor", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project)
    assert "D170" in codes(report)
    subprocess.run(
        ["git", "config", "filter.nbstripout.clean", "python -m nbstripout --keep-id"],
        cwd=project, env=git_env, check=True,
    )  # fmt: skip
    report = env.json("doctor", "--plugin-data", str(ready_runtime(tmp_path / "2")), cwd=project)
    assert "D170" not in codes(report)


def test_settings_with_deny_rules(env, project, tmp_path):
    machine(env)
    env.json("scaffold", cwd=project)
    env.json("settings", "apply", "--yes", cwd=project)
    report = env.json("doctor", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project)
    assert report["settings"] == {"deny_rules": True}
    assert "D160" not in codes(report)


def test_home_folder_is_blocking(env, tmp_path):
    machine(env)
    report = env.json(
        "doctor", "--plugin-data", str(ready_runtime(tmp_path)), cwd=env.home, expect=1
    )
    assert codes(report).get("D106") is True
    assert report["project"]["data_candidates"] == []


def test_live_old_server_for_this_folder(env, project, tmp_path):
    machine(env)
    info = {"url": "http://localhost:8888/", "pid": os.getpid(), "root_dir": str(tmp_path),
            "version": "1.24.0", "token": "do-not-print"}  # fmt: skip
    (env.runtime / f"jpserver-{os.getpid()}.json").write_text(json.dumps(info))
    proc = env.run("doctor", "--json", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project)
    assert "do-not-print" not in proc.stdout
    report = json.loads(proc.stdout)
    assert report["jupyter"]["servers"] == [{
        "url": "http://localhost:8888/", "pid": os.getpid(), "root_dir": str(tmp_path),
        "version": "1.24.0", "serves_project": True, "usable": False,
    }]  # fmt: skip
    assert codes(report).get("D149") is False


def test_lab_started_outside_nh_counts_as_running(env, project, tmp_path):
    """No lab.json, but a live project server: the gateway uses it, so doctor says running."""
    machine(env)
    env.json("scaffold", cwd=project)
    info = {"url": "http://127.0.0.1:8899/?token=do-not-print", "pid": os.getpid(),
            "root_dir": str(project), "version": "2.17.0", "token": "do-not-print"}  # fmt: skip
    (env.runtime / f"jpserver-{os.getpid()}.json").write_text(json.dumps(info))
    proc = env.run("doctor", "--json", "--plugin-data", str(ready_runtime(tmp_path)), cwd=project)
    assert "do-not-print" not in proc.stdout
    report = json.loads(proc.stdout)
    assert report["lab"] == {
        "running": True, "url": "http://127.0.0.1:8899/", "pid": os.getpid(), "registered": False,
    }  # fmt: skip
    problem = next(p for p in report["problems"] if p["code"] == "D148")
    assert "started outside nh" in problem["message"] and "nhctl lab start" in problem["fix"]
