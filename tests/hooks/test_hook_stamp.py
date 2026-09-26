"""PreToolUse stamp hook for nh write tools, and the foreign MCP guard."""

from __future__ import annotations

import json
import os
import time

import pytest
from hookenv import Sandbox, needs_system_python

from nh_gateway._shared.stamp_spec import stamp_filename, stamp_key

pytestmark = needs_system_python

ADD = "mcp__plugin_nh_nh__nh_add_cell"
ADD_INPUT = {
    "title": "Load raw data and check schema",
    "notes": ["Reads sales.csv with pandas.", "Shows dtypes and null counts per column."],
    "intent": "load the sales data",
    "code": "df = pd.read_csv(DATA_PATH)\ndf.shape",
}


def stamps(sandbox: Sandbox) -> list:
    folder = sandbox.nh / "state" / "stamps"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def test_stamp_is_written_without_a_decision(sandbox: Sandbox) -> None:
    before = time.time()
    run = sandbox.tool(ADD, ADD_INPUT, tool_use_id="toolu_add_1", prompt_id="prompt-7")
    assert run.returncode == 0
    assert run.stdout == ""
    key = stamp_key(ADD, ADD_INPUT)
    path = sandbox.nh / "state" / "stamps" / stamp_filename(key, "toolu_add_1")
    assert stamps(sandbox) == [path]
    body = json.loads(path.read_text())
    assert body == {
        "v": 1,
        "key": key,
        "tool": "nh_add_cell",
        "session_id": "sess-1",
        "prompt_id": "prompt-7",
        "agent_id": None,
        "agent_type": None,
        "tool_use_id": "toolu_add_1",
        "permission_mode": "default",
        "cc_pid": os.getpid(),  # the shim's $PPID is whoever spawned it: here, pytest
        "ts": body["ts"],
    }
    assert before <= body["ts"] <= time.time()


def test_defaults_and_nulls_give_the_same_key_as_the_gateway(sandbox: Sandbox) -> None:
    tool = "mcp__plugin_nh_nh__nh_run"
    sandbox.tool(
        tool, {"cell_id": "nh-1a2b3c4d5e", "mode": "run", "notebook": None}, tool_use_id="toolu_a"
    )
    sandbox.tool(tool, {"cell_id": "nh-1a2b3c4d5e"}, tool_use_id="toolu_b")
    keys = {path.name.split("--")[0] for path in stamps(sandbox)}
    assert keys == {stamp_key("nh_run", {"cell_id": "nh-1a2b3c4d5e"})}
    assert len(stamps(sandbox)) == 2


@pytest.mark.parametrize("tool", ["nh_edit_cell", "nh_run", "nh_undo"])
def test_every_write_tool_is_stamped(sandbox: Sandbox, tool: str) -> None:
    run = sandbox.tool(f"mcp__plugin_nh_nh__{tool}", {"cell_id": "nh-1a2b3c4d5e"})
    assert run.stdout == ""
    assert len(stamps(sandbox)) == 1


def test_subagent_calls_are_denied_and_not_stamped(sandbox: Sandbox) -> None:
    run = sandbox.tool(ADD, ADD_INPUT, agent_id="agent-42", agent_type="general-purpose")
    assert run.decision == "deny"
    assert "main conversation" in run.reason
    assert stamps(sandbox) == []


@pytest.mark.parametrize(
    "agent_type", ["nh:cell-qa", "general-purpose", "cell-writer", "nh:cell-writerx", None]
)
def test_only_the_cell_writer_may_write_from_a_subagent(
    sandbox: Sandbox, agent_type: str | None
) -> None:
    for tool in ("nh_add_cell", "nh_edit_cell", "nh_run", "nh_undo"):
        run = sandbox.tool(
            f"mcp__plugin_nh_nh__{tool}", {"cell_id": "nh-1"}, agent_id="a1", agent_type=agent_type
        )
        assert run.decision == "deny", (tool, agent_type)
        assert "main conversation" in run.reason
        assert "nh:cell-writer inside the nh:qa-cell workflow" in run.reason
    assert stamps(sandbox) == []


def test_cell_writer_calls_are_stamped_with_its_agent_fields(sandbox: Sandbox) -> None:
    writer = {"agent_id": "a79642cdfddbb0a18", "agent_type": "nh:cell-writer"}
    run = sandbox.tool(ADD, ADD_INPUT, tool_use_id="toolu_w1", prompt_id="prompt-7", **writer)
    assert (run.returncode, run.stdout) == (0, "")
    [path] = stamps(sandbox)
    body = json.loads(path.read_text())
    main = sandbox.tool(ADD, ADD_INPUT, tool_use_id="toolu_m1", prompt_id="prompt-7")
    assert main.stdout == ""
    main_body = json.loads(next(p for p in stamps(sandbox) if p != path).read_text())
    assert set(body) == set(main_body)  # the same fields; only the agent's are filled in
    assert {k: body[k] for k in ("agent_id", "agent_type", "prompt_id", "tool")} == dict(
        writer, prompt_id="prompt-7", tool="nh_add_cell"
    )
    assert (main_body["agent_id"], main_body["agent_type"]) == (None, None)


