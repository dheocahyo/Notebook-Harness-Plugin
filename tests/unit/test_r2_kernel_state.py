"""Round-2 regressions for the kernel group: kernel identities with an unknown incarnation, the
drift store's order and re-run baselines, the grouped 'Kernel ≠ notebook' lead, and running
records left by another nh process (V33)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nh_gateway._shared.paths import Layout
from nh_gateway.backend.base import CellView, NotebookRef, OutputSummary
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.state import DriftStore, same_kernel
from nh_gateway.tools.common import clear_rerun_drift, drift_lead

NB = "notebooks/01_eda.ipynb"
SPLIT = 'still holds results from the undone "Split the data" [2]'
SCALE = 'still holds results from the undone "Scale features" [3]'


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    (tmp_path / ".nh").mkdir()
    return Layout(tmp_path)


def code(
    cell_id: str, source: str, count: int | None, running: bool = False, error: str | None = None
) -> CellView:
    return CellView(
        id=cell_id,
        index=0,
        cell_type="code",
        source=source,
        metadata={},
        execution_count=count,
        running=running,
        summary=OutputSummary(error=error),
    )


# ---------------------------------------------------------------------------- identities


def test_an_unknown_incarnation_is_not_another_kernel() -> None:
    assert same_kernel("k1:10@1.5", "k1:10@1.5")
    assert same_kernel("k1:10@1.5", "k1:")  # nh couldn't look inside the busy kernel
    assert same_kernel("k1:", "k1:10@1.5")
    assert not same_kernel("k1:10@1.5", "k1:11@2.5")  # restarted: same id, new process
    assert not same_kernel("k1:10@1.5", "k2:")  # another kernel
    assert same_kernel("", "k1:10@1.5")  # undo couldn't reach the kernel at all


def test_drift_survives_an_add_with_an_unknown_incarnation(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1:10@1.5", {"total": SPLIT}, exec_count=4)
    drift.add(NB, "k1:", {"scaled": SCALE}, exec_count=6)
    entry = drift.get(NB)
    assert entry is not None and entry["kernel_id"] == "k1:10@1.5"
    assert list(entry["names"]) == ["total", "scaled"]
    assert entry["exec_counts"] == {"total": 4, "scaled": 6}
    drift.add(NB, "k2:20@3.5", {"other": SCALE})  # a new kernel starts afresh
    entry = drift.get(NB)
    assert entry is not None and list(entry["names"]) == ["other"]
    assert "exec_counts" not in entry


def test_a_name_recorded_again_becomes_the_newest(layout: Layout) -> None:
    drift = DriftStore(layout)
    drift.add(NB, "k1:1@1", {"a": SPLIT, "b": SPLIT}, exec_count=2)
    drift.add(NB, "k1:1@1", {"a": SCALE}, exec_count=5)
    entry = drift.get(NB)
    assert entry is not None and list(entry["names"].items()) == [("b", SPLIT), ("a", SCALE)]
    assert entry["exec_counts"] == {"a": 5, "b": 2}
    drift.clear(NB, {"a"})
    entry = drift.get(NB)
    assert entry is not None and entry["exec_counts"] == {"b": 2}


# ---------------------------------------------------------------------------- re-runs and the lead


def test_only_a_later_run_that_binds_the_name_clears_it(layout: Layout) -> None:
    svc = SimpleNamespace(drift=DriftStore(layout))
    ref = SimpleNamespace(rel_path=NB)
    svc.drift.add(NB, "k1:1@1", {"total": SPLIT, "scaled": SCALE}, exec_count=5)
    cells = [
        code("c1", "total = 1\ntotal", 3),  # ran before the undo
        code("c2", "other = total + 1", 7),  # ran since, but binds another name
    ]
    clear_rerun_drift(svc, ref, cells)  # type: ignore[arg-type]
    assert set(svc.drift.get(NB)["names"]) == {"total", "scaled"}
    cells.append(code("c3", "total = 2", 8))
    clear_rerun_drift(svc, ref, cells)  # type: ignore[arg-type]
    assert set(svc.drift.get(NB)["names"]) == {"scaled"}


# W5 + W6: only a fresh, successful binding means the kernel's value is the notebook's again
@pytest.mark.parametrize(
    "rerun",
    [
        code("c2", "total = total + 1", 9),  # builds on the undone value
        code("c2", "total += 1", 9),
        code("c2", "total['x'] = 1", 9),  # a mutation, not a binding
        code("c2", "total = compute()", 9, error="KeyError: 'price'"),  # failed before binding
    ],
)
def test_a_rerun_that_builds_on_the_value_keeps_the_warning(layout: Layout, rerun) -> None:
    svc = SimpleNamespace(drift=DriftStore(layout))
    ref = SimpleNamespace(rel_path=NB)
    svc.drift.add(NB, "k1:1@1", {"total": SPLIT}, exec_count=5)
    clear_rerun_drift(svc, ref, [rerun])  # type: ignore[arg-type]
    assert set(svc.drift.get(NB)["names"]) == {"total"}


def test_the_lead_groups_names_by_why_newest_first_and_counts_the_rest(layout: Layout) -> None:
    svc = SimpleNamespace(drift=DriftStore(layout))
    ref = SimpleNamespace(rel_path=NB)
    svc.drift.add(
        NB, "k1:1@1", {n: SPLIT for n in ["X_test", "X_train", "df", "y_test", "y_train"]}
    )
    [lead] = drift_lead(svc, ref)  # type: ignore[arg-type]
    assert lead.startswith(
        "Kernel ≠ notebook: `X_test`, `X_train`, `df`, `y_test`, `y_train` still hold results "
        'from the undone "Split the data" [2]. To rebuild:'
    )
    svc.drift.add(NB, "k1:1@1", {"X_scaled": SCALE, "X_scaled_2": SCALE})
    [lead] = drift_lead(svc, ref)  # type: ignore[arg-type]
    assert lead.startswith(
        "Kernel ≠ notebook: `X_scaled`, `X_scaled_2` still hold results from the undone "
        '"Scale features" [3]; `X_test`, `X_train`, `df`, `y_test` still hold results from the '
        'undone "Split the data" [2]; +1 more. To rebuild:'
    )
    svc.drift.clear(NB, {"X_scaled_2", "X_test", "X_train", "df", "y_test", "y_train"})
    [lead] = drift_lead(svc, ref)  # type: ignore[arg-type]
    assert lead.startswith('Kernel ≠ notebook: `X_scaled` still holds results from the undone "')


# ---------------------------------------------------------------------------- V33: foreign records


class StubDoc:
    """nh's synced copy of the notebook: which cells it marks running."""

    synced = True

    def __init__(self, running: set[str]) -> None:
        self.running = running

    def cells(self, outputs: str = "summary") -> list[CellView]:
        return [code(cell_id, "", None, cell_id in self.running) for cell_id in ("c1", "c2")]


