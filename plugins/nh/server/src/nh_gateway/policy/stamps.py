"""Claim the PreToolUse stamp that proves which user prompt a tool call belongs to (plan §4.3)."""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._shared import turn_record
from .._shared.paths import Layout, read_json
from .._shared.stamp_spec import stamp_key

# How close together identical stamps from different agents are ambiguous, and how long a
# set-aside stamp answers its own call.
AMBIGUOUS_WINDOW_S = 60.0
RUNS_KEEP_S = 86400.0  # a session's nh:qa-cell runs file, a day after its last launch or report


@dataclass(frozen=True)
class Stamp:
    key: str
    tool: str
    session_id: str
    prompt_id: str | None
    agent_id: str | None
    agent_type: str | None
    tool_use_id: str
    permission_mode: str | None
    cc_pid: int | None
    ts: float
    path: Path


def _load(path: Path) -> Stamp | None:
    data = read_json(path)
    if not isinstance(data, dict) or "session_id" not in data:
        return None
    try:
        return Stamp(
            key=str(data.get("key", "")),
            tool=str(data.get("tool", "")),
            session_id=str(data["session_id"]),
            prompt_id=data.get("prompt_id") or None,
            agent_id=data.get("agent_id") or None,
            agent_type=data.get("agent_type") or None,
            tool_use_id=str(data.get("tool_use_id", "")),
            permission_mode=data.get("permission_mode") or None,
            cc_pid=int(data["cc_pid"]) if data.get("cc_pid") not in (None, "") else None,
            ts=float(data.get("ts", 0.0)),
            path=path,
        )
    except (TypeError, ValueError):
        return None


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


class Ambiguous:
    """``claim``'s answer when identical calls from different agents (the main conversation
    and a workflow's cell writer) wait together, so nh can't tell whose call this is."""


AMBIGUOUS = Ambiguous()


def latest_turn(layout: Layout, session_id: str) -> dict[str, Any] | None:
    return turn_record.read(layout, session_id)


def claim(
    layout: Layout,
    tool: str,
    raw_args: dict[str, Any] | None,
    *,
    my_cc_pid: int | None,
    ttl: float,
    now: float | None = None,
) -> Stamp | Ambiguous | None:
    """Atomically take the stamp matching this call, or return None.

    Preference: stamps from our own Claude Code process, then stamps whose prompt belongs to
    the session's current turn (a background task's alias counts as its turn), newest first.
    Same-key stamps from an older turn of the same session are purged; parallel identical
    calls in one prompt keep their own stamps.

    Same-key stamps from different agents, of one turn and at most AMBIGUOUS_WINDOW_S apart,
    are ambiguous: all are set aside and each of their calls, this one first, gets
    ``AMBIGUOUS``, so a retry starts clean. A leftover from an earlier message (a call the user
    rejected at its permission prompt) never is.
    """
    now = time.time() if now is None else now
    key = stamp_key(tool, raw_args)
    candidates: list[Stamp] = []
    for path in layout.stamps.glob(f"{key}--*.json"):
        stamp = _load(path)
        if stamp is None:
            continue
        if now - stamp.ts > ttl:
            _unlink(path)
            continue
        candidates.append(stamp)
    if not candidates:
        return _take_ambiguous(layout, key, now)

    records: dict[str, dict[str, Any] | None] = {}
    for stamp in candidates:
        if stamp.session_id not in records:
            records[stamp.session_id] = latest_turn(layout, stamp.session_id)

    def turn_of(stamp: Stamp) -> str | None:
        return turn_record.canonical(records[stamp.session_id], stamp.prompt_id)

    def current(stamp: Stamp) -> bool:
        record = records[stamp.session_id]
        return (
            record is not None
            and stamp.prompt_id is not None
            and turn_of(stamp) == record["turn_id"]
        )

    candidates.sort(
        key=lambda s: (s.cc_pid is not None and s.cc_pid == my_cc_pid, current(s), s.ts),
        reverse=True,
    )
    first = candidates[0]
    group = [
        s
        for s in candidates
        if s.session_id == first.session_id
        and turn_of(s) == turn_of(first)
        and abs(s.ts - first.ts) <= AMBIGUOUS_WINDOW_S
    ]
    if len({s.agent_id for s in group}) > 1:
        return _set_aside(layout, key, group, now)
    layout.claimed.mkdir(parents=True, exist_ok=True)
    for stamp in candidates:
        target = layout.claimed / stamp.path.name
        try:
            os.rename(stamp.path, target)
        except FileNotFoundError:
            continue
        _unlink(target)
        for other in candidates:
            if (
                other is not stamp
                and other.session_id == stamp.session_id
                and turn_of(other) != turn_of(stamp)
                and other.ts < stamp.ts
            ):
                _unlink(other.path)
        return stamp
    return None


def _set_aside(layout: Layout, key: str, group: list[Stamp], now: float) -> Ambiguous | None:
    layout.ambiguous.mkdir(parents=True, exist_ok=True)
    for stamp in group:
        target = layout.ambiguous / stamp.path.name
        try:
            os.rename(stamp.path, target)
        except FileNotFoundError:
            continue
        with contextlib.suppress(OSError):
            os.utime(target, (now, now))
    return _take_ambiguous(layout, key, now)


def _take_ambiguous(layout: Layout, key: str, now: float) -> Ambiguous | None:
    """One set-aside stamp for this key: the answer for a call whose stamp was set aside."""
    if not layout.ambiguous.is_dir():
        return None
    for path in layout.ambiguous.glob(f"{key}--*.json"):
        try:
            fresh = now - path.stat().st_mtime <= AMBIGUOUS_WINDOW_S
            path.unlink()
        except OSError:
            continue
        if fresh:
            return AMBIGUOUS
    return None


def gc(layout: Layout, ttl: float, now: float | None = None) -> int:
    """Delete expired stamps. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    for folder in (layout.stamps, layout.claimed, layout.ambiguous):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > ttl:
                _unlink(path)
                removed += 1
    return removed


def gc_runs(layout: Layout, keep: float = RUNS_KEEP_S, now: float | None = None) -> int:
    """Delete the nh:qa-cell runs files of sessions with no launch or report for ``keep``
    seconds; none of their runs is open (1 h), so a late writer gets E103 instead of E107.
    Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    if not layout.workflows.is_dir():
        return removed
    for path in layout.workflows.glob("*.json"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > keep:
            _unlink(path)
            removed += 1
    return removed
