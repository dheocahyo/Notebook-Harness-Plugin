"""The nh-hook shim: project discovery, fail-open behaviour, interpreters, speed."""

from __future__ import annotations

import itertools
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hookenv import (
    NH_HOOK,
    PLUGIN,
    SYSTEM_PYTHON,
    SYSTEM_VERSION,
    HookRun,
    Sandbox,
    command_line,
    hook_entries,
    needs_system_python,
)

pytestmark = needs_system_python


def edit_notebook(sandbox: Sandbox, env: dict[str, str] | None = None, cwd: Path | None = None):
    nb = str((cwd or sandbox.project) / "notebooks" / "01_eda.ipynb")
    payload = sandbox.tool_payload("Edit", {"file_path": nb, "old_string": "a", "new_string": "b"})
    handler = sandbox.handlers("PreToolUse", "Edit")[0]
    return sandbox.run_argv(command_line(handler), payload, env)


def all_commands(sandbox: Sandbox):
    """One representative payload for every handler in hooks.json except Setup."""
    for event, groups in hook_entries().items():
        if event == "Setup":
            continue
        for group in groups:
            for handler in group["hooks"]:
                payload = sandbox.payload(event, prompt="go", source="startup")
                if event == "PreToolUse":
                    payload = sandbox.tool_payload(
                        "Write", {"file_path": str(sandbox.project / "a.ipynb"), "content": "x"}
                    )
                yield handler, payload


def test_subdirectory_of_a_project_finds_the_project(sandbox: Sandbox) -> None:
    sub = sandbox.project / "notebooks"
    run = edit_notebook(sandbox, dict(sandbox.env, CLAUDE_PROJECT_DIR=str(sub)))
    assert run.decision == "deny"


def test_no_nh_folder_means_no_output_and_no_state(sandbox: Sandbox) -> None:
    shutil.rmtree(sandbox.nh)
    for handler, payload in all_commands(sandbox):
        run = sandbox.run_argv(command_line(handler), payload)
        assert (run.returncode, run.stdout) == (0, ""), handler
    assert not sandbox.nh.exists()


def test_search_stops_at_home(sandbox: Sandbox) -> None:
    (sandbox.home / ".nh").mkdir()
    inside_home = sandbox.home / "work"
    (inside_home / "notebooks").mkdir(parents=True)
    env = dict(sandbox.env, CLAUDE_PROJECT_DIR=str(inside_home))
    assert edit_notebook(sandbox, env, cwd=inside_home).stdout == ""
    env = dict(sandbox.env, CLAUDE_PROJECT_DIR=str(sandbox.home))
    assert edit_notebook(sandbox, env, cwd=sandbox.home).stdout == ""


def test_falls_back_to_pwd_without_claude_project_dir(sandbox: Sandbox) -> None:
    env = {k: v for k, v in sandbox.env.items() if k != "CLAUDE_PROJECT_DIR"}
    assert edit_notebook(sandbox, env).decision == "deny"


def test_shim_locates_the_plugin_without_claude_plugin_root(sandbox: Sandbox) -> None:
    env = {k: v for k, v in sandbox.env.items() if k != "CLAUDE_PLUGIN_ROOT"}
    assert edit_notebook(sandbox, env).decision == "deny"


def test_malformed_input_fails_open_and_is_logged(sandbox: Sandbox) -> None:
    for handler, _ in all_commands(sandbox):
        run = sandbox.run_argv(command_line(handler), '{"tool_name": "Write", "ipynb": ')
        assert (run.returncode, run.stdout) == (0, "")
    log = (sandbox.nh / "logs" / "hooks.log").read_text()
    assert "JSONDecodeError" in log


def test_hooks_log_starts_over_past_256_kb(sandbox: Sandbox) -> None:
    log = sandbox.nh / "logs" / "hooks.log"
    log.parent.mkdir()
    log.write_text("x" * (300 * 1024))
    edit = sandbox.handlers("PreToolUse", "Edit")[0]
    sandbox.run_argv(command_line(edit), "[ipynb")
    assert log.stat().st_size < 20 * 1024
    assert "pre-tool file" in log.read_text()


def test_unusable_python_fails_open(sandbox: Sandbox) -> None:
    run = edit_notebook(sandbox, dict(sandbox.env, NH_PYTHON="/bin/echo"))
    assert (run.returncode, run.stdout) == (0, "")


