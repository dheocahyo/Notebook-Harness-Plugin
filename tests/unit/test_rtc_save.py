"""a8's fix (design §6.13): nh asks the room to save after each of its writes and each run.

jupyter-collaboration saves a room only after ``document_save_delay`` with no change, so while a
cell prints nothing reaches the file; after a JupyterLab restart the room takes the file over its
YStore and the unsaved cells are gone. The integration tests (``test_lab_restart.py``) show the
loss and the fix against a real JupyterLab; these pin the pieces, including the two private parts
of jupyter-nbmodel-client and websockets they rely on (the drift job runs them against the latest
releases): the client's send queue, and a normal close ending the client's run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import jupyter_nbmodel_client.client as nbmodel_client
import pytest
from pycrdt import Decoder, Doc, Encoder, YMessageType, YSyncMessageType, handle_sync_message
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from nh_gateway._shared.paths import Layout
from nh_gateway.backend import rtc
from nh_gateway.backend.base import CellPatch, NewCell, NotebookRef
from nh_gateway.backend.rtc import (
    RAW_MESSAGE,
    NhNbModelClient,
    RtcDocument,
    save_message,
    save_reply,
)
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache

NB = "notebooks/01_eda.ipynb"


def decoded(message: bytes) -> tuple[int, str, int]:
    """Read a save request the way jupyter-collaboration's YDocWebSocketHandler does."""
    decoder = Decoder(message)
    return decoder.read_var_uint(), decoder.read_var_string(), decoder.read_var_uint()


def answer(reply: dict[str, Any]) -> bytes:
    """A RAW JSON answer, as YDocWebSocketHandler._encode_json_message writes it."""
    encoder = Encoder()
    encoder.write_var_uint(RAW_MESSAGE)
    encoder.write_var_string(json.dumps(reply))
    return encoder.to_bytes()


def kind(message: bytes) -> str:
    if message[0] == YMessageType.SYNC:
        return YSyncMessageType(message[1]).name.lower()
    if message[0] == RAW_MESSAGE:
        return f"raw:{decoded(message)[1]}"
    return "awareness"


def test_the_save_message_is_jupyter_collaborations_raw_save() -> None:
    assert RAW_MESSAGE == 2
    assert decoded(save_message(1)) == (2, "save", 1)
    assert decoded(save_message(300)) == (2, "save", 300)


def test_the_rooms_answers_are_read_as_jupyter_collaboration_writes_them() -> None:
    for status in ("success", "skipped", "failed"):
        reply = {"type": "save", "responseTo": 3, "status": status}
        assert save_reply(answer(reply)) == reply
    assert save_reply(answer({"type": "other"})) is None
    assert save_reply(answer([1, 2])) is None  # type: ignore[arg-type]
    assert save_reply(save_message(1)) is None  # a request, not an answer
    assert save_reply(bytes([RAW_MESSAGE, 5]) + b"\xff\xfe") is None
    assert save_reply(bytes([YMessageType.AWARENESS])) is None


# ---------------------------------------------------------------------------- the client


class Room:
    """The room's end of nh's connection, in memory, for upstream's real ``NbModelClient.run()``.

    It answers the sync, records what nh sends in order, and can hold nh's sends after writing
    them, as a congested connection does (websockets writes a message, then waits in ``drain()``).
    Its iteration calls ``recv()`` and ends on a normal close, as websockets' does; once closed,
    ``recv()`` raises again at once, as websockets' does (a listener that iterates again spins:
    after 50 such calls it raises AssertionError instead, so a regression fails, not hangs).
    """

    remote_address = ("127.0.0.1", 9)

    def __init__(self) -> None:
        self.doc = Doc()
        self.to_nh: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.read: list[bytes] = []
        self.congested: asyncio.Event | None = None
        self.closed = False
        self.recv_after_close = 0

    async def connect(self, *args: Any, **kwargs: Any) -> Room:  # jupyter_nbmodel_client's connect
        return self

    async def send(self, message: bytes) -> None:
        self.read.append(bytes(message))
        if message[0] == YMessageType.SYNC and message[1] == YSyncMessageType.SYNC_STEP1:
            reply = handle_sync_message(message[1:], self.doc)
            assert reply is not None
            self.to_nh.put_nowait(reply)
        if self.congested is not None:
            await self.congested.wait()

    async def recv(self) -> bytes:
        if not self.closed:
            message = await self.to_nh.get()
            if message is not None:
                return message
            self.closed = True
        self.recv_after_close += 1
        if self.recv_after_close > 50:
            raise AssertionError("the listener spins on the closed connection")
        raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            while True:
                yield await self.recv()
        except ConnectionClosedOK:
            return

    async def close(self) -> None:
        self.closed = True

    def sent_after_sync(self) -> list[str]:
        kinds = [kind(m) for m in self.read]
        return [k for k in kinds[kinds.index("sync_step1") + 1 :] if k != "awareness"]


