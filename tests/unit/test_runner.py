"""The request loop, the output flusher and run classification, without a kernel or a room."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any

import pytest

from nh_gateway.backend.kernel import KernelRequest
from nh_gateway.exec.flusher import OutputFlusher
from nh_gateway.exec.runner import error_info, strip_ansi


class Channel:
    def __init__(self) -> None:
        self.queue: queue.Queue[dict] = queue.Queue()

    def get_msg(self, timeout: float | None = None) -> dict:
        return self.queue.get(timeout=timeout)

    def get_msgs(self) -> list[dict]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out


class StubClient:
    """The slice of KernelWebSocketClient that KernelRequest uses; scripted kernel messages."""

    def __init__(self) -> None:
        self.iopub_channel = Channel()
        self.shell_channel = Channel()
        self.connection_ready = threading.Event()
        self.connection_ready.set()
        self.sent: list[dict] = []

    def execute(self, code: str, **kwargs: Any) -> str:
        self.sent.append({"code": code, **kwargs})
        return "req-1"

    def iopub(
        self,
        kind: str,
        parent: str | None = "req-1",
        parent_kind: str | None = "execute_request",
        **content: Any,
    ) -> None:
        header = {"msg_id": parent, "msg_type": parent_kind} if parent else {}
        self.iopub_channel.queue.put(
            {"header": {"msg_type": kind}, "parent_header": header, "content": content}
        )

    def reply(self, status: str = "ok", parent: str = "req-1", **content: Any) -> None:
        self.shell_channel.queue.put(
            {
                "header": {"msg_type": "execute_reply"},
                "parent_header": {"msg_id": parent},
                "content": {"status": status, **content},
            }
        )


def deadline(seconds: float):
    until = time.monotonic() + seconds
    return lambda: until


def test_request_follows_only_its_own_messages():
    client = StubClient()
    seen: list[str] = []
    started: list[bool] = []
    request = KernelRequest(
        client,
        "x = 1",
        on_message=lambda m: seen.append(m["header"]["msg_type"]),
        on_started=lambda: started.append(True),
    )
    client.iopub("stream", name="stdout", text="stale")  # queued before we sent: dropped
    request.send()
    assert client.sent[0]["stop_on_error"] is False and client.sent[0]["allow_stdin"] is False
    client.iopub("status", parent="someone-else", execution_state="busy")
    client.iopub("status", execution_state="busy")
    client.iopub("execute_input", execution_count=7, code="x = 1")
    client.iopub("stream", name="stdout", text="hi")
    client.iopub("status", execution_state="idle")
    client.reply("ok", parent="old-request")
    client.reply("ok", execution_count=7)
    assert request.pump(deadline(5)) == "idle"
    assert seen == ["status", "execute_input", "stream", "status"]
    assert started == [True] and request.execution_count == 7
    assert request.reply(1.0) == {"status": "ok", "execution_count": 7}


def test_silent_requests_do_not_store_history():
    client = StubClient()
    KernelRequest(client, "probe", silent=True).send()
    assert client.sent[0]["silent"] is True and client.sent[0]["store_history"] is False


def test_restart_while_running_is_lost():
    client = StubClient()
    request = KernelRequest(client, "sleep")
    request.send()
    client.iopub("status", execution_state="busy")
    client.iopub(
        "shutdown_reply", parent="x", parent_kind="shutdown_request", status="ok", restart=True
    )
    client.iopub("error", ename="KeyboardInterrupt", evalue="", traceback=[])
    client.iopub("status", execution_state="idle")
    assert request.pump(deadline(5)) == "lost"
    assert request.lost_reason == "the kernel was restarted"


def test_restart_while_queued_is_lost_after_a_grace_period(monkeypatch):
    monkeypatch.setattr("nh_gateway.backend.kernel.RESTART_GRACE_S", 0.3)
    client = StubClient()
    request = KernelRequest(client, "x")
    request.send()
    client.iopub("status", parent=None, parent_kind=None, execution_state="restarting")
    began = time.monotonic()
    assert request.pump(deadline(5)) == "lost"
    assert time.monotonic() - began < 2


def test_queued_request_that_the_new_kernel_runs_is_not_lost(monkeypatch):
    monkeypatch.setattr("nh_gateway.backend.kernel.RESTART_GRACE_S", 0.3)
    client = StubClient()
    request = KernelRequest(client, "x")
    request.send()
    client.iopub("shutdown_reply", parent="x", parent_kind="shutdown_request")
    client.iopub("status", execution_state="busy")
    client.iopub("status", execution_state="idle")
    assert request.pump(deadline(5)) == "idle"


def test_timeout_abort_and_dropped_connection():
    client = StubClient()
    request = KernelRequest(client, "x")
    request.send()
    assert request.pump(deadline(0.3)) == "timeout" and not request.started
    abort = threading.Event()
    abort.set()
    assert request.pump(deadline(5), abort) == "abort"
    client.connection_ready.clear()
    assert request.pump(deadline(5)) == "lost"
    assert request.reply(0.5) is None


def test_error_info_strips_ansi_and_finds_the_cell_line():
    outputs = [
        {
            "output_type": "error",
            "ename": "KeyError",
            "evalue": "'prce'",
            "traceback": [
                "\x1b[31m---------------------------------------------------------------------------\x1b[39m",
                "\x1b[31mKeyError\x1b[39m    Traceback (most recent call last)",
                "File \x1b[32m~/lib/pandas/core/frame.py:4102\x1b[39m, in DataFrame.__getitem__(self, key)",
                "Cell \x1b[32mIn[4]\x1b[39m, line 3",
                "\x1b[32m----> 3\x1b[39m df[\x1b[33m'prce'\x1b[39m]",
            ],
        }
    ]
    info = error_info(outputs)
    assert info is not None and info.ename == "KeyError" and info.line == 3
    assert "\x1b" not in info.traceback
    assert (
        error_info(
            [],
            {
                "status": "error",
                "ename": "E",
                "evalue": "v",
                "traceback": ['File "<string>", line 9'],
            },
        ).line
        == 9
    )
    assert error_info([{"output_type": "stream", "name": "stdout", "text": "x"}]) is None
    assert strip_ansi("\x1b]8;;http://x\x07link\x1b]8;;\x07") == "link"


class Target:
    """An in-memory stand-in for RtcDocument's output writes."""

    def __init__(self) -> None:
        self.generation = 1
        self.synced = True
        self.outputs: list[dict] = []
        self.writes: list[tuple[str, int]] = []  # (kind, entries touched)
        self.missing = False
        self.ensured = 0

    async def ensure(self) -> None:
        self.ensured += 1
        self.synced = True
        self.generation += 1
        self.outputs = []  # a fresh room document

    def write_outputs(self, cell_id: str, outputs: list[dict], previous: list[dict] | None) -> str:
        if self.missing:
            return "missing"
        if not self.synced:
            return "unsynced"
        if previous is None or len(previous) != len(self.outputs):
            self.outputs = list(outputs)
            self.writes.append(("full", len(outputs)))
            return "ok"
        touched = sum(1 for i, o in enumerate(outputs) if i >= len(previous) or o != previous[i])
        self.outputs = list(outputs)
        self.writes.append(("diff", touched))
        return "ok"


