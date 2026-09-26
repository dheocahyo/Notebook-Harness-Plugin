"""UserPromptSubmit: open the turn for the gateway and remind Claude of the one-cell rule.

Writes ``.nh/state/turns/<session>.json`` (a human message opens a new turn and closes the
previous one) and adds a reminder of at most 400 characters: the last cell by title and [n]
with its status, and whether the kernel still holds results the notebook no longer has.

A background task's completion (a prompt made only of ``<task-notification>`` blocks) opens
no turn: its prompt id becomes an alias of the open human turn, so it shares that message's
one cell, or an orphan with no turn when none is open. Its runs are marked as reported.
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Any

from common import Payload, context, text_field

from nh_gateway._shared import turn_record
from nh_gateway._shared.paths import Layout, append_jsonl, atomic_write_json, read_json

REMINDER_MAX_CHARS = 400
TITLE_MAX_CHARS = 60
DRIFT_MAX_NAMES = 4

NO_PROMPT_ID = (
    "[nh] Claude Code sent no prompt id, so nh can't count cells per message and will refuse "
    "to write. Ask the user to run `claude update` (2.1.196 or newer)."
)
RULE = "[nh] One new cell this turn; go/next/y = do the proposed step."
QA_REPORT = (
    "[nh] The nh:qa-cell report for this message arrived (not a new user message): reply "
    "from it; write no new cell unless its writer wrote none."
)
QA_EARLIER = (
    "[nh] An nh:qa-cell report for an earlier message arrived; the user has written since: "
    "report it, change no cell for it."
)
BACKGROUND = "[nh] A background task finished; this is not a new user message: write no new cell."
STATUS_TEXT = {
    "error": "FAILING",
    "running": "RUNNING (output keeps streaming into JupyterLab)",
    "queued": "QUEUED behind a running cell; add nothing",
    "aborted": "didn't run (a cell ahead of it failed)",
    "timeout": "TIMED OUT",
    "interrupted": "INTERRUPTED (stopped early; ask before re-running or changing it)",
    "lost": "stopped (the kernel restarted or went away)",
    "deleted": "deleted by the user in JupyterLab (a no; don't re-add it)",
    "conflict": "not run (the user typed into it first; ask before running it)",
    "undone": "undone (the user asked nh to remove or restore it)",
}


def handle(layout: Layout, payload: Payload) -> Payload | None:
    now = time.time()
    session_id = text_field(payload, "session_id")
    prompt_id = text_field(payload, "prompt_id")
    prompt = payload.get("prompt")
    blocks = turn_record.notification_blocks(prompt) if isinstance(prompt, str) else None
    if blocks is not None:
        head = notification(layout, session_id, prompt_id, len(prompt), blocks, now)
    else:
        head = RULE
        if session_id:
            previous = turn_record.read(layout, session_id)
            record = turn_record.opened(session_id, prompt_id, now, previous)
            atomic_write_json(layout.turn_file(session_id), record)
            append_jsonl(
                layout.log_file,
                {
                    "v": 1,
                    "ts": now,
                    "event": "turn_open",
                    "session_id": session_id,
                    "turn_id": prompt_id or None,
                    "prompt_chars": len(prompt) if isinstance(prompt, str) else None,
                },
            )

    last = read_json(layout.last_cell)
    parts = [NO_PROMPT_ID] if not prompt_id else []
    parts.append(head)
    parts.append(last_cell_line(last, now) or "")
    notebook = last.get("notebook") if isinstance(last, dict) else None
    parts.append(drift_line(read_json(layout.drift_json), notebook) or "")
    return context("UserPromptSubmit", clip(" ".join(part for part in parts if part)))


def notification(
    layout: Layout,
    session_id: str,
    prompt_id: str,
    prompt_chars: int,
    blocks: list[dict[str, Any]],
    now: float,
) -> str:
    """Alias a background task's prompt to the open human turn (an orphan when none is open),
    mark the runs it reports as done, and return the reminder's first line."""
    if not session_id:
        return BACKGROUND
    current = turn_record.read(layout, session_id)
    runs: list[dict[str, Any]] = []
    for block in blocks:
        runs += turn_record.mark_done(
            layout,
            session_id,
            block["tool_use_id"],
            task_id=block["task_id"],
            status=block["status"],
            now=now,
        )
    turn_id = current["turn_id"] if current else None
    if prompt_id:
        if current:
            record = turn_record.aliased(current, prompt_id, now)
        else:
            record = turn_record.orphan(session_id, prompt_id, now)
        atomic_write_json(layout.turn_file(session_id), record)
    append_jsonl(
        layout.log_file,
        {
            "v": 1,
            "ts": now,
            "event": "turn_alias",
            "session_id": session_id,
            "turn_id": turn_id,
            "prompt_id": prompt_id or None,
            "prompt_chars": prompt_chars,
            "tasks": [block["task_id"] for block in blocks],
        },
    )
    qa_turns = [run.get("turn_id") for run in runs if turn_record.is_own_run(run)]
    if turn_id and turn_id in qa_turns:
        return QA_REPORT
    return QA_EARLIER if qa_turns else BACKGROUND


