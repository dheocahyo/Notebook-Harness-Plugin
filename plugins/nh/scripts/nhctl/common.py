"""Shared helpers for nhctl: plugin paths, project lookup, subprocesses, output.

Stdlib + nh_gateway._shared only; runs on the system Python (>= 3.9).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from nh_gateway._shared import paths, secrets, tomlread
from nh_gateway._shared.scaffold import core

HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parent.parent  # plugins/nh
MAIN = HERE / "main.py"

MIN_CLAUDE = (2, 1, 282)
MIN_UV = (0, 10)
MIN_JUPYTERLAB = (4, 6)
MIN_COLLABORATION = (5,)


class NhctlError(Exception):
    """A failure reported as {code, message, fix}; exits 1 unless told otherwise."""

    def __init__(
        self, code: str, message: str, fix: str = "", exit_code: int = 1, data: dict | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.exit_code = exit_code
        self.data = data or {}


class Result:
    """What a command returns: a JSON-able dict, the human text, and the exit code."""

    def __init__(self, data: dict, text: str, code: int = 0) -> None:
        self.data = data
        self.text = text
        self.code = code


def scrub(text: str) -> str:
    """The installed redactor (design §6.8): the project's once ``project_root`` found it."""
    return secrets.current().redact(text)


# ------------------------------------------------------------------------ locations


def plugin_data_dir(explicit: str | None) -> Path:
    """DATA: --plugin-data, else $CLAUDE_PLUGIN_DATA, else derived from the plugin cache path."""
    value = explicit or os.environ.get("CLAUDE_PLUGIN_DATA")
    if value:
        return Path(os.path.expanduser(value))
    derived = derive_plugin_data(ROOT)
    if derived is None:
        raise NhctlError(
            "D121",
            "Can't tell where nh's plugin data lives (the plugin isn't loaded from the plugin cache).",
            'Pass --plugin-data "${CLAUDE_PLUGIN_DATA}".',
        )
    return derived


def derive_plugin_data(root: Path) -> Path | None:
    """``<cfg>/plugins/cache/<mkt>/<plugin>/<ver>`` -> ``<cfg>/plugins/data/<plugin>-<mkt>``.

    ``<cfg>`` is whatever config dir the cache lives in (``~/.claude`` or
    ``$CLAUDE_CONFIG_DIR``), so a custom config dir is followed without reading the
    variable. Returns None for --plugin-dir and in-place marketplace loads.
    """
    parts = root.parts
    if len(parts) < 6 or parts[-5] != "plugins" or parts[-4] != "cache":
        return None
    marketplace, plugin = parts[-3], parts[-2]
    return Path(*parts[:-4]) / "data" / f"{plugin}-{marketplace}"


def project_root(explicit: str | None, required: bool = True) -> Path | None:
    """The nh project containing --project (or the cwd)."""
    start = os.path.abspath(explicit or os.getcwd())
    found = paths.find_project(start)
    if found is not None:  # what nhctl prints is redacted with the project's values (§6.8)
        secrets.install(secrets.Redactor.for_project(found))
    if found is None and required:
        raise NhctlError(
            "D105",
            f"{start} is not inside an nh project (no .nh/ folder).",
            "Run /nh:init in the project folder first.",
        )
    return found


def rel(project: Path, path: Path | str) -> str:
    return core.display_path(project, Path(path))


def harness_section(project: Path, name: str) -> dict:
    section = tomlread.load(project / paths.HARNESS_TOML).get(name)
    return section if isinstance(section, dict) else {}


def harness_project(project: Path) -> dict:
    return harness_section(project, "project")


def configured_kernel(project: Path) -> str:
    """harness.toml ``[jupyter].kernel_name``; "" = use the notebook's kernelspec."""
    name = harness_section(project, "jupyter").get("kernel_name")
    return name.strip() if isinstance(name, str) else ""


