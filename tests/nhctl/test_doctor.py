"""nhctl doctor on synthetic machines and projects (fake claude/uv/git on PATH)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest
from nhctl_testlib import SERVER

from nh_gateway._shared import tomlread


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
    harness = (project / "harness.toml").read_text()
    harness = harness.replace('# level = "junior"', "name = 'x'")  # [preset] is a real section now
    (project / "harness.toml").write_text(
        harness + "\n[guardrails]\nname = 'x'\n\n[turnz]\nmax_cells = 2\n"
    )
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
    assert "unknown key turnz" in problem["message"] and "guardrails" not in problem["message"]
    assert "unknown key preset.name" in problem["message"]  # "preset" left RESERVED_SECTIONS
    assert report["project"]["preset"] == "junior" and "D171" not in found
    assert report["lab"] == {"running": False}
    assert report["settings"] == {"deny_rules": False}


# --- [preset] level (design §6.9) --------------------------------------------------------------


def set_level(project, line: str) -> None:
    """The scaffolded harness.toml with its commented ``# level = "junior"`` replaced by ``line``."""
    path = project / "harness.toml"
    text = path.read_text()
    assert text.count('\n# level = "junior"') == 1
    path.write_text(text.replace('\n# level = "junior"', "\n" + line))


def test_d171_a_level_that_isnt_junior_or_senior(env, project, tmp_path):
    machine(env)
    env.json("scaffold", cwd=project)
    data = str(ready_runtime(tmp_path))
    report = env.json("doctor", "--plugin-data", data, cwd=project)
    assert report["project"]["preset"] == "junior" and "D171" not in codes(report)
    original = (project / "harness.toml").read_text()
    for value in ('"expert-level-x"', '""', '"Senior"', "16", "true"):
        (project / "harness.toml").write_text(original)
        set_level(project, f"level = {value}")
        proc = env.run("doctor", "--json", "--plugin-data", data, cwd=project)
        report = json.loads(proc.stdout)
        assert codes(report).get("D171") is False, value  # a warning, not blocking
        problem = next(p for p in report["problems"] if p["code"] == "D171")
        assert problem["message"] == (
            "harness.toml's [preset] level isn't junior or senior, so nh uses junior."
        )
        assert problem["fix"] == (
            "Run: nhctl preset junior (or senior), or fix the level line in harness.toml."
        )
        assert "expert-level-x" not in proc.stdout  # never the user's text
        assert report["project"]["preset"] == "junior"
        assert "D131" not in codes(report)
    human = env.run("doctor", "--plugin-data", data, cwd=project)
    assert "warning D171: harness.toml's [preset] level isn't junior or senior" in human.stdout
    assert "; preset: junior)" in human.stdout


def test_doctor_reports_the_level_nh_reads(env, project, tmp_path):
    machine(env)
    env.json("scaffold", cwd=project)
    data = str(ready_runtime(tmp_path))
    original = (project / "harness.toml").read_text()
    for line, level in (('level = "senior"', "senior"), ("level = 'junior'", "junior")):
        (project / "harness.toml").write_text(original)
        set_level(project, line)
        report = env.json("doctor", "--plugin-data", data, cwd=project)
        assert report["project"]["preset"] == level and "D171" not in codes(report), line
    human = env.run("doctor", "--plugin-data", data, cwd=project)
    assert "; preset: junior)" in human.stdout
    (project / "harness.toml").write_text(original)
    set_level(project, 'level = "senior"')
    human = env.run("doctor", "--plugin-data", data, cwd=project)
    assert "; preset: senior)" in human.stdout


def test_d171_for_a_preset_that_isnt_a_table_and_none_for_a_file_that_doesnt_parse(
    env, project, tmp_path
):
    machine(env)
    env.json("scaffold", cwd=project)
    data = str(ready_runtime(tmp_path))
    (project / "harness.toml").write_text('version = 1\npreset = "senior"\n')
    report = env.json("doctor", "--plugin-data", data, cwd=project)
    assert report["project"]["preset"] == "junior"
    problem = next(p for p in report["problems"] if p["code"] == "D171")
    assert problem["message"] == "harness.toml's preset isn't a [preset] table, so nh uses junior."
    assert problem["fix"] == (
        "Delete the top-level preset line, then run: nhctl preset junior (or senior)."
    )
    (project / "harness.toml").write_text('version = 1\n[preset]\nlevel = "expert\n')
    report = env.json("doctor", "--plugin-data", data, cwd=project)
    assert "D131" in codes(report) and "D171" not in codes(report)  # D131: can't be parsed
    assert report["project"]["preset"] == "junior"