@pytest.fixture
async def connected(monkeypatch) -> AsyncIterator[tuple[Room, NhNbModelClient, RtcDocument]]:
    """nh's room client running upstream's run() against :class:`Room`, synced."""
    room = Room()
    monkeypatch.setattr(nbmodel_client, "connect", room.connect)
    nb = NhNbModelClient("ws://127.0.0.1:9/room", path=NB, username="nh-agent")
    task = asyncio.create_task(nb.run())
    doc = RtcDocument(api=None, api_path=NB)  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(nb.wait_until_synced(), 5)
        doc.nb, doc._task = nb, task
        yield room, nb, doc
    finally:
        if room.congested is not None:
            room.congested.set()
        task.cancel()
        await asyncio.wait({task}, timeout=10)


async def eventually(check, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def cell(cell_id: str, source: str) -> list[NewCell]:
    return [NewCell(id=cell_id, cell_type="code", source=source, metadata={})]


async def test_the_save_goes_out_after_the_writes_queued_before_it(connected) -> None:
    """The request goes through run()'s own queue: even while the connection is congested (an
    update written, the sender waiting for the drain, the next update still queued), the room
    reads every write before the save, and ``save()`` doesn't wait for the network."""
    room, nb, doc = connected
    assert nb._send_queue is not None and nb._send_queue is rtc.send_queue(nb._doc.awareness)
    await doc.settle()
    before = len(room.sent_after_sync())  # what the sync itself sent
    room.congested = asyncio.Event()
    doc.insert(-1, cell("a", "x = 1"))
    await doc.settle()  # the sender wrote the insert's update and now waits for the drain
    doc.update([CellPatch(id="a", source="x = 2")])  # queued behind it
    await asyncio.wait_for(doc.save(), 1)
    assert room.sent_after_sync()[before:] == ["sync_update"]
    room.congested.set()
    await eventually(lambda: "raw:save" in room.sent_after_sync())
    assert room.sent_after_sync()[before:] == ["sync_update", "sync_update", "raw:save"]
    await doc.save()
    await eventually(lambda: room.sent_after_sync().count("raw:save") == 2)
    saves = [decoded(m) for m in room.read if m[0] == RAW_MESSAGE]
    assert saves == [(2, "save", 1), (2, "save", 2)]


async def test_a_normal_close_ends_the_run_and_unsyncs_the_document(connected) -> None:
    room, nb, doc = connected
    room.to_nh.put_nowait(None)  # the room closes the connection with 1000
    assert doc._task is not None
    with pytest.raises(rtc.RoomClosed):
        await asyncio.wait_for(asyncio.shield(doc._task), 10)
    assert room.recv_after_close == 1 and not doc.synced


def test_a_normal_close_ends_the_run_with_websockets_own_connection(tmp_path: Path) -> None:
    """The same against a real websockets server, which closes normally after the sync. Without
    the guard nh's event loop would spin forever, so the client runs in a process of its own."""
    script = tmp_path / "close.py"
    script.write_text(
        textwrap.dedent(
            """
            import asyncio, sys
            from pycrdt import Doc, handle_sync_message
            from websockets.asyncio.server import serve
            from nh_gateway.backend.rtc import NhNbModelClient, RoomClosed

            async def handler(ws):
                ws_doc = Doc()
                await ws.send(handle_sync_message((await ws.recv())[1:], ws_doc))
                await asyncio.sleep(0.3)
                await ws.close(int(sys.argv[1]))

            async def main():
                async with serve(handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    nb = NhNbModelClient(f"ws://127.0.0.1:{port}/room", username="nh-agent")
                    task = asyncio.create_task(nb.run())
                    await asyncio.wait_for(nb.wait_until_synced(), 10)
                    try:
                        await asyncio.wait_for(task, 20)
                    except RoomClosed as exc:
                        print("ended", exc.__cause__.rcvd.code, flush=True)

            asyncio.run(main())
            """
        )
    )
    src = Path(rtc.__file__).parents[2]  # nh_gateway's source tree, as tests/conftest.py adds it
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(src), os.environ.get("PYTHONPATH", "")]),
    }
    for code in (1000, 1001):
        done = subprocess.run(
            [sys.executable, str(script), str(code)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert done.stdout.strip() == f"ended {code}", (done.stdout, done.stderr[-2000:])


async def test_the_client_saves_only_once_connected_and_logs_a_save_not_made(caplog) -> None:
    caplog.set_level(logging.INFO, logger="nh_gateway.rtc")
    client = NhNbModelClient("ws://127.0.0.1:9/room", path=NB, username="nh-agent")
    assert client.request_save() is False  # no connection yet: nothing to queue on
    room = Room()
    client._connection, client._send_queue = room, asyncio.Queue()
    for status in ("success", "skipped", "failed"):
        await client._on_message(room, answer({"type": "save", "responseTo": 1, "status": status}))
    assert [r.getMessage() for r in caplog.records] == [
        f"the room did not save {NB}: skipped",
        f"the room did not save {NB}: failed",
    ]
    assert {r.levelno for r in caplog.records} == {logging.WARNING}
    assert client.request_save() and client.request_save()
    assert [decoded(m) for m in client._send_queue._queue] == [(2, "save", 1), (2, "save", 2)]  # type: ignore[attr-defined]


async def test_without_run_s_queue_the_client_says_so_once(caplog, monkeypatch) -> None:
    """Were upstream's internals to change, nh logs it and falls back to JupyterLab's autosave."""
    monkeypatch.setattr(rtc, "_no_queue_logged", False)
    caplog.set_level(logging.WARNING, logger="nh_gateway.rtc")
    for _ in range(2):  # two connections whose run() registered nothing nh can find
        client = NhNbModelClient("ws://127.0.0.1:9/room", path=NB, username="nh-agent")
        await client._on_message(Room(), answer({"type": "save", "status": "success"}))
        assert client.request_save() is False
    assert [r.getMessage() for r in caplog.records] == [
        "nh can't ask the room to save: jupyter-nbmodel-client's send queue was not found "
        "(its internals changed). JupyterLab's autosave still runs."
    ]


# ---------------------------------------------------------------------------- the document


class Nb:
    """A synced room client for RtcDocument."""

    def __init__(self) -> None:
        self.saves = 0
        self.synced = True

    def request_save(self) -> bool:
        self.saves += 1
        return True


async def room(nb: Nb | None) -> RtcDocument:
    doc = RtcDocument(api=None, api_path=NB)  # type: ignore[arg-type]
    doc.nb = nb  # type: ignore[assignment]
    doc._task = asyncio.create_task(asyncio.sleep(3600))
    return doc


async def test_the_document_asks_only_while_synced() -> None:
    nb = Nb()
    doc = await room(nb)
    try:
        await doc.save()
        assert nb.saves == 1
        nb.synced = False  # the room dropped: the next write reconnects first
        await doc.save()
        assert nb.saves == 1
    finally:
        assert doc._task is not None
        doc._task.cancel()
    await (await room(None)).save()  # never connected: nothing to do


# ---------------------------------------------------------------------------- the backend


class WriteDoc:
    """The backend's room for one notebook: the order of nh's writes and save requests."""

    upgraded_minor = False

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stuck: list[str] = []

    def insert(self, index: int, cells: list[NewCell]) -> None:
        self.calls.append("insert")

    def update(self, patches: list[CellPatch]) -> None:
        self.calls.append("update")

    def delete(self, ids: list[str]) -> list:
        self.calls.append("delete")
        return []

    def set_meta(self, key: str, value: Any) -> None:
        self.calls.append("set_meta")

    def stuck_cells(self, active: set[str]) -> list[str]:
        return [cell_id for cell_id in self.stuck if cell_id not in active]

    def end_run(self, cell_id: str, execution_count: int | None, notice: str | None = None) -> str:
        self.calls.append(f"end_run {cell_id}")
        self.stuck.remove(cell_id)
        return "ok"

    async def save(self) -> None:
        self.calls.append("save")


@pytest.fixture
def backend(tmp_path: Path, monkeypatch) -> Iterator[tuple[RtcBackend, WriteDoc, NotebookRef]]:
    project = tmp_path / "proj"
    (project / ".nh" / "state" / "running").mkdir(parents=True)
    rtc_backend = RtcBackend(Layout(project), ConfigCache(project))
    doc = WriteDoc()

    async def the_doc(ref: NotebookRef) -> WriteDoc:
        return doc

    monkeypatch.setattr(rtc_backend, "_doc", the_doc)
    ref = NotebookRef(abs_path=project / NB, api_path=NB, rel_path=NB)
    yield rtc_backend, doc, ref
    rtc_backend._pool.shutdown(wait=False)


async def test_every_backend_write_asks_the_room_to_save_after_it(backend) -> None:
    rtc_backend, doc, ref = backend
    await rtc_backend.insert_cells(
        ref, -1, [NewCell(id="c", cell_type="code", source="x", metadata={})]
    )
    await rtc_backend.update_cells(ref, [CellPatch(id="c", source="y")])
    await rtc_backend.delete_cells(ref, ["c"])
    await rtc_backend.set_notebook_meta(ref, "nh", {"v": 1})
    assert doc.calls == [
        "insert",
        "save",
        "update",
        "save",
        "delete",
        "save",
        "set_meta",
        "save",
    ]


async def test_cells_reset_from_an_earlier_session_are_saved(backend, monkeypatch) -> None:
    rtc_backend, doc, ref = backend
    doc.stuck = ["a", "b"]

    async def idle(_doc: Any) -> bool:
        return False

    monkeypatch.setattr(rtc_backend, "_session_busy", idle)
    await rtc_backend._reset_stuck(ref, doc)  # type: ignore[arg-type]
    assert doc.calls == ["end_run a", "end_run b", "save"]
    assert rtc_backend.take_notices(ref) == [
        "2 nh cell(s) were still marked running from an earlier nh session; nh marked them stopped."
    ]


async def test_a_finished_run_drops_its_record_and_asks_for_a_save(backend) -> None:
    rtc_backend, doc, _ref = backend
    record = rtc_backend._running_file("c")
    record.write_text(json.dumps({"cell_id": "c"}))
    rtc_backend._run_done("c", doc)  # type: ignore[arg-type]
    assert not record.exists()
    await asyncio.gather(*rtc_backend._saves)
    assert doc.calls == ["save"] and not rtc_backend._saves