@pytest.fixture
def unrelated():
    """A live process that isn't nh: a crashed gateway's pid, reused."""
    proc = subprocess.Popen(["/bin/sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


async def test_a_foreign_record_counts_only_while_its_cell_runs(tmp_path: Path, unrelated) -> None:
    project = tmp_path / "proj"
    (project / ".nh" / "state" / "running").mkdir(parents=True)
    layout = Layout(project)
    backend = RtcBackend(layout, ConfigCache(project))
    ref = NotebookRef(abs_path=project / NB, api_path=NB, rel_path=NB)
    doc = StubDoc(running=set())
    backend._docs[str(ref.abs_path)] = doc  # type: ignore[assignment]
    record = layout.running / "c1.json"

    def write(started: float, pid: int = unrelated) -> None:
        record.write_text(
            json.dumps(
                {"cell_id": "c1", "notebook": NB, "kernel_id": "k", "started": started, "pid": pid}
            )
        )

    try:
        write(time.time())  # just written: its running mark may still be on the way
        assert backend.live_run(ref) == "c1"
        doc.running = {"c1"}  # the other nh process really runs it
        write(time.time() - 3600)
        assert backend.live_run(ref) == "c1" and record.exists()
        doc.running = set()  # an old record for a cell that isn't running: pid reused
        assert backend.live_run(ref) is None and not record.exists()
        write(time.time(), pid=os.getpid())  # this process's own record is never foreign
        assert backend.live_run(ref) is None
        write(time.time() - 3600, pid=2**22 + 12345)  # its process is gone
        assert backend.live_run(ref) is None and not record.exists()
    finally:
        backend._docs.clear()
        await backend.aclose()
