"""UserPromptSubmit: the turn record and the per-message reminder."""

from __future__ import annotations

import importlib
import json
import sys
import time
from collections.abc import Iterator
from types import ModuleType

import pytest
from hookenv import PLUGIN, Sandbox, needs_system_python

from nh_gateway._shared.paths import Layout
from tests.fakes.turns import notification_prompt

pytestmark = needs_system_python

RULE = "[nh] One new cell this turn; go/next/y = do the proposed step."


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
        "mode": None,
        "request": None,
        "answer": "yes",  # "go"
        "prev_turn_id": None,
        "prev_request": None,
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
        run = submit(sandbox, prompt_id=f"p-{level}", effort={"level": level})
        assert run.context == "[nh] One new cell this turn; go/next/y = do the proposed step."


# --- the message's intent (design §6.1) ----------------------------------------------------

INTENT_KEYS = ("mode", "request", "answer", "prev_turn_id", "prev_request")
BATCH3 = {"batch": True, "n": 3}


def say(sandbox: Sandbox, prompt: str, prompt_id: str, **fields):
    return sandbox.run(
        "UserPromptSubmit",
        sandbox.payload("UserPromptSubmit", prompt=prompt, prompt_id=prompt_id, **fields),
    )


def intent_of(record: dict) -> tuple:
    return tuple(record[key] for key in INTENT_KEYS)


def test_a_new_turn_records_the_message_intent(sandbox: Sandbox) -> None:
    secret = "run the next 3 on the zq_private_ledger table"
    assert say(sandbox, secret, "p1").context == RULE
    assert intent_of(turn(sandbox)) == ("ask", BATCH3, None, None, None)
    assert say(sandbox, "Yes, please!", "p2").context == RULE
    assert intent_of(turn(sandbox)) == (None, None, "yes", "p1", BATCH3)
    say(sandbox, "explain the fixed-width parse", "p3")
    assert intent_of(turn(sandbox)) == ("explain", None, None, "p2", None)
    opened = [e for e in events(sandbox) if e["event"] == "turn_open"]
    assert [(e["turn_id"], e["mode"], e["request"], e["answer"]) for e in opened] == [
        ("p1", "ask", BATCH3, None),
        ("p2", None, None, "yes"),
        ("p3", "explain", None, None),
    ]
    assert opened[0]["prompt_chars"] == len(secret)
    # The prompt text is stored nowhere.
    for path in sandbox.nh.rglob("*"):
        if path.is_file():
            assert "zq_private_ledger" not in path.read_text(errors="replace"), path


def test_a_notification_keeps_the_turns_intent(sandbox: Sandbox) -> None:
    say(sandbox, "/nh:plan the cleaning", "p1")
    notify(sandbox, "note-1")
    record = turn(sandbox)
    assert (record["prompt_id"], intent_of(record)) == ("note-1", ("plan", None, None, None, None))


def test_a_message_after_an_orphan_has_no_previous_turn(sandbox: Sandbox) -> None:
    notify(sandbox, "note-1")
    say(sandbox, "go", "p1")
    assert intent_of(turn(sandbox)) == (None, None, "yes", None, None)


# --- a message typed mid-turn (spike V16) ------------------------------------------------


def test_a_mid_turn_message_is_absorbed(sandbox: Sandbox) -> None:
    say(sandbox, "run the next 3", "p1")
    notify(sandbox, "note-1")
    opened = turn(sandbox)
    run = say(sandbox, "yes", "p1")  # typed while Claude works: the running turn's prompt_id
    assert run.returncode == 0 and run.stdout == ""  # no RULE, and nothing else to say
    record = turn(sandbox)
    assert record["answer"] is None  # a mid-turn "yes" answers nothing
    assert (record["turn_id"], record["prompt_id"]) == ("p1", "p1")
    for key in ("aliases", "ts", "alias_ts", "earlier", "human", "mode", "request"):
        assert record[key] == opened[key], key
    last = events(sandbox)[-1]
    assert {k: last[k] for k in ("event", "session_id", "turn_id", "mode", "request")} == {
        "event": "turn_absorbed",
        "session_id": "sess-1",
        "turn_id": "p1",
        "mode": None,
        "request": None,
    }
    assert (last["answer"], last["prompt_chars"]) == ("yes", 3)  # logged, never applied


