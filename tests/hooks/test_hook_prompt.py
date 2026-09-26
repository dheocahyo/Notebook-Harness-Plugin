"""UserPromptSubmit: the turn record and the per-message reminder."""

from __future__ import annotations

import json
import time

from hookenv import Sandbox, needs_system_python

from tests.fakes.turns import notification_prompt

pytestmark = needs_system_python


def submit(sandbox: Sandbox, **fields):
    return sandbox.run(
        "UserPromptSubmit", sandbox.payload("UserPromptSubmit", prompt="go", **fields)
    )


def write_state(sandbox: Sandbox, name: str, data: dict) -> None:
    path = sandbox.nh / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def last_cell(**fields) -> dict:
    base = {
        "v": 1,
        "session_id": "sess-1",
        "notebook": "notebooks/01_eda.ipynb",
        "cell_id": "nh-4f2a91c07b",
        "title": "Drop rows with missing price",
        "exec": 14,
        "status": "ok",
        "turn_id": "prompt-0",
        "retries_left": 2,
        "finished_at": time.time() - 192,
    }
    base.update(fields)
    return base


def test_turn_record_is_written(sandbox: Sandbox) -> None:
    before = time.time()
    run = submit(sandbox, session_id="0c9e-uuid", prompt_id="prompt-9")
    assert run.returncode == 0
    record = json.loads((sandbox.nh / "state" / "turns" / "0c9e-uuid.json").read_text())
    assert before <= record.pop("ts") <= time.time()
    assert record == {
        "v": 2,
        "session_id": "0c9e-uuid",
        "prompt_id": "prompt-9",
        "turn_id": "prompt-9",
        "aliases": [],
        "human": True,
        "alias_ts": None,
        "earlier": {},
    }


def test_session_id_is_made_file_safe(sandbox: Sandbox) -> None:
    submit(sandbox, session_id="../../etc/passwd")
    names = [path.name for path in (sandbox.nh / "state" / "turns").iterdir()]
    assert names == [".._.._etc_passwd.json"]


def test_reminder_without_history(sandbox: Sandbox) -> None:
    run = submit(sandbox)
    assert run.output == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "[nh] One new cell this turn; go/next/y = do the proposed step.",
        }
    }


def test_reminder_names_the_last_cell_by_title_and_count(sandbox: Sandbox) -> None:
    write_state(sandbox, "last_cell.json", last_cell())
    text = submit(sandbox).context
    assert (
        'Last cell: "Drop rows with missing price" [14] in 01_eda.ipynb: finished: ok, 3m1' in text
    )
    assert "ago" in text
    assert "nh-4f2a91c07b" not in text


def test_reminder_flags_failing_and_running_cells(sandbox: Sandbox) -> None:
    write_state(sandbox, "last_cell.json", last_cell(status="error", finished_at=None))
    assert (
        '"Drop rows with missing price" [14] in 01_eda.ipynb: FAILING.' in submit(sandbox).context
    )
    write_state(sandbox, "last_cell.json", last_cell(status="running", exec=None, finished_at=None))
    assert '"Drop rows with missing price" in 01_eda.ipynb: RUNNING' in submit(sandbox).context


def test_iso_finished_at(sandbox: Sandbox) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7300))
    write_state(sandbox, "last_cell.json", last_cell(finished_at=stamp))
    assert "finished: ok, 2h01m ago" in submit(sandbox).context


def test_reminder_flags_kernel_drift(sandbox: Sandbox) -> None:
    write_state(sandbox, "last_cell.json", last_cell())
    write_state(
        sandbox,
        "kernel_drift.json",
        {
            "notebooks/01_eda.ipynb": {
                "kernel_id": "k1",
                "since": 1.0,
                "names": {"df": "changed by removed cell", "model": "x"},
            }
        },
    )
    text = submit(sandbox).context
    assert "Kernel ≠ notebook: model, df still hold results of undone cells" in text
    assert "Restart Kernel and Run Up to Selected Cell" in text


def test_reminder_stays_under_400_chars(sandbox: Sandbox) -> None:
    write_state(sandbox, "last_cell.json", last_cell(title="word " * 60, status="error"))
    names = {f"frame_number_{i}": "x" for i in range(30)}
    write_state(sandbox, "kernel_drift.json", {"a.ipynb": {"names": names}, "b.ipynb": {}})
    text = submit(sandbox, prompt_id=None).context
    assert len(text) <= 400
    assert text.startswith("[nh] Claude Code sent no prompt id")


