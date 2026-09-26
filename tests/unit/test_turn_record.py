"""Turn record v2, notification parsing and the nh:qa-cell runs file (_shared/turn_record.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nh_gateway._shared import turn_record as tr
from nh_gateway._shared.paths import Layout, atomic_write_json
from tests.fakes.turns import notification_prompt

SESSION = "sess-1"


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    (tmp_path / ".nh").mkdir()
    return Layout(tmp_path)


def write_record(layout: Layout, data: dict | str) -> None:
    path = layout.turn_file(SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


# --- turn record -------------------------------------------------------------------------


def test_a_v1_record_reads_as_its_own_turn(layout: Layout) -> None:
    write_record(layout, {"v": 1, "session_id": SESSION, "prompt_id": "p1", "ts": 12.5})
    record = tr.read(layout, SESSION)
    assert record is not None
    assert (record["v"], record["turn_id"], record["prompt_id"]) == (2, "p1", "p1")
    assert (record["aliases"], record["human"], record["ts"]) == ([], True, 12.5)
    assert tr.canonical(record, "p1") == "p1"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_unreadable_records_are_none(layout: Layout, raw: str) -> None:
    write_record(layout, raw)
    assert tr.read(layout, SESSION) is None
    assert tr.read(layout, "no-such-session") is None


def test_bad_fields_are_normalised(layout: Layout) -> None:
    write_record(
        layout,
        {"v": 2, "prompt_id": "n1", "turn_id": "p1", "aliases": ["n1", 7, ""], "ts": "soon"},
    )
    record = tr.read(layout, SESSION)
    assert record is not None
    assert (record["aliases"], record["ts"], record["session_id"]) == (["n1"], 0.0, SESSION)
    assert record["human"] is True  # a v2 record without the flag is a human turn


def test_opened_record_round_trips(layout: Layout) -> None:
    atomic_write_json(layout.turn_file(SESSION), tr.opened(SESSION, "p1", 100.0))
    assert tr.read(layout, SESSION) == {
        "v": 2,
        "session_id": SESSION,
        "prompt_id": "p1",
        "turn_id": "p1",
        "aliases": [],
        "human": True,
        "ts": 100.0,
        "alias_ts": None,
        "earlier": {},
    }


def test_aliases_resolve_to_the_human_turn_beyond_twenty() -> None:
    record = tr.opened(SESSION, "p1", 100.0)
    for i in range(50):
        record = tr.aliased(record, f"n{i}", 200.0 + i)
    assert (record["turn_id"], record["prompt_id"], record["ts"]) == ("p1", "n49", 100.0)
    assert record["alias_ts"] == 249.0
    assert all(tr.canonical(record, f"n{i}") == "p1" for i in range(50))
    assert tr.canonical(record, "p1") == "p1"


def test_aliases_are_capped_and_a_human_turn_resets_them() -> None:
    record = tr.opened(SESSION, "p1", 1.0)
    for i in range(tr.MAX_ALIASES + 5):
        record = tr.aliased(record, f"n{i}", 2.0)
    assert len(record["aliases"]) == tr.MAX_ALIASES
    assert list(record["earlier"]) == [f"n{i}" for i in range(5)]
    assert tr.canonical(record, "n0") == "p1"  # trimmed, still this turn's
    assert tr.canonical(record, f"n{tr.MAX_ALIASES + 4}") == "p1"
    assert tr.opened(SESSION, "p2", 3.0)["aliases"] == []


def test_a_human_message_remembers_earlier_aliases() -> None:
    record = tr.aliased(tr.opened(SESSION, "p1", 1.0), "n1", 2.0)
    record = tr.aliased(tr.opened(SESSION, "p2", 3.0, record), "n2", 4.0)
    record = tr.opened(SESSION, "p3", 5.0, record)
    assert (record["turn_id"], record["aliases"]) == ("p3", [])
    assert record["earlier"] == {"n1": "p1", "n2": "p2"}
    # A late call from an earlier notification's prompt counts against that message.
    assert (tr.canonical(record, "n1"), tr.canonical(record, "n2")) == ("p1", "p2")
    assert tr.canonical(record, "p1") == "p1"


def test_an_orphans_aliases_stay_turnless() -> None:
    record = tr.opened(SESSION, "p1", 2.0, tr.aliased(tr.orphan(SESSION, "n1", 1.0), "n2", 1.5))
    assert record["earlier"] == {"n1": None, "n2": None}
    assert tr.canonical(record, "n2") is None


def test_earlier_aliases_are_capped() -> None:
    record = tr.opened(SESSION, "p0", 0.0)
    record["earlier"] = {f"old{i}": "p0" for i in range(tr.MAX_EARLIER)}
    record = tr.opened(SESSION, "p1", 1.0, tr.aliased(record, "n1", 0.5))
    assert len(record["earlier"]) == tr.MAX_EARLIER
    assert "old0" not in record["earlier"]
    assert record["earlier"]["n1"] == "p0"


def test_bad_earlier_entries_are_dropped(layout: Layout) -> None:
    write_record(
        layout,
        {
            "v": 2,
            "prompt_id": "p2",
            "turn_id": "p2",
            "earlier": {"n1": "p1", "n2": None, "n3": 7, "": "p1", "n4": ""},
        },
    )
    record = tr.read(layout, SESSION)
    assert record is not None and record["earlier"] == {"n1": "p1", "n2": None}
    write_record(layout, {"v": 2, "prompt_id": "p2", "turn_id": "p2", "earlier": ["n1"]})
    record = tr.read(layout, SESSION)
    assert record is not None and record["earlier"] == {}


def test_unknown_prompts_are_returned_unchanged() -> None:
    record = tr.aliased(tr.opened(SESSION, "p2", 1.0), "n1", 2.0)
    assert tr.canonical(record, "p1") == "p1"  # an older prompt: the gate answers E102
    assert tr.canonical(None, "p1") == "p1"
    assert tr.canonical(record, None) is None
    assert tr.canonical(record, "") == ""


def test_an_orphan_has_no_turn() -> None:
    record = tr.orphan(SESSION, "n1", 5.0)
    assert (record["turn_id"], record["human"], record["aliases"]) == (None, False, ["n1"])
    assert tr.canonical(record, "n1") is None
    later = tr.aliased(record, "n2", 6.0)
    assert (tr.canonical(later, "n1"), tr.canonical(later, "n2"), later["ts"]) == (None, None, 5.0)


# --- task notifications ------------------------------------------------------------------


def test_a_real_notification_parses() -> None:
    prompt = notification_prompt("w2g9v11xz", "toolu_01KSY", "completed")
    assert tr.notification_blocks(prompt) == [
        {"task_id": "w2g9v11xz", "tool_use_id": "toolu_01KSY", "status": "completed"}
    ]


def test_several_blocks_and_surrounding_whitespace() -> None:
    prompt = "\n " + notification_prompt("t1") + "\n\n" + notification_prompt("t2", None, "killed")
    assert tr.notification_blocks(prompt + "\n") == [
        {"task_id": "t1", "tool_use_id": "toolu_launch", "status": "completed"},
        {"task_id": "t2", "tool_use_id": None, "status": "killed"},
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "go",
        "",
        "look at this: " + notification_prompt(),  # pasted into a human message
        notification_prompt() + "\nalso drop the nulls please",  # merged with typed text
        notification_prompt().replace("<task-id>w2g9v11xz</task-id>", ""),
        notification_prompt().replace("<task-id>w2g9v11xz</task-id>", "<task-id> </task-id>"),
        notification_prompt().replace("<status>completed</status>", ""),
        notification_prompt().replace("</task-notification>", ""),  # unclosed
        notification_prompt() + "\n<task-notification><task-id>t2</task-id>",
    ],
    ids=[
        "plain",
        "empty",
        "pasted",
        "merged",
        "no-task-id",
        "blank-task-id",
        "no-status",
        "unclosed",
        "trailing-open-block",
    ],
)
def test_other_prompts_are_not_notifications(prompt: str) -> None:
    assert tr.notification_blocks(prompt) is None


# --- workflow runs -----------------------------------------------------------------------


def run(run_id: str, *, turn_id: str = "p1", ts: float = 1000.0, **fields) -> dict:
    base = {
        "run_id": run_id,
        "task_id": f"task-{run_id}",
        "tool_use_id": f"toolu-{run_id}",
        "name": "qa-cell",
        "launched_by": "name",
        "transcript_dir": None,
        "turn_id": turn_id,
        "prompt_id": turn_id,
        "ts": ts,
        "done_ts": None,
    }
    base.update(fields)
    return base


def test_record_and_find_runs(layout: Layout) -> None:
    assert tr.find_runs(layout, SESSION) == []
    tr.record_run(layout, SESSION, run("wf_1"))
    tr.record_run(layout, SESSION, run("wf_2"))
    tr.record_run(layout, SESSION, run("wf_1", turn_id="p2"))  # replaced, moves last
    assert [(r["run_id"], r["turn_id"]) for r in tr.find_runs(layout, SESSION)] == [
        ("wf_2", "p1"),
        ("wf_1", "p2"),
    ]
    tr.record_run(layout, SESSION, {"task_id": "no run id"})
    assert len(tr.find_runs(layout, SESSION)) == 2
    assert layout.workflow_file(SESSION) == layout.nh / "state" / "workflows" / "sess-1.json"


def test_only_the_last_twenty_runs_are_kept(layout: Layout) -> None:
    for i in range(25):
        tr.record_run(layout, SESSION, run(f"wf_{i}"))
    runs = tr.find_runs(layout, SESSION)
    assert [r["run_id"] for r in runs] == [f"wf_{i}" for i in range(5, 25)]


def test_garbage_runs_files_are_ignored(layout: Layout) -> None:
    path = layout.workflow_file(SESSION)
    path.parent.mkdir(parents=True)
    path.write_text('{"v": 1, "runs": [7, {"run_id": ""}, {"run_id": "wf_ok"}]}')
    assert [r["run_id"] for r in tr.find_runs(layout, SESSION)] == ["wf_ok"]
    path.write_text("{torn")
    assert tr.find_runs(layout, SESSION) == []


def test_mark_done_by_tool_use_id_or_task_id(layout: Layout) -> None:
    for run_id in ("wf_1", "wf_2", "wf_3"):
        tr.record_run(layout, SESSION, run(run_id))
    marked = tr.mark_done(layout, SESSION, "toolu-wf_1", status="completed", now=2000.0)
    assert [(r["run_id"], r["done_ts"], r["status"]) for r in marked] == [
        ("wf_1", 2000.0, "completed")
    ]
    marked = tr.mark_done(layout, SESSION, None, task_id="task-wf_2", status="killed", now=2001.0)
    assert [r["run_id"] for r in marked] == ["wf_2"]
    again = tr.mark_done(layout, SESSION, "toolu-wf_1", status="failed", now=3000.0)
    assert [(r["done_ts"], r["status"]) for r in again] == [(2000.0, "completed")]  # first wins
    assert tr.mark_done(layout, SESSION, "toolu-unknown", now=1.0) == []
    done = {r["run_id"]: r["done_ts"] for r in tr.find_runs(layout, SESSION)}
    assert done == {"wf_1": 2000.0, "wf_2": 2001.0, "wf_3": None}


def test_open_runs_are_this_turns_own_unreported_runs_for_an_hour(layout: Layout) -> None:
    tr.record_run(layout, SESSION, run("wf_open"))
    tr.record_run(layout, SESSION, run("wf_done", done_ts=1500.0))
    tr.record_run(layout, SESSION, run("wf_other_turn", turn_id="p0"))
    tr.record_run(layout, SESSION, run("wf_inline", launched_by="script"))
    tr.record_run(layout, SESSION, run("wf_foreign", name="nh-research"))
    tr.record_run(layout, SESSION, run("wf_slash", name="nh:qa-cell"))
    ids = [r["run_id"] for r in tr.open_runs(layout, SESSION, "p1", now=1000.0 + 3599)]
    assert ids == ["wf_open", "wf_slash"]
    assert tr.open_runs(layout, SESSION, "p1", now=1000.0 + 3600) == []  # past the TTL
    assert tr.open_runs(layout, SESSION, None, now=1001.0) == []  # an orphan has no runs


def test_run_open_and_own_run() -> None:
    assert tr.run_open(run("a"), 1000.0)
    assert not tr.run_open(run("a", done_ts=1.0), 1000.0)
    assert not tr.run_open(run("a", ts="later"), 1000.0)
    assert tr.is_own_run(run("a")) and tr.is_own_run(run("a", name="nh:qa-cell"))
    assert not tr.is_own_run(run("a", launched_by="scriptPath"))
    assert not tr.is_own_run(run("a", name="qa-cell-2"))


# --- run_for_agent -----------------------------------------------------------------------


def meta(folder: Path, agent_id: str, agent_type: str = tr.WRITER_AGENT) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = {"agentType": agent_type, "workflowPhase": "Write", "spawnDepth": 1}
    (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps(body))


def no_sleep(seconds: float) -> None:
    raise AssertionError(f"slept {seconds}s")


def test_run_for_agent_finds_the_one_run_holding_its_meta_file(
    layout: Layout, tmp_path: Path
) -> None:
    runs_dir = tmp_path / "transcripts" / "subagents" / "workflows"
    for run_id in ("wf_1", "wf_2"):
        tr.record_run(layout, SESSION, run(run_id, transcript_dir=str(runs_dir / run_id)))
    meta(runs_dir / "wf_2", "a79642cdfddbb0a18")
    found = tr.run_for_agent(layout, SESSION, "a79642cdfddbb0a18", sleep=no_sleep)
    assert found is not None and found["run_id"] == "wf_2"


def test_run_for_agent_zero_or_several_matches_is_none(layout: Layout, tmp_path: Path) -> None:
    runs_dir = tmp_path / "workflows"
    for run_id in ("wf_1", "wf_2"):
        tr.record_run(layout, SESSION, run(run_id, transcript_dir=str(runs_dir / run_id)))
    assert tr.run_for_agent(layout, SESSION, "a1", wait_s=0, sleep=no_sleep) is None
    meta(runs_dir / "wf_1", "a1")
    meta(runs_dir / "wf_2", "a1")
    assert tr.run_for_agent(layout, SESSION, "a1", sleep=no_sleep) is None  # no waiting either


@pytest.mark.parametrize(
    ("agent_id", "agent_type", "relative"),
    [
        ("a1", tr.QA_AGENT, False),
        ("a1", "general-purpose", False),
        ("a1", tr.WRITER_AGENT, True),
        ("a1/../a1", tr.WRITER_AGENT, False),  # would reach agent-a1/../a1.meta.json
        ("", tr.WRITER_AGENT, False),
    ],
)
def test_run_for_agent_needs_a_writer_meta_file_under_an_absolute_folder(
    layout: Layout, tmp_path: Path, agent_id: str, agent_type: str, relative: bool
) -> None:
    folder = tmp_path / "workflows" / "wf_1"
    meta(folder, "a1", agent_type)
    (folder / "agent-a1").mkdir()
    (folder / "a1.meta.json").write_text((folder / "agent-a1.meta.json").read_text())
    where = "workflows/wf_1" if relative else str(folder)
    tr.record_run(layout, SESSION, run("wf_1", transcript_dir=where))
    assert tr.run_for_agent(layout, SESSION, agent_id, wait_s=0, sleep=no_sleep) is None


def test_run_for_agent_rereads_while_the_run_is_being_recorded(
    layout: Layout, tmp_path: Path
) -> None:
    """The writer's first call can beat the PostToolUse hook that records its run."""
    folder = tmp_path / "workflows" / "wf_1"
    meta(folder, "a1")
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        if len(slept) == 3:
            tr.record_run(layout, SESSION, run("wf_1", transcript_dir=str(folder)))

    found = tr.run_for_agent(layout, SESSION, "a1", sleep=sleep, clock=lambda: clock[0])
    assert found is not None and found["run_id"] == "wf_1"
    assert len(slept) == 3


def test_run_for_agent_gives_up_after_the_wait(layout: Layout) -> None:
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    assert tr.run_for_agent(layout, SESSION, "a1", sleep=sleep, clock=lambda: clock[0]) is None
    assert sum(slept) == pytest.approx(tr.META_WAIT_S)
