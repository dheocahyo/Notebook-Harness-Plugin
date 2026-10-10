#!/usr/bin/env python3
"""Spike logger for UserPromptSubmit / PreToolUse / PostToolUse.

Appends one JSON line per hook call to <project>/.spike/hooklog.jsonl with the full stdin
payload, wall-clock ns timestamps and a snapshot of <project>/.nh/state/turns/*.json.
On UserPromptSubmit it also forks a detached poller (so the hook itself returns at once and
does not slow Claude Code) that logs when the turn record first names this prompt id.
Prints nothing on stdout (no decision, no context).
"""

import contextlib
import json
import os
import sys
import time

T0 = time.time_ns()


def project_dir(payload):
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def snapshot(project):
    turns = os.path.join(project, ".nh", "state", "turns")
    out = {}
    try:
        names = sorted(os.listdir(turns))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(turns, name)
        try:
            st = os.stat(path)
            with open(path, "rb") as fh:
                raw = fh.read()
            out[name] = {"mtime_ns": st.st_mtime_ns, "data": json.loads(raw)}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": repr(exc)}
    return out


def append(path, record):
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def knows(snap, session_id, prompt_id):
    if not snap or not prompt_id:
        return False
    for entry in snap.values():
        data = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(data, dict):
            continue
        if data.get("session_id") not in (None, session_id):
            continue
        if data.get("turn_id") == prompt_id or prompt_id in (data.get("aliases") or []):
            return True
    return False


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_unparsed": raw.decode("utf-8", "replace")}
    project = project_dir(payload)
    logdir = os.path.join(project, ".spike")
    os.makedirs(logdir, exist_ok=True)
    log = os.path.join(logdir, "hooklog.jsonl")
    event = payload.get("hook_event_name")
    record = {
        "kind": "hook",
        "t_ns": T0,
        "t_done_ns": None,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "event": event,
        "payload": payload,
        "turns_at_start": snapshot(project),
        "env_project": os.environ.get("CLAUDE_PROJECT_DIR"),
    }
    record["t_done_ns"] = time.time_ns()
    append(log, record)
    if event == "UserPromptSubmit":
        session_id = payload.get("session_id")
        prompt_id = payload.get("prompt_id")
        pid = os.fork()
        if pid == 0:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            deadline = time.time() + 5.0
            polls = 0
            seen = None
            while time.time() < deadline:
                snap = snapshot(project)
                polls += 1
                if knows(snap, session_id, prompt_id):
                    seen = snap
                    break
                time.sleep(0.005)
            append(
                log,
                {
                    "kind": "record_poll",
                    "hook_t_ns": T0,
                    "t_ns": time.time_ns(),
                    "prompt_id": prompt_id,
                    "found": seen is not None,
                    "polls": polls,
                    "turns": seen,
                },
            )
            os._exit(0)


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