def test_a_mid_turn_explain_sets_the_mode(sandbox: Sandbox) -> None:
    say(sandbox, "run the next 3", "p1")
    write_state(sandbox, "last_cell.json", last_cell())
    run = say(sandbox, "explain the fixed-width parse", "p1")
    assert run.context.startswith('Last cell: "Drop rows with missing price" [14]')
    assert RULE not in run.context
    record = turn(sandbox)
    assert (record["mode"], record["request"]) == ("explain", BATCH3)
    say(sandbox, "re-run the stale cells", "p1")
    assert (turn(sandbox)["mode"], turn(sandbox)["request"]) == ("ask", {"rerun_stale": True})
    say(sandbox, "go", "p2")  # the next message opens a turn and sees the kept request
    assert intent_of(turn(sandbox)) == (None, None, "yes", "p1", {"rerun_stale": True})
    assert [e["event"] for e in events(sandbox)] == [
        "turn_open",
        "turn_absorbed",
        "turn_absorbed",
        "turn_open",
    ]


def test_a_mid_turn_message_keeps_prev_turn(sandbox: Sandbox) -> None:
    say(sandbox, "run the next 3", "p1")
    say(sandbox, "yes", "p2")
    say(sandbox, "no", "p2")
    record = turn(sandbox)
    assert intent_of(record) == (None, None, "yes", "p1", BATCH3)


def test_a_mid_turn_yes_in_a_notifications_turn_answers_nothing(sandbox: Sandbox) -> None:
    """A background task's notification started the running turn: a message typed into it
    carries that turn's prompt id, an alias of the human turn."""
    say(sandbox, "run the next 3", "p1")
    notify(sandbox, "note-1")
    opened = turn(sandbox)
    run = say(sandbox, "yes", "note-1")
    assert run.returncode == 0 and run.stdout == ""  # no RULE: no budget of its own
    record = turn(sandbox)
    assert (record["turn_id"], record["prompt_id"], record["answer"]) == ("p1", "note-1", None)
    for key in ("aliases", "ts", "alias_ts", "earlier", "mode", "request", "prev_turn_id"):
        assert record[key] == opened[key], key
    last = events(sandbox)[-1]
    assert {k: last[k] for k in ("event", "turn_id", "prompt_id", "answer")} == {
        "event": "turn_absorbed",
        "turn_id": "p1",  # the human turn, not the alias
        "prompt_id": "note-1",
        "answer": "yes",
    }
    say(sandbox, "explain the parse", "note-1")
    assert (turn(sandbox)["mode"], turn(sandbox)["turn_id"]) == ("explain", "p1")
    say(sandbox, "go", "p2")  # the next message is a turn of its own
    assert intent_of(turn(sandbox)) == (None, None, "yes", "p1", BATCH3)


def test_a_message_typed_into_an_orphans_turn_gets_no_budget(sandbox: Sandbox) -> None:
    notify(sandbox, "o1")  # no human turn open: an orphan
    run = say(sandbox, "run the next 3", "o1")
    assert run.returncode == 0 and run.stdout == ""
    record = turn(sandbox)
    assert (record["turn_id"], record["human"], record["aliases"]) == (None, False, ["o1"])
    assert (record["mode"], record["request"], record["answer"]) == ("ask", BATCH3, None)
    last = events(sandbox)[-1]
    assert (last["event"], last["turn_id"], last["prompt_id"]) == ("turn_absorbed", None, "o1")


def test_an_earlier_turns_ids_open_a_turn(sandbox: Sandbox) -> None:
    say(sandbox, "go", "p1")
    notify(sandbox, "note-1")
    say(sandbox, "go", "p2")
    # p1 and its alias note-1 are no longer running: a message with one is a new turn.
    for prompt_id in ("note-1", "p1"):
        assert say(sandbox, "go", prompt_id).context == RULE
        assert (turn(sandbox)["turn_id"], events(sandbox)[-1]["event"]) == (
            prompt_id,
            "turn_open",
        )
    # Without a prompt id nothing can be matched: a new turn, as in v0.1.
    say(sandbox, "go", None)
    say(sandbox, "go", None)
    assert [e["event"] for e in events(sandbox)][-2:] == ["turn_open", "turn_open"]


def test_only_a_human_records_turn_id_is_absorbed(sandbox: Sandbox) -> None:
    write_state(
        sandbox,
        "turns/sess-1.json",
        {"v": 2, "session_id": "sess-1", "prompt_id": "p1", "turn_id": "p1", "aliases": [],
         "human": False, "ts": 1.0},
    )  # fmt: skip
    assert say(sandbox, "go", "p1").context == RULE
    record = turn(sandbox)
    assert (record["turn_id"], record["human"], events(sandbox)[-1]["event"]) == (
        "p1",
        True,
        "turn_open",
    )