def stream(text: str, name: str = "stdout") -> dict:
    return {"header": {"msg_type": "stream"}, "content": {"name": name, "text": text}}


@pytest.mark.asyncio
async def test_flusher_writes_only_changes_and_rewrites_after_a_reconnect():
    target = Target()
    flusher = OutputFlusher(target, "c1", period=0.01)
    flusher.apply(stream("a\n"))
    assert flusher.flush() and target.writes == [("full", 1)]
    assert flusher.flush() and len(target.writes) == 1  # nothing new, nothing written
    flusher.apply(stream("b\n"))
    flusher.apply(
        {
            "header": {"msg_type": "display_data"},
            "content": {
                "data": {"text/plain": "1"},
                "metadata": {},
                "transient": {"display_id": "d"},
            },
        }
    )
    assert flusher.flush() and target.writes[-1] == ("diff", 2)
    assert target.outputs[0]["text"] == "a\nb\n" and "transient" not in target.outputs[1]
    assert "transient" not in flusher.snapshot()[1]

    target.synced = False  # the room dropped
    flusher.apply(stream("c\n"))
    flusher.start()
    await asyncio.sleep(0.1)
    await flusher.close()
    assert target.ensured >= 1
    assert target.writes[-1] == ("full", 3)  # the new room document gets every output again
    assert [o.get("text") for o in target.outputs] == ["a\nb\n", None, "c\n"]