def test_missing_prompt_id_warns_and_records_null(sandbox: Sandbox) -> None:
    run = submit(sandbox, prompt_id=None)
    assert "claude update" in run.context
    record = json.loads((sandbox.nh / "state" / "turns" / "sess-1.json").read_text())
    assert record["prompt_id"] is None and record["turn_id"] is None


def test_corrupt_state_files_are_ignored(sandbox: Sandbox) -> None:
    (sandbox.nh / "state").mkdir()
    (sandbox.nh / "state" / "last_cell.json").write_text("{not json")
    (sandbox.nh / "state" / "kernel_drift.json").write_text('["unexpected"]')
    run = submit(sandbox)
    assert run.context == "[nh] One new cell this turn; go/next/y = do the proposed step."


def test_drift_reminder_grammar_for_one_name(sandbox: Sandbox) -> None:
    write_state(sandbox, "last_cell.json", last_cell())
    write_state(
        sandbox,
        "kernel_drift.json",
        {"notebooks/01_eda.ipynb": {"names": {"df_clean": "changed by removed cell"}}},
    )
    text = submit(sandbox).context
    assert "Kernel ≠ notebook: df_clean still holds results of undone cells" in text


# --- background task notifications ---------------------------------------------------------

QA_REPORT = (
    "[nh] The nh:qa-cell report for this message arrived (not a new user message): reply "
    "from it; write no new cell unless its writer wrote none."
)
QA_EARLIER = (
    "[nh] An nh:qa-cell report for an earlier message arrived; the user has written since: "
    "report it, change no cell for it."
)
BACKGROUND = "[nh] A background task finished; this is not a new user message: write no new cell."


def notify(sandbox: Sandbox, prompt_id: str = "note-1", **fields):
    prompt = fields.pop("prompt", None) or notification_prompt(**fields)
    return sandbox.run(
        "UserPromptSubmit",
        sandbox.payload("UserPromptSubmit", prompt=prompt, prompt_id=prompt_id),
    )


def turn(sandbox: Sandbox) -> dict:
    return json.loads((sandbox.nh / "state" / "turns" / "sess-1.json").read_text())


def events(sandbox: Sandbox) -> list[dict]:
    return [json.loads(line) for line in (sandbox.nh / "log.jsonl").read_text().splitlines()]


def write_runs(sandbox: Sandbox, *runs: dict) -> None:
    write_state(sandbox, "workflows/sess-1.json", {"v": 1, "runs": list(runs)})


def read_runs(sandbox: Sandbox) -> dict[str, dict]:
    data = json.loads((sandbox.nh / "state" / "workflows" / "sess-1.json").read_text())
    return {run["run_id"]: run for run in data["runs"]}


def qa_run(run_id: str, turn_id: str, tool_use_id: str, **fields) -> dict:
    base = {
        "run_id": run_id,
        "task_id": f"task-{run_id}",
        "tool_use_id": tool_use_id,
        "name": "qa-cell",
        "launched_by": "name",
        "transcript_dir": None,
        "turn_id": turn_id,
        "prompt_id": turn_id,
        "ts": time.time() - 30,
        "done_ts": None,
    }
    base.update(fields)
    return base