def test_no_session_id_writes_no_record(sandbox: Sandbox) -> None:
    run = say(sandbox, "go", "p1", session_id=None)
    assert run.context == RULE
    assert not (sandbox.nh / "state" / "turns").exists()


# --- the reminder's order and clip ---------------------------------------------------------

NO_PROMPT_ID = (
    "[nh] Claude Code sent no prompt id, so nh can't count cells per message and will refuse "
    "to write. Ask the user to run `claude update` (2.1.196 or newer)."
)
LAST_FAILING = 'Last cell: "Drop rows with missing price" [14] in 01_eda.ipynb: FAILING.'
DRIFT_LINE = (
    "Kernel ≠ notebook: model, df still hold results of undone cells (as of nh's last "
    "check); suggest Kernel → Restart Kernel and Run Up to Selected Cell."
)


def write_last_cell_and_drift(sandbox: Sandbox, names: dict | None = None) -> None:
    write_state(sandbox, "last_cell.json", last_cell(status="error", finished_at=None))
    write_state(
        sandbox,
        "kernel_drift.json",
        {"notebooks/01_eda.ipynb": {"names": names or {"df": "x", "model": "y"}}},
    )


def test_the_reminder_order_for_a_new_turn(sandbox: Sandbox) -> None:
    write_last_cell_and_drift(sandbox)
    assert say(sandbox, "go", "p1").context == f"{RULE} {LAST_FAILING} {DRIFT_LINE}"
    # With the missing-prompt-id warning first it runs past 400: the drift tail is clipped.
    full = f"{NO_PROMPT_ID} {RULE} {LAST_FAILING} {DRIFT_LINE}"
    assert len(full) > 400
    assert say(sandbox, "go", None).context == full[:399].rstrip() + "…"


def test_the_reminder_order_for_an_absorbed_message(sandbox: Sandbox) -> None:
    say(sandbox, "go", "p1")
    write_last_cell_and_drift(sandbox)
    assert say(sandbox, "explain the plot", "p1").context == f"{LAST_FAILING} {DRIFT_LINE}"


def test_the_clip_cuts_the_drift_tail_first(sandbox: Sandbox) -> None:
    write_last_cell_and_drift(sandbox, {f"frame_number_{i:02d}_{'x' * 40}": "x" for i in range(8)})
    text = say(sandbox, "go", "p1").context
    assert len(text) <= 400 and text.endswith("…")
    assert text.startswith(f"{RULE} {LAST_FAILING} Kernel ≠ notebook: frame_number_07_")
    assert "Restart Kernel" not in text


# --- in process: the hook module itself ----------------------------------------------------


@pytest.fixture
def prompt_submit(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """The hook's prompt_submit module, imported the way main.py imports it (no bytecode
    next to the plugin's sources: the hooks keep theirs in the plugin data dir)."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(PLUGIN / "hooks" / "nh_hooks"))
    for name in ("common", "prompt_submit"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield importlib.import_module("prompt_submit")
    for name in ("common", "prompt_submit"):
        sys.modules.pop(name, None)


def test_the_pinned_reminders_are_unchanged(prompt_submit: ModuleType) -> None:
    assert prompt_submit.RULE == RULE
    assert prompt_submit.QA_REPORT == QA_REPORT
    assert prompt_submit.REMINDER_MAX_CHARS == 400
    assert prompt_submit.NO_PROMPT_ID == NO_PROMPT_ID


def test_a_failing_classifier_still_opens_the_turn(
    sandbox: Sandbox, prompt_submit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text: object) -> dict:
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(prompt_submit.intent, "classify", boom)
    payload = sandbox.payload("UserPromptSubmit", prompt="zq_private_ledger yes", prompt_id="p1")
    output = prompt_submit.handle(Layout(sandbox.project), payload)
    assert output["hookSpecificOutput"]["additionalContext"] == RULE
    record = turn(sandbox)
    assert (record["turn_id"], intent_of(record)) == ("p1", (None,) * 5)
    failed, opened = events(sandbox)
    assert set(failed) == {"v", "ts", "event", "session_id", "turn_id"}
    assert (failed["event"], failed["session_id"], failed["turn_id"]) == (
        "classify_failed",
        "sess-1",
        "p1",
    )
    assert (opened["event"], opened["mode"], opened["request"], opened["answer"]) == (
        "turn_open",
        None,
        None,
        None,
    )
    assert "zq_private_ledger" not in (sandbox.nh / "log.jsonl").read_text()
