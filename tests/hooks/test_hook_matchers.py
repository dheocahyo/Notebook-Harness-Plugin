"""hooks.json shape, and every matcher compiled by node's JavaScript RegExp as Claude Code does."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest
from hookenv import HOOKS_JSON, LIBEXEC, NH_HOOK, PLUGIN, hook_entries

from nh_gateway._shared.stamp_spec import TOOL_PREFIX
from nh_gateway._shared.tool_defaults import TOOL_DEFAULTS, WRITE_TOOLS

FILE = "^(Write|Edit|MultiEdit|NotebookEdit)$"
READ = "^Read$"
SHELL = "^(Bash|PowerShell)$"
STAMP = "^mcp__plugin_nh_nh__nh_(add_cell|edit_cell|run|undo)$"
# Every foreign MCP tool reaches the hook; pre_tool.notebook_tool decides which ones count.
FOREIGN = "^mcp__(?!plugin_nh_nh__).+__.+$"
# Workflow launches: the qa-cell launch guard (PreToolUse) and run records (PostToolUse).
WORKFLOW = "^Workflow$"
# A stopped workflow sends no notification: its run is marked done on TaskStop (PostToolUse).
TASKSTOP = "^TaskStop$"

EXPECTED = {
    FILE: (
        ["Write", "Edit", "MultiEdit", "NotebookEdit"],
        ["Read", "NotebookRead", "EditX", "mcp__x__Edit", "Bash", "WebFetch"],
    ),
    READ: (["Read"], ["ReadX", "NotebookRead", "mcp__x__Read", "Write"]),
    SHELL: (["Bash", "PowerShell"], ["BashOutput", "KillBash", "mcp__shell__Bash"]),
    STAMP: (
        [f"{TOOL_PREFIX}{tool}" for tool in WRITE_TOOLS],
        [f"{TOOL_PREFIX}nh_inspect", "mcp__other__nh_add_cell", f"{TOOL_PREFIX}nh_add_cell_x"],
    ),
    FOREIGN: (
        [
            "mcp__datalayer__insert_cell",
            "mcp__datalayer__execute_code",
            "mcp__plugin_datalayer_datalayer__overwrite_cell_source",
            "mcp__jupyter__use_notebook",
            "mcp__jupyter__restart_kernel",
            "mcp__jupyter__list_notebooks",
            "mcp__jupyter__execute_ipython",
            "mcp__ide__executeCode",
            "mcp__jupyter__InsertCell",
            "mcp__datalayer__NotebookInsert",
            "mcp__github__create_issue",
        ],
        [
            f"{TOOL_PREFIX}nh_inspect",
            f"{TOOL_PREFIX}nh_add_cell",
            f"{TOOL_PREFIX}nh_run",
            "mcp__jupyter",
            "mcp__jupyter__",
            "NotebookEdit",
        ],
    ),
    WORKFLOW: (
        ["Workflow"],
        ["WorkflowX", "Workflows", "workflow", "mcp__a__Workflow", "Agent", "Task"],
    ),
    TASKSTOP: (
        ["TaskStop"],
        ["TaskStopX", "taskstop", "mcp__a__TaskStop", "TaskOutput", "KillShell", "Task"],
    ),
}

NODE_SCRIPT = r"""
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = {};
for (const [matcher, names] of Object.entries(cases)) {
  const re = new RegExp(matcher);
  out[matcher] = Object.fromEntries(names.map((n) => [n, re.test(n)]));
}
process.stdout.write(JSON.stringify(out));
"""


def pre_tool_matchers() -> list[str]:
    return [group["matcher"] for group in hook_entries()["PreToolUse"]]


def post_tool_matchers() -> list[str]:
    return [group["matcher"] for group in hook_entries()["PostToolUse"]]


def test_pre_tool_matchers_are_the_planned_ones() -> None:
    assert pre_tool_matchers() == [FILE, READ, SHELL, STAMP, FOREIGN, WORKFLOW]


def test_post_tool_matchers_are_the_planned_ones() -> None:
    assert post_tool_matchers() == [WORKFLOW, TASKSTOP]
    args = [
        [handler["args"][1:] for handler in group["hooks"]]
        for group in hook_entries()["PostToolUse"]
    ]
    assert args == [[["post-tool", "workflow"]], [["post-tool", "task-stop"]]]


def test_matchers_are_anchored_regexes() -> None:
    for matcher in pre_tool_matchers() + post_tool_matchers():
        # Anything beyond [A-Za-z0-9_ ,|-] makes Claude Code treat it as an unanchored regex.
        assert re.search(r"[^A-Za-z0-9_ ,|\-]", matcher)
        assert matcher.startswith("^") and matcher.endswith("$")


def test_stamp_matcher_covers_exactly_the_write_tools() -> None:
    names = re.fullmatch(r"\^mcp__plugin_nh_nh__nh_\((.*)\)\$", STAMP).group(1).split("|")
    assert {f"nh_{name}" for name in names} == set(WRITE_TOOLS)
    assert set(WRITE_TOOLS) < set(TOOL_DEFAULTS)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_matchers_under_javascript_regexp() -> None:
    cases = {matcher: hits + misses for matcher, (hits, misses) in EXPECTED.items()}
    proc = subprocess.run(
        ["node", "-e", NODE_SCRIPT], input=json.dumps(cases), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    for matcher, (hits, misses) in EXPECTED.items():
        assert [name for name in hits if not results[matcher][name]] == [], matcher
        assert [name for name in misses if results[matcher][name]] == [], matcher


def test_handlers_use_exec_form_through_the_shim() -> None:
    events = hook_entries()
    assert set(events) == {
        "Setup",
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
    }
    placeholder = "${CLAUDE_PLUGIN_ROOT}/hooks/nh-hook"
    for event, groups in events.items():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["type"] == "command"
                assert handler["command"] == "/bin/sh"
                assert handler["args"][0] == placeholder
                assert handler["timeout"] == (600 if event == "Setup" else 5)
    assert json.loads(HOOKS_JSON.read_text())["description"]


def test_scripts_are_executable_posix_sh() -> None:
    scripts = [NH_HOOK, LIBEXEC / "nh-mcp", LIBEXEC / "nh-sync", LIBEXEC / "nh-python"]
    shells = ["/bin/sh"] + [shell for shell in ("/bin/dash",) if os.path.exists(shell)]
    for script in scripts:
        assert os.access(script, os.X_OK), script
        assert script.read_text().startswith("#!/bin/sh\n")
        for shell in shells:
            proc = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
            assert proc.returncode == 0, (shell, script, proc.stderr)
    assert (PLUGIN / "hooks" / "nh_hooks" / "main.py").is_file()