def test_d171s_fixes_lead_to_a_working_preset(env, project, tmp_path):
    """Each D171 fix, followed, ends with nh reading the asked level and no D171: for a
    non-table ``preset`` through D171's own fix, and for a top-level dotted bad level through
    nhctl preset's D172 fix (review of C9a: the fixes went round in a circle)."""
    machine(env)
    env.json("scaffold", cwd=project)
    data = str(ready_runtime(tmp_path))
    path = project / "harness.toml"
    # the scaffolded file without its [preset] block: a top-level preset line can't sit beside it
    scaffolded = "".join(
        line
        for line in path.read_text().splitlines(keepends=True)
        if line.rstrip("\n") != "[preset]" and not line.startswith("# level = ")
    )
    for line in ('preset = "senior"', 'preset.level = "expert"'):
        path.write_text(f"{line}\n{scaffolded}")
        report = env.json("doctor", "--plugin-data", data, cwd=project)
        assert "D171" in codes(report) and report["project"]["preset"] == "junior", line
        refused = env.json("preset", "senior", cwd=project, expect=1)
        assert refused["error"]["code"] == "D172", line
        assert refused["error"]["fix"].startswith("Delete the top-level preset line"), line
        path.write_text(path.read_text().replace(f"{line}\n", ""))  # the fix, followed
        assert env.json("preset", "senior", cwd=project)["changed"] is True, line
        report = env.json("doctor", "--plugin-data", data, cwd=project)
        assert report["project"]["preset"] == "senior" and "D171" not in codes(report), line
        assert "D131" not in codes(report), line


def test_reserved_sections_have_one_definition_and_doctor_uses_it():
    """design §6.9: one RESERVED_SECTIONS, in _shared/harness_toml.py; config.py and doctor.py
    both use it (config.py's is checked in tests/unit/test_config.py)."""
    import ast

    from nhctl_testlib import PLUGIN

    from nh_gateway._shared import harness_toml

    source = (PLUGIN / "scripts" / "nhctl" / "doctor.py").read_text()
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert "RESERVED_SECTIONS" not in assigned
    assert "harness_toml.RESERVED_SECTIONS" in source
    assert "preset" not in harness_toml.RESERVED_SECTIONS
    config_py = PLUGIN / "server" / "src" / "nh_gateway" / "config.py"
    assert "RESERVED_SECTIONS = harness_toml.RESERVED_SECTIONS\n" in config_py.read_text()


def test_what_doctor_prints_is_redacted_with_the_projects_env(env, project, tmp_path):
    """doctor installs the project's redactor (design §6.8): D131 echoes what it finds in
    harness.toml, which may hold a .env value (review of C3)."""
    machine(env)
    env.json("scaffold", cwd=project)
    secret = "Sup3rS3cret-" + "Passw0rd-2026"  # fake
    with open(project / ".env", "a") as handle:
        handle.write(f"\nDB_PASSWORD={secret}\n")
    with open(project / "harness.toml", "a") as handle:
        handle.write(f"\n[{secret}]\n")
    data = str(ready_runtime(tmp_path))
    proc = env.run("doctor", "--json", "--plugin-data", data, cwd=project)
    problem = next(p for p in json.loads(proc.stdout)["problems"] if p["code"] == "D131")
    assert "unknown key [redacted:DB_PASSWORD]" in problem["message"]
    human = env.run("doctor", "--plugin-data", data, cwd=project)
    assert "unknown key [redacted:DB_PASSWORD]" in human.stdout
    assert secret not in proc.stdout + proc.stderr + human.stdout + human.stderr


@pytest.mark.parametrize("line", ["password = {}", 'password = "{}'])
def test_the_fallback_toml_parser_names_a_bad_value_by_its_line(line):
    """Python 3.9's parser echoed an unquoted value into doctor's D131 (review of C3)."""
    secret = "Sup3rS3cret-" + "Passw0rd-2026"  # fake
    with pytest.raises(ValueError) as caught:
        tomlread._mini_loads(f"[jupyter]\nurl = 'x'\n{line.format(secret)}\n")
    assert str(caught.value) == "Invalid value for password (at line 3)"


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
