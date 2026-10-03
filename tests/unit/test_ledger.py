"""TurnLedger v2 (policy/turn.py, design §6.1): v1 load, v2 save, the pending question, grant(),
and a writer's question granted only after its run reported (design §6.4, C5d2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from nh_gateway._shared import turn_record as tr
from nh_gateway._shared.paths import Layout
from nh_gateway.policy.turn import (
    KEEP_TURNS,
    LEDGER_VERSION,
    TurnLedger,
    TurnState,
    grant,
    pending_key,
    reported_runs,
    valid_pending,
)

SESSION = "sess-1"
KEY = pending_key("df = df.dropna()")
OTHER_KEY = pending_key("df = df.fillna(0)")


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    (tmp_path / ".nh").mkdir()
    return Layout(tmp_path)


def saved(layout: Layout) -> dict[str, Any]:
    return json.loads(layout.ledger_file(SESSION).read_text())


def write(layout: Layout, data: Any) -> None:
    path = layout.ledger_file(SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def state(prompt_id: str, opened_at: float, **fields: Any) -> TurnState:
    return TurnState(session_id=SESSION, prompt_id=prompt_id, opened_at=opened_at, **fields)


def pending(
    turn_id: str = "p1", kind: str = "cell", key: str = KEY, run_id: str | None = None
) -> dict[str, Any]:
    return {"kind": kind, "key": key, "turn_id": turn_id, "ts": 1.0, "run_id": run_id}


def record(turn_id: str = "p2", answer: str | None = "yes", prev: str | None = "p1") -> dict:
    opened = tr.opened(SESSION, turn_id, 2.0, None, {"answer": answer})
    return dict(opened, prev_turn_id=prev)


# --- persistence ---------------------------------------------------------------------------


def test_pending_key_is_the_sha256_hex_of_the_text() -> None:
    assert hashlib.sha256(b"df = df.dropna()").hexdigest() == KEY
    assert pending_key("é") == hashlib.sha256("é".encode()).hexdigest()
    assert len(KEY) == 64 and KEY != OTHER_KEY


def test_a_v1_ledger_loads_with_no_pending(layout: Layout) -> None:
    old = state("p1", 1.0, claims=["u1"], kinds={"u1": "add"})
    write(layout, {"v": 1, "turns": {"p1": old.__dict__}, "pending": pending()})
    ledger = TurnLedger(layout)
    assert ledger.get(SESSION, "p1") == old
    assert ledger.pending(SESSION) is None  # v1 has none, whatever the file holds


def test_save_writes_v2_with_the_last_turns_and_the_pending(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    for i in range(KEEP_TURNS + 2):
        ledger.save(state(f"p{i}", float(i)))
    data = saved(layout)
    assert (data["v"], LEDGER_VERSION) == (2, 2)
    assert list(data["turns"]) == [f"p{i}" for i in range(KEEP_TURNS + 1, 1, -1)]
    assert data["pending"] is None
    assert ledger.set_pending(SESSION, "cell", KEY, "p6", now=9.0)
    assert saved(layout)["pending"] == {
        "kind": "cell",
        "key": KEY,
        "turn_id": "p6",
        "ts": 9.0,
        "run_id": None,
    }
    ledger.save(state("p7", 7.0))
    assert saved(layout)["pending"]["turn_id"] == "p6"  # saving a turn keeps the pending


def test_every_write_keeps_only_the_newest_turns(layout: Layout) -> None:
    """The pending helpers write the file too: never more than KEEP_TURNS turns, including
    turns get() made that no save() kept yet."""
    ledger = TurnLedger(layout)
    made = 0

    def more_turns() -> list[str]:
        nonlocal made
        for _ in range(KEEP_TURNS + 1):
            ledger.get(SESSION, f"t{made}").opened_at = float(made)
            made += 1
        return [f"t{i}" for i in range(made - 1, made - 1 - KEEP_TURNS, -1)]

    newest = more_turns()
    assert ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    assert list(saved(layout)["turns"]) == newest
    newest = more_turns()
    assert ledger.use_grant(SESSION, record(), "p2", "cell", KEY)
    assert list(saved(layout)["turns"]) == newest
    assert ledger.set_pending(SESSION, "cell", KEY, "p3", now=3.0)
    newest = more_turns()
    ledger.clear_pending(SESSION)
    assert list(saved(layout)["turns"]) == newest
    assert list(TurnLedger(layout)._load(SESSION)) == newest


def test_a_v2_ledger_round_trips(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.save(state("p1", 1.0, claims=["u1"]))
    ledger.set_pending(SESSION, "rerun", KEY, "p1", now=3.0)
    fresh = TurnLedger(layout)
    assert fresh.get(SESSION, "p1").claims == ["u1"]
    assert fresh.pending(SESSION) == {
        "kind": "rerun",
        "key": KEY,
        "turn_id": "p1",
        "ts": 3.0,
        "run_id": None,
    }


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "cell",
        {"kind": "batch", "key": KEY, "turn_id": "p1", "ts": 1.0},
        {"kind": "cell", "key": KEY.upper(), "turn_id": "p1", "ts": 1.0},
        {"kind": "cell", "key": KEY[:-1], "turn_id": "p1", "ts": 1.0},
        {"kind": "cell", "key": "df = df.dropna()", "turn_id": "p1", "ts": 1.0},
        {"kind": "cell", "key": KEY, "turn_id": "", "ts": 1.0},
        {"kind": "cell", "key": KEY, "turn_id": 7, "ts": 1.0},
        {"kind": "cell", "key": KEY, "turn_id": "p1", "ts": "now"},
        {"kind": "cell", "key": KEY, "turn_id": "p1", "ts": True},
        {"kind": "cell", "key": KEY, "turn_id": "p1"},
        # A run id that is present is a non-empty string or null; anything else fails closed.
        dict(pending(), run_id=""),
        dict(pending(), run_id=7),
        dict(pending(), run_id=["wf_run-1"]),
        dict(pending(), run_id=True),
    ],
)
def test_a_bad_pending_loads_as_none(layout: Layout, bad: Any) -> None:
    write(layout, {"v": 2, "turns": {}, "pending": bad})
    assert TurnLedger(layout).pending(SESSION) is None
    assert valid_pending(bad) is None


@pytest.mark.parametrize(
    ("stored", "run_id"),
    [
        (dict(pending(), run_id="wf_run-1"), "wf_run-1"),
        (dict(pending(), run_id=None), None),
        ({k: v for k, v in pending().items() if k != "run_id"}, None),
    ],
    ids=["a writer's run", "null", "missing"],
)
def test_a_pending_run_id_is_kept_or_none(layout: Layout, stored: Any, run_id: Any) -> None:
    write(layout, {"v": 2, "turns": {}, "pending": stored})
    assert TurnLedger(layout).pending(SESSION) == pending(run_id=run_id)
    assert valid_pending(stored) == pending(run_id=run_id)


def test_a_v2_ledger_from_before_run_ids_loads_as_the_main_conversations(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v2 file written before C5d2 has no run id: its question is the main conversation's,
    granted as before, without reading the runs."""
    old = {"kind": "cell", "key": KEY, "turn_id": "p1", "ts": 1.0}
    write(layout, {"v": 2, "turns": {"p1": state("p1", 1.0).__dict__}, "pending": old})
    monkeypatch.setattr(tr, "find_runs", lambda *a: pytest.fail("read the runs"))
    ledger = TurnLedger(layout)
    assert ledger.pending(SESSION) == pending() and ledger.get(SESSION, "p1").opened_at == 1.0
    assert ledger.use_grant(SESSION, record(), "p2", "cell", KEY)
    assert saved(layout)["pending"] is None


