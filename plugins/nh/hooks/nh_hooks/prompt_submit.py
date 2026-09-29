"""UserPromptSubmit: open the turn for the gateway and remind Claude of the one-cell rule.

Writes ``.nh/state/turns/<session>.json`` (a human message opens a new turn and closes the
previous one) and adds a reminder of at most 400 characters: the one-cell rule and the
message's mode part, whether the kernel still holds results the notebook no longer has, and
the last cell by title and [n] with its status (design §6.2 settles the order).

A background task's completion (a prompt made only of ``<task-notification>`` blocks) opens
no turn: its prompt id becomes an alias of the open human turn, so it shares that message's
one cell, or an orphan with no turn when none is open. Its runs are marked as reported.

A human message is classified (``intent.classify``: mode, request, answer; never its text).
One typed while Claude works arrives with the running turn's prompt id (``turn_record.running``:
the human turn's own, or a notification's alias): it opens no turn and only tightens the
running one (``turn_record.absorbed``), so the reminder skips ``RULE``. An explain message
(or one absorbed into a turn that became explain, or a notification of such a turn) gets
``EXPLAIN``: the gateway refuses its writes (E109).
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Any

from common import Payload, context, text_field

from nh_gateway._shared import intent, secrets, turn_record
from nh_gateway._shared.paths import Layout, append_jsonl, atomic_write_json, read_json

REMINDER_MAX_CHARS = 400
TITLE_MAX_CHARS = 60
DRIFT_MAX_NAMES = 4

NO_PROMPT_ID = (
    "[nh] Claude Code sent no prompt id, so nh can't count cells per message and will refuse "
    "to write. Ask the user to run `claude update` (2.1.196 or newer)."
)
RULE = "[nh] One new cell this turn; go/next/y = do the proposed step."
EXPLAIN = (
    "[nh] Explain only this message: a numbered walkthrough in chat, never in the notebook; "
    "change nothing."
)
# The reminder part for a turn's mode (design §6.2). C6 adds the ask-turn part here.
MODE_PARTS = {"explain": EXPLAIN}
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
        head = human_message(layout, session_id, prompt_id, prompt, now)
    # The project's redactor (design §6.8), after the turn record is written so a failure here
    # can't cost the gate its record: cell titles and the reminder, before any cut.
    redactor = secrets.install(secrets.Redactor.for_project(layout.project))

    last = read_json(layout.last_cell)
    notebook = last.get("notebook") if isinstance(last, dict) else None
    parts = [NO_PROMPT_ID] if not prompt_id else []
    parts.extend(head)
    # Drift before the last cell (design §6.2): the clip cuts the last cell's tail first.
    parts.append(drift_line(read_json(layout.drift_json), notebook) or "")
    parts.append(last_cell_line(last, now) or "")
    text = clip(redactor.redact(" ".join(part for part in parts if part)))
    return context("UserPromptSubmit", text) if text else None


def human_message(
    layout: Layout, session_id: str, prompt_id: str, prompt: Any, now: float
) -> list[str]:
    """Open the turn (or tighten the running one, for a message typed mid-turn) and return
    the reminder's head."""
    if not session_id:
        return human_head(None, absorbed=False)
    previous = turn_record.read(layout, session_id)
    absorbed = turn_record.running(previous, prompt_id)
    # The human turn the message belongs to (None when absorbed into an orphan).
    turn_id = previous["turn_id"] if previous is not None and absorbed else prompt_id or None
    try:
        classified = intent.classify(prompt)
    except Exception:
        classified = intent.empty()
        log_event(layout, now, "classify_failed", session_id=session_id, turn_id=turn_id)
    fields: dict[str, Any] = {"session_id": session_id, "turn_id": turn_id}
    if previous is not None and absorbed:
        record = turn_record.absorbed(previous, prompt_id, classified)
        fields["prompt_id"] = prompt_id
    else:
        record = turn_record.opened(session_id, prompt_id, now, previous, classified)
    atomic_write_json(layout.turn_file(session_id), record)
    log_event(
        layout,
        now,
        "turn_absorbed" if absorbed else "turn_open",
        **fields,
        prompt_chars=len(prompt) if isinstance(prompt, str) else None,
        mode=classified["mode"],
        request=classified["request"],
        answer=classified["answer"],
    )
    return human_head(record, absorbed=absorbed)


def human_head(record: dict[str, Any] | None, *, absorbed: bool) -> list[str]:
    """The reminder's head for a human message: the one-cell rule for a new turn, nothing for
    a message folded into the running turn (it has no budget of its own), then the turn's
    mode parts."""
    return ([] if absorbed else [RULE]) + mode_parts(record)


def mode_parts(record: dict[str, Any] | None) -> list[str]:
    """What the turn's record asks of this reply: ``EXPLAIN`` for an explain turn (a mode
    absorbed mid-turn included), else nothing. The seam for C6's ask-turn part (mode ask)
    and approved-batch part (a yes to the previous turn's request)."""
    mode = record.get("mode") if record else None
    part = MODE_PARTS.get(mode) if isinstance(mode, str) else None
    return [part] if part else []


def log_event(layout: Layout, now: float, event: str, **fields: Any) -> None:
    append_jsonl(layout.log_file, {"v": 1, "ts": now, "event": event, **fields})


def notification(
    layout: Layout,
    session_id: str,
    prompt_id: str,
    prompt_chars: int,
    blocks: list[dict[str, Any]],
    now: float,
) -> list[str]:
    """Alias a background task's prompt to the open human turn (an orphan when none is open),
    mark the runs it reports as done, and return the reminder's head: its first line, then
    the turn's mode parts, as for a message typed into it (in an explain turn nh:qa-cell's
    writer wrote nothing, and the main agent may not either: E109, design §6.2)."""
    if not session_id:
        return [BACKGROUND]
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
        first = QA_REPORT
    elif qa_turns:
        first = QA_EARLIER
    else:
        first = BACKGROUND
    return [first] + mode_parts(current)  # aliased() keeps the turn's mode


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
    label = " ".join(secrets.current().redact(title).split()) if isinstance(title, str) else ""
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
