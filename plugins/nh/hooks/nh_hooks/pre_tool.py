"""PreToolUse hooks: turn stamps for nh write tools, and guards against raw notebook edits.

Variants (the second argument in hooks.json):
- ``file``: Write/Edit/MultiEdit/NotebookEdit of an in-project notebook or of .nh/, also
  through symlinks (the shim runs Python for every one of these calls).
- ``read``: Read of an in-project notebook (the shim skips paths without "ipynb").
- ``shell``: Bash/PowerShell commands that write notebooks or .nh/ (best effort).
- ``nh``: stamp an nh write call for the gateway's turn gate. Subagents are denied, except
  nh:cell-writer (inside the nh:qa-cell workflow), which can't undo.
- ``foreign-mcp``: every other MCP server's tool; only notebook, cell, kernel and
  code-execution tools get a decision.
- ``workflow``: a launch of nh's qa-cell workflow (by its name ``nh:qa-cell``, or its script
  inline, by path or as a resume of one of its runs); denied when harness.toml asks to approve
  each cell (its writer can't ask), from a subagent, in an explain, plan or ask message, or
  when this message already had a run.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any

from common import Payload, context, deny, setting, settings, text_field

from nh_gateway._shared.paths import Layout, atomic_write_json

FILE_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
CASE_INSENSITIVE_FS = sys.platform == "darwin"
SCRIPT_MAX_BYTES = 1 << 20  # a Workflow scriptPath larger than this isn't nh's qa-cell.js

# Commands in a shell line: split on && || ; | newline and a lone & (a background job), but
# not on the & of >& <& &> or the | of >|, which are redirections.
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n]|(?<!>)\||(?<![<>])&(?!>)")
# Where a nested command starts inside a segment: $( ( or a backtick.
NESTED_START = re.compile(r"\$\(|[(`]")
# nh's own CLI as a command word, bare, by path, quoted, or run through sh/bash.
NHCTL_COMMAND = re.compile(
    r"""[\s{!]*(?:(?:[^\s;&|()`<>'"]*/)?(?:ba|z|da)?sh\s+)?"""
    r"""["']?(?:[^\s;&|()`<>'"]*/)?nhctl["']?(?=[\s;&|()`<>]|$)"""
)

# Foreign MCP tools: the verb that makes a tool read-only, and the words that make it a
# notebook, kernel or code-execution tool (matched case-insensitively, camelCase too).
READ_ONLY_VERBS = frozenset(
    ("list", "read", "get", "describe", "search", "find", "inspect", "show", "view")
)
NOTEBOOK_TERMS = ("notebook", "kernel", "ipynb", "ipython", "jupyter")
NOTEBOOK_SERVER_TERMS = ("jupyter", "ipython")
RUN_WORDS = frozenset(("run", "runs", "runner", "running"))
CODE_WORDS = frozenset(("code", "python", "python3", "snippet"))
NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

NOTEBOOK_WRITE_REASON = (
    "nh: this notebook is live in JupyterLab, so raw edits get lost or duplicate cells, and "
    "they skip nh's one-cell-per-message record. Change it with "
    "mcp__plugin_nh_nh__nh_add_cell, nh_edit_cell or nh_undo. To create a new notebook, run "
    "`nhctl notebook new <path>`."
)
STATE_WRITE_REASON = (
    "nh: .nh/ holds nh's own state; don't edit it. Settings live in harness.toml at the "
    "project root."
)
NOTEBOOK_READ_REASON = (
    "nh: reading {path} returns raw JSON with every output. Call "
    'mcp__plugin_nh_nh__nh_inspect(view="outline", notebook="{path}") instead.'
)
SHELL_REASON = (
    "nh: this command looks like it writes a notebook or nh's state ({rule}). Notebooks "
    "change only through the nh tools. If it really must run, ask the user to run it "
    "themselves."
)
SUBAGENT_REASON = (
    "nh: only the main conversation, or nh:cell-writer inside the nh:qa-cell workflow, changes "
    "the notebook (one cell per user message); other subagents are read-only. Return your "
    "findings to the main agent instead."
)
WRITER_UNDO_REASON = (
    "nh: nh:cell-writer can't undo; only the main conversation can. Return this refusal to "
    "the workflow as your final answer; don't retry or reply to the user."
)
WORKFLOW_APPROVAL_REASON = (
    "nh: harness.toml has [approval] approve_before_run = true, and the nh:qa-cell workflow "
    "can't ask the user to approve its cell. Write the cell yourself instead."
)
WORKFLOW_SUBAGENT_REASON = (
    "nh: only the main conversation launches the nh:qa-cell workflow. Return your findings "
    "to the main agent instead."
)
WORKFLOW_MODE_REASON = (
    "nh: this user message only asks to explain, plan or ask, so no cell is written in it. "
    "Don't launch nh:qa-cell: answer in chat (a numbered walkthrough, the numbered plan or the "
    "one question) and write nothing."
)
WORKFLOW_AGAIN_REASON = (
    "nh: this user message already had its nh:qa-cell run (one per message). Reply from its "
    "report when it arrives; if its writer wrote no cell, write the cell yourself, unless the "
    "report says needs_approval: then ask its question and stop."
)
FOREIGN_DENY_REASON = (
    "nh project: notebook edits and runs go through the mcp__plugin_nh_nh__* tools, so each "
    "user message stays one reviewed cell. {tool} from {server} is blocked here; use "
    "nh_add_cell, nh_edit_cell or nh_run instead."
)
FOREIGN_WARN = (
    "nh project: {tool} from {server} changes notebooks outside nh, so it skips the "
    "one-cell-per-message record, notes and undo. Prefer the mcp__plugin_nh_nh__* tools."
)


def handle(layout: Layout, payload: Payload, variant: str) -> Payload | None:
    if variant in ("file", "read"):
        return file_guard(layout, payload)
    if variant == "shell":
        return shell_guard(payload)
    if variant == "nh":
        return stamp(layout, payload)
    if variant == "foreign-mcp":
        return foreign_mcp_guard(layout, payload)
    if variant == "workflow":
        return workflow_guard(layout, payload)
    return None


def file_guard(layout: Layout, payload: Payload) -> Payload | None:
    tool = text_field(payload, "tool_name")
    if tool not in FILE_WRITE_TOOLS and tool != "Read":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return None
    base = text_field(payload, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = os.path.abspath(os.path.join(base, os.path.expanduser(raw)))
    real = os.path.realpath(path)
    rel = project_relative(real, str(layout.project))
    if rel is None:
        rel = project_relative(path, str(layout.project))  # a symlink into the project
    if rel is None:
        return None
    notebook = is_notebook(raw) or is_notebook(real)
    if tool == "Read":
        return deny(NOTEBOOK_READ_REASON.format(path=rel)) if notebook else None
    first = rel.split(os.sep, 1)[0]
    if (first.lower() if CASE_INSENSITIVE_FS else first) == ".nh":
        return deny(STATE_WRITE_REASON)
    return deny(NOTEBOOK_WRITE_REASON) if notebook else None


def is_notebook(path: str) -> bool:
    return path.lower().endswith(".ipynb")


def project_relative(path: str, project: str) -> str | None:
    for root in dict.fromkeys((os.path.realpath(project), os.path.abspath(project))):
        rel = relative_to(path, root)
        if rel is not None:
            return rel
    return None


def relative_to(path: str, root: str) -> str | None:
    """``path`` relative to ``root``, or None when it lies outside (case-folded on macOS)."""
    here, top = (path.lower(), root.lower()) if CASE_INSENSITIVE_FS else (path, root)
    prefix = top.rstrip(os.sep) + os.sep
    if not here.startswith(prefix):
        return None
    return path[len(prefix) :]


def shell_guard(payload: Payload) -> Payload | None:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    from nh_gateway._shared.patterns import shell_notebook_write

    for segment in SEGMENT_SPLIT.split(command):
        rule = shell_notebook_write(without_nhctl_words(segment))
        if rule:
            return deny(SHELL_REASON.format(rule=rule))
    return None


def without_nhctl_words(segment: str) -> str:
    """``segment`` with the words of every nhctl command in it blanked out.

    nh's own CLI writes notebook templates and .nh/ state, and its arguments may look like
    a rule (``--adopt notebooks/touch-points.ipynb``), so only its own words are exempt:
    its redirections and any command nested in ``$(...)``, ``(...)`` or backticks stay
    for the rules to see.
    """
    chars = list(segment)
    starts = [0] + [match.end() for match in NESTED_START.finditer(segment)]
    for start in starts:
        if is_nhctl(segment[start:]):
            in_backticks = start > 0 and segment[start - 1] == "`"
            for index in nhctl_word_positions(segment, start, in_backticks):
                chars[index] = " "
    return "".join(chars)


def nhctl_word_positions(segment: str, start: int, in_backticks: bool) -> list[int]:
    """Positions of the nhctl command's own text, from ``start`` to its first redirection
    or the end of the command it is nested in; nested commands are skipped, not included."""
    positions: list[int] = []
    depth = 0
    index = start
    while index < len(segment):
        char = segment[index]
        opens = 2 if segment.startswith("$(", index) else 1 if char == "(" else 0
        if depth:  # inside a nested command, whose text stays for the rules
            if opens:
                depth += 1
            elif char == ")":
                depth -= 1
            index += opens or 1
            continue
        if char in "<>)" or (char == "`" and in_backticks):
            break
        if opens:
            depth = 1
            index += opens
            continue
        if char == "`":  # a nested `command`: skip to its closing backtick
            close = segment.find("`", index + 1)
            if close < 0:
                break
            index = close + 1
            continue
        positions.append(index)
        index += 1
    return positions


def is_nhctl(command: str) -> bool:
    """Whether the command starts with nh's CLI (``nhctl``, ``.../nhctl``, ``sh .../nhctl``)."""
    return NHCTL_COMMAND.match(command) is not None


def stamp(layout: Layout, payload: Payload) -> Payload | None:
    """Record which user prompt this call belongs to; the gateway claims the file.

    A subagent's call is denied unless it is nh:cell-writer's; the gateway then checks that
    the writer works for this message's nh:qa-cell run.
    """
    from nh_gateway._shared.stamp_spec import (
        STAMP_VERSION,
        bare_tool_name,
        stamp_filename,
        stamp_key,
    )

    tool_name = text_field(payload, "tool_name")
    if payload.get("agent_id"):
        from nh_gateway._shared.turn_record import WRITER_AGENT

        if payload.get("agent_type") != WRITER_AGENT:
            return deny(SUBAGENT_REASON)
        if bare_tool_name(tool_name) == "nh_undo":
            return deny(WRITER_UNDO_REASON)
    tool_input = payload.get("tool_input")
    key = stamp_key(tool_name, tool_input if isinstance(tool_input, dict) else {})
    tool_use_id = text_field(payload, "tool_use_id")
    body = {
        "v": STAMP_VERSION,
        "key": key,
        "tool": bare_tool_name(tool_name),
        "session_id": payload.get("session_id"),
        "prompt_id": payload.get("prompt_id"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "tool_use_id": tool_use_id or None,
        "permission_mode": payload.get("permission_mode"),
        "cc_pid": claude_code_pid(),
        "ts": time.time(),
    }
    atomic_write_json(layout.stamps / stamp_filename(key, tool_use_id), body)
    return None


def claude_code_pid() -> int | None:
    try:
        return int(os.environ.get("NH_CC_PID", ""))
    except ValueError:
        return None


def workflow_guard(layout: Layout, payload: Payload) -> Payload | None:
    """Deny an nh:qa-cell launch that can't write this message's cell; other workflows pass."""
    from nh_gateway._shared import turn_record

    tool_input = payload.get("tool_input")
    if text_field(payload, "tool_name") != "Workflow" or not isinstance(tool_input, dict):
        return None
    session_id = text_field(payload, "session_id")
    if not is_nh_launch(layout, payload, tool_input, session_id):
        return None
    if payload.get("agent_id"):
        return deny(WORKFLOW_SUBAGENT_REASON)
    approve = setting(settings(layout), "approval", "approve_before_run", False)
    if approve and os.environ.get("NH_HEADLESS") != "1":  # as the gateway's tool_meta
        return deny(WORKFLOW_APPROVAL_REASON)
    if not session_id:
        return None
    record = turn_record.read(layout, session_id)
    turn = turn_record.canonical(record, text_field(payload, "prompt_id") or None)
    if turn_record.no_write_mode(record, turn):  # design §6.2; the gateway's E109 enforces it
        return deny(WORKFLOW_MODE_REASON)
    runs = turn_record.find_runs(layout, session_id)
    if turn and any(run.get("turn_id") == turn and turn_record.is_own_run(run) for run in runs):
        return deny(WORKFLOW_AGAIN_REASON)
    return None


def is_nh_launch(
    layout: Layout, payload: Payload, tool_input: dict[str, Any], session_id: str
) -> bool:
    """Whether this Workflow call runs nh's qa-cell.js: by nh's plugin name, inline, by a path
    to a byte-identical copy (the launch result offers its saved copy), or as a resume of one
    of nh's own runs. The PostToolUse hook records nothing else as nh's own run.

    PreToolUse sees only the call's own fields; Claude Code resolves a name to its script
    later. A bare ``qa-cell`` names a project or user workflow, never the plugin's.
    """
    from post_tool import digest, own_run, own_script_digest

    from nh_gateway._shared import turn_record

    if tool_input.get("name") == turn_record.LAUNCH_NAME:
        return True
    resume = tool_input.get("resumeFromRunId")
    if isinstance(resume, str) and resume and session_id and own_run(layout, session_id, resume):
        return True
    own = own_script_digest()
    if own is None:
        return False
    script = tool_input.get("script")
    if isinstance(script, str) and digest(script) == own:
        return True
    raw = tool_input.get("scriptPath")
    if not isinstance(raw, str) or not raw:
        return False
    base = text_field(payload, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        with open(os.path.join(base, os.path.expanduser(raw)), "rb") as handle:
            data = handle.read(SCRIPT_MAX_BYTES + 1)
        return len(data) <= SCRIPT_MAX_BYTES and digest(data.decode("utf-8")) == own
    except (OSError, ValueError):
        return False


def foreign_mcp_guard(layout: Layout, payload: Payload) -> Payload | None:
    from nh_gateway._shared.stamp_spec import TOOL_PREFIX

    tool_name = text_field(payload, "tool_name")
    if not tool_name.startswith("mcp__") or tool_name.startswith(TOOL_PREFIX):
        return None
    server, _, tool = tool_name[len("mcp__") :].partition("__")
    if not tool or not notebook_tool(server, tool) or read_only(tool):
        return None
    mode = setting(settings(layout), "guard", "foreign_mcp", "deny")
    if mode == "off":
        return None
    info = payload.get("mcp_server")
    if isinstance(info, dict) and isinstance(info.get("name"), str) and info["name"]:
        server = info["name"]
    if mode == "warn":
        warning = FOREIGN_WARN.format(tool=tool, server=server)
        return context("PreToolUse", warning)
    return deny(FOREIGN_DENY_REASON.format(tool=tool, server=server))


def name_words(name: str) -> list[str]:
    """``executeCode`` and ``execute_code`` both give ["execute", "code"]."""
    return [word.lower() for word in NAME_WORD.findall(name)]


def read_only(tool: str) -> bool:
    words = name_words(tool)
    return bool(words) and words[0] in READ_ONLY_VERBS


def notebook_tool(server: str, tool: str) -> bool:
    """A notebook, cell or kernel tool, any tool of a Jupyter server, or one that runs code.

    ``execute_sql`` or ``run_query`` alone don't count: running code needs a code word.
    """
    lowered = tool.lower()
    if any(term in lowered for term in NOTEBOOK_TERMS):
        return True
    if any(term in server.lower() for term in NOTEBOOK_SERVER_TERMS):
        return True
    words = name_words(tool)
    if any(word.startswith("cell") or word.endswith(("cell", "cells")) for word in words):
        return True
    runs = any(word.startswith("exec") or word in RUN_WORDS for word in words)
    return runs and any(word in CODE_WORDS for word in words)