def test_a_notification_aliases_the_open_turn(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    opened = turn(sandbox)
    run = notify(sandbox, "note-1", task_id="t1")
    assert run.returncode == 0
    record = turn(sandbox)
    assert (record["turn_id"], record["prompt_id"], record["aliases"]) == (
        "p1",
        "note-1",
        ["note-1"],
    )
    assert record["ts"] == opened["ts"] and record["human"] is True
    assert record["alias_ts"] >= opened["ts"]
    notify(sandbox, "note-2")
    assert turn(sandbox)["aliases"] == ["note-1", "note-2"]
    opened_event, alias_event, _ = events(sandbox)
    assert opened_event["event"] == "turn_open"
    assert {k: alias_event[k] for k in ("event", "turn_id", "prompt_id", "tasks")} == {
        "event": "turn_alias",
        "turn_id": "p1",
        "prompt_id": "note-1",
        "tasks": ["t1"],
    }
    assert alias_event["prompt_chars"] == len(notification_prompt(task_id="t1"))


def test_a_human_message_after_notifications_opens_a_fresh_turn(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    notify(sandbox, "note-1")
    submit(sandbox, prompt_id="p2")
    record = turn(sandbox)
    assert (record["turn_id"], record["prompt_id"], record["aliases"]) == ("p2", "p2", [])
    assert record["earlier"] == {"note-1": "p1"}  # a late call from note-1 is still p1's


def test_a_notification_with_no_open_turn_is_an_orphan(sandbox: Sandbox) -> None:
    run = notify(sandbox, "note-1")
    assert run.context.startswith(BACKGROUND)
    record = turn(sandbox)
    assert (record["turn_id"], record["human"], record["aliases"]) == (None, False, ["note-1"])
    notify(sandbox, "note-2")
    record = turn(sandbox)
    assert (record["turn_id"], record["aliases"]) == (None, ["note-1", "note-2"])
    assert [(e["event"], e["turn_id"]) for e in events(sandbox)] == [
        ("turn_alias", None),
        ("turn_alias", None),
    ]


def test_this_messages_qa_report_is_marked_done(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    write_runs(sandbox, qa_run("wf_1", "p1", "toolu_launch"), qa_run("wf_0", "p0", "toolu_old"))
    before = time.time()
    run = notify(sandbox, "note-1", tool_use_id="toolu_launch", status="completed")
    assert run.context == QA_REPORT
    runs = read_runs(sandbox)
    assert runs["wf_1"]["status"] == "completed" and before <= runs["wf_1"]["done_ts"]
    assert runs["wf_0"]["done_ts"] is None


def test_a_qa_report_is_matched_by_task_id_too(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    write_runs(sandbox, qa_run("wf_1", "p1", "toolu_launch", name="nh:qa-cell"))
    assert notify(sandbox, task_id="task-wf_1", tool_use_id=None).context == QA_REPORT
    assert read_runs(sandbox)["wf_1"]["done_ts"] is not None


def test_an_earlier_messages_qa_report(sandbox: Sandbox) -> None:
    write_runs(sandbox, qa_run("wf_1", "p1", "toolu_launch"))
    submit(sandbox, prompt_id="p2")
    run = notify(sandbox, "note-1", tool_use_id="toolu_launch", status="killed")
    assert run.context == QA_EARLIER
    assert read_runs(sandbox)["wf_1"]["status"] == "killed"
    assert turn(sandbox)["turn_id"] == "p2"


def test_other_background_tasks_and_the_last_cell(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    write_runs(sandbox, qa_run("wf_r", "p1", "toolu_r", name="nh-research"))
    write_state(sandbox, "last_cell.json", last_cell(status="error", finished_at=None))
    text = notify(sandbox, tool_use_id="toolu_r").context
    assert text.startswith(BACKGROUND + ' Last cell: "Drop rows with missing price" [14]')
    assert "FAILING" in text
    assert notify(sandbox, tool_use_id="toolu_unknown").context.startswith(BACKGROUND)


def test_a_foreign_qa_cell_runs_report_is_a_background_task(sandbox: Sandbox) -> None:
    """A workflow named qa-cell that isn't nh's own (an inline or path script) reports as
    any background task, for this message or an earlier one."""
    write_runs(
        sandbox,
        qa_run("wf_0", "p0", "toolu_old", launched_by="scriptPath"),
        qa_run("wf_1", "p1", "toolu_inline", launched_by="script"),
        qa_run("wf_2", "p1", "toolu_named", name="nh:qa-cell", launched_by="script"),
    )
    submit(sandbox, prompt_id="p1")
    for tool_use_id in ("toolu_inline", "toolu_named", "toolu_old"):
        assert notify(sandbox, tool_use_id=tool_use_id).context.startswith(BACKGROUND)
    assert all(run["done_ts"] is not None for run in read_runs(sandbox).values())


def test_several_notifications_in_one_prompt(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    write_runs(sandbox, qa_run("wf_1", "p1", "toolu_launch"))
    prompt = (
        notification_prompt("bash-1", "toolu_bash")
        + "\n"
        + notification_prompt("wf-task", "toolu_launch")
    )
    assert notify(sandbox, prompt=prompt).context == QA_REPORT
    assert events(sandbox)[-1]["tasks"] == ["bash-1", "wf-task"]


def test_a_pasted_notification_is_a_human_message(sandbox: Sandbox) -> None:
    submit(sandbox, prompt_id="p1")
    run = notify(sandbox, "p2", prompt="why did this fail?\n" + notification_prompt())
    assert run.context == "[nh] One new cell this turn; go/next/y = do the proposed step."
    assert (turn(sandbox)["turn_id"], events(sandbox)[-1]["event"]) == ("p2", "turn_open")


def test_effort_adds_no_nudge(sandbox: Sandbox) -> None:
    for level in ("xhigh", "max", "low"):
        run = submit(sandbox, effort={"level": level})
        assert run.context == "[nh] One new cell this turn; go/next/y = do the proposed step."
