"""nh:qa-cell launches: the PreToolUse launch guard and the PostToolUse run record, through
the real hooks (and the fakes the gateway tests drive them with)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from hookenv import PLUGIN, HookRun, Sandbox, needs_system_python

from nh_gateway._shared import turn_record
from nh_gateway._shared.paths import Layout
from tests.fakes.turns import Turns

pytestmark = needs_system_python

RUN = "wf_b07c10f6-05a"
TASK = "w2g9v11xz"


@pytest.fixture
def turns(sandbox: Sandbox) -> Turns:
    return Turns(sandbox.project, sandbox.data)


def own_script() -> str:
    return (PLUGIN / "workflows" / "qa-cell.js").read_text()


def runs(sandbox: Sandbox, session_id: str = "sess-1") -> list[dict[str, Any]]:
    return turn_record.find_runs(Layout(sandbox.project), session_id)


def launched(sandbox: Sandbox, **fields: Any) -> dict[str, Any]:
    """A Workflow tool_response for a background launch, as Claude Code 2.1.282 gives it."""
    base = {
        "status": "async_launched",
        "taskId": TASK,
        "taskType": "local_workflow",
        "workflowName": "qa-cell",
        "runId": RUN,
        "summary": "Write this message's one notebook cell, then live-check it",
        "transcriptDir": str(sandbox.tmp / "transcripts" / RUN),
        "scriptPath": str(sandbox.tmp / "scripts" / f"qa-cell-{RUN}.js"),
    }
    base.update(fields)
    return base


def post(sandbox: Sandbox, tool_input: dict[str, Any], response: Any, **fields: Any) -> HookRun:
    payload = sandbox.payload(
        "PostToolUse",
        tool_name="Workflow",
        tool_use_id="toolu_launch",
        tool_input=tool_input,
        tool_response=response,
        **fields,
    )
    return sandbox.run("PostToolUse", payload, tool_name="Workflow")


def launch(sandbox: Sandbox, prompt_id: str = "p1", name: str = "nh:qa-cell", **fields) -> HookRun:
    payload = sandbox.tool_payload(
        "Workflow", {"name": name, "args": "load sales.csv"}, prompt_id=prompt_id, **fields
    )
    return sandbox.run("PreToolUse", payload, tool_name="Workflow")


# --- PostToolUse: the run record -------------------------------------------------------------


def test_a_launch_by_name_is_recorded_as_nhs_own_run(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    before = time.time()
    folder = turns.workflow_launched("p1", RUN, tool_use_id="toolu_L1", task_id=TASK)
    assert folder.is_dir() and folder.name == RUN
    [run] = runs(sandbox)
    assert before <= run.pop("ts") <= time.time()
    assert run == {
        "run_id": RUN,
        "task_id": TASK,
        "tool_use_id": "toolu_L1",
        "name": "qa-cell",
        "launched_by": "name",
        "transcript_dir": str(folder),
        "turn_id": "p1",
        "prompt_id": "p1",
        "done_ts": None,
        "status": None,
    }
    layout = Layout(sandbox.project)
    assert [r["run_id"] for r in turn_record.open_runs(layout, "sess-1", "p1")] == [RUN]
    turns.notification("note-1", tool_use_id="toolu_L1", task_id=TASK)
    [run] = runs(sandbox)
    assert (run["status"], turn_record.open_runs(layout, "sess-1", "p1")) == ("completed", [])


def test_a_launch_from_a_notification_belongs_to_the_human_turn(
    sandbox: Sandbox, turns: Turns
) -> None:
    turns.prompt("p1")
    turns.notification("note-1", task_id="bash-1", tool_use_id="toolu_bash")
    turns.workflow_launched("note-1")
    [run] = runs(sandbox)
    assert (run["turn_id"], run["prompt_id"]) == ("p1", "note-1")


def test_a_launch_without_an_open_turn_has_none(sandbox: Sandbox, turns: Turns) -> None:
    turns.notification("note-1")  # an orphan: no human message is open
    turns.workflow_launched("note-1")
    assert runs(sandbox)[0]["turn_id"] is None
    post(sandbox, {"name": "nh:qa-cell"}, launched(sandbox, runId="wf_2"), prompt_id=None)
    assert [(r["run_id"], r["turn_id"]) for r in runs(sandbox)][-1] == ("wf_2", None)


@pytest.mark.parametrize(
    ("launched_by", "recorded_as"),
    [("name", "name"), ("script", "script"), ("scriptPath", "scriptPath")],
)
def test_how_it_was_launched_is_recorded(
    sandbox: Sandbox, turns: Turns, launched_by: str, recorded_as: str
) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", launched_by=launched_by)
    [run] = runs(sandbox)
    assert run["launched_by"] == recorded_as
    assert turn_record.is_own_run(run) == (recorded_as == "name")


def test_own_script_ignores_line_ends_and_outer_whitespace_only(sandbox: Sandbox) -> None:
    script = own_script()
    cases = {
        "wf_crlf": ("\n  " + script.replace("\n", "\r\n") + "\n\n", "name"),
        "wf_edit": (script + "\n// edited\n", "script"),
        "wf_inner": (script.replace("\n", "\n\n", 1), "script"),
        "wf_empty": ("", "script"),
    }
    for run_id, (text, _) in cases.items():
        tool_input = {"name": "nh:qa-cell", "args": "go", "script": text}
        assert post(sandbox, tool_input, launched(sandbox, runId=run_id)).stdout == ""
    assert {r["run_id"]: r["launched_by"] for r in runs(sandbox)} == {
        run_id: expected for run_id, (_, expected) in cases.items()
    }


@pytest.mark.parametrize(
    ("tool_input", "recorded_as"),
    [
        ({"name": "nh:qa-cell", "script": "own"}, "name"),
        ({"script": "own"}, "name"),  # inline, no name
        ({"scriptPath": "/x/saved.js", "script": "own"}, "name"),
        ({"name": "qa-cell", "scriptPath": "/x/saved.js", "script": "own"}, "name"),
        ({"name": "qa-cell", "script": "own"}, "script"),  # a copy among the user's workflows
        ({"name": "code-review", "script": "own"}, "script"),
        ({"name": "nh:qa-cell"}, "name"),
        ({"name": "qa-cell"}, "script"),
        ({"name": "qa-cell", "scriptPath": "/x/saved.js"}, "scriptPath"),
    ],
)
def test_only_launches_the_guard_checks_are_nhs_own(
    sandbox: Sandbox, tool_input: dict, recorded_as: str
) -> None:
    if tool_input.get("script") == "own":
        tool_input = dict(tool_input, script=own_script())
    post(sandbox, dict(tool_input, args="go"), launched(sandbox))
    assert [r["launched_by"] for r in runs(sandbox)] == [recorded_as]


def test_a_resume_is_nhs_own_only_when_the_run_was(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    post(sandbox, {"name": "nh:qa-cell", "script": own_script()}, launched(sandbox))
    post(sandbox, {"name": "qa-cell", "script": own_script()}, launched(sandbox, runId="wf_2"))
    for run_id in (RUN, "wf_2", "wf_unknown"):
        resumed = {"resumeFromRunId": run_id, "script": own_script()}
        post(sandbox, resumed, launched(sandbox, runId=run_id))
    assert {r["run_id"]: r["launched_by"] for r in runs(sandbox)} == {
        RUN: "name",
        "wf_2": "script",
        "wf_unknown": "script",
    }


def test_a_json_text_tool_response_is_read(sandbox: Sandbox) -> None:
    tool_input = {"name": "nh:qa-cell", "args": "go", "script": own_script()}
    post(sandbox, tool_input, json.dumps(launched(sandbox)))
    assert [(r["run_id"], r["launched_by"]) for r in runs(sandbox)] == [(RUN, "name")]


@pytest.mark.parametrize(
    ("tool_input", "response", "fields"),
    [
        ({"name": "code-review"}, {"workflowName": "code-review"}, {}),
        ({"name": "nh:qa-cell"}, {"status": "completed"}, {}),
        ({"name": "nh:qa-cell"}, {"status": "error"}, {}),
        ({"name": "nh:qa-cell"}, {"runId": None}, {}),
        ({"name": "nh:qa-cell"}, {}, {"agent_id": "a1", "agent_type": "general-purpose"}),
        ({"name": "nh:qa-cell"}, {}, {"session_id": None}),
        ({"name": "nh:qa-cell"}, "not json", {}),
        ({"name": "nh:qa-cell"}, ["async_launched"], {}),
    ],
)
def test_other_launches_and_results_are_not_recorded(
    sandbox: Sandbox, tool_input: dict, response: Any, fields: dict
) -> None:
    if isinstance(response, dict):
        response = {k: v for k, v in launched(sandbox, **response).items() if v is not None}
    run = post(sandbox, tool_input, response, **fields)
    assert (run.returncode, run.stdout) == (0, "")
    assert not (sandbox.nh / "state" / "workflows").exists()
    assert not (sandbox.nh / "logs" / "hooks.log").exists()


def test_the_fakes_bind_a_writer_to_its_run(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN)
    turns.workflow_launched("p1", "wf_other", tool_use_id="toolu_other")
    out = turns.writer_stamp("a79642cdfddbb0a18", RUN, "nh_run", {"cell_id": "nh-1"}, "p1")
    assert out is None
    layout = Layout(sandbox.project)
    run = turn_record.run_for_agent(layout, "sess-1", "a79642cdfddbb0a18", wait_s=0)
    assert run is not None and run["run_id"] == RUN
    [stamp] = [json.loads(p.read_text()) for p in layout.stamps.glob("*.json")]
    assert (stamp["agent_id"], stamp["agent_type"]) == ("a79642cdfddbb0a18", "nh:cell-writer")
    turns.writer_meta("a-qa", RUN, agent_type="nh:cell-qa")
    assert turn_record.run_for_agent(layout, "sess-1", "a-qa", wait_s=0) is None


# --- PostToolUse: TaskStop ------------------------------------------------------------------


def stop(sandbox: Sandbox, tool_input: dict[str, Any], response: Any, **fields: Any) -> HookRun:
    payload = sandbox.payload(
        "PostToolUse",
        tool_name=fields.pop("tool_name", "TaskStop"),
        tool_use_id="toolu_stop",
        tool_input=tool_input,
        tool_response=response,
        **fields,
    )
    return sandbox.run("PostToolUse", payload, tool_name="TaskStop")


def stopped(task_id: str) -> dict[str, Any]:
    """A TaskStop tool_response, as Claude Code 2.1.282 gives it."""
    return {
        "message": f"Successfully stopped task: {task_id} (Write and check the cell)",
        "task_id": task_id,
        "task_type": "local_workflow",
        "command": "Write and check the cell",
    }


def test_a_task_stop_marks_the_run_done(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN, task_id=TASK)
    turns.workflow_launched("p1", "wf_other", tool_use_id="toolu_2", task_id="t-other")
    layout = Layout(sandbox.project)
    assert len(turn_record.open_runs(layout, "sess-1", "p1")) == 2
    before = time.time()
    turns.task_stopped("p1", TASK)
    by_id = {r["run_id"]: r for r in runs(sandbox)}
    assert by_id[RUN]["status"] == "killed" and before <= by_id[RUN]["done_ts"] <= time.time()
    # The gateway reads this status as "stopped, no report" (design §6.4, C5d2).
    assert by_id[RUN]["status"] == turn_record.STOPPED
    assert by_id["wf_other"]["done_ts"] is None
    assert [r["run_id"] for r in turn_record.open_runs(layout, "sess-1", "p1")] == ["wf_other"]
    assert launch(sandbox, prompt_id="p1").decision == "deny"  # the message still had its run
    turns.task_stopped("p1", "t-other", agent_id="a1")  # whoever stops it, it is gone
    assert turn_record.open_runs(layout, "sess-1", "p1") == []


@pytest.mark.parametrize(
    ("tool_input", "response"),
    [
        ({"task_id": "ignored"}, json.dumps(stopped(TASK))),
        ({"task_id": TASK}, "Stopped."),
        ({"shell_id": TASK}, {"message": "Stopped."}),
    ],
    ids=["json-text", "input-task-id", "shell-id"],
)
def test_a_task_stop_names_its_task(
    sandbox: Sandbox, turns: Turns, tool_input: dict, response: Any
) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN, task_id=TASK)
    run = stop(sandbox, tool_input, response, prompt_id="p1")
    assert (run.returncode, run.stdout) == (0, "")
    assert runs(sandbox)[0]["status"] == "killed"


@pytest.mark.parametrize(
    ("tool_input", "response", "fields"),
    [
        ({"task_id": "bash-1"}, stopped("bash-1"), {}),
        ({}, {"message": "No task"}, {}),
        ({"task_id": TASK}, stopped(TASK), {"tool_name": "TaskOutput"}),
        ({"task_id": TASK}, stopped(TASK), {"session_id": "sess-2"}),
        ({"task_id": TASK}, stopped(TASK), {"session_id": None}),
    ],
)
def test_other_stops_change_no_run(
    sandbox: Sandbox, turns: Turns, tool_input: dict, response: Any, fields: dict
) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN, task_id=TASK)
    run = stop(sandbox, tool_input, response, prompt_id="p1", **fields)
    assert (run.returncode, run.stdout) == (0, "")
    assert runs(sandbox)[0]["done_ts"] is None


# --- PreToolUse: the launch guard ------------------------------------------------------------


def test_the_first_launch_of_a_message_passes(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    run = launch(sandbox, prompt_id="p1")
    assert (run.returncode, run.stdout) == (0, "")


def test_approve_before_run_denies_the_launch(sandbox: Sandbox, turns: Turns) -> None:
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p1")
    run = launch(sandbox)
    assert run.decision == "deny"
    assert "approve_before_run" in run.reason and "Write the cell yourself" in run.reason
    payload = sandbox.tool_payload("Workflow", {"name": "nh:qa-cell"}, prompt_id="p1")
    headless = sandbox.run(
        "PreToolUse", payload, tool_name="Workflow", env=dict(sandbox.env, NH_HEADLESS="1")
    )
    assert headless.stdout == ""  # NH_HEADLESS=1 turns approval off, as in the gateway


def test_a_second_launch_in_one_message_is_denied(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN, tool_use_id="toolu_L1")
    again = launch(sandbox, prompt_id="p1")
    assert again.decision == "deny"
    assert "already had its nh:qa-cell run" in again.reason
    # no leave to write in place of nh's question (design §6.4, C5d)
    assert "unless the report says needs_approval: then ask its question and stop" in again.reason
    turns.notification("note-1", tool_use_id="toolu_L1", task_id=TASK)  # the run reported
    assert launch(sandbox, prompt_id="note-1").decision == "deny"  # an alias of p1: still p1
    assert launch(sandbox, prompt_id="p1").decision == "deny"
    turns.prompt("p2")
    assert launch(sandbox, prompt_id="p2").stdout == ""
    assert launch(sandbox, prompt_id="p2", session_id="sess-2").stdout == ""


# Design §6.2's text, pinned: the model reads it in place of a launch.
WORKFLOW_MODE_REASON = (
    "nh: this user message only asks to explain, plan or ask, so no cell is written in it. "
    "Don't launch nh:qa-cell: answer in chat (a numbered walkthrough, the numbered plan or the "
    "one question) and write nothing."
)


def assert_mode_denied(run: HookRun) -> None:
    assert (run.returncode, run.decision, run.reason) == (0, "deny", WORKFLOW_MODE_REASON), (
        run.stdout
    )


@pytest.mark.parametrize(
    "message",
    ["explain the load cell", "/nh:explain [1]", "/nh:plan a churn model", "run the next 3"],
)
def test_an_explain_plan_or_ask_message_denies_the_launch(
    sandbox: Sandbox, turns: Turns, message: str
) -> None:
    """Design §6.2: advisory, the gateway's E109 refuses the writer anyway."""
    turns.prompt("p1", text=message)
    assert_mode_denied(launch(sandbox, prompt_id="p1"))
    turns.notification("note-1")  # an alias of p1: still p1's mode
    assert_mode_denied(launch(sandbox, prompt_id="note-1"))
    turns.prompt("p2", text="go")  # the next message has no mode
    assert launch(sandbox, prompt_id="p2").stdout == ""


