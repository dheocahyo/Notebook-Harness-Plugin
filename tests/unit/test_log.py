"""The event log: envelope, token scrubbing, privacy, rotation and concurrent appends."""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import nh_gateway
from nh_gateway._shared.paths import Layout
from nh_gateway.log import ROTATED_NAME, EventLog


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    (tmp_path / ".nh").mkdir()
    return Layout(tmp_path)


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_envelope_comes_first(layout: Layout) -> None:
    started = time.time()
    EventLog(layout).emit(
        "cell_added", session_id="s1", turn_id="p1", ms={"total": 812}, note_words=31
    )

    [record] = lines(layout.log_file)
    assert list(record) == ["v", "ts", "event", "session_id", "turn_id", "ms", "note_words"]
    assert record["v"] == 1
    assert record["event"] == "cell_added"
    assert (record["session_id"], record["turn_id"]) == ("s1", "p1")
    assert started - 0.01 <= record["ts"] <= time.time() + 0.01


def test_envelope_defaults_and_cannot_be_overridden(layout: Layout) -> None:
    EventLog(layout).emit("inspect", v=9, ts="yesterday")
    [record] = lines(layout.log_file)
    assert record["v"] == 1
    assert isinstance(record["ts"], float)
    assert record["session_id"] is None
    assert record["turn_id"] is None


def test_tokens_are_scrubbed_everywhere(layout: Layout) -> None:
    EventLog(layout).emit(
        "probe",
        url="ws://127.0.0.1:8888/api/kernels/k/channels?session_id=x&token=abc123&y=1",
        err='connect failed: token=s3cret"; retry',
        nested={"urls": ["http://h/?TOKEN=zzz"], "token=kkk": 1},
    )

    text = layout.log_file.read_text(encoding="utf-8")
    for secret in ("abc123", "s3cret", "zzz", "kkk"):
        assert secret not in text
    [record] = lines(layout.log_file)  # a token next to a quote still leaves valid JSON
    assert record["url"].endswith("token=[redacted:token]&y=1")
    assert record["err"] == 'connect failed: token=[redacted:token]"; retry'
    assert record["nested"] == {
        "urls": ["http://h/?TOKEN=[redacted:token]"],
        "token=[redacted:token]": 1,
    }


def test_code_and_outputs_are_never_stored(layout: Layout) -> None:
    EventLog(layout).emit("cell_added", code="df = secret()", source="x", outputs=[1], chars=40)
    [record] = lines(layout.log_file)
    assert "code" not in record
    assert "source" not in record
    assert "outputs" not in record
    assert record["chars"] == 40


def test_values_are_made_json_safe(layout: Layout) -> None:
    EventLog(layout).emit(
        "cell_added",
        bullet_words=(12, 9),
        hints={"L113", "L101"},
        path=Path("notebooks/a.ipynb"),
        ratio=math.nan,
        big=math.inf,
        obj=object(),
    )
    [record] = lines(layout.log_file)
    assert record["bullet_words"] == [12, 9]
    assert record["hints"] == ["L101", "L113"]
    assert record["path"] == "notebooks/a.ipynb"
    assert record["ratio"] is None
    assert record["big"] is None
    assert record["obj"].startswith("<object object")


def test_outside_an_nh_project_nothing_is_written(tmp_path: Path) -> None:
    EventLog(Layout(tmp_path)).emit("inspect")
    assert not (tmp_path / ".nh").exists()


def test_a_failed_write_is_logged_not_raised(
    layout: Layout, caplog: pytest.LogCaptureFixture
) -> None:
    layout.log_file.mkdir()  # the log path is unusable
    with caplog.at_level(logging.WARNING):
        EventLog(layout).emit("inspect", url="http://h/?token=abc")
    assert "inspect not written" in caplog.text


def test_a_cyclic_value_is_logged_not_raised(
    layout: Layout, caplog: pytest.LogCaptureFixture
) -> None:
    cyclic: list = []
    cyclic.append(cyclic)
    with caplog.at_level(logging.WARNING):
        EventLog(layout).emit("inspect", bad=cyclic)
    assert "not written" in caplog.text
    EventLog(layout).emit("inspect")
    assert len(lines(layout.log_file)) == 1


def test_rotation_keeps_one_previous_file(layout: Layout) -> None:
    events = EventLog(layout, max_bytes=2000)
    for i in range(200):
        events.emit("probe", i=i, pad="x" * 50)

    current, previous = lines(layout.log_file), lines(layout.nh / ROTATED_NAME)
    assert previous
    assert not (layout.nh / "log.2.jsonl").exists()
    assert layout.log_file.stat().st_size < 2000 + 200
    kept = [r["i"] for r in previous + current]
    assert kept == list(range(kept[0], 200))  # nothing lost across the last rotation


WORKER = """
import sys, time
from pathlib import Path
from nh_gateway._shared.paths import Layout
from nh_gateway.log import EventLog
project, worker, count, max_bytes = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
events = EventLog(Layout(project), max_bytes=max_bytes)
Path(project, f"ready-{worker}").touch()
while not Path(project, "go").exists():
    time.sleep(0.001)
for i in range(count):
    events.emit("probe", session_id=f"s{worker}", worker=worker, i=i, pad=str(worker) * (i * 211 % 20000))
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


def test_four_processes_append_without_torn_lines(layout: Layout) -> None:
    count = 300
    run_workers(layout.project, WORKER, 4, str(count), str(2**30))

    records = lines(layout.log_file)  # json.loads fails on any torn or interleaved line
    assert len(records) == 4 * count
    for worker in range(4):
        mine = [r for r in records if r["worker"] == worker]
        assert sorted(r["i"] for r in mine) == list(range(count))
        assert all(r["pad"] == str(worker) * (r["i"] * 211 % 20000) for r in mine)


def test_four_processes_rotating_leave_only_whole_lines(layout: Layout) -> None:
    run_workers(layout.project, WORKER, 4, "300", str(256 * 1024))

    rotated = layout.nh / ROTATED_NAME
    records = lines(rotated) + lines(layout.log_file)
    assert records
    assert all(r["pad"] == str(r["worker"]) * (r["i"] * 211 % 20000) for r in records)
    assert not (layout.nh / "log.2.jsonl").exists()
