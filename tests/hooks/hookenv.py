"""Helpers for the hook and launcher tests.

Hooks run through the exact command lines in ``plugins/nh/hooks/hooks.json`` (``/bin/sh
nh-hook ...``), with fixture JSON on stdin and the system Python 3.9 forced via
``NH_PYTHON``. A fake ``uv`` on PATH stands in for real syncs: it records each call and
builds a minimal venv whose ``bin/python`` points at the system Python.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "nh"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
NH_HOOK = PLUGIN / "hooks" / "nh-hook"
LIBEXEC = PLUGIN / "libexec"
SYSTEM_PYTHON = "/usr/bin/python3"
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

FAKE_UV = """#!/bin/sh
# Test double for uv: record the call, optionally sleep or fail, then build a tiny venv.
printf '%s|%s|%s|%s\\n' "$*" "${UV_PROJECT_ENVIRONMENT:-}" "${UV_PYTHON-unset}" \\
    "${VIRTUAL_ENV-unset}" >>"${FAKE_UV_LOG:-/dev/null}"
if [ "${1:-}" = python ]; then
    printf '%s\\n' "$FAKE_UV_PYTHON"
    exit 0
fi
[ -n "${FAKE_UV_PIDFILE:-}" ] && echo "$$" >"$FAKE_UV_PIDFILE"
[ -n "${FAKE_UV_SLEEP:-}" ] && sleep "$FAKE_UV_SLEEP"
[ -n "${FAKE_UV_FAIL:-}" ] && exit 1
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin" &&
    ln -sf "$FAKE_UV_TARGET" "$UV_PROJECT_ENVIRONMENT/bin/python"
"""


def system_python() -> tuple[tuple[int, int] | None, str]:
    """Version and real executable of /usr/bin/python3.

    On macOS that path is a stub that dispatches on argv[0], so symlinks in tests must
    point at the real executable it reports.
    """
    code = "import sys; print(*sys.version_info[:2], sys.executable)"
    try:
        out = subprocess.run(
            [SYSTEM_PYTHON, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=30
        ).stdout.split(" ", 2)
        return (int(out[0]), int(out[1])), out[2].strip()
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None, SYSTEM_PYTHON


SYSTEM_VERSION, SYSTEM_PYTHON_REAL = system_python()
needs_system_python = pytest.mark.skipif(
    SYSTEM_VERSION is None or SYSTEM_VERSION < (3, 9),
    reason="needs /usr/bin/python3 >= 3.9 (the hooks' floor)",
)


def lock_hash(root: Path = PLUGIN) -> str:
    return hashlib.sha256((root / "server" / "uv.lock").read_bytes()).hexdigest()[:12]


def hook_entries() -> dict[str, list[dict[str, Any]]]:
    return json.loads(HOOKS_JSON.read_text())["hooks"]


def command_line(handler: dict[str, Any], root: Path = PLUGIN) -> list[str]:
    """The argv Claude Code spawns for an exec-form handler."""
    placeholder = "${CLAUDE_PLUGIN_ROOT}"
    return [handler["command"]] + [arg.replace(placeholder, str(root)) for arg in handler["args"]]


@dataclass
class HookRun:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def output(self) -> dict[str, Any] | None:
        return json.loads(self.stdout) if self.stdout.strip() else None

    @property
    def decision(self) -> str | None:
        out = self.output or {}
        return out.get("hookSpecificOutput", {}).get("permissionDecision")

    @property
    def reason(self) -> str:
        out = self.output or {}
        return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")

    @property
    def context(self) -> str:
        out = self.output or {}
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")


@dataclass
class Sandbox:
    """A temp project with .nh/, a plugin data dir, a HOME and a fake uv."""

    tmp: Path
    project: Path = field(init=False)
    data: Path = field(init=False)
    home: Path = field(init=False)
    bin: Path = field(init=False)
    uv_log: Path = field(init=False)
    env: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.home = self.tmp / "home"
        self.project = self.tmp / "proj"
        self.data = self.tmp / "data"
        self.bin = self.tmp / "bin"
        self.uv_log = self.tmp / "uv.log"
        for path in (self.home, self.project / ".nh", self.project / "notebooks", self.bin):
            path.mkdir(parents=True)
        fake_uv = self.bin / "uv"
        fake_uv.write_text(FAKE_UV)
        fake_uv.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:{BASE_PATH}",
            "HOME": str(self.home),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
            "CLAUDE_PROJECT_DIR": str(self.project),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "NH_PYTHON": SYSTEM_PYTHON,
            "FAKE_UV_LOG": str(self.uv_log),
            "FAKE_UV_TARGET": SYSTEM_PYTHON_REAL,
            "FAKE_UV_PYTHON": SYSTEM_PYTHON_REAL,
        }

    @property
    def nh(self) -> Path:
        return self.project / ".nh"

    def payload(self, event: str, **fields: Any) -> dict[str, Any]:
        base = {
            "session_id": "sess-1",
            "prompt_id": "prompt-1",
            "transcript_path": str(self.tmp / "transcript.jsonl"),
            "cwd": str(self.project),
            "permission_mode": "default",
            "hook_event_name": event,
        }
        base.update(fields)
        return {key: value for key, value in base.items() if value is not None}

    def tool_payload(self, tool_name: str, tool_input: dict[str, Any], **fields: Any) -> dict:
        fields.setdefault("tool_use_id", "toolu_01")
        return self.payload("PreToolUse", tool_name=tool_name, tool_input=tool_input, **fields)

    def handlers(self, event: str, tool_name: str | None = None) -> list[dict[str, Any]]:
        """Handlers Claude Code would run: matchers are unanchored regexes over the tool name."""
        chosen = []
        for group in hook_entries().get(event, []):
            matcher = group.get("matcher")
            if matcher and (tool_name is None or not re.search(matcher, tool_name)):
                continue
            chosen.extend(group["hooks"])
        return chosen

    def run_argv(
        self, argv: list[str], payload: Any, env: dict[str, str] | None = None, timeout: float = 30
    ) -> HookRun:
        data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        started = time.perf_counter()
        proc = subprocess.run(
            argv,
            input=data.encode() if isinstance(data, str) else data,
            capture_output=True,
            cwd=str(self.project) if self.project.is_dir() else str(self.tmp),
            env=env or self.env,
            timeout=timeout,
        )
        return HookRun(
            argv,
            proc.returncode,
            proc.stdout.decode(),
            proc.stderr.decode(),
            time.perf_counter() - started,
        )

    def run(
        self,
        event: str,
        payload: Any,
        tool_name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> HookRun:
        """Run the single hooks.json handler for this event (and tool)."""
        handlers = self.handlers(event, tool_name)
        assert len(handlers) == 1, f"expected one handler for {event} {tool_name}: {handlers}"
        handler = handlers[0]
        return self.run_argv(command_line(handler), payload, env, timeout=handler["timeout"] + 25)

    def tool(self, tool_name: str, tool_input: dict[str, Any], **fields: Any) -> HookRun:
        payload = self.tool_payload(tool_name, tool_input, **fields)
        return self.run("PreToolUse", payload, tool_name=tool_name)

    def uv_calls(self) -> list[list[str]]:
        if not self.uv_log.exists():
            return []
        return [line.split("|") for line in self.uv_log.read_text().splitlines()]

    def ready_runtime(self) -> Path:
        """Pretend nh-sync already built this plugin version's venv."""
        venv = self.data / f"venv-{lock_hash()}"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(SYSTEM_PYTHON_REAL)
        (venv / ".nh-ready").write_text("test\n")
        return venv

    def wait_for(self, path: Path, timeout: float = 15) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return path.exists()
