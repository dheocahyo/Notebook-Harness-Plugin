"""Small JSON state shared across turns, sessions and the hooks (plan §4.6–4.7).

- ``stale.json``: cells whose results are outdated. It lives outside the notebook, so human
  cells never get nh metadata.
- ``kernel_drift.json``: names the kernel still holds from undone cells.
- ``last_cell.json``: the last cell nh wrote or ran; the UserPromptSubmit hook reads it.

Files are replaced atomically; read-modify-write updates also hold a cross-process lock,
because every Claude Code session in the project runs its own gateway.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ._shared.paths import Layout, atomic_write_json, read_json

if sys.platform != "win32":  # Windows is refused with E137; stay importable there
    import fcntl

log = logging.getLogger(__name__)

LAST_CELL_VERSION = 1
LAST_CELL_FIELDS = (
    "session_id",
    "notebook",
    "cell_id",
    "title",
    "exec",
    "status",
    "turn_id",
    "retries_left",
    "finished_at",
)
LOCK_TIMEOUT_S = 2.0


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[bool]:
    """Hold an exclusive lock on ``path`` across processes; yields False if it stayed busy.

    Holders keep it for milliseconds, so a timeout means a stuck process, not a queue.
    """
    if sys.platform == "win32":
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    held = False
                    break
                time.sleep(0.005)
        yield held
    finally:
        os.close(fd)  # closing the descriptor releases the lock


class _NotebookMap:
    """A JSON object keyed by notebook path."""

    def __init__(self, path: Path, lock: Path) -> None:
        self.path = path
        self._lock = lock

    def _read(self) -> dict[str, Any]:
        data = read_json(self.path, default={})
        return data if isinstance(data, dict) else {}

    def _entry(self, notebook: str) -> dict[str, Any] | None:
        entry = self._read().get(notebook)
        return entry if isinstance(entry, dict) else None

    @contextlib.contextmanager
    def _update(self) -> Iterator[dict[str, Any]]:
        """Yield the current data for in-place changes; write it back if it changed."""
        with file_lock(self._lock, LOCK_TIMEOUT_S) as held:
            if not held:  # a lost update of advisory state beats failing the tool call
                log.warning("%s stayed locked; updating it anyway", self.path.name)
            data = self._read()
            original = copy.deepcopy(data)
            yield data
            if data != original:
                atomic_write_json(self.path, data)


class StaleStore(_NotebookMap):
    """``stale.json``: {notebook: {uid: {"reason", "by", "turn_id", "exec_count"}}}."""

    def __init__(self, layout: Layout) -> None:
        super().__init__(layout.stale_json, layout.locks / "state.lock")

    def mark(
        self,
        notebook: str,
        uids: Iterable[str],
        *,
        reason: str,
        by: str | None,
        turn_id: str | None,
        exec_counts: dict[str, int | None],
    ) -> None:
        """Mark cells stale. ``exec_counts`` holds each cell's count now: a re-run changes it."""
        uids = list(uids)
        if not uids:
            return
        with self._update() as data:
            cells = data.get(notebook)
            if not isinstance(cells, dict):
                cells = data[notebook] = {}
            for uid in uids:
                cells[uid] = {
                    "reason": reason,
                    "by": by,
                    "turn_id": turn_id,
                    "exec_count": exec_counts.get(uid),
                }

    def clear(self, notebook: str, uids: Iterable[str]) -> None:
        uids = set(uids)
        if not uids:
            return
        with self._update() as data:
            if notebook not in data:
                return
            cells = data[notebook]
            if isinstance(cells, dict):
                for uid in uids:
                    cells.pop(uid, None)
            if not isinstance(cells, dict) or not cells:
                del data[notebook]

    def get(self, notebook: str) -> dict[str, dict]:
        cells = self._entry(notebook) or {}
        return {uid: dict(info) for uid, info in cells.items() if isinstance(info, dict)}


def same_kernel(a: Any, b: Any) -> bool:
    """Whether two ``kernel_id:incarnation`` identities may be the same kernel process.

    An empty part is unknown (nh couldn't look inside a busy kernel), not a different kernel.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return a == b
    kernel_a, _, incarnation_a = a.partition(":")
    kernel_b, _, incarnation_b = b.partition(":")
    if not kernel_a or not kernel_b:
        return True
    return kernel_a == kernel_b and (
        not incarnation_a or not incarnation_b or incarnation_a == incarnation_b
    )


class DriftStore(_NotebookMap):
    """``kernel_drift.json``: {notebook: {"kernel_id", "since", "names": {name: why},
    "exec_counts": {name: n}}}.

    ``names`` is oldest first. ``exec_counts`` holds the notebook's highest execution count when
    the name was recorded: a cell that binds the name and shows a higher count ran since.
    """

    def __init__(self, layout: Layout) -> None:
        super().__init__(layout.drift_json, layout.locks / "state.lock")

    def add(
        self,
        notebook: str,
        kernel_id: str | None,
        names: dict[str, str],
        *,
        exec_count: int | None = None,
    ) -> None:
        """Record names the kernel still holds from undone cells. A new kernel starts afresh."""
        if not names:
            return
        with self._update() as data:
            entry = data.get(notebook)
            if not (
                isinstance(entry, dict)
                and same_kernel(entry.get("kernel_id"), kernel_id)
                and isinstance(entry.get("names"), dict)
            ):
                entry = data[notebook] = {"kernel_id": kernel_id, "since": time.time(), "names": {}}
            elif kernel_id and kernel_id.partition(":")[2]:
                entry["kernel_id"] = kernel_id  # now with the process, if nh couldn't see it before
            counts = entry.get("exec_counts")
            counts = counts if isinstance(counts, dict) else {}
            for name, why in names.items():
                entry["names"].pop(name, None)  # recorded again: it moves to the newest end
                entry["names"][name] = why
                if exec_count is None:
                    counts.pop(name, None)
                else:
                    counts[name] = exec_count
            if counts:
                entry["exec_counts"] = counts
            else:
                entry.pop("exec_counts", None)

    def get(self, notebook: str) -> dict | None:
        entry = self._entry(notebook)
        if entry is None or not isinstance(entry.get("names"), dict) or not entry["names"]:
            return None
        return entry

    def clear(self, notebook: str, names: set[str] | None = None) -> None:
        """Forget ``names`` (all of them when None), e.g. after a restart or a re-run."""
        with self._update() as data:
            if notebook not in data:
                return
            entry = data[notebook]
            known = entry.get("names") if isinstance(entry, dict) else None
            if names is not None and isinstance(known, dict):
                counts = entry.get("exec_counts")
                for name in names:
                    known.pop(name, None)
                    if isinstance(counts, dict):
                        counts.pop(name, None)
                if known:
                    return
            del data[notebook]


def write_last_cell(layout: Layout, **fields: Any) -> None:
    """Replace ``last_cell.json``. Fields not given are null; unknown fields are a bug."""
    unknown = set(fields) - set(LAST_CELL_FIELDS)
    if unknown:
        raise TypeError(f"unknown last_cell fields: {', '.join(sorted(unknown))}")
    record: dict[str, Any] = {"v": LAST_CELL_VERSION, **dict.fromkeys(LAST_CELL_FIELDS)}
    record.update(fields)
    atomic_write_json(layout.last_cell, record)


def read_last_cell(layout: Layout) -> dict | None:
    data = read_json(layout.last_cell)
    return data if isinstance(data, dict) else None
