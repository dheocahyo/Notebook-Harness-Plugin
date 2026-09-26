"""nhctl settings apply: merge nh's deny rules into .claude/settings.json."""

from __future__ import annotations

import json

import pytest

RULES = ["Edit(/**/*.ipynb)", "Edit(/.nh/**)"]


@pytest.fixture
def nh_project(project):
    (project / ".nh").mkdir()
    return project


def test_without_yes_shows_diff_and_writes_nothing(env, nh_project):
    report = env.json("settings", "apply", cwd=nh_project, expect=2)
    assert report["needs_confirmation"] is True
    assert '+      "Edit(/**/*.ipynb)",' in report["diff"]
    assert not (nh_project / ".claude").exists()
    human = env.run("settings", "apply", cwd=nh_project)
    assert human.returncode == 2 and "Rerun with --yes" in human.stdout


def test_creates_settings_file(env, nh_project):
    report = env.json("settings", "apply", "--yes", cwd=nh_project)
    assert report["changed"] is True
    data = json.loads((nh_project / ".claude/settings.json").read_text())
    assert data == {"permissions": {"deny": RULES}}


def test_merge_keeps_every_existing_key(env, nh_project):
    settings = nh_project / ".claude/settings.json"
    settings.parent.mkdir()
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(./secrets/**)", RULES[1]]},
        "env": {"A": "1"},
        "hooks": {"Stop": []},
    }
    settings.write_text(json.dumps(original, indent=4))
    settings.chmod(0o640)
    env.json("settings", "apply", "--yes", cwd=nh_project)
    merged = json.loads(settings.read_text())
    assert merged["permissions"]["deny"] == ["Read(./secrets/**)", RULES[1], RULES[0]]
    assert merged["permissions"]["allow"] == ["Bash(ls:*)"]
    assert {k: v for k, v in merged.items() if k != "permissions"} == {
        k: v for k, v in original.items() if k != "permissions"
    }
    assert list(merged) == list(original)
    assert settings.stat().st_mode & 0o777 == 0o640
    again = env.json("settings", "apply", "--yes", cwd=nh_project)
    assert again["changed"] is False


def test_invalid_json_is_left_alone(env, nh_project):
    settings = nh_project / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text("{ not json")
    report = env.json("settings", "apply", "--yes", cwd=nh_project, expect=1)
    assert report["error"]["code"] == "D161"
    assert settings.read_text() == "{ not json"


def test_needs_an_nh_project(env, project):
    report = env.json("settings", "apply", "--yes", cwd=project, expect=1)
    assert report["error"]["code"] == "D105"
