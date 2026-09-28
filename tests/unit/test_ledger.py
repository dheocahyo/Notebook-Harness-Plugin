"""TurnLedger v2 (policy/turn.py, design §6.1): v1 load, v2 save, the pending question, grant()."""

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


def pending(turn_id: str = "p1", kind: str = "cell", key: str = KEY) -> dict[str, Any]:
    return {"kind": kind, "key": key, "turn_id": turn_id, "ts": 1.0}


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
    assert saved(layout)["pending"] == {"kind": "cell", "key": KEY, "turn_id": "p6", "ts": 9.0}
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
    assert fresh.pending(SESSION) == {"kind": "rerun", "key": KEY, "turn_id": "p1", "ts": 3.0}


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
    ],
)
def test_a_bad_pending_loads_as_none(layout: Layout, bad: Any) -> None:
    write(layout, {"v": 2, "turns": {}, "pending": bad})
    assert TurnLedger(layout).pending(SESSION) is None
    assert valid_pending(bad) is None


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
    assert ledger.pending(SESSION) == {"kind": "cell", "key": KEY, "turn_id": "p1", "ts": 1.0}
    # A later turn's ask replaces an earlier turn's.
    assert ledger.set_pending(SESSION, "rerun", OTHER_KEY, "p2", now=3.0)
    assert ledger.pending(SESSION) == {
        "kind": "rerun",
        "key": OTHER_KEY,
        "turn_id": "p2",
        "ts": 3.0,
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
