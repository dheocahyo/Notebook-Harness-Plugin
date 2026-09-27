"""libexec/nh-python: which interpreter the stdlib helpers run on."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hookenv import LIBEXEC, SYSTEM_PYTHON, SYSTEM_PYTHON_REAL, Sandbox, needs_system_python

pytestmark = needs_system_python


def real(path: str) -> str:
    return subprocess.run(
        [path, "-I", "-S", "-c", "import sys; print(sys.executable)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def nh_python(sandbox: Sandbox, **env: str) -> subprocess.CompletedProcess:
    full = {k: v for k, v in sandbox.env.items() if k != "NH_PYTHON"}
    full.update(env)
    return subprocess.run(
        ["/bin/sh", str(LIBEXEC / "nh-python")], capture_output=True, text=True, env=full
    )


def test_nh_python_override_wins(sandbox: Sandbox) -> None:
    sandbox.ready_runtime()
    proc = nh_python(sandbox, NH_PYTHON=SYSTEM_PYTHON)
    assert proc.returncode == 0
    assert proc.stdout.strip() == real(SYSTEM_PYTHON)
    assert not (sandbox.data / "python-path").exists()


def test_bad_override_fails(sandbox: Sandbox) -> None:
    proc = nh_python(sandbox, NH_PYTHON="/bin/echo")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "not a Python >= 3.9" in proc.stderr


def test_ready_runtime_venv_comes_first(sandbox: Sandbox) -> None:
    venv = sandbox.ready_runtime()
    (sandbox.data / "python-path").write_text("/somewhere/else/python3\n")
    assert nh_python(sandbox).stdout.strip() == str(venv / "bin" / "python")


def test_venv_without_ready_marker_is_skipped(sandbox: Sandbox) -> None:
    venv = sandbox.ready_runtime()
    (venv / ".nh-ready").unlink()
    assert nh_python(sandbox).stdout.strip() != str(venv / "bin" / "python")


def test_cached_path_is_used_while_it_exists(sandbox: Sandbox) -> None:
    sandbox.data.mkdir()
    fake = sandbox.tmp / "cached-python"
    fake.symlink_to(SYSTEM_PYTHON_REAL)
    (sandbox.data / "python-path").write_text(f"{fake}\n")
    assert nh_python(sandbox).stdout.strip() == str(fake)
    fake.unlink()
    assert nh_python(sandbox).stdout.strip() != str(fake)


def test_search_caches_the_real_executable(sandbox: Sandbox) -> None:
    proc = nh_python(sandbox)
    assert proc.returncode == 0
    found = proc.stdout.strip()
    assert found and os.access(found, os.X_OK)
    assert (sandbox.data / "python-path").read_text() == f"{found}\n"
    version = subprocess.run(
        [found, "-I", "-S", "-c", "import sys; print(sys.version_info >= (3, 9))"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert version == "True"


def test_uv_python_find_is_the_last_resort(sandbox: Sandbox) -> None:
    """No python3 on PATH, no Homebrew, and xcode-select failing on macOS: ask uv.

    Runs a copy of nh-python whose Homebrew paths point nowhere, since this machine may
    have a Homebrew python3 that would be found first. uname says Darwin so that on Linux,
    where /usr/bin/python3 is a real Python, the xcode-select check still skips it.
    """
    script = (LIBEXEC / "nh-python").read_text()
    script = script.replace("/opt/homebrew/bin/python3", "/nonexistent/brew/python3")
    script = script.replace("/usr/local/bin/python3", "/nonexistent/local/python3")
    copy = sandbox.tmp / "nh-python"
    copy.write_text(script)
    tools = sandbox.tmp / "tools"
    tools.mkdir()
    for name in ("mkdir", "mv", "rm", "cat"):
        source = Path("/bin", name) if Path("/bin", name).exists() else Path("/usr/bin", name)
        (tools / name).symlink_to(source)
    for name, body in (("uname", "echo Darwin"), ("xcode-select", "exit 2")):
        fake = tools / name
        fake.write_text(f"#!/bin/sh\n{body}\n")
        fake.chmod(0o755)
    target = sandbox.tmp / "uv-python"
    target.symlink_to(SYSTEM_PYTHON_REAL)
    env = {k: v for k, v in sandbox.env.items() if k != "NH_PYTHON"}
    env.update(PATH=f"{sandbox.bin}:{tools}", FAKE_UV_PYTHON=str(target))
    proc = subprocess.run(["/bin/sh", str(copy)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == real(str(target))
    assert any(call[0].startswith("python find --no-project") for call in sandbox.uv_calls())


def test_no_python_at_all_exits_1(sandbox: Sandbox) -> None:
    script = (LIBEXEC / "nh-python").read_text()
    for path in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        script = script.replace(path, "/nonexistent/python3")
    copy = sandbox.tmp / "nh-python"
    copy.write_text(script)
    (sandbox.bin / "uv").unlink()
    env = {k: v for k, v in sandbox.env.items() if k != "NH_PYTHON"}
    env["PATH"] = str(sandbox.bin)
    proc = subprocess.run(["/bin/sh", str(copy)], capture_output=True, text=True, env=env)
    assert (proc.returncode, proc.stdout) == (1, "")
