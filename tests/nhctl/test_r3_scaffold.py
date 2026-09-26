"""Round 3 (W23): a secret data URL never goes into a .env that git already tracks."""

from __future__ import annotations

import subprocess

from nh_gateway._shared.scaffold import core

SECRET_URL = "https://api.example.com/data.csv?token=SECRETQ9"


def git(project, *args: str) -> None:
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def test_tracked_env_file_gets_no_secret(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".env").write_text("FOO=1\n")
    git(tmp_path, "add", ".env")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "env")
    tools = core.Tools(uv="/fake/uv", conda=None, mamba=None)
    report = core.scaffold(tmp_path, data=SECRET_URL, tools=tools)
    assert (tmp_path / ".env").read_text() == "FOO=1\n"
    assert report.data["secret_in_env"] is False
    assert any("git rm --cached .env" in warning for warning in report.warnings)
    assert {"path": ".env", "action": "skipped"} in report.paths
    for name in ("harness.toml", "NOTEBOOK.md"):
        assert "SECRETQ9" not in (tmp_path / name).read_text(), name


def test_untracked_env_file_still_gets_the_secret(tmp_path):
    git(tmp_path, "init", "-q")
    tools = core.Tools(uv="/fake/uv", conda=None, mamba=None)
    report = core.scaffold(tmp_path, data=SECRET_URL, tools=tools)
    assert report.data["secret_in_env"] is True
    assert SECRET_URL in (tmp_path / ".env").read_text()
