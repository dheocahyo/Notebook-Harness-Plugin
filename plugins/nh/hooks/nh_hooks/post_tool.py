"""PostToolUse: record nh:qa-cell launches and stops for the gateway.

Variant ``workflow`` (main conversation only): a Workflow call that Claude Code started in
the background (status ``async_launched``) under nh's name is added to
``.nh/state/workflows/<session>.json`` with its run and task ids, the human message it
belongs to, and how it was launched. The gateway lets a cell writer write only for a run
launched by name that runs nh's own script.

Variant ``task-stop``: a stopped workflow sends no task notification, so a TaskStop of a
recorded run marks it done here: the main conversation may write again (no E108), and the
run's late writes get E107.

The hooks make no decision and print nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from common import PLUGIN_ROOT, Payload, text_field

from nh_gateway._shared import turn_record
from nh_gateway._shared.paths import Layout

OWN_SCRIPT = os.path.join(PLUGIN_ROOT, "workflows", "qa-cell.js")
STOPPED = "killed"  # the task status Claude Code gives a stopped workflow


def handle(layout: Layout, payload: Payload, variant: str) -> Payload | None:
    if variant == "workflow":
        record_launch(layout, payload)
    elif variant == "task-stop":
        record_stop(layout, payload)
    return None


def record_launch(layout: Layout, payload: Payload) -> None:
    session_id = text_field(payload, "session_id")
    if payload.get("agent_id") or not session_id or text_field(payload, "tool_name") != "Workflow":
        return
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return
    if not isinstance(response, dict) or response.get("status") != "async_launched":
        return
    run_id = text(response.get("runId"))
    name = text(response.get("workflowName")) or text(tool_input.get("name"))
    names = turn_record.WORKFLOW_NAMES
    if run_id is None or (name not in names and tool_input.get("name") not in names):
        return
    how = launched_by(tool_input)
    resume = text(tool_input.get("resumeFromRunId"))
    if how == "name" and resume is not None and not own_run(layout, session_id, resume):
        how = "script"  # the launch guard checks a resume of nh's own runs only
    prompt_id = text_field(payload, "prompt_id") or None
    record = turn_record.read(layout, session_id)
    turn_record.record_run(
        layout,
        session_id,
        {
            "run_id": run_id,
            "task_id": text(response.get("taskId")),
            "tool_use_id": text_field(payload, "tool_use_id") or None,
            "name": name,
            "launched_by": how,
            "transcript_dir": text(response.get("transcriptDir")),
            "turn_id": turn_record.canonical(record, prompt_id),
            "prompt_id": prompt_id,
            "ts": time.time(),
            "done_ts": None,
            "status": None,
        },
    )


def launched_by(tool_input: dict[str, Any]) -> str:
    """``name`` when the launched script is nh's own qa-cell.js (Claude Code passes the
    resolved script text in ``tool_input.script``), else ``script`` or ``scriptPath``.

    Only launches the PreToolUse guard checked count: by nh's name, by path or inline. nh's
    script under another name (a copy among the project's workflows) is ``script``.
    """
    name = tool_input.get("name")
    checked = name is None or name == turn_record.LAUNCH_NAME or bool(tool_input.get("scriptPath"))
    script = tool_input.get("script")
    if script is not None:
        own = own_script_digest()
        mine = isinstance(script, str) and own is not None and digest(script) == own
        return "name" if mine and checked else "script"
    if tool_input.get("scriptPath"):
        return "scriptPath"
    return "name" if name == turn_record.LAUNCH_NAME else "script"


def own_run(layout: Layout, session_id: str, run_id: str) -> bool:
    """Whether ``run_id`` is recorded as one of nh's own runs."""
    runs = turn_record.find_runs(layout, session_id)
    return any(run.get("run_id") == run_id and turn_record.is_own_run(run) for run in runs)


def record_stop(layout: Layout, payload: Payload) -> None:
    """Mark the run a TaskStop stopped as done (whoever stopped it, it is gone)."""
    session_id = text_field(payload, "session_id")
    if not session_id or text_field(payload, "tool_name") != "TaskStop":
        return
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            response = None
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    task_id = (
        (text(response.get("task_id")) if isinstance(response, dict) else None)
        or text(tool_input.get("task_id"))
        or text(tool_input.get("shell_id"))
    )
    if task_id:
        turn_record.mark_done(layout, session_id, None, task_id=task_id, status=STOPPED)


def own_script_digest() -> str | None:
    try:
        with open(OWN_SCRIPT, "rb") as handle:
            return digest(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def digest(script: str) -> str:
    """sha256 of the script with CRLF line ends made LF and outer whitespace stripped."""
    data = script.replace("\r\n", "\n").strip().encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