def test_hooks_run_on_system_python_with_bytecode_in_plugin_data(sandbox: Sandbox) -> None:
    """/usr/bin/python3 is 3.9 on macOS and newer on Linux, so the bytecode tag follows it."""
    assert edit_notebook(sandbox).decision == "deny"
    compiled = {path.name for path in (sandbox.data / "pycache").rglob("*.pyc")}
    major, minor = SYSTEM_VERSION or (0, 0)  # the module is skipped without a system Python
    assert f"common.cpython-{major}{minor}.pyc" in compiled
    assert not list((PLUGIN / "hooks").rglob("__pycache__"))


def test_without_plugin_data_no_bytecode_is_written(sandbox: Sandbox) -> None:
    env = {k: v for k, v in sandbox.env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert edit_notebook(sandbox, env).decision == "deny"
    assert not sandbox.data.exists()


def test_hooks_also_run_under_dash(sandbox: Sandbox) -> None:
    if not Path("/bin/dash").exists():
        pytest.skip("no /bin/dash")
    payload = sandbox.tool_payload(
        "Bash", {"command": "jupytext --to notebook notebooks/a.py"}, tool_use_id="t1"
    )
    run = sandbox.run_argv(["/bin/dash", str(NH_HOOK), "pre-tool", "shell"], payload)
    assert run.decision == "deny"
    stamp = sandbox.tool_payload("mcp__plugin_nh_nh__nh_run", {"cell_id": "nh-1"})
    run = sandbox.run_argv(["/bin/dash", str(NH_HOOK), "pre-tool", "nh"], stamp)
    assert run.stdout == "" and len(list((sandbox.nh / "state" / "stamps").glob("*.json"))) == 1


def test_hooks_run_on_the_current_interpreter_too(sandbox: Sandbox) -> None:
    """3.11+ reads harness.toml with tomllib; 3.9 uses the fallback parser."""
    (sandbox.project / "harness.toml").write_text('[guard]\nforeign_mcp = "warn"\n')
    env = dict(sandbox.env, NH_PYTHON=sys.executable)
    payload = sandbox.tool_payload("mcp__jupyter__execute_cell", {"cell_index": 1})
    handler = sandbox.handlers("PreToolUse", "mcp__jupyter__execute_cell")[0]
    run = sandbox.run_argv(command_line(handler), payload, env)
    assert "execute_cell from jupyter" in run.context


PYTHON_SPY = """#!/bin/sh
echo "$*" >>"$PY_SPY_LOG"
exec "$REAL_PYTHON" "$@"
"""


@pytest.mark.parametrize(
    ("tool", "tool_input", "reaches_python"),
    [
        ("Read", {"file_path": "/work/a.py"}, False),
        ("Read", {"file_path": "/work/a.ipynb"}, True),
        ("Bash", {"command": "ls -la && git status"}, False),
        ("Bash", {"command": "rm -rf .nh"}, True),  # review finding 36
        ("mcp__github__create_issue", {"title": "x"}, False),
        ("mcp__ide__executeCode", {}, True),  # review finding 47
        ("mcp__jupyter__InsertCell", {}, True),
        # Review finding 46: writes always reach the Python guard, whatever the path says.
        ("Write", {"file_path": "/work/current", "content": ""}, True),
        ("Edit", {"file_path": "/work/a.py", "old_string": "", "new_string": ""}, True),
        # The user's own workflows skip Python; only nh's qa-cell can get a decision.
        ("Workflow", {"name": "code-review", "args": "look at the diff"}, False),
        ("Workflow", {"name": "nh:qa-cell", "args": "load sales.csv"}, True),
        ("Workflow", {"name": "qa-cell", "args": "load sales.csv"}, True),
        # A path or a resume may run nh's script under any name: the launch guard reads it.
        ("Workflow", {"scriptPath": "/work/flows/saved.js", "args": "go"}, True),
        ("Workflow", {"resumeFromRunId": "wf_1"}, True),
    ],
)
def test_pre_filter_skips_python_only_for_irrelevant_calls(
    sandbox: Sandbox, tool: str, tool_input: dict, reaches_python: bool
) -> None:
    payload = {"tool_name": tool, "tool_input": tool_input}  # no paths that name a notebook
    assert python_ran(sandbox, "PreToolUse", tool, payload) == reaches_python, (tool, tool_input)


@pytest.mark.parametrize(
    ("name", "workflow_name", "reaches_python"),
    [
        ("code-review", "code-review", False),
        ("nh:qa-cell", "qa-cell", True),
        (None, "qa-cell", True),  # launched by script path
    ],
)
def test_post_tool_pre_filter_skips_python_for_other_workflows(
    sandbox: Sandbox, name: str | None, workflow_name: str, reaches_python: bool
) -> None:
    tool_input = {"name": name, "args": "x", "script": "export const meta = {}"}
    response = {
        "status": "async_launched",
        "workflowName": workflow_name,
        "runId": "wf_1",
        "scriptPath": "/tmp/scripts/wf_1.js",  # every launch result has one
    }
    payload = {
        "tool_name": "Workflow",
        "tool_input": {k: v for k, v in tool_input.items() if v is not None},
        "tool_response": response,
    }
    assert python_ran(sandbox, "PostToolUse", "Workflow", payload) == reaches_python


def python_ran(sandbox: Sandbox, event: str, tool: str, payload: dict) -> bool:
    """Whether the shim started Python for this hook call (a spy logs each start)."""
    spy = sandbox.bin / "python-spy"
    spy.write_text(PYTHON_SPY)
    spy.chmod(0o755)
    log = sandbox.tmp / "python-calls.log"
    env = dict(sandbox.env, NH_PYTHON=str(spy), PY_SPY_LOG=str(log), REAL_PYTHON=SYSTEM_PYTHON)
    run = sandbox.run(event, payload, tool_name=tool, env=env)
    assert run.returncode == 0, run.stderr
    return log.exists()


IMPORT_PROBE = """
import json, os, runpy, sys, sysconfig
main, event, variant = sys.argv[1:4]
sys.argv = [main, event, variant]
runpy.run_path(main, run_name="__main__")
stdlib = {os.path.realpath(sysconfig.get_paths()[k]) for k in ("stdlib", "platstdlib")}
plugin = os.path.realpath(os.path.join(os.path.dirname(main), "..", ".."))
bad = []
for name, module in sorted(sys.modules.items()):
    path = os.path.realpath(getattr(module, "__file__", None) or "")
    if not getattr(module, "__file__", None) or any(path.startswith(d) for d in stdlib):
        continue
    if path.startswith(os.path.join(plugin, "hooks", "nh_hooks")):
        continue
    if name == "nh_gateway" or name.startswith("nh_gateway._shared"):
        continue
    bad.append(name)
sys.stderr.write("NH-IMPORTS " + json.dumps(bad) + "\\n")
"""


@pytest.mark.parametrize(
    ("event", "variant", "tool"),
    [
        ("session-start", "", None),
        ("prompt-submit", "", None),
        ("pre-tool", "file", "Write"),
        ("pre-tool", "read", "Read"),
        ("pre-tool", "shell", "Bash"),
        ("pre-tool", "nh", "mcp__plugin_nh_nh__nh_add_cell"),
        ("pre-tool", "foreign-mcp", "mcp__datalayer__insert_cell"),
        ("pre-tool", "workflow", "Workflow"),
        ("post-tool", "workflow", "Workflow"),
        ("post-tool", "task-stop", "TaskStop"),
    ],
)
def test_hooks_import_only_stdlib_and_shared(sandbox, event, variant, tool) -> None:
    payload = sandbox.payload("Hook", prompt="go")
    if tool == "Workflow":  # an nh:qa-cell launch, so the handler does its whole job
        payload = sandbox.tool_payload(
            tool,
            {"name": "nh:qa-cell", "args": "go", "script": "export const meta = {}"},
            tool_response={
                "status": "async_launched",
                "taskId": "w1",
                "workflowName": "qa-cell",
                "runId": "wf_1",
                "transcriptDir": str(sandbox.tmp / "wf_1"),
            },
        )
    elif tool == "TaskStop":
        payload = sandbox.tool_payload(
            tool, {"task_id": "w1"}, tool_response={"task_id": "w1", "task_type": "local_workflow"}
        )
    elif tool:
        command = "echo {} > a.ipynb"
        payload = sandbox.tool_payload(
            tool, {"file_path": str(sandbox.project / "a.ipynb"), "command": command}
        )
    env = dict(sandbox.env, NH_PROJECT_DIR=str(sandbox.project))
    main = str(PLUGIN / "hooks" / "nh_hooks" / "main.py")
    proc = subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "-B", "-c", IMPORT_PROBE, main, event, variant],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        cwd=str(sandbox.project),
        timeout=30,
    )
    line = [x for x in proc.stderr.decode().splitlines() if x.startswith("NH-IMPORTS ")]
    assert line, proc.stderr.decode()
    assert json.loads(line[0][len("NH-IMPORTS ") :]) == []
    assert not (sandbox.nh / "logs" / "hooks.log").exists()


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@pytest.mark.slow
def test_hook_latency_p95(sandbox: Sandbox) -> None:
    """p95 < 150 ms per hook on the system 3.9, as users run before the runtime is ready.

    The interpreter is picked the way production picks it (a ready runtime venv, whose
    python is the system 3.9 here), not via NH_PYTHON. Each case must also give its real
    result, so a hook that fails fast can't pass. The default margin (x1.5, x2 when CI is
    set) absorbs a busy machine; NH_HOOK_TIME_FACTOR=1 checks the bare budget. Measured on
    an M-series Mac: 35-56 ms idle, 80-135 ms at load average 7.
    """
    factor = float(os.environ.get("NH_HOOK_TIME_FACTOR", "2" if os.environ.get("CI") else "1.5"))
    sandbox.ready_runtime()
    env = {k: v for k, v in sandbox.env.items() if k != "NH_PYTHON"}
    (sandbox.project / "harness.toml").write_text('[project]\ngoal = "g"\n')
    nb = str(sandbox.project / "notebooks" / "a.ipynb")
    run_tool = "mcp__plugin_nh_nh__nh_run"
    stamps = sandbox.nh / "state" / "stamps"

    def denied(run: HookRun) -> bool:
        return run.decision == "deny"

    def has_context(run: HookRun) -> bool:
        return bool(run.context)

    def silent(run: HookRun) -> bool:
        return run.stdout == ""

    def stamped(run: HookRun) -> bool:
        return run.stdout == "" and any(stamps.glob("*.json"))

    def recorded(run: HookRun) -> bool:
        return run.stdout == "" and (sandbox.nh / "state" / "workflows" / "sess-2.json").is_file()

    messages = itertools.count(1)
    launch = {"name": "nh:qa-cell", "args": "go"}
    launched = {
        "status": "async_launched",
        "taskId": "w1",
        "workflowName": "qa-cell",
        "runId": "wf_1",
        "transcriptDir": str(sandbox.tmp / "wf_1"),
    }

    cases = {
        "session-start": ("SessionStart", None, sandbox.payload("SessionStart"), has_context),
        "prompt-submit": (
            "UserPromptSubmit",
            None,
            # A new message each run: the same prompt id again is one typed mid-turn.
            lambda: sandbox.payload(
                "UserPromptSubmit",
                prompt_id=f"prompt-{next(messages)}",
                prompt="Load the sales data, then run the next 3 steps of the plan.",
            ),
            has_context,
        ),
        "file": ("PreToolUse", "Edit", sandbox.tool_payload("Edit", {"file_path": nb}), denied),
        "file-skip": (
            "PreToolUse",
            "Read",
            sandbox.tool_payload("Read", {"file_path": "/a.py"}),
            silent,
        ),
        "shell": (
            "PreToolUse",
            "Bash",
            sandbox.tool_payload("Bash", {"command": f"cp x {nb}"}),
            denied,
        ),
        "nh": (
            "PreToolUse",
            run_tool,
            sandbox.tool_payload(run_tool, {"cell_id": "nh-1"}),
            stamped,
        ),
        "foreign": (
            "PreToolUse",
            "mcp__d__insert_cell",
            sandbox.tool_payload("mcp__d__insert_cell", {}),
            denied,
        ),
        "workflow": ("PreToolUse", "Workflow", sandbox.tool_payload("Workflow", launch), silent),
        "post-tool": (
            "PostToolUse",
            "Workflow",
            sandbox.tool_payload(
                "Workflow",
                dict(launch, script=(PLUGIN / "workflows" / "qa-cell.js").read_text()),
                session_id="sess-2",  # the run it records doesn't concern "workflow" above
                hook_event_name="PostToolUse",
                tool_response=launched,
            ),
            recorded,
        ),
    }
    report = {}
    for name, (event, tool, payload, check) in cases.items():
        argv = command_line(sandbox.handlers(event, tool)[0])

        def run(argv=argv, payload=payload) -> HookRun:
            return sandbox.run_argv(argv, payload() if callable(payload) else payload, env)

        for _ in range(3):  # warm the bytecode cache and the OS file cache
            assert check(run()), name
        samples = [run().seconds for _ in range(20)]
        report[name] = round(p95(samples) * 1000)
    print("hook p95 ms:", report)
    assert not (sandbox.nh / "logs" / "hooks.log").exists()
    assert all(ms < 150 * factor for ms in report.values()), report