def test_a_mode_typed_mid_message_denies_the_launch(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1", text="drop the rows with missing price")
    assert launch(sandbox, prompt_id="p1").stdout == ""
    turns.prompt("p1", text="explain what you will do first")  # absorbed into p1
    assert_mode_denied(launch(sandbox, prompt_id="p1"))


def test_the_mode_is_checked_before_one_run_per_message(sandbox: Sandbox, turns: Turns) -> None:
    """Deny order (design §6.2): the mode, then one run per message. A message that had its
    run and then turned explain hears "answer in chat", not "reply from its report"."""
    turns.prompt("p1", text="drop the rows with missing price")
    turns.workflow_launched("p1")
    again = launch(sandbox, prompt_id="p1")
    assert again.decision == "deny" and "already had its nh:qa-cell run" in again.reason
    turns.prompt("p1", text="explain what it is doing")  # absorbed into p1
    assert_mode_denied(launch(sandbox, prompt_id="p1"))


def test_a_subagent_is_denied_before_the_mode(sandbox: Sandbox, turns: Turns) -> None:
    """Deny order (design §6.2): a subagent first, so it hears why it may never launch."""
    turns.prompt("p1", text="explain the load cell")
    run = launch(sandbox, prompt_id="p1", agent_id="a1", agent_type="general-purpose")
    assert run.decision == "deny" and "only the main conversation" in run.reason, run.stdout
    assert run.reason != WORKFLOW_MODE_REASON


def test_approve_before_run_is_checked_before_the_mode(sandbox: Sandbox, turns: Turns) -> None:
    """Deny order (design §6.2): approve_before_run before the mode, as the setting holds for
    every message."""
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p1", text="explain the load cell")
    run = launch(sandbox, prompt_id="p1")
    assert run.decision == "deny" and "approve_before_run" in run.reason, run.stdout
    assert run.reason != WORKFLOW_MODE_REASON


def test_a_message_without_a_mode_launches_as_before(sandbox: Sandbox, turns: Turns) -> None:
    for prompt_id, message in (("p1", "yes"), ("p2", "explain and fix the parse")):
        turns.prompt(prompt_id, text=message)
        assert launch(sandbox, prompt_id=prompt_id).stdout == "", message


def test_other_workflows_pass_in_an_explain_message(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1", text="explain the load cell")
    payload = sandbox.tool_payload("Workflow", {"name": "code-review", "args": "x"}, prompt_id="p1")
    run = sandbox.run("PreToolUse", payload, tool_name="Workflow")
    assert (run.returncode, run.stdout) == (0, "")


def test_only_nhs_own_runs_use_up_the_message(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", "wf_inline", launched_by="script")
    turns.workflow_launched("p1", "wf_path", launched_by="scriptPath", tool_use_id="toolu_2")
    assert launch(sandbox, prompt_id="p1").stdout == ""


def test_other_workflows_get_no_decision(sandbox: Sandbox, turns: Turns) -> None:
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p1")
    turns.workflow_launched("p1")
    for tool_input in (
        {"name": "code-review", "args": "the qa-cell change"},
        {"scriptPath": "/tmp/qa-cell.js", "args": "go"},
        {"script": "export const meta = { name: 'qa-cell' }", "args": "go"},
    ):
        payload = sandbox.tool_payload("Workflow", tool_input, prompt_id="p1")
        run = sandbox.run("PreToolUse", payload, tool_name="Workflow")
        assert (run.returncode, run.stdout) == (0, ""), tool_input


def test_a_subagent_cannot_launch_it(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    run = launch(sandbox, agent_id="a1", agent_type="general-purpose")
    assert run.decision == "deny" and "only the main conversation" in run.reason


def test_a_bare_qa_cell_is_the_users_own_workflow(sandbox: Sandbox, turns: Turns) -> None:
    """Plugin workflows are namespaced: ``qa-cell`` names a project or user workflow."""
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN)
    for fields in ({}, {"agent_id": "a1", "agent_type": "general-purpose"}):
        run = launch(sandbox, prompt_id="p1", name="qa-cell", **fields)
        assert (run.returncode, run.stdout) == (0, ""), fields


# --- an approved batch: one run per step (design §6.3, C6b) ------------------------------------

# Design §6.3's texts, pinned: the model reads them in place of a launch.
WORKFLOW_BATCH_OPEN_REASON = (
    "nh: the approved batch's last step is still being written and checked; wait for its report, "
    "then launch the next step's run."
)


def batch_done(k: int) -> str:
    return (
        f"nh: this user message's approved batch already had its {k} nh:qa-cell runs, one per "
        "step. Reply from their reports; the rest of the plan waits for the user's next message."
    )


def assert_denied(run: HookRun, reason: str) -> None:
    assert (run.returncode, run.decision, run.reason) == (0, "deny", reason), run.stdout


def step_run(turns: Turns, prompt_id: str, k: int) -> None:
    """Step k's run is launched (PostToolUse records it) from ``prompt_id``."""
    turns.workflow_launched(prompt_id, f"wf_{k}", tool_use_id=f"toolu_{k}", task_id=f"task-{k}")


def step_reported(turns: Turns, k: int) -> str:
    """Step k's run reports: its notification, an alias of the message. Returns its id."""
    turns.notification(f"note-{k}", tool_use_id=f"toolu_{k}", task_id=f"task-{k}")
    return f"note-{k}"


def test_a_batch_launches_its_k_runs_one_after_another(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p2", text="yes")
    at = "p2"
    for k in (1, 2, 3):
        assert launch(sandbox, prompt_id=at).stdout == "", k  # step k's launch
        step_run(turns, at, k)
        # The message and the alias it replies in; at step 3 the count comes first.
        held = WORKFLOW_BATCH_OPEN_REASON if k < 3 else batch_done(3)
        for prompt_id in ("p2", at):
            assert_denied(launch(sandbox, prompt_id=prompt_id), held)
        at = step_reported(turns, k)  # the report arrives: the reply to it launches the next
    for prompt_id in ("p2", at):
        assert_denied(launch(sandbox, prompt_id=prompt_id), batch_done(3))
    turns.prompt("p3", text="go")  # the next message: one run, as always
    assert launch(sandbox, prompt_id="p3").stdout == ""
    step_run(turns, "p3", 9)
    assert "already had its nh:qa-cell run" in launch(sandbox, prompt_id="p3").reason


def test_the_count_comes_before_the_open_run(sandbox: Sandbox, turns: Turns) -> None:
    """The last step's run still open: the batch has had its k runs, so no launch is offered
    back ("wait for its report, then launch the next")."""
    turns.prompt("p1", text="run the next 2")
    turns.prompt("p2", text="yes")
    step_run(turns, "p2", 1)
    step_reported(turns, 1)
    step_run(turns, "p2", 2)  # open
    assert_denied(launch(sandbox, prompt_id="p2"), batch_done(2))


@pytest.mark.parametrize("answer", ["no", "go on", "what would that change?"])
def test_any_other_answer_gets_one_run(sandbox: Sandbox, turns: Turns, answer: str) -> None:
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p2", text=answer)
    assert launch(sandbox, prompt_id="p2").stdout == ""
    step_run(turns, "p2", 1)
    note = step_reported(turns, 1)
    again = launch(sandbox, prompt_id=note)
    assert again.decision == "deny" and "already had its nh:qa-cell run" in again.reason


def test_a_yes_two_messages_later_or_typed_mid_turn_gets_one_run(
    sandbox: Sandbox, turns: Turns
) -> None:
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p1", text="yes")  # absorbed: never an answer, and p1 is still the ask
    assert launch(sandbox, prompt_id="p1").reason == WORKFLOW_MODE_REASON
    turns.prompt("p2", text="no")
    turns.prompt("p3", text="yes")
    step_run(turns, "p3", 1)
    step_reported(turns, 1)
    assert "already had its nh:qa-cell run" in launch(sandbox, prompt_id="p3").reason


def test_headless_gets_one_run(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p2", text="yes")
    step_run(turns, "p2", 1)
    note = step_reported(turns, 1)
    payload = sandbox.tool_payload("Workflow", {"name": "nh:qa-cell"}, prompt_id=note)
    headless = sandbox.run(
        "PreToolUse", payload, tool_name="Workflow", env=dict(sandbox.env, NH_HEADLESS="1")
    )
    assert headless.decision == "deny" and "already had its nh:qa-cell run" in headless.reason
    assert launch(sandbox, prompt_id=note).stdout == ""  # interactive: step 2's run


@pytest.mark.parametrize(
    ("toml", "k"),
    [
        ("[turn]\nmax_batch = 2\n", 2),
        ("[turn]\nmax_batch = 0\n", 5),
        ("[turn]\nmax_batch = 1\n", 5),  # a batch is two or more steps
        ("[turn]\nmax_batch = 21\n", 5),  # past the run registry's 20 runs
    ],
)
def test_k_is_capped_by_max_batch_from_harness_toml(
    sandbox: Sandbox, turns: Turns, toml: str, k: int
) -> None:
    (sandbox.project / "harness.toml").write_text(toml)
    turns.prompt("p1", text="run the next 7")
    turns.prompt("p2", text="yes")
    for step in range(1, k + 1):
        assert launch(sandbox, prompt_id="p2").stdout == "", step
        step_run(turns, "p2", step)
        step_reported(turns, step)
    assert_denied(launch(sandbox, prompt_id="p2"), batch_done(k))


def test_the_last_of_twenty_runs_still_counts(sandbox: Sandbox, turns: Turns) -> None:
    """``max_batch``'s top, 20, is the run registry's size (``turn_record.MAX_RUNS``): every run
    of the message is still there to count, so the 21st launch is DONE (design §6.3; with no
    bound a k past 20 could never be reached and the guard let launches through)."""
    assert turn_record.MAX_RUNS == 20 == turn_record.MAX_BATCH_RANGE[1]
    (sandbox.project / "harness.toml").write_text("[turn]\nmax_batch = 20\n")
    turns.prompt("p0")
    for k in range(5):  # an earlier message's runs, older than the batch's
        turns.workflow_launched("p0", f"wf_old{k}", tool_use_id=f"toolu_old{k}")
    turns.prompt("p1", text="run the next 25")
    turns.prompt("p2", text="yes")
    at = "p2"
    for k in range(1, 21):
        assert launch(sandbox, prompt_id=at).stdout == "", k
        step_run(turns, at, k)
        at = step_reported(turns, k)
    assert len(runs(sandbox)) == 20
    assert_denied(launch(sandbox, prompt_id=at), batch_done(20))


def test_a_run_past_its_hour_no_longer_holds_the_batch(sandbox: Sandbox, turns: Turns) -> None:
    """Open is ``turn_record.run_open``: a step's run that never reported stops holding the
    next launch an hour after it was launched (as the gateway's E107 and E108)."""
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p2", text="yes")
    step_run(turns, "p2", 1)
    assert_denied(launch(sandbox, prompt_id="p2"), WORKFLOW_BATCH_OPEN_REASON)
    path = Layout(sandbox.project).workflow_file(turns.session_id)
    data = json.loads(path.read_text())
    for entry in data["runs"]:
        entry["ts"] = time.time() - turn_record.RUN_OPEN_TTL_S - 1
    path.write_text(json.dumps(data))
    assert launch(sandbox, prompt_id="p2").stdout == ""


def test_an_earlier_messages_report_gets_no_batch(sandbox: Sandbox, turns: Turns) -> None:
    """The launch's canonical turn decides, not the record's: a launch sent with an earlier
    message's alias id belongs to that message, which had its one run."""
    turns.prompt("p0", text="load the sales data")
    assert launch(sandbox, prompt_id="p0").stdout == ""
    turns.workflow_launched("p0", "wf_0", tool_use_id="toolu_0", task_id="task-0")
    turns.notification("note-0", tool_use_id="toolu_0", task_id="task-0")  # p0's report
    turns.prompt("p1", text="run the next 3")
    turns.prompt("p2", text="yes")
    record = turn_record.read(Layout(sandbox.project), turns.session_id)
    assert turn_record.canonical(record, "note-0") == "p0"
    late = launch(sandbox, prompt_id="note-0")
    assert late.decision == "deny" and "already had its nh:qa-cell run" in late.reason
    assert launch(sandbox, prompt_id="p2").stdout == ""  # the yes message's own: the batch


def test_a_stopped_run_is_one_of_the_k_and_no_longer_open(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1", text="run the next 2")
    turns.prompt("p2", text="yes")
    step_run(turns, "p2", 1)
    turns.task_stopped("p2", "task-1")  # TaskStop: no report, but the run is done
    assert launch(sandbox, prompt_id="p2").stdout == ""
    step_run(turns, "p2", 2)
    turns.task_stopped("p2", "task-2")
    assert_denied(launch(sandbox, prompt_id="p2"), batch_done(2))


def test_only_this_messages_own_runs_count(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p0")
    turns.workflow_launched("p0", "wf_old", tool_use_id="toolu_old")  # an earlier message's, open
    turns.prompt("p1", text="run the next 2")
    turns.prompt("p2", text="yes")
    turns.workflow_launched("p2", "wf_inline", launched_by="script", tool_use_id="toolu_x")
    assert launch(sandbox, prompt_id="p2").stdout == ""


def test_the_deny_order_in_a_batch(sandbox: Sandbox, turns: Turns) -> None:
    """Design §6.3: a subagent, approve_before_run, the mode, then the run count."""
    turns.prompt("p1", text="run the next 2")
    turns.prompt("p2", text="yes")
    step_run(turns, "p2", 1)  # open: the count would deny with OPEN
    run = launch(sandbox, prompt_id="p2", agent_id="a1", agent_type="general-purpose")
    assert run.decision == "deny" and "only the main conversation" in run.reason
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    assert "approve_before_run" in launch(sandbox, prompt_id="p2").reason
    (sandbox.project / "harness.toml").unlink()
    assert_denied(launch(sandbox, prompt_id="p2"), WORKFLOW_BATCH_OPEN_REASON)
    turns.prompt("p2", text="explain what step 1 does")  # absorbed: mode explain
    assert_denied(launch(sandbox, prompt_id="p2"), WORKFLOW_MODE_REASON)


def own_launches(sandbox: Sandbox) -> list[dict[str, Any]]:
    """nh's script launched without its name: inline, by its path, by a renamed copy's path
    (CRLF line ends too) and by a path relative to the session's cwd."""
    copy = sandbox.tmp / "saved" / "wf-7f3a.js"
    copy.parent.mkdir(exist_ok=True)
    copy.write_bytes(own_script().replace("\n", "\r\n").encode())
    relative = sandbox.project / "flows" / "mine.js"
    relative.parent.mkdir(exist_ok=True)
    relative.write_text(own_script())
    return [
        {"script": own_script(), "args": "go"},
        {"scriptPath": str(PLUGIN / "workflows" / "qa-cell.js"), "args": "go"},
        {"scriptPath": str(copy), "args": "go"},
        {"name": "code-review", "scriptPath": "flows/mine.js", "args": "go"},
    ]


def test_nhs_script_by_path_or_inline_is_guarded(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    forms = own_launches(sandbox)
    for tool_input in forms:
        run = sandbox.tool("Workflow", tool_input, prompt_id="p1", agent_id="a1", agent_type="x")
        assert run.decision == "deny" and "only the main conversation" in run.reason, tool_input
        run = sandbox.tool("Workflow", tool_input, prompt_id="p1")
        assert run.stdout == "", tool_input  # the first launch of the message passes
    turns.workflow_launched("p1", RUN)
    for tool_input in forms:
        run = sandbox.tool("Workflow", tool_input, prompt_id="p1")
        assert run.decision == "deny" and "already had its nh:qa-cell run" in run.reason
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p2")
    for tool_input in forms:
        run = sandbox.tool("Workflow", tool_input, prompt_id="p2")
        assert run.decision == "deny" and "approve_before_run" in run.reason, tool_input


def test_other_scripts_by_path_get_no_decision(sandbox: Sandbox, turns: Turns) -> None:
    (sandbox.project / "harness.toml").write_text("[approval]\napprove_before_run = true\n")
    turns.prompt("p1")
    edited = sandbox.tmp / "qa-cell.js"
    edited.write_text(own_script() + "\n// edited\n")
    big = sandbox.tmp / "big.js"
    big.write_text(own_script() + " " * (1 << 20))
    for path in (str(edited), str(big), str(sandbox.tmp), "missing/qa-cell.js"):
        run = sandbox.tool("Workflow", {"scriptPath": path}, prompt_id="p1")
        assert (run.returncode, run.stdout) == (0, ""), path


def test_a_resume_of_nhs_run_is_guarded(sandbox: Sandbox, turns: Turns) -> None:
    turns.prompt("p1")
    turns.workflow_launched("p1", RUN, tool_use_id="toolu_L1")
    turns.workflow_launched("p1", "wf_inline", launched_by="script", tool_use_id="toolu_2")

    def resume(run_id: str, prompt_id: str, **fields: Any) -> HookRun:
        return sandbox.tool("Workflow", {"resumeFromRunId": run_id}, prompt_id=prompt_id, **fields)

    run = resume(RUN, "p1")
    assert run.decision == "deny" and "already had its nh:qa-cell run" in run.reason
    assert resume(RUN, "p1", agent_id="a1", agent_type="general-purpose").decision == "deny"
    assert resume("wf_inline", "p1").stdout == ""  # not nh's run: the guard leaves it alone
    assert resume("wf_unknown", "p1").stdout == ""
    turns.prompt("p2")
    assert resume(RUN, "p2").stdout == ""
    assert resume(RUN, "p2", session_id="sess-2").stdout == ""  # another session's run
