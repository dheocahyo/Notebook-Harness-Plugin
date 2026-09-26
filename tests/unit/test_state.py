"""Stale cells, kernel drift and last_cell.json: shapes, merging, damaged files and locking."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import nh_gateway
from nh_gateway import state
from nh_gateway._shared.paths import Layout
from nh_gateway.state import (
    LAST_CELL_FIELDS,
    DriftStore,
    StaleStore,
    file_lock,
    read_last_cell,
    write_last_cell,
)

NB = "notebooks/01_eda.ipynb"


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    (tmp_path / ".nh").mkdir()
    return Layout(tmp_path)


def on_disk(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- stale


def test_mark_records_reason_cause_turn_and_exec_count(layout: Layout) -> None:
    stale = StaleStore(layout)
    stale.mark(NB, ["nh-b", "c9"], reason="undo", by="nh-a", turn_id="t2", exec_counts={"nh-b": 7})

    assert stale.get(NB) == {
        "nh-b": {"reason": "undo", "by": "nh-a", "turn_id": "t2", "exec_count": 7},
        "c9": {"reason": "undo", "by": "nh-a", "turn_id": "t2", "exec_count": None},
    }
    assert stale.get("other.ipynb") == {}
    assert on_disk(layout.stale_json) == {NB: stale.get(NB)}


def test_marking_again_replaces_the_entry(layout: Layout) -> None:
    stale = StaleStore(layout)
    stale.mark(NB, ["x"], reason="undo", by="a", turn_id="t1", exec_counts={"x": 3})
    stale.mark(NB, ["x"], reason="upstream-edit", by="b", turn_id="t2", exec_counts={"x": 5})
    assert stale.get(NB)["x"] == {
        "reason": "upstream-edit",
        "by": "b",
        "turn_id": "t2",
        "exec_count": 5,
    }


def test_clear_removes_cells_then_the_notebook(layout: Layout) -> None:
    stale = StaleStore(layout)
    stale.mark(NB, ["x", "y"], reason="undo", by="a", turn_id="t1", exec_counts={})
    stale.mark("b.ipynb", ["z"], reason="undo", by="a", turn_id="t1", exec_counts={})

    stale.clear(NB, ["x", "never-marked"])
    assert set(stale.get(NB)) == {"y"}
    stale.clear(NB, {"y"})
    assert on_disk(layout.stale_json) == {"b.ipynb": stale.get("b.ipynb")}


def test_noops_do_not_touch_the_file(layout: Layout) -> None:
    stale = StaleStore(layout)
    stale.mark(NB, [], reason="undo", by="a", turn_id="t1", exec_counts={})
    stale.clear(NB, ["x"])
    stale.clear(NB, [])
    assert not layout.stale_json.exists()


def test_get_returns_copies(layout: Layout) -> None:
    stale = StaleStore(layout)
    stale.mark(NB, ["x"], reason="undo", by="a", turn_id="t1", exec_counts={})
    stale.get(NB)["x"]["reason"] = "tampered"
    assert stale.get(NB)["x"]["reason"] == "undo"


@pytest.mark.parametrize("content", ["not json", "[1, 2]", json.dumps({NB: [1, 2]})])
def test_damaged_stale_file_is_treated_as_empty_and_repaired(layout: Layout, content: str) -> None:
    layout.stale_json.parent.mkdir(parents=True)
    layout.stale_json.write_text(content, encoding="utf-8")
    stale = StaleStore(layout)

    assert stale.get(NB) == {}
    stale.mark(NB, ["x"], reason="undo", by="a", turn_id="t1", exec_counts={"x": 1})
    assert set(stale.get(NB)) == {"x"}


def test_non_dict_cell_entries_are_ignored_and_cleared(layout: Layout) -> None:
    layout.stale_json.parent.mkdir(parents=True)
    layout.stale_json.write_text(json.dumps({NB: {"x": "junk", "y": {"reason": "undo"}}}))
    stale = StaleStore(layout)
    assert stale.get(NB) == {"y": {"reason": "undo"}}
    stale.clear(NB, ["x", "y"])
    assert on_disk(layout.stale_json) == {}


# ---------------------------------------------------------------------------- drift


def test_drift_merges_names_for_the_same_kernel(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1", {"df": "changed by removed cell 'Drop rows'"})
    since = drift.get(NB)["since"]
    drift.add(NB, "k1", {"model": "defined by removed cell 'Fit'", "df": "newer reason"})

    entry = drift.get(NB)
    assert entry == {
        "kernel_id": "k1",
        "since": since,
        "names": {"df": "newer reason", "model": "defined by removed cell 'Fit'"},
    }


def test_a_new_kernel_starts_a_fresh_entry(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1", {"df": "old"})
    before = drift.get(NB)["since"]
    time.sleep(0.01)
    drift.add(NB, "k2", {"model": "new"})

    entry = drift.get(NB)
    assert entry is not None
    assert entry["kernel_id"] == "k2"
    assert entry["names"] == {"model": "new"}
    assert entry["since"] > before


def test_drift_clear_by_name_and_all(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1", {"df": "a", "model": "b"})
    drift.add("b.ipynb", "k9", {"x": "c"})

    drift.clear(NB, {"df"})
    assert drift.get(NB)["names"] == {"model": "b"}
    drift.clear(NB, {"model"})
    assert drift.get(NB) is None
    assert NB not in on_disk(layout.drift_json)

    drift.clear("b.ipynb")
    assert drift.get("b.ipynb") is None
    drift.clear("never.ipynb")


def test_drift_ignores_empty_and_damaged_entries(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1", {})
    assert not layout.drift_json.exists()

    layout.state.mkdir(parents=True)
    layout.drift_json.write_text(json.dumps({NB: {"kernel_id": "k1", "names": []}, "b": 3}))
    assert drift.get(NB) is None
    assert drift.get("b") is None
    drift.add(NB, "k1", {"df": "a"})
    assert drift.get(NB)["names"] == {"df": "a"}
    drift.clear("b", {"x"})
    assert "b" not in on_disk(layout.drift_json)


def test_stale_and_drift_share_no_data(layout: Layout) -> None:
    StaleStore(layout).mark(NB, ["x"], reason="undo", by="a", turn_id="t", exec_counts={})
    DriftStore(layout).add(NB, "k", {"df": "a"})
    assert set(on_disk(layout.stale_json)[NB]) == {"x"}
    assert on_disk(layout.drift_json)[NB]["names"] == {"df": "a"}


# ---------------------------------------------------------------------------- last cell


def test_last_cell_round_trip(layout: Layout) -> None:
    write_last_cell(
        layout,
        session_id="s1",
        notebook=NB,
        cell_id="nh-4f2a91c07b",
        title="Drop rows with missing price",
        exec=14,
        status="ok",
        turn_id="p1",
        retries_left=2,
        finished_at=1_790_000_000.5,
    )
    data = read_last_cell(layout)
    assert data == {
        "v": 1,
        "session_id": "s1",
        "notebook": NB,
        "cell_id": "nh-4f2a91c07b",
        "title": "Drop rows with missing price",
        "exec": 14,
        "status": "ok",
        "turn_id": "p1",
        "retries_left": 2,
        "finished_at": 1_790_000_000.5,
    }
    assert list(data) == ["v", *LAST_CELL_FIELDS]


def test_last_cell_is_replaced_whole(layout: Layout) -> None:
    write_last_cell(layout, cell_id="a", title="First", status="ok", finished_at=1.0)
    write_last_cell(layout, cell_id="b", status="running")
    data = read_last_cell(layout)
    assert data is not None
    assert (data["cell_id"], data["title"], data["status"], data["finished_at"]) == (
        "b",
        None,
        "running",
        None,
    )
    assert [p.name for p in layout.state.iterdir()] == ["last_cell.json"]  # no temp files left


def test_unknown_last_cell_fields_are_refused(layout: Layout) -> None:
    with pytest.raises(TypeError, match="exec_count"):
        write_last_cell(layout, cell_id="a", exec_count=3)
    assert not layout.last_cell.exists()


@pytest.mark.parametrize("content", [None, "{broken", "[1]", '"text"'])
def test_read_last_cell_tolerates_missing_or_damaged_files(
    layout: Layout, content: str | None
) -> None:
    if content is not None:
        layout.state.mkdir(parents=True)
        layout.last_cell.write_text(content, encoding="utf-8")
    assert read_last_cell(layout) is None


# ---------------------------------------------------------------------------- locking


def test_file_lock_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "x.lock"
    with file_lock(lock) as outer, file_lock(lock, timeout=0.05) as inner:
        assert outer
        assert not inner
    with file_lock(lock, timeout=0.05) as again:
        assert again


def test_a_stuck_lock_does_not_block_updates(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(state, "LOCK_TIMEOUT_S", 0.05)
    stale = StaleStore(layout)
    with file_lock(layout.locks / "state.lock"), caplog.at_level(logging.WARNING):
        stale.mark(NB, ["x"], reason="undo", by="a", turn_id="t", exec_counts={})
    assert set(stale.get(NB)) == {"x"}
    assert "stale.json stayed locked" in caplog.text


WORKER = """
import sys, time
from pathlib import Path
from nh_gateway._shared.paths import Layout
from nh_gateway.state import DriftStore, StaleStore
project, worker, count = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
layout = Layout(project)
stale, drift = StaleStore(layout), DriftStore(layout)
Path(project, f"ready-{worker}").touch()
while not Path(project, "go").exists():
    time.sleep(0.001)
for i in range(count):
    stale.mark("shared.ipynb", [f"w{worker}-{i}"], reason="undo", by="a", turn_id="t",
               exec_counts={f"w{worker}-{i}": i})
    drift.add("shared.ipynb", "k1", {f"name_{worker}_{i}": "undone"})
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


def test_four_processes_lose_no_updates(layout: Layout) -> None:
    run_workers(layout.project, WORKER, 4, "40")

    marked = StaleStore(layout).get("shared.ipynb")
    assert len(marked) == 4 * 40
    assert marked["w3-39"]["exec_count"] == 39
    entry = DriftStore(layout).get("shared.ipynb")
    assert entry is not None
    assert len(entry["names"]) == 4 * 40
