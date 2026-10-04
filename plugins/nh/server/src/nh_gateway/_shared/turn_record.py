"""The turn record and the nh:qa-cell run records, shared by the hooks and the gateway.

``.nh/state/turns/<session>.json`` names the open human message (``turn_id``). A background
task's completion reaches Claude Code as a UserPromptSubmit with a new prompt id; the hook
records that id as an alias of the open turn, so it shares the message's one-cell budget, or
as an orphan (``turn_id`` None) when no human message is open, so writes get E102. A new
human message keeps the earlier aliases in ``earlier`` (alias -> its human turn), so a late
call from an earlier notification's prompt still counts against that message's budget.

Since v0.2 a record also carries the opening message's ``mode``, ``request`` and ``answer``
(``intent.classify``) and the previous record's ``prev_turn_id`` and ``prev_request``. A
message typed while Claude works resubmits the running turn's prompt id (``running()``):
``absorbed()`` lets it only tighten the turn (design §6.1).

``.nh/state/workflows/<session>.json`` holds the last Workflow launches of the session and
tells the gateway which run a cell writer works for.

Stdlib only and Python 3.9 compatible: hooks may run on the system Python.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from . import intent
from .paths import Layout, atomic_write_json, read_json, safe_name

RECORD_VERSION = 2
RUNS_VERSION = 1
WRITER_AGENT = "nh:cell-writer"
QA_AGENT = "nh:cell-qa"
WORKFLOW_NAME = "qa-cell"
# The model launches it as "nh:qa-cell" (a bare "qa-cell" names a project or user workflow);
# Claude Code records workflowName "qa-cell".
LAUNCH_NAME = "nh:" + WORKFLOW_NAME
WORKFLOW_NAMES = (WORKFLOW_NAME, LAUNCH_NAME)
MAX_ALIASES = 1000  # reset by every human message
MAX_EARLIER = 1000  # aliases of earlier turns remembered across human messages
MAX_RUNS = 20
RUN_OPEN_TTL_S = 3600.0  # a run that never reported stops counting as open
# The status a TaskStop marks a run done with (hooks/nh_hooks/post_tool.py): a stopped run
# sends no report, so its writer's question never reached the user (design §6.4, C5d2).
STOPPED = "killed"
META_WAIT_S = 2.0  # the writer can call before PostToolUse has recorded its run
META_POLL_S = 0.1
# v0.2's fields (design §6.1): the opening message's intent and what the previous turn asked.
INTENT_FIELDS = ("mode", "request", "answer", "prev_turn_id", "prev_request")

BLOCK = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)


def _field(name: str) -> re.Pattern[str]:
    return re.compile(rf"<{name}>(.*?)</{name}>", re.S)


TASK_ID = _field("task-id")
TOOL_USE_ID = _field("tool-use-id")
STATUS = _field("status")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --- turn record -------------------------------------------------------------------------


def read(layout: Layout, session_id: str) -> dict[str, Any] | None:
    """The session's turn record in v2 shape, or None when missing or unreadable.

    A v1 record (``{v:1, prompt_id, ts}``) is its own turn: ``turn_id = prompt_id``. The
    v0.2 fields are whitelisted: absent, invalid or from a v1 record, they read as None.
    """
    data = read_json(layout.turn_file(session_id))
    if not isinstance(data, dict):
        return None
    prompt_id = _text(data.get("prompt_id"))
    if data.get("v") == RECORD_VERSION:
        turn_id = _text(data.get("turn_id"))
        raw = data.get("aliases")
        aliases = [a for a in raw if isinstance(a, str) and a] if isinstance(raw, list) else []
        human = data.get("human") is not False
    else:
        turn_id, aliases, human = prompt_id, [], True
    alias_ts = data.get("alias_ts")
    raw = data.get("earlier")
    earlier = (
        {
            alias: turn
            for alias, turn in raw.items()
            if isinstance(alias, str) and alias and (turn is None or _text(turn))
        }
        if isinstance(raw, dict)
        else {}
    )
    v2 = data.get("v") == RECORD_VERSION
    mode, answer = data.get("mode"), data.get("answer")
    return {
        "v": RECORD_VERSION,
        "session_id": _text(data.get("session_id")) or session_id,
        "prompt_id": prompt_id,
        "turn_id": turn_id,
        "aliases": aliases,
        "human": human,
        "ts": _number(data.get("ts")),
        "alias_ts": _number(alias_ts) if alias_ts is not None else None,
        "earlier": earlier,
        "mode": mode if v2 and mode in intent.MODES else None,
        "request": intent.valid_request(data.get("request")) if v2 else None,
        "answer": answer if v2 and answer in intent.ANSWERS else None,
        "prev_turn_id": _text(data.get("prev_turn_id")) if v2 else None,
        "prev_request": intent.valid_request(data.get("prev_request")) if v2 else None,
    }


def canonical(record: dict[str, Any] | None, prompt_id: str | None) -> str | None:
    """The human turn ``prompt_id`` belongs to: the turn itself for an alias of this or an
    earlier turn (None for an orphan's), else ``prompt_id`` unchanged, so an older prompt
    still gets E102."""
    if record is None or not prompt_id:
        return prompt_id
    if prompt_id == record.get("turn_id") or prompt_id in (record.get("aliases") or ()):
        return record.get("turn_id")
    earlier = record.get("earlier")
    if isinstance(earlier, dict) and prompt_id in earlier:
        return earlier[prompt_id]
    return prompt_id


def known(record: dict[str, Any] | None, prompt_id: str | None) -> bool:
    """Whether ``record`` has seen ``prompt_id``: its turn, an alias of it or of an earlier
    turn, or the previous turn. An unknown id in a human turn is a message nh missed (D1)."""
    if record is None or not prompt_id:
        return False
    earlier = record.get("earlier")
    return (
        prompt_id == record.get("turn_id")
        or prompt_id in (record.get("aliases") or ())
        or (isinstance(earlier, dict) and prompt_id in earlier)
        or prompt_id == record.get("prev_turn_id")
    )


def no_write_mode(record: dict[str, Any] | None, turn_id: str | None) -> str | None:
    """The mode (explain, plan or ask) under which ``turn_id`` changes nothing in the notebook
    (E109, design §6.2): the record's mode when ``turn_id`` is the record's turn, else None.
    The gateway and the workflow guard pass the call's canonical turn."""
    if record is None or not turn_id or turn_id != record.get("turn_id"):
        return None
    mode = record.get("mode")
    return mode if mode in intent.MODES else None


def approved_batch(record: dict[str, Any] | None, turn_id: str | None) -> int | None:
    """The ``n`` of the batch the previous message asked for, when ``turn_id`` is the record's
    turn and its message is a yes (design §6.3), else None. The gateway also needs no pending
    question of the previous message and an interactive session before it grants one."""
    if record is None or not turn_id or turn_id != record.get("turn_id"):
        return None
    if record.get("answer") != "yes":
        return None
    request = intent.valid_request(record.get("prev_request"))
    if request is None or not request.get("batch"):
        return None
    return int(request["n"])


def running(record: dict[str, Any] | None, prompt_id: str | None) -> bool:
    """Whether a human message with ``prompt_id`` was typed into the running turn (spike V16:
    Claude Code resubmits that turn's prompt id). The running turn is the human turn itself,
    or a turn a background task's notification started: an alias, of the human turn or of an
    orphan. A human message never reuses a prompt id otherwise."""
    if record is None or not prompt_id:
        return False
    if record.get("human") and prompt_id == record.get("turn_id"):
        return True
    return prompt_id in (record.get("aliases") or ())


def _remember(record: dict[str, Any], aliases: list[str]) -> dict[str, str | None]:
    """``record``'s earlier aliases plus ``aliases`` (of its turn), newest last, at most
    MAX_EARLIER."""
    earlier = dict(record.get("earlier") or {})
    for alias in aliases:
        earlier.pop(alias, None)
        earlier[alias] = record.get("turn_id")
    return dict(list(earlier.items())[-MAX_EARLIER:])


def _intent(classified: dict[str, Any] | None) -> tuple[str | None, Any, str | None]:
    """``intent.classify``'s mode, request and answer, each None unless valid."""
    if not isinstance(classified, dict):
        return None, None, None
    mode, answer = classified.get("mode"), classified.get("answer")
    return (
        mode if mode in intent.MODES else None,
        intent.valid_request(classified.get("request")),
        answer if answer in intent.ANSWERS else None,
    )


def opened(
    session_id: str,
    prompt_id: str | None,
    now: float,
    previous: dict[str, Any] | None = None,
    classified: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A human message: a new turn with no aliases (``previous``'s are kept in ``earlier``).

    ``classified`` is ``intent.classify``'s result for the message; ``prev_turn_id`` and
    ``prev_request`` come from ``previous``, so a "yes" can find the question it answers.
    """
    mode, request, answer = _intent(classified)
    return {
        "v": RECORD_VERSION,
        "session_id": session_id,
        "prompt_id": prompt_id or None,
        "turn_id": prompt_id or None,
        "aliases": [],
        "human": True,
        "ts": now,
        "alias_ts": None,
        "earlier": _remember(previous, previous.get("aliases") or []) if previous else {},
        "mode": mode,
        "request": request,
        "answer": answer,
        "prev_turn_id": _text(previous.get("turn_id")) if previous else None,
        "prev_request": intent.valid_request(previous.get("request")) if previous else None,
    }


def absorbed(
    record: dict[str, Any], prompt_id: str, classified: dict[str, Any] | None
) -> dict[str, Any]:
    """A message typed while Claude works: Claude Code folds it into the running turn and
    resubmits that turn's prompt id (spike V16; see ``running()``). It opens no turn and can
    only tighten one: its mode holds for the rest of the turn and its request waits for the
    next reply, but its yes or no is never an answer. The turn (an orphan stays one), its
    aliases, ``earlier``, ``ts``, ``answer``, ``prev_turn_id`` and ``prev_request`` are kept."""
    mode, request, _answer = _intent(classified)
    return dict(
        record,
        v=RECORD_VERSION,
        prompt_id=prompt_id,
        mode=mode or record.get("mode"),
        request=request or record.get("request"),
    )


def aliased(record: dict[str, Any], prompt_id: str, now: float) -> dict[str, Any]:
    """``record`` with ``prompt_id`` as one more alias; ``turn_id`` and ``ts`` (when the
    human turn opened, which E102 compares) are kept. Past MAX_ALIASES the oldest move to
    ``earlier``."""
    aliases = [a for a in record.get("aliases") or [] if a != prompt_id] + [prompt_id]
    return dict(
        record,
        v=RECORD_VERSION,
        prompt_id=prompt_id,
        aliases=aliases[-MAX_ALIASES:],
        alias_ts=now,
        earlier=_remember(record, aliases[:-MAX_ALIASES]),
    )


def orphan(session_id: str, prompt_id: str, now: float) -> dict[str, Any]:
    """A background task finished while no human message is open: no turn, so no budget."""
    return {
        "v": RECORD_VERSION,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "turn_id": None,
        "aliases": [prompt_id],
        "human": False,
        "ts": now,
        "alias_ts": now,
        "earlier": {},
        "mode": None,
        "request": None,
        "answer": None,
        "prev_turn_id": None,
        "prev_request": None,
    }


# --- task notifications ------------------------------------------------------------------


def notification_blocks(prompt: str) -> list[dict[str, str | None]] | None:
    """``[{task_id, tool_use_id, status}]`` when the prompt is nothing but complete
    ``<task-notification>`` blocks, each with ``<task-id>`` and ``<status>``; else None.

    A human message that quotes a notification, or one merged with typed text, is a human
    message: it keeps its own turn.
    """
    blocks = BLOCK.findall(prompt)
    if not blocks or BLOCK.sub("", prompt).strip():
        return None
    parsed: list[dict[str, str | None]] = []
    for body in blocks:
        task_id, status = TASK_ID.search(body), STATUS.search(body)
        if not task_id or not status or not task_id.group(1).strip():
            return None
        tool_use_id = TOOL_USE_ID.search(body)
        parsed.append(
            {
                "task_id": task_id.group(1).strip(),
                "tool_use_id": (tool_use_id.group(1).strip() or None) if tool_use_id else None,
                "status": status.group(1).strip(),
            }
        )
    return parsed


# --- workflow runs -----------------------------------------------------------------------


def is_own_run(run: dict[str, Any]) -> bool:
    """nh's own qa-cell workflow, launched by name (not an inline or foreign script)."""
    return run.get("name") in WORKFLOW_NAMES and run.get("launched_by") == "name"


def run_open(run: dict[str, Any], now: float) -> bool:
    """Not reported yet and launched less than an hour ago (a run without a launch time isn't)."""
    launched = run.get("ts")
    if run.get("done_ts") is not None or isinstance(launched, bool):
        return False
    return isinstance(launched, (int, float)) and 0.0 <= now - launched < RUN_OPEN_TTL_S


def find_runs(layout: Layout, session_id: str) -> list[dict[str, Any]]:
    """The session's recorded runs, oldest first."""
    data = read_json(layout.workflow_file(session_id))
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict) and _text(run.get("run_id"))]


def _save_runs(layout: Layout, session_id: str, runs: list[dict[str, Any]]) -> None:
    atomic_write_json(
        layout.workflow_file(session_id), {"v": RUNS_VERSION, "runs": runs[-MAX_RUNS:]}
    )


def record_run(layout: Layout, session_id: str, run: dict[str, Any]) -> None:
    """Add a launch (replacing one with the same ``run_id``); keeps the last 20."""
    if not _text(run.get("run_id")):
        return
    runs = [r for r in find_runs(layout, session_id) if r.get("run_id") != run["run_id"]]
    _save_runs(layout, session_id, [*runs, run])


def mark_done(
    layout: Layout,
    session_id: str,
    tool_use_id: str | None,
    *,
    task_id: str | None = None,
    status: str | None = None,
    now: float | None = None,
    turn_id: str | None = None,
) -> list[dict[str, Any]]:
    """Mark the runs a task notification names (by launching tool_use_id or task id) as
    reported. ``turn_id`` is the human turn the notification is an alias of (None: an orphan,
    or TaskStop), kept as ``done_turn``: the message whose reply the report reached, which
    ``policy.turn.reported_runs`` compares without clocks (design §6.4, C5d3). Returns them,
    including runs an earlier notification already marked."""
    now = time.time() if now is None else now
    runs = find_runs(layout, session_id)
    named = [
        run
        for run in runs
        if (tool_use_id and run.get("tool_use_id") == tool_use_id)
        or (task_id and run.get("task_id") == task_id)
    ]
    changed = False
    for run in named:
        if run.get("done_ts") is None:
            run["done_ts"] = now
            run["status"] = status
            run["done_turn"] = turn_id
            changed = True
    if changed:
        _save_runs(layout, session_id, runs)
    return named


def open_runs(
    layout: Layout, session_id: str, turn_id: str | None, now: float | None = None
) -> list[dict[str, Any]]:
    """nh's own qa-cell runs launched in this turn that haven't reported (for at most 1 h)."""
    if not turn_id:
        return []
    now = time.time() if now is None else now
    return [
        run
        for run in find_runs(layout, session_id)
        if run.get("turn_id") == turn_id and is_own_run(run) and run_open(run, now)
    ]


def _writer_meta(run: dict[str, Any], agent_id: str) -> bool:
    folder = _text(run.get("transcript_dir"))
    if folder is None or not Path(folder).is_absolute():
        return False
    meta = read_json(Path(folder) / f"agent-{agent_id}.meta.json")
    return isinstance(meta, dict) and meta.get("agentType") == WRITER_AGENT


def run_for_agent(
    layout: Layout,
    session_id: str,
    agent_id: str,
    *,
    wait_s: float = META_WAIT_S,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The one recorded run whose transcript folder holds ``agent-<agent_id>.meta.json`` with
    ``agentType`` nh:cell-writer, or None (no match, or several).

    While nothing matches it re-reads for up to ``wait_s`` (the writer's first call can beat
    the PostToolUse hook that records the run). It blocks: call it off the event loop.
    """
    if not agent_id or safe_name(agent_id) != agent_id:
        return None
    deadline = clock() + wait_s
    while True:
        matches = [r for r in find_runs(layout, session_id) if _writer_meta(r, agent_id)]
        if len(matches) == 1:
            return matches[0]
        if matches or clock() >= deadline:
            return None
        sleep(META_POLL_S)