@pytest.mark.asyncio
async def test_flusher_stops_when_the_cell_is_deleted():
    target = Target()
    flusher = OutputFlusher(target, "c1")
    target.missing = True
    flusher.apply(stream("x"))
    assert flusher.flush() and flusher.missing
    flusher.apply(stream("y"))
    await flusher.close()
    assert target.writes == []
    assert flusher.snapshot()[0]["text"] == "xy"


# ---------------------------------------------------------------------------- finding 20: stream text grows in place


class _Nb:
    """The slice of NbModelClient that RtcDocument's writes touch, over a real pycrdt document."""

    def __init__(self) -> None:
        import pycrdt

        self._lock = threading.Lock()
        self._changes_origin = "nh"
        self.synced = True
        ydoc = pycrdt.Doc()
        ycells = ydoc.get("cells", type=pycrdt.Array)
        ycells.append(
            pycrdt.Map(
                {
                    "id": "c1",
                    "cell_type": "code",
                    "source": pycrdt.Text("print(...)"),
                    "metadata": pycrdt.Map(),
                    "outputs": pycrdt.Array(),
                    "execution_count": None,
                }
            )
        )
        self._doc = type("YNotebook", (), {"ycells": ycells, "_ydoc": ydoc})()


class Frontend:
    """How JupyterLab 4.6 applies remote output changes (CodeCellModel._onSharedModelChanged): of a
    transaction's events it takes the first stream-text delta and applies it to its LAST output as
    "delete n from the end, then append"; array deltas add and remove whole outputs."""

    def __init__(self, cell) -> None:
        self.outputs: list[dict] = []
        self.stream_events: list[int] = []
        self.cell = cell  # pycrdt drops the subscription with the last reference to the Map
        self.subscription = cell.observe_deep(self._on)

    def _on(self, events) -> None:
        streams = [
            e for e in events if len(e.path) == 3 and e.path[0] == "outputs" and e.path[2] == "text"
        ]
        self.stream_events.append(len(streams))
        if streams:
            last = self.outputs[-1]
            for op in streams[0].delta:
                if "delete" in op:
                    last["text"] = last["text"][: len(last["text"]) - op["delete"]]
                if "insert" in op:
                    last["text"] += str(op["insert"])
        for event in events:
            if list(event.path) != ["outputs"]:
                continue
            at = 0
            for op in event.delta:
                if "retain" in op:
                    at += op["retain"]
                if "delete" in op:
                    del self.outputs[at : at + op["delete"]]
                if "insert" in op:
                    items = [i.to_py() if hasattr(i, "to_py") else dict(i) for i in op["insert"]]
                    self.outputs[at:at] = items
                    at += len(items)


async def _room() -> tuple:
    import pycrdt

    from nh_gateway.backend.rtc import RtcDocument

    doc = RtcDocument(api=None, api_path="nb.ipynb")  # type: ignore[arg-type]
    nb = _Nb()
    doc.nb = nb  # type: ignore[assignment]
    doc._task = asyncio.create_task(asyncio.sleep(3600))
    doc.generation = 1
    updates: list[int] = []
    nb._doc._ydoc.observe(lambda event: updates.append(len(event.update)))
    replica = pycrdt.Doc()
    nb._doc._ydoc.observe(lambda event: replica.apply_update(event.update))
    replica.apply_update(nb._doc._ydoc.get_update())
    frontend = Frontend(replica.get("cells", type=pycrdt.Array)[0])
    return doc, updates, replica, frontend


