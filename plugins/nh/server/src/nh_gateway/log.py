"""The transparency log, ``.nh/log.jsonl`` (plan §4.6): one JSON line per event.

It never holds code, outputs or tokens. Several gateway processes may append at once: each event
is a single write() on an O_APPEND descriptor, and rotation happens under a cross-process lock.
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from ._shared.paths import Layout, append_jsonl
from .policy.errors import scrub
from .state import file_lock

log = logging.getLogger(__name__)

LOG_VERSION = 1
ROTATED_NAME = "log.1.jsonl"
ROTATE_LOCK_TIMEOUT_S = 0.5
PRIVATE_FIELDS = frozenset({"code", "source", "outputs"})
_ENVELOPE = frozenset({"v", "ts", "event"})


class EventLog:
    def __init__(self, layout: Layout, max_bytes: int = 5 * 2**20) -> None:
        self.layout = layout
        self.max_bytes = max_bytes
        self.path = layout.log_file
        self.rotated = layout.log_file.with_name(ROTATED_NAME)

    def emit(self, event: str, **fields: Any) -> None:
        """Append ``{"v", "ts", "event", "session_id", "turn_id", **fields}``.

        Tokens are scrubbed from every string, fields named code, source or outputs are dropped,
        and a failed write is only logged: the event log never fails a tool call.
        """
        if not self.layout.nh.is_dir():  # creating .nh/ would turn the folder into an nh project
            return
        try:
            record: dict[str, Any] = {
                "v": LOG_VERSION,
                "ts": round(time.time(), 3),
                "event": _clean(event),
                "session_id": None,
                "turn_id": None,
            }
            for key, value in fields.items():
                if key not in _ENVELOPE and key not in PRIVATE_FIELDS:
                    record[key] = _clean(value)
            self._rotate_if_full()
            append_jsonl(self.path, record)
        except Exception as exc:
            log.warning("nh event log: %s", scrub(f"{event} not written: {exc}"))

    def _rotate_if_full(self) -> None:
        if _size(self.path) < self.max_bytes:
            return
        with file_lock(self.layout.locks / "log.lock", ROTATE_LOCK_TIMEOUT_S) as held:
            # Re-check under the lock: another process may have rotated while we waited.
            if held and _size(self.path) >= self.max_bytes:
                os.replace(self.path, self.rotated)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _clean(value: Any) -> Any:
    """A JSON-safe copy of ``value`` with tokens scrubbed from every string."""
    if isinstance(value, str):
        return scrub(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {scrub(str(key)): _clean(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_clean(item) for item in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return scrub(str(value))