def last_cell_line(last: Any, now: float) -> str | None:
    if not isinstance(last, dict) or not isinstance(last.get("status"), str):
        return None
    status = last["status"]
    if status == "ok":
        age = age_text(last.get("finished_at"), now)
        state = f"finished: ok, {age} ago" if age else "finished: ok"
    else:
        state = STATUS_TEXT.get(status, status)
    where = last.get("notebook")
    where = f" in {os.path.basename(where)}" if isinstance(where, str) and where else ""
    return f"Last cell: {cell_label(last.get('title'), last.get('exec'))}{where}: {state}."


def cell_label(title: Any, execution_count: Any) -> str:
    label = " ".join(title.split()) if isinstance(title, str) else ""
    if len(label) > TITLE_MAX_CHARS:
        label = label[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    label = f'"{label}"' if label else "untitled cell"
    if isinstance(execution_count, int) and not isinstance(execution_count, bool):
        label += f" [{execution_count}]"
    return label


def age_text(finished_at: Any, now: float) -> str | None:
    """``finished_at`` may be epoch seconds or ISO 8601; returns e.g. "3m12s"."""
    if isinstance(finished_at, bool):
        return None
    if isinstance(finished_at, (int, float)):
        then = float(finished_at)
    elif isinstance(finished_at, str) and finished_at:
        try:
            then = float(finished_at)
        except ValueError:
            try:
                parsed = datetime.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            except ValueError:
                return None
            then = parsed.timestamp()
    else:
        return None
    seconds = int(max(0.0, now - then))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d"


def drift_line(drift: Any, notebook: Any) -> str | None:
    """Names the kernel still holds from undone cells (the last cell's notebook first, each
    notebook's newest names first, as nh's own lead lists them).

    The hook can't see the kernel (a restart or re-run since nh last looked clears it), so the
    line says when it was true rather than asserting it.
    """
    if not isinstance(drift, dict):
        return None
    names: list[str] = []
    for key in sorted(drift, key=lambda path: path != notebook):
        entry = drift[key]
        held = entry.get("names") if isinstance(entry, dict) else None
        for name in reversed(list(held)) if isinstance(held, dict) else ():
            if name not in names:
                names.append(str(name))
    if not names:
        return None
    shown = ", ".join(names[:DRIFT_MAX_NAMES])
    if len(names) > DRIFT_MAX_NAMES:
        shown += f" and {len(names) - DRIFT_MAX_NAMES} more"
    verb = "holds" if len(names) == 1 else "hold"
    return (
        f"Kernel ≠ notebook: {shown} still {verb} results of undone cells (as of nh's last "
        "check); suggest Kernel → Restart Kernel and Run Up to Selected Cell."
    )


def clip(text: str) -> str:
    if len(text) <= REMINDER_MAX_CHARS:
        return text
    return text[: REMINDER_MAX_CHARS - 1].rstrip() + "…"