def _replica_outputs(replica) -> list[dict]:
    import pycrdt

    return replica.get("cells", type=pycrdt.Array)[0].to_py()["outputs"]


@pytest.mark.asyncio
async def test_growing_stream_sends_only_the_new_text():
    doc, updates, replica, frontend = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        chunk = "loss=0.123456 " * 140 + "\n"  # ~2 KB per flush, like the reviewer's repro
        for _ in range(100):
            flusher.apply(stream(chunk))
            assert flusher.flush()
        total = len(chunk) * 100
        assert sum(updates) < 1.5 * total, f"{sum(updates)} bytes sent for {total} of output"
        assert _replica_outputs(replica)[0]["text"] == chunk * 100
        assert frontend.outputs == _replica_outputs(replica)
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_stream_edits_stay_in_step_with_jupyterlab(monkeypatch):
    """Progress bars (\\r), new outputs, a second stream, clear_output and the head trim at the cap:
    JupyterLab's view (delete-from-end + append on the last output) always matches the document."""
    from nh_gateway.exec import docsafe

    monkeypatch.setattr(docsafe, "STREAM_KEEP_CHARS", 1000)
    monkeypatch.setattr(docsafe, "STREAM_SLACK_CHARS", 500)
    doc, updates, replica, frontend = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        steps = [
            stream("épochs ✓\n"),
            *[stream(f"\r{i:3d}% [{'#' * (i // 10):10s}]") for i in range(0, 101, 7)],
            stream("\n"),
            {
                "header": {"msg_type": "display_data"},
                "content": {"data": {"text/plain": "<Figure>"}, "metadata": {}},
            },
            stream("warning\n", "stderr"),
            *[stream(f"line {i:04d} " + "x" * 60 + "\n", "stderr") for i in range(60)],
            {"header": {"msg_type": "clear_output"}, "content": {"wait": False}},
            *[stream(f"after clear {i}\n") for i in range(5)],
        ]
        for message in steps:
            flusher.apply(message)
            assert flusher.flush()
            assert frontend.outputs == _replica_outputs(replica), message
            assert max(frontend.stream_events) <= 1, (
                "JupyterLab applies one stream delta per update"
            )
        texts = [o.get("text") for o in _replica_outputs(replica)]
        assert texts == ["".join(f"after clear {i}\n" for i in range(5))]
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_capped_stream_trims_its_head_in_rare_rewrites_and_exactly_at_the_end(monkeypatch):
    from nh_gateway.exec import docsafe

    monkeypatch.setattr(docsafe, "STREAM_KEEP_CHARS", 1000)
    monkeypatch.setattr(docsafe, "STREAM_SLACK_CHARS", 500)
    doc, updates, replica, frontend = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        raw = ""
        for i in range(100):
            line = f"step {i:05d} " + "y" * 88 + "\n"  # 100 chars per flush
            raw += line
            flusher.apply(stream(line))
            assert flusher.flush()
            saved = _replica_outputs(replica)[0]["text"]
            assert saved.endswith(line) and len(saved) < 1600
            assert frontend.outputs == _replica_outputs(replica)
        assert sum(updates) < 4 * len(raw)  # appends, plus a 1000-char rewrite every 500 chars
        await flusher.close()  # the final flush keeps exactly the cap
        saved = _replica_outputs(replica)[0]["text"]
        assert saved.endswith(raw[-1000:]) and saved.startswith("[nh: 9,000 earlier characters")
        assert frontend.outputs == _replica_outputs(replica)
        assert flusher.snapshot()[0]["text"] == raw  # the tools layer still sees everything
    finally:
        doc._task.cancel()


# ---------------------------------------------------------------------------- findings 19 and 21: classification


