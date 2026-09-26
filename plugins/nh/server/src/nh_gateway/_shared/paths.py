"""Project discovery and atomic file I/O shared by the gateway, hooks and nhctl.

Stdlib only and Python 3.9 compatible: hooks and nhctl may run on the system Python.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any

NH_DIR = ".nh"
HARNESS_TOML = "harness.toml"


def find_project(start: str | None = None) -> Path | None:
    """Walk up from ``start`` to the first directory holding ``.nh/``.

    Stops at ``$HOME`` and at the filesystem root, so a stray ``~/.nh`` never
    turns every folder into an nh project.
    """
    raw = (
        start
        or os.environ.get("NH_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    try:
        here = Path(raw).resolve()
    except OSError:
        return None
    home = Path.home().resolve()
    for candidate in (here, *here.parents):
        if candidate == home or candidate == Path(candidate.anchor):
            return None
        if (candidate / NH_DIR).is_dir():
            return candidate
    return None


class Layout:
    """Paths inside one nh project. Nothing here touches the disk."""

    def __init__(self, project: Path) -> None:
        self.project = project
        self.nh = project / NH_DIR
        self.state = self.nh / "state"
        self.stamps = self.state / "stamps"
        self.claimed = self.stamps / "claimed"
        self.ambiguous = self.stamps / "ambiguous"
        self.turns = self.state / "turns"
        self.workflows = self.state / "workflows"
        self.ledger = self.state / "ledger"
        self.locks = self.state / "locks"
        self.running = self.state / "running"
        self.history = self.nh / "history"
        self.outputs = self.nh / "outputs"
        self.logs = self.nh / "logs"
        self.tmp = self.nh / "tmp"
        self.log_file = self.nh / "log.jsonl"
        self.harness_toml = project / HARNESS_TOML
        self.last_cell = self.state / "last_cell.json"
        self.env_json = self.state / "env.json"
        self.lab_json = self.state / "lab.json"
        self.stale_json = self.state / "stale.json"
        self.drift_json = self.state / "kernel_drift.json"

    def turn_file(self, session_id: str) -> Path:
        return self.turns / f"{safe_name(session_id)}.json"

    def ledger_file(self, session_id: str) -> Path:
        return self.ledger / f"{safe_name(session_id)}.json"

    def workflow_file(self, session_id: str) -> Path:
        return self.workflows / f"{safe_name(session_id)}.json"


def safe_name(value: str) -> str:
    """Keep ids usable as file names (session ids are UUIDs in practice)."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    return cleaned[:128] or "_"


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON through a temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line with a single write() on an O_APPEND descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8", "replace"
    )
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(errno.EIO, f"short write to {path}")
    finally:
        os.close(fd)
