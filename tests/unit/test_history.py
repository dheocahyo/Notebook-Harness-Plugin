"""Undo history: same-turn folding, undo bookkeeping, recent-op lookup and crash-tolerant JSONL."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import nh_gateway
from nh_gateway import meta
from nh_gateway._shared.paths import Layout
from nh_gateway.history import INDEX_NAME, HistoryStore, Op

NB = "notebooks/01_eda.ipynb"


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    (tmp_path / ".nh").mkdir()
    return HistoryStore(Layout(tmp_path))


def before(source: str) -> dict:
    return {
        "source": source,
        "nh": {"v": 1, "role": "code", "uid": "nh-x"},
        "tags": ["nh-agent"],
        "note_source": "### Title\n\n- a\n- b",
        "note_nh": {"v": 1, "role": "note"},
        "execution_count": 4,
    }


def write(
    store: HistoryStore,
    *,
    op: str = "insert",
    uid: str = "nh-4f2a91c07b",
    turn: str = "t1",
    session: str = "s1",
    notebook: str = NB,
    source: str = "df = load()",
    prev: dict | None = None,
    defs: set[str] | None = None,
    index: int = 3,
) -> Op:
    return store.record(
        op=op,
        notebook=notebook,
        session_id=session,
        turn_id=turn,
        uid=uid,
        note_uid=uid + "-n" if op == "insert" else None,
        index=index,
        before=prev,
        after_source=source,
        defs=defs if defs is not None else {"df"},
    )


def index_lines(store: HistoryStore) -> list[dict]:
    text = store.index_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------- folding


def test_insert_followed_by_same_turn_retries_stays_one_insert(store: HistoryStore) -> None:
    first = write(store, source="df = pd.read_csv(PATH)", defs={"df"})
    time.sleep(0.002)
    write(store, op="edit", prev=before("df = pd.read_csv(PATH)"), source="x", defs={"df", "tmp"})
    last = write(
        store, op="edit", prev=before("x"), source="df = pd.read_csv(DATA_PATH)\n", defs={"df"}
    )

    assert [o.op_id for o in store.ops_for(first.uid)] == [first.op_id]
    assert last.op_id == first.op_id
    assert last.op == "insert"
    assert last.before is None  # undo deletes the cell instead of restoring a failed attempt
    assert last.attempts == 3
    assert last.ts == first.ts
    assert last.after_sha == meta.source_sha("df = pd.read_csv(DATA_PATH)")
    assert last.defs == ["df", "tmp"]  # the kernel may still hold names from any attempt
    assert last.note_uid == first.uid + "-n"
    assert store.last_ops(NB, "s1", ["t1"]) == [last]
    assert len(index_lines(store)) == 1


def test_edit_followed_by_same_turn_retries_keeps_the_original_before(store: HistoryStore) -> None:
    original = before("df = df.dropna()")
    write(store, uid="c1", op="edit", prev=original, source="df = df.dropna(subset=['p'])")
    folded = write(
        store, uid="c1", op="edit", prev=before("df = df.dropna(subset=['p'])"), source="ok"
    )

    assert folded.op == "edit"
    assert folded.before == original
    assert folded.attempts == 2
    assert store.ops_for("c1")[0].before == original
    assert folded.after_sha == meta.source_sha("ok")


def test_fold_updates_the_cells_latest_index(store: HistoryStore) -> None:
    write(store, index=3)
    folded = write(store, op="edit", prev=before("df = load()"), source="df = load(1)", index=5)
    assert folded.index == 5
    assert HistoryStore(Layout(store.dir.parent.parent)).ops_for(folded.uid)[0].index == 5


def test_a_later_turn_starts_a_new_op(store: HistoryStore) -> None:
    inserted = write(store, turn="t1")
    edited = write(store, turn="t2", op="edit", prev=before("df = load()"), source="df = 1")

    assert edited.op_id != inserted.op_id
    assert [o.op for o in store.ops_for(inserted.uid)] == ["insert", "edit"]
    assert store.last_ops(NB, "s1", ["t1", "t2"]) == [edited, inserted]
    assert len(index_lines(store)) == 2


def test_the_same_uid_in_another_notebook_is_a_separate_op(store: HistoryStore) -> None:
    a = write(store, notebook="a.ipynb")
    b = write(store, notebook="b.ipynb", op="edit", prev=before("x"))
    assert a.op_id != b.op_id
    assert {o.notebook for o in store.ops_for(a.uid)} == {"a.ipynb", "b.ipynb"}


def test_an_undone_op_is_never_folded_into(store: HistoryStore) -> None:
    first = write(store, uid="h1", op="edit", prev=before("human code"), source="nh code")
    store.mark_undone(first, turn_id="t1")
    again = write(store, uid="h1", op="edit", prev=before("human code"), source="nh code 2")

    assert again.op_id != first.op_id
    assert again.attempts == 1
    assert store.last_ops(NB, "s1", ["t1"]) == [again]


def test_record_rejects_inconsistent_calls(store: HistoryStore) -> None:
    with pytest.raises(ValueError):
        write(store, op="delete")
    with pytest.raises(ValueError):
        write(store, op="edit", prev=None)
    with pytest.raises(ValueError):
        write(store, op="insert", prev=before("x"))
    assert not store.dir.exists()


def test_record_raises_oserror_when_history_is_unwritable(tmp_path: Path) -> None:
    (tmp_path / ".nh").mkdir()
    (tmp_path / ".nh" / "history").write_text("not a directory")
    with pytest.raises(OSError):
        write(HistoryStore(Layout(tmp_path)))


# ---------------------------------------------------------------------------- lookup and undo


def test_last_ops_filters_by_notebook_session_and_turns(store: HistoryStore) -> None:
    a1 = write(store, uid="A", turn="t1")
    b2 = write(store, uid="B", turn="t2")
    a3 = write(store, uid="A", turn="t3", op="edit", prev=before("df = load()"))
    write(store, uid="C", turn="t4", session="s2")
    write(store, uid="D", turn="t3", notebook="other.ipynb")

    assert store.last_ops(NB, "s1", ["t1", "t2", "t3"]) == [a3, b2, a1]
    assert store.last_ops(NB, "s1", ["t3", "t2"]) == [a3, b2]
    assert [o.uid for o in store.last_ops(NB, "s2", ["t4"])] == ["C"]
    assert store.last_ops(NB, "s1", ["t4"]) == []
    assert store.last_ops(NB, "s1", []) == []
    assert store.last_ops("missing.ipynb", "s1", ["t1"]) == []


def test_undone_ops_are_skipped_and_the_previous_one_is_next(store: HistoryStore) -> None:
    load = write(store, uid="A", turn="t1")
    plot = write(store, uid="B", turn="t2")

    store.mark_undone(plot, turn_id="t3")

    assert plot.undone
    assert store.last_ops(NB, "s1", ["t1", "t2", "t3"]) == [load]
    reopened = HistoryStore(Layout(store.dir.parent.parent))
    assert [o.undone for o in reopened.ops_for("B")] == [True]
    assert reopened.last_ops(NB, "s1", ["t1", "t2", "t3"]) == [load]


def test_ops_for_lists_every_op_in_creation_order(store: HistoryStore) -> None:
    ins = write(store, turn="t1")
    ed = write(store, turn="t2", op="edit", prev=before("df = load()"))
    store.mark_undone(ed, turn_id="t3")
    assert [(o.op_id, o.undone) for o in store.ops_for(ins.uid)] == [
        (ins.op_id, False),
        (ed.op_id, True),
    ]
    # nh_undo(cell_id=...) takes the newest op that is still live
    assert next(o for o in reversed(store.ops_for(ins.uid)) if not o.undone) == ins
    assert store.ops_for("nh-unknown") == []


def test_after_sha_detects_a_later_human_edit(store: HistoryStore) -> None:
    op = write(store, source="df = load()\n")
    assert op.after_sha == meta.source_sha("df = load()")
    assert op.after_sha != meta.source_sha("df = load()  # checked by me")


def test_history_survives_a_new_store(store: HistoryStore) -> None:
    op = write(store, prev=None, defs={"df", "model"})
    reopened = HistoryStore(Layout(store.dir.parent.parent))
    assert reopened.ops_for(op.uid) == [op]


def test_unicode_sources_round_trip(store: HistoryStore) -> None:
    tricky = "label = 'a b'  # 📈 café"
    op = write(store, uid="h", op="edit", prev=before(tricky), source=tricky)
    [loaded] = HistoryStore(Layout(store.dir.parent.parent)).ops_for("h")
    assert loaded.before == op.before
    assert loaded.before is not None and loaded.before["source"] == tricky


# ---------------------------------------------------------------------------- damaged files


def test_corrupt_and_foreign_lines_are_skipped(store: HistoryStore) -> None:
    good = write(store, uid="A")
    path = store.dir / "A.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("not json at all\n\n")
        handle.write('{"v":1,"kind":"op","op_id":7,"op":"insert"}\n')  # wrong types
        handle.write('{"v":1,"kind":"op","op_id":"x","op":"rename","uid":"A"}\n')
        handle.write('[1, 2, 3]\n{"v":1,"kind":"undo","of":null,"uid":"A"}\n')
    with open(store.index_path, "a", encoding="utf-8") as handle:
        handle.write('{"v":1,"kind":"index","uid":["A"],"turn_id":{"t":1}}\n\x00\x00\n')

    assert store.ops_for("A") == [good]
    assert store.last_ops(NB, "s1", ["t1"]) == [good]


def test_a_record_glued_to_a_torn_line_is_recovered(store: HistoryStore) -> None:
    first = write(store, uid="A", turn="t1")
    path = store.dir / "A.jsonl"
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: len(whole) // 2], encoding="utf-8")  # a write cut short, no newline

    second = write(store, uid="A", turn="t2", op="edit", prev=before("df = load()"))

    lines = path.read_text(encoding="utf-8").split("\n")
    assert len([line for line in lines if line]) == 1  # the torn record and the new one share it
    assert store.ops_for("A") == [second]
    assert first.op_id not in {o.op_id for o in store.ops_for("A")}


def test_a_complete_record_missing_only_its_newline_is_kept(store: HistoryStore) -> None:
    first = write(store, uid="A", turn="t1")
    path = store.dir / "A.jsonl"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    second = write(store, uid="A", turn="t2", op="edit", prev=before("x"))
    assert store.ops_for("A") == [first, second]


def test_uids_sharing_a_file_name_stay_apart(store: HistoryStore) -> None:
    slash = write(store, uid="a/b", turn="t1")
    under = write(store, uid="a_b", turn="t2")
    index_like = write(store, uid="_ops", turn="t3")

    assert store.ops_for("a/b") == [slash]
    assert store.ops_for("a_b") == [under]
    assert store.ops_for("_ops") == [index_like]
    assert store.last_ops(NB, "s1", ["t1", "t2", "t3"]) == [index_like, under, slash]


# ---------------------------------------------------------------------------- properties


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.tuples(st.just("write"), st.integers(0, 2), st.integers(0, 2)),
            st.just(("undo", 0, 0)),
        ),
        max_size=25,
    )
)
def test_one_live_op_per_turn_and_cell(actions: list[tuple[str, int, int]]) -> None:
    """A model of plan §4.7: writes fold per (turn, uid) unless that op was undone."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".nh").mkdir()
        store = HistoryStore(Layout(Path(tmp)))
        model: list[dict] = []  # creation order
        seen_uids: set[str] = set()
        turns = ["t0", "t1", "t2"]
        for n, (action, turn_no, uid_no) in enumerate(actions):
            if action == "undo":
                live = store.last_ops(NB, "s1", turns)
                if live:
                    store.mark_undone(live[0], turn_id="t2")
                    next(m for m in reversed(model) if not m["undone"])["undone"] = True
                continue
            turn, uid = turns[turn_no], f"nh-{uid_no}"
            kind = "edit" if uid in seen_uids else "insert"
            prev = before(f"v{n}") if kind == "edit" else None
            write(store, uid=uid, turn=turn, op=kind, prev=prev, source=f"s{n}", defs={f"d{n}"})
            seen_uids.add(uid)
            match = next(
                (m for m in model if (m["turn"], m["uid"]) == (turn, uid) and not m["undone"]),
                None,
            )
            if match is None:
                model.append(
                    {
                        "turn": turn,
                        "uid": uid,
                        "op": kind,
                        "before": prev,
                        "undone": False,
                        "attempts": 1,
                        "sha": meta.source_sha(f"s{n}"),
                        "defs": {f"d{n}"},
                    }
                )
            else:
                match["attempts"] += 1
                match["sha"] = meta.source_sha(f"s{n}")
                match["defs"].add(f"d{n}")

        expected = [
            (m["turn"], m["uid"], m["op"], m["before"], m["attempts"], m["sha"], sorted(m["defs"]))
            for m in reversed(model)
            if not m["undone"]
        ]
        got = [
            (o.turn_id, o.uid, o.op, o.before, o.attempts, o.after_sha, o.defs)
            for o in HistoryStore(Layout(Path(tmp))).last_ops(NB, "s1", turns)
        ]
        assert got == expected