@pytest.mark.parametrize("tool", ["nh_edit_cell", "nh_run"])
def test_cell_writer_edits_and_runs_are_stamped(sandbox: Sandbox, tool: str) -> None:
    run = sandbox.tool(
        f"mcp__plugin_nh_nh__{tool}",
        {"cell_id": "nh-1a2b3c4d5e"},
        agent_id="a1",
        agent_type="nh:cell-writer",
    )
    assert run.stdout == ""
    assert len(stamps(sandbox)) == 1


def test_cell_writer_undo_is_denied(sandbox: Sandbox) -> None:
    run = sandbox.tool("mcp__plugin_nh_nh__nh_undo", {}, agent_id="a1", agent_type="nh:cell-writer")
    assert run.decision == "deny"
    assert "can't undo" in run.reason
    assert "Return this refusal to the workflow" in run.reason
    assert stamps(sandbox) == []


def test_missing_prompt_id_is_stamped_for_the_gateway_to_refuse(sandbox: Sandbox) -> None:
    run = sandbox.tool(ADD, ADD_INPUT, prompt_id=None)
    assert run.stdout == ""
    [path] = stamps(sandbox)
    assert json.loads(path.read_text())["prompt_id"] is None


def test_plan_mode_is_recorded(sandbox: Sandbox) -> None:
    sandbox.tool(ADD, ADD_INPUT, permission_mode="plan")
    [path] = stamps(sandbox)
    assert json.loads(path.read_text())["permission_mode"] == "plan"


def test_nh_inspect_runs_no_hook(sandbox: Sandbox) -> None:
    assert sandbox.handlers("PreToolUse", "mcp__plugin_nh_nh__nh_inspect") == []


def test_gateway_claims_the_stamp_the_hook_wrote(sandbox: Sandbox) -> None:
    stamps_mod = pytest.importorskip("nh_gateway.policy.stamps")
    from nh_gateway._shared.paths import Layout

    sandbox.tool(ADD, ADD_INPUT, tool_use_id="toolu_claim")
    claimed = stamps_mod.claim(
        Layout(sandbox.project), "nh_add_cell", dict(ADD_INPUT), my_cc_pid=os.getpid(), ttl=1800
    )
    assert claimed is not None


def test_foreign_mutating_tool_is_denied(sandbox: Sandbox) -> None:
    run = sandbox.tool("mcp__datalayer__insert_cell", {"cell_source": "x = 1"})
    assert run.decision == "deny"
    assert "insert_cell from datalayer" in run.reason
    assert "mcp__plugin_nh_nh__" in run.reason


def test_foreign_tool_names_its_server_from_mcp_server(sandbox: Sandbox) -> None:
    run = sandbox.tool(
        "mcp__plugin_datalayer_datalayer__execute_code",
        {"code": "1"},
        mcp_server={"name": "plugin:datalayer:datalayer", "source": "plugin"},
    )
    assert run.decision == "deny"
    assert "execute_code from plugin:datalayer:datalayer" in run.reason


@pytest.mark.parametrize(
    "tool", ["mcp__jupyter__list_notebooks", "mcp__jupyter__read_cell", "mcp__x__get_kernel_info"]
)
def test_foreign_read_only_tools_are_allowed(sandbox: Sandbox, tool: str) -> None:
    run = sandbox.tool(tool, {})
    assert run.returncode == 0
    assert run.stdout == ""


def test_foreign_guard_warn_mode_adds_context(sandbox: Sandbox) -> None:
    (sandbox.project / "harness.toml").write_text('version = 1\n[guard]\nforeign_mcp = "warn"\n')
    run = sandbox.tool("mcp__datalayer__insert_cell", {})
    assert run.decision is None
    assert run.output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "insert_cell from datalayer" in run.context


def test_foreign_guard_off(sandbox: Sandbox) -> None:
    (sandbox.project / "harness.toml").write_text('[guard]\nforeign_mcp = "off"  # trust it\n')
    run = sandbox.tool("mcp__datalayer__insert_cell", {})
    assert run.stdout == ""


@pytest.mark.parametrize(
    "tool",
    [
        # Review finding 47: names the old case-sensitive matcher missed.
        "mcp__jupyter__execute_ipython",
        "mcp__ide__executeCode",
        "mcp__jupyter__InsertCell",
        "mcp__datalayer__NotebookInsert",
        "mcp__x__IPythonExec",
        "mcp__sandbox__run_code",
        "mcp__x__RunPythonCode",
        "mcp__jupyter__connect",  # any mutating tool of a Jupyter server
        "mcp__x__update_cells",
    ],
)
def test_foreign_notebook_and_code_tools_are_denied_in_any_case(
    sandbox: Sandbox, tool: str
) -> None:
    payload = {"tool_name": tool, "tool_input": {}}  # nothing else names a notebook
    run = sandbox.run("PreToolUse", payload, tool_name=tool)
    assert run.decision == "deny", (tool, run.stderr)
    assert f"{tool.split('__', 2)[2]} from {tool.split('__')[1]}" in run.reason


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__github__create_issue",
        "mcp__postgres__execute_sql",
        "mcp__bigquery__execute_query",
        "mcp__docker__exec_container",
        "mcp__stripe__create_cancellation",
        "mcp__x__getNotebook",
        "mcp__jupyter__GetCells",
        "mcp__jupyter__list_files",
    ],
)
def test_other_foreign_tools_get_no_decision(sandbox: Sandbox, tool: str) -> None:
    run = sandbox.run("PreToolUse", {"tool_name": tool, "tool_input": {}}, tool_name=tool)
    assert (run.returncode, run.stdout) == (0, ""), tool