def default_notebook(project: Path) -> str:
    value = harness_project(project).get("notebook")
    return value if isinstance(value, str) and value else core.DEFAULT_NOTEBOOK


def resolve_notebook(project: Path, value: str | None) -> Path:
    """A notebook argument: relative to the cwd if it exists there, else to the project."""
    raw = value or default_notebook(project)
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        here = Path.cwd() / candidate
        candidate = here if here.exists() else project / candidate
    return candidate.resolve()


def read_env_json(layout: paths.Layout) -> dict:
    data = paths.read_json(layout.env_json, {})
    return data if isinstance(data, dict) else {}


def env_prefix(layout: paths.Layout) -> Path:
    env = read_env_json(layout)
    prefix = env.get("prefix")
    if not prefix or not (Path(prefix) / "bin" / "python").exists():
        raise NhctlError(
            "D140",
            "The project environment isn't built yet.",
            "Run nhctl env sync (or /nh:init).",
        )
    return Path(prefix)


def runtime_venv(data: Path) -> Path | None:
    """The versioned gateway venv libexec/nh-sync builds: venv-<sha256(uv.lock)[:12]>."""
    lock = ROOT / "server" / "uv.lock"
    try:
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()[:12]
    except OSError:
        return None
    return data / f"venv-{digest}"


# ---------------------------------------------------------------------- processes


def run(
    cmd: list[str], timeout: float = 30, cwd: Path | None = None, env: dict | None = None
) -> tuple[int, str]:
    """Run ``cmd``; return (exit code, combined output). Missing binaries give 127."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except PermissionError:
        return 126, f"{cmd[0]}: not executable"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout:.0f}s"
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def version_tuple(text: str | None) -> tuple[int, ...]:
    """Leading numeric parts: '4.6.4' -> (4, 6, 4); '5.0.0rc1' -> (5, 0, 0)."""
    match = re.search(r"\d+(?:\.\d+)*", text or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def tail(path: Path, lines: int = 20) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return scrub("\n".join(text.splitlines()[-lines:]))


def env_with(prefix: Path | None = None, drop: tuple[str, ...] = (), **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if prefix is not None:
        env["PATH"] = f"{prefix / 'bin'}{os.pathsep}{env.get('PATH', os.defpath)}"
    env.update(extra)
    return env


def emit_event(layout: paths.Layout, event: str, **fields: Any) -> None:
    """Append one event to .nh/log.jsonl with the gateway's envelope."""
    envelope = {"v": 1, "ts": round(time.time(), 3), "event": event, "session_id": None}
    record = dict(envelope, turn_id=None, **fields)
    with contextlib.suppress(OSError):
        paths.append_jsonl(layout.log_file, record)


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


# -------------------------------------------------------------------------- output


def scrub_data(value: Any) -> Any:
    """``value`` with every string in it redacted, keys too, before JSON escapes a value's
    quotes and backslashes out of the redactor's sight. Other objects become their redacted
    ``str``, as ``json.dumps(default=str)`` would print them."""
    if isinstance(value, str):
        return scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            (scrub(key) if isinstance(key, str) else key): scrub_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_data(item) for item in value]
    return scrub(str(value))


def print_result(result: Result, as_json: bool) -> None:
    """Everything nhctl prints passes the redactor (design §6.8): --json's strings before they
    are escaped, then the whole line."""
    if as_json:
        line = json.dumps(scrub_data(result.data), ensure_ascii=False, default=str)
        sys.stdout.write(scrub(line) + "\n")
    elif result.text:
        sys.stdout.write(scrub(result.text.rstrip("\n")) + "\n")


def error_result(exc: NhctlError) -> Result:
    data = {"ok": False, "error": {"code": exc.code, "message": exc.message, "fix": exc.fix}}
    data.update(exc.data)
    text = f"error: {exc.message}" + (f"\nfix: {exc.fix}" if exc.fix else "")
    return Result(data, text, exc.exit_code)