# ---------------------------------------------------------------------------- processes

WORKER = """
import os, sys, time
from pathlib import Path
from nh_gateway._shared.paths import Layout
from nh_gateway.history import HistoryStore
project, worker, count = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
store = HistoryStore(Layout(project))
Path(project, f"ready-{worker}").touch()
while not Path(project, "go").exists():
    time.sleep(0.001)
for i in range(count):
    uid = f"nh-w{worker}-{i}"
    store.record(op="insert", notebook="nb.ipynb", session_id=f"s{worker}", turn_id=f"t{worker}-{i}",
                 uid=uid, note_uid=uid + "-n", index=i, before=None,
                 after_source="x = 1\\n" * (1 + (i * 37) % 400), defs={f"name_{i}"})
    store.record(op="edit", notebook="nb.ipynb", session_id=f"s{worker}", turn_id=f"t{worker}-{i}",
                 uid=uid, note_uid=None, index=i, before={"source": "y" * ((i * 997) % 9000)},
                 after_source="x = 2", defs={f"retry_{i}"})
"""


def run_workers(project: Path, script: str, workers: int, *args: str) -> None:
    src = Path(nh_gateway.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(src)}
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(project), str(w), *args], env=env)
        for w in range(workers)
    ]
    deadline = time.monotonic() + 60
    while len(list(project.glob("ready-*"))) < workers and time.monotonic() < deadline:
        time.sleep(0.01)
    (project / "go").touch()
    assert [p.wait(timeout=120) for p in procs] == [0] * workers


def test_four_processes_append_without_torn_lines(tmp_path: Path) -> None:
    (tmp_path / ".nh").mkdir()
    run_workers(tmp_path, WORKER, 4, "60")

    store = HistoryStore(Layout(tmp_path))
    raw = (store.dir / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    assert len(raw) == 4 * 60
    assert all(json.loads(line)["kind"] == "index" for line in raw)
    for worker in range(4):
        turns = [f"t{worker}-{i}" for i in range(60)]
        ops = store.last_ops("nb.ipynb", f"s{worker}", turns)
        assert len(ops) == 60
        assert all(o.op == "insert" and o.attempts == 2 and o.before is None for o in ops)
        assert ops[0].uid == f"nh-w{worker}-59"