def test_extra_pending_keys_are_dropped(layout: Layout) -> None:
    write(layout, {"v": 2, "turns": {}, "pending": dict(pending(), text="df = df.dropna()")})
    assert TurnLedger(layout).pending(SESSION) == pending()


@pytest.mark.parametrize("raw", [[1, 2], "text", {"v": 2, "turns": ["p1"]}, {"v": 2}])
def test_an_unreadable_ledger_is_empty(layout: Layout, raw: Any) -> None:
    write(layout, raw)
    ledger = TurnLedger(layout)
    assert ledger.recent_turn_ids(SESSION) == [] and ledger.pending(SESSION) is None


def test_a_bad_turn_entry_is_skipped(layout: Layout) -> None:
    write(layout, {"v": 2, "turns": {"p1": "junk", "p2": state("p2", 2.0).__dict__}})
    assert TurnLedger(layout).recent_turn_ids(SESSION) == ["p2"]


# --- the pending question --------------------------------------------------------------------


def test_the_first_ask_of_a_turn_wins(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    assert ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    assert not ledger.set_pending(SESSION, "rerun", OTHER_KEY, "p1", now=2.0)
    assert ledger.pending(SESSION) == {
        "kind": "cell",
        "key": KEY,
        "turn_id": "p1",
        "ts": 1.0,
        "run_id": None,
    }
    # A later turn's ask replaces an earlier turn's.
    assert ledger.set_pending(SESSION, "rerun", OTHER_KEY, "p2", now=3.0)
    assert ledger.pending(SESSION) == {
        "kind": "rerun",
        "key": OTHER_KEY,
        "turn_id": "p2",
        "ts": 3.0,
        "run_id": None,
    }


def test_set_pending_refuses_a_bad_question(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    with pytest.raises(ValueError):
        ledger.set_pending(SESSION, "batch", KEY, "p1")
    with pytest.raises(ValueError):
        ledger.set_pending(SESSION, "cell", "not a hash", "p1")
    with pytest.raises(ValueError):
        ledger.set_pending(SESSION, "cell", KEY, "")
    assert ledger.pending(SESSION) is None


def test_set_pending_records_the_writers_run(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    assert ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0, run_id="wf_run-1")
    assert ledger.pending(SESSION) == pending(run_id="wf_run-1")
    assert saved(layout)["pending"]["run_id"] == "wf_run-1"
    assert TurnLedger(layout).pending(SESSION) == pending(run_id="wf_run-1")
    for bad in ("", 7):
        with pytest.raises(ValueError):
            ledger.set_pending(SESSION, "cell", KEY, "p2", run_id=bad)  # type: ignore[arg-type]
    assert ledger.pending(SESSION) == pending(run_id="wf_run-1")


def test_set_pending_stamps_the_time(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1")
    current = ledger.pending(SESSION)
    assert current is not None and current["ts"] > 1_000_000_000


def test_pending_returns_a_copy(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    copy = ledger.pending(SESSION)
    assert copy is not None
    copy["turn_id"] = "p9"
    assert ledger.pending(SESSION) == pending()


def test_clear_pending(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    ledger.clear_pending(SESSION, turn_id="p2")  # another turn's: kept
    assert ledger.pending(SESSION) == pending()
    ledger.clear_pending(SESSION, turn_id="p1")
    assert ledger.pending(SESSION) is None and saved(layout)["pending"] is None
    ledger.set_pending(SESSION, "cell", KEY, "p3", now=1.0)
    ledger.clear_pending(SESSION)
    assert TurnLedger(layout).pending(SESSION) is None
    ledger.clear_pending("no-such-session")
    assert not layout.ledger_file("no-such-session").exists()


def test_sessions_are_separate(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1")
    assert ledger.pending("sess-2") is None


# --- grant() -------------------------------------------------------------------------------

KEPT = "kept"

GRANT_TABLE: list[tuple[str, Any, Any, Any, str, str, bool, Any]] = [
    # (case, pending, record, call's turn, kind, key, granted, pending after)
    ("no pending", None, record(), "p2", "cell", KEY, False, None),
    ("asked this turn", pending("p2"), record(), "p2", "cell", KEY, False, KEPT),
    ("no record", pending(), None, "p2", "cell", KEY, False, KEPT),
    ("an older call", pending(), record(), "p0", "cell", KEY, False, KEPT),
    ("no turn", pending(), record(), None, "cell", KEY, False, KEPT),
    ("an orphan record", pending(), tr.orphan(SESSION, "n1", 3.0), "p2", "cell", KEY, False, KEPT),
    ("yes to it", pending(), record(), "p2", "cell", KEY, True, None),
    ("yes to a re-run", pending(kind="rerun"), record(), "p2", "rerun", KEY, True, None),
    ("yes, another key", pending(), record(), "p2", "cell", OTHER_KEY, False, KEPT),
    ("yes, another kind", pending(), record(), "p2", "rerun", KEY, False, KEPT),
    ("no", pending(), record(answer="no"), "p2", "cell", KEY, False, None),
    ("any other reply", pending(), record(answer=None), "p2", "cell", KEY, False, None),
    ("yes, a turn later", pending("p0"), record(), "p2", "cell", KEY, False, None),
    ("yes after an orphan", pending(), record(prev=None), "p2", "cell", KEY, False, None),
]


@pytest.mark.parametrize(
    ("case", "before", "rec", "turn_id", "kind", "key", "granted", "after"),
    GRANT_TABLE,
    ids=[row[0] for row in GRANT_TABLE],
)
def test_grant(
    case: str,
    before: Any,
    rec: Any,
    turn_id: Any,
    kind: str,
    key: str,
    granted: bool,
    after: Any,
) -> None:
    result = grant(before, rec, turn_id, kind, key)
    assert result == (granted, before if after == KEPT else after), case


def test_a_grant_is_used_once(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    assert not ledger.use_grant(SESSION, record(), "p2", "cell", OTHER_KEY)
    assert ledger.pending(SESSION) == pending()  # only the exact call is granted
    assert ledger.use_grant(SESSION, record(), "p2", "cell", KEY)
    assert ledger.pending(SESSION) is None and saved(layout)["pending"] is None
    assert not ledger.use_grant(SESSION, record(), "p2", "cell", KEY)


def test_any_other_reply_drops_the_pending(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "rerun", KEY, "p1", now=1.0)
    assert not ledger.use_grant(SESSION, record(answer=None), "p2", "rerun", KEY)
    assert TurnLedger(layout).pending(SESSION) is None


def test_a_call_in_the_asking_turn_keeps_waiting(layout: Layout) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    asking = tr.opened(SESSION, "p1", 1.0, None, {"answer": "yes"})
    assert not ledger.use_grant(SESSION, asking, "p1", "cell", KEY)
    assert ledger.pending(SESSION) == pending()


# --- a writer's question: granted only after its run reported (design §6.4, C5d2) ------------

RUN = "wf_run-1"
WRITER = pending(run_id=RUN)  # asked in p1 by nh:cell-writer of run wf_run-1
# The yes message p2 opened at 2.0 (record()); the run's report reached the main conversation
# at done_ts.
WRITER_TABLE: list[tuple[str, Any, Any, Any, str, Any, bool, Any]] = [
    # (case, pending, record, call's turn, key, reported, granted, pending after)
    ("reported before the yes", WRITER, record(), "p2", KEY, {RUN: 1.5}, True, None),
    ("reported long before", WRITER, record(), "p2", KEY, {RUN: 0.0}, True, None),
    ("reported as the yes opened", WRITER, record(), "p2", KEY, {RUN: 2.0}, False, KEPT),
    ("reported after the yes", WRITER, record(), "p2", KEY, {RUN: 2.5}, False, KEPT),
    ("not reported", WRITER, record(), "p2", KEY, {}, False, KEPT),
    ("no runs given", WRITER, record(), "p2", KEY, None, False, KEPT),
    ("another run reported", WRITER, record(), "p2", KEY, {"wf_run-2": 1.5}, False, KEPT),
    ("done_ts a bool", WRITER, record(), "p2", KEY, {RUN: True}, False, KEPT),
    ("done_ts a string", WRITER, record(), "p2", KEY, {RUN: "1.5"}, False, KEPT),
    ("an unreadable record ts", WRITER, dict(record(), ts=0.0), "p2", KEY, {RUN: 1.5}, False, KEPT),
    ("no record ts", WRITER, dict(record(), ts=None), "p2", KEY, {RUN: 1.5}, False, KEPT),
    ("reported, another key", WRITER, record(), "p2", OTHER_KEY, {RUN: 1.5}, False, KEPT),
    ("not reported, another key", WRITER, record(), "p2", OTHER_KEY, {}, False, KEPT),
    ("asked this turn", pending("p2", run_id=RUN), record(), "p2", KEY, {RUN: 1.5}, False, KEPT),
    ("a no", WRITER, record(answer="no"), "p2", KEY, {}, False, None),
    ("any other reply", WRITER, record(answer=None), "p2", KEY, {RUN: 1.5}, False, None),
    ("a yes a turn later", pending("p0", run_id=RUN), record(), "p2", KEY, {}, False, None),
    ("the main conversation's", pending(), record(), "p2", KEY, {RUN: 2.5}, True, None),
]


@pytest.mark.parametrize(
    ("case", "before", "rec", "turn_id", "key", "reported", "granted", "after"),
    WRITER_TABLE,
    ids=[row[0] for row in WRITER_TABLE],
)
def test_grant_of_a_writers_question(
    case: str,
    before: Any,
    rec: Any,
    turn_id: Any,
    key: str,
    reported: Any,
    granted: bool,
    after: Any,
) -> None:
    """A yes means "write and run this exact cell": the user must have seen the question, which
    a writer's run brings to the main conversation only with its report. Not seen: no grant,
    and the question stays for 6.1's rules to drop at the next message."""
    result = grant(before, rec, turn_id, "cell", key, reported=reported)
    assert result == (granted, before if after == KEPT else after), case


def runs_file(layout: Layout, *runs: dict[str, Any]) -> None:
    for run in runs:
        tr.record_run(
            layout,
            SESSION,
            # no done_turn key unless a row sets one: a run marked done before C5d3 has none
            dict({"ts": 0.5, "done_ts": None, "status": None}, **run),
        )


def test_reported_runs_reads_the_runs_only_for_a_writers_question(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    asking = {"done_turn": "p1"}  # the report reached the asking message's reply
    runs_file(
        layout,
        {"run_id": "wf_1", "done_ts": 1.5, "status": "completed", **asking},
        {"run_id": "wf_2", "done_ts": 1.25, "status": "failed", **asking},  # a failure's report
        {"run_id": "wf_3", "done_ts": 1.5, "status": tr.STOPPED, **asking},  # stopped: no report
        {"run_id": "wf_4"},  # not reported
        {"run_id": "wf_5", "done_ts": True, "status": "completed", **asking},
        {"run_id": "wf_6", "done_ts": "1.5", "status": "completed", **asking},
        {"run_id": "wf_7", "done_ts": 1, "status": None, **asking},
        # the report reached a later message's reply (a yes typed during the run), whatever
        # its time says; and a run marked done before C5d3, with no done_turn
        {"run_id": "wf_8", "done_ts": 1.5, "status": "completed", "done_turn": "p2"},
        {"run_id": "wf_10", "done_ts": 1.5, "status": "completed"},
    )
    assert reported_runs(layout, SESSION, pending(run_id="wf_9")) == {
        "wf_1": 1.5,
        "wf_2": 1.25,
        "wf_7": 1.0,
    }
    # the same runs for a question the later message asked: only wf_8 reached its reply
    assert reported_runs(layout, SESSION, pending(turn_id="p2", run_id="wf_9")) == {"wf_8": 1.5}
    assert reported_runs(layout, "sess-2", pending(run_id="wf_1")) == {}  # no runs file
    monkeypatch.setattr(tr, "find_runs", lambda *a: pytest.fail("read the runs"))
    assert reported_runs(layout, SESSION, pending()) == {}
    assert reported_runs(layout, SESSION, None) == {}


@pytest.mark.parametrize(
    ("done_ts", "status", "done_turn", "granted"),
    [(1.5, "completed", "p1", True), (None, None, "p1", False), (2.5, "completed", "p1", False)]
    + [(1.5, tr.STOPPED, "p1", False), (1.5, tr.STOPPED, None, False)]
    + [(1.5, "completed", "p2", False), (2.5, "completed", "p2", False)]
    + [(1.5, "completed", None, False)],
    ids=[
        "reported before",
        "not reported",
        # in the asking message's reply, but its time reads at or after the yes (a clock
        # stepped back between the report and the yes): the time check, a second condition
        "reported after",
        "stopped",  # a killed notification in the asking message's reply: the status check
        "stopped by TaskStop",
        # a clock stepped back: the time reads before the yes, but the report reached the
        # yes message's reply (C5d3)
        "reported in the yes message",
        "reported in the yes message, after it opened",  # a yes typed during the run
        "reported as an orphan",
    ],
)
def test_use_grant_reads_the_sessions_runs(
    layout: Layout, done_ts: Any, status: Any, done_turn: Any, granted: bool
) -> None:
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0, run_id=RUN)
    runs_file(layout, {"run_id": RUN, "task_id": "task-1"})
    if done_ts is not None:
        tr.mark_done(
            layout, SESSION, None, task_id="task-1", status=status, now=done_ts, turn_id=done_turn
        )
    assert ledger.use_grant(SESSION, record(), "p2", "cell", KEY) is granted
    assert ledger.pending(SESSION) == (None if granted else WRITER)
    assert TurnLedger(layout).pending(SESSION) == (None if granted else WRITER)


@pytest.mark.parametrize(
    ("done_ts", "status", "changed"),
    [(None, None, True), (1.5, tr.STOPPED, True), (1.5, "completed", False)],
    ids=["not reported", "stopped", "reported"],
)
def test_asked_directly_makes_a_writers_unreported_question_the_main_conversations(
    layout: Layout, done_ts: Any, status: Any, changed: bool
) -> None:
    """The main conversation asks the user itself (E122 "repeated", design §6.4): a question
    whose run won't bring it to the user stops waiting for its report; one whose run reported
    is left as it is (a later yes is granted either way)."""
    ledger = TurnLedger(layout)
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0, run_id=RUN)
    runs_file(layout, {"run_id": RUN, "task_id": "task-1"})
    if done_ts is not None:
        tr.mark_done(
            layout, SESSION, None, task_id="task-1", status=status, now=done_ts, turn_id="p1"
        )
    assert ledger.asked_directly(SESSION, "p2") is False  # another turn's question: kept
    assert ledger.pending(SESSION) == WRITER
    assert ledger.asked_directly(SESSION, "p1") is changed
    expected = pending() if changed else WRITER
    assert ledger.pending(SESSION) == expected and TurnLedger(layout).pending(SESSION) == expected
    assert ledger.use_grant(SESSION, record(), "p2", "cell", KEY)  # the yes after it grants
    assert ledger.pending(SESSION) is None


def test_asked_directly_reads_and_writes_nothing_for_the_main_conversations_question(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = TurnLedger(layout)
    assert ledger.asked_directly(SESSION, "p1") is False  # nothing pending
    ledger.set_pending(SESSION, "cell", KEY, "p1", now=1.0)
    monkeypatch.setattr(tr, "find_runs", lambda *a: pytest.fail("read the runs"))
    monkeypatch.setattr(TurnLedger, "_write", lambda *a: pytest.fail("wrote the ledger"))
    assert ledger.asked_directly(SESSION, "p1") is False
    assert ledger.pending(SESSION) == pending()