class StubApi:
    """REST for Runner: the kernel model's state and interrupts."""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.interrupts = 0

    async def call(self, fn, *args):
        return fn(*args)

    def kernel(self, kernel_id: str) -> dict:
        return {"id": kernel_id, "execution_state": self.state}

    def interrupt(self, kernel_id: str) -> None:
        self.interrupts += 1


class RunDoc(Target):
    def end_run(self, cell_id: str, execution_count, notice=None) -> str:
        self.ended = (execution_count, notice)
        return "ok"


def _runner(client: StubClient, api: StubApi):
    from nh_gateway.exec.runner import Runner

    return Runner(
        doc=RunDoc(),
        cell_id="c1",
        code="x",
        client=client,
        api=api,  # type: ignore[arg-type]
        kernel_id="k1",
        hard_timeout=30,
    )


@pytest.fixture
def fast_watch(monkeypatch):
    from nh_gateway.exec import runner

    monkeypatch.setattr(runner, "POLL_S", 0.05)
    monkeypatch.setattr(runner, "IDLE_LOST_S", 0.8)
    monkeypatch.setattr(runner, "REPLIED_LOST_S", 2.0)
    monkeypatch.setattr("nh_gateway.backend.kernel.POLL_S", 0.02)


@pytest.mark.asyncio
async def test_rest_idle_while_a_big_result_is_in_flight_is_not_lost(fast_watch):
    """Finding 21: REST reports idle while a huge message is still being decoded; two idle polls
    used to call the run lost. Now it takes a long quiet idle, and a reply keeps it alive."""
    client, api = StubClient(), StubApi("idle")
    execution = _runner(client, api).start()
    client.iopub("status", execution_state="busy")
    client.iopub("execute_input", execution_count=3, code="x")
    await asyncio.sleep(0.5)  # ten idle polls, nothing arriving yet
    client.reply("ok", execution_count=3)
    await asyncio.sleep(1.0)  # past IDLE_LOST_S, but the execute_reply is in
    assert not execution.future.done()
    client.iopub("execute_result", execution_count=3, data={"text/plain": "'xxx…'"}, metadata={})
    client.iopub("status", execution_state="idle")
    result = await asyncio.wait_for(execution.future, 5)
    assert result.status == "ok" and result.execution_count == 3


@pytest.mark.asyncio
async def test_a_kernel_that_forgot_the_request_is_lost_after_a_quiet_idle(fast_watch):
    client, api = StubClient(), StubApi("idle")
    began = time.monotonic()
    result = await asyncio.wait_for(_runner(client, api).start().future, 10)
    assert result.status == "lost" and "stopped running this cell" in result.note
    assert time.monotonic() - began >= 0.8


@pytest.mark.asyncio
async def test_keyboard_interrupt_from_jupyterlab_is_interrupted(fast_watch):
    """Finding 19: the user's stop button ends the run as 'interrupted', noting who stopped it."""
    client, api = StubClient(), StubApi("busy")
    execution = _runner(client, api).start()
    client.iopub("status", execution_state="busy")
    client.iopub("error", ename="KeyboardInterrupt", evalue="", traceback=["KeyboardInterrupt"])
    client.iopub("status", execution_state="idle")
    client.reply("error", ename="KeyboardInterrupt", evalue="", traceback=[])
    result = await asyncio.wait_for(execution.future, 5)
    assert result.status == "interrupted" and result.note == "interrupted from JupyterLab"
    assert api.interrupts == 0


@pytest.mark.asyncio
async def test_nh_interrupt_is_interrupted_without_a_note(fast_watch):
    client, api = StubClient(), StubApi("busy")
    runner = _runner(client, api)
    execution = runner.start()
    client.iopub("status", execution_state="busy")
    await asyncio.sleep(0.1)
    await runner.interrupt()
    client.iopub("error", ename="KeyboardInterrupt", evalue="", traceback=[])
    client.iopub("status", execution_state="idle")
    client.reply("error", ename="KeyboardInterrupt", evalue="", traceback=[])
    result = await asyncio.wait_for(execution.future, 5)
    assert result.status == "interrupted" and result.note == "" and api.interrupts == 1
