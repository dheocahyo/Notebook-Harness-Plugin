"""The notebook's collaboration room (plan §4.4 "RTC"): one long-lived client per notebook.

Every read and write runs synchronously on the event loop inside ``with nb._lock:`` with no
``await``, so remote updates (applied on the loop under the same lock) can't interleave. The lock
is a non-reentrant ``threading.Lock`` that every public ``NotebookModel`` method also takes, so
inside it this module touches only ``nb._doc.ycells``, ``ycell[...]``, ``nb._doc._ymeta``,
``nb._doc._ydoc.transaction`` and ``len(nb)``; calling a public model method there would deadlock.
A reconnect replaces ``nb`` (and the model's ``_doc``), so cells are always resolved by id at
write time, never kept across awaits.
"""

from __future__ import annotations

from . import rest  # first: it sets NO_PROXY before websockets is imported

# isort: split

import asyncio
import difflib
import json
import logging
import time
from typing import Any, Literal, cast

import nbformat
from jupyter_nbmodel_client import NbModelClient
from pycrdt import Array, Awareness, Decoder, Encoder, Map, Text, YMessageType
from pycrdt import read_message as _read_message
from websockets.exceptions import ConnectionClosedOK

from ..exec.docsafe import output_summary, safe_text, sanitize_for_doc
from ..meta import normalize_source
from ..policy.errors import NhError, scrub
from .base import (
    CellConflict,
    CellMissing,
    CellPatch,
    CellView,
    NewCell,
    OutputsMode,
    OutputSummary,
)

log = logging.getLogger("nh_gateway.rtc")

SYNC_TIMEOUT_S = 8.0
WS_MAX_BODY = 100 * 2**20
PRESENCE = {
    "username": "nh-agent",
    "name": "nh (agent)",
    "display_name": "nh (agent)",
    "initials": "nh",
    "color": "#6d28d9",
}
WriteState = Literal["ok", "unsynced", "missing"]
RAW_MESSAGE = 2  # jupyter-collaboration's message type for requests such as "save"


def save_message(request_id: int) -> bytes:
    """The room message that saves the notebook now: what JupyterLab sends for File → Save in a
    collaborative session. jupyter-collaboration handles it at once, with no save delay (and even
    when its ``document_save_delay`` is None, which turns only its autosave off)."""
    encoder = Encoder()
    encoder.write_var_uint(RAW_MESSAGE)
    encoder.write_var_string("save")
    encoder.write_var_uint(request_id)
    return encoder.to_bytes()


def save_reply(message: bytes) -> dict[str, Any] | None:
    """The room's answer to a save request, ``{"type": "save", "responseTo", "status"}`` with status
    "success", "skipped" (the room was loading the file) or "failed"; None for anything else."""
    try:
        decoder = Decoder(message)
        if decoder.read_var_uint() != RAW_MESSAGE:
            return None
        reply = json.loads(decoder.read_var_string())
    except Exception:  # not a JSON RAW message
        return None
    return reply if isinstance(reply, dict) and reply.get("type") == "save" else None


def send_queue(awareness: Any) -> asyncio.Queue[bytes] | None:
    """The queue ``NbModelClient.run()`` sends the room's messages from, in order, or None.

    run() keeps it in a local variable; the one place it is reachable is the awareness callback
    run() registers, a ``partial`` bound to it (jupyter-nbmodel-client 1.5's ``_on_awareness_event``
    in pycrdt's ``Awareness._subscriptions``). ``tests/unit/test_rtc_save.py`` pins this against
    the installed versions; the drift job against their latest releases.
    """
    subscriptions = getattr(awareness, "_subscriptions", None)
    if not isinstance(subscriptions, dict):
        return None
    for callback in subscriptions.values():
        for arg in getattr(callback, "args", ()):
            if isinstance(arg, asyncio.Queue):
                return arg
    return None


class RoomClosed(ConnectionError):
    """The room server closed nh's room connection normally (close code 1000, 1001 or 1005)."""


def end_run_on_clean_close(websocket: Any) -> None:
    """Have a normal close of this room connection end ``NbModelClient.run()``, as a dropped one does.

    jupyter-nbmodel-client's listener is ``while True: async for message in websocket``. websockets
    ends that iteration quietly on a normal close, and every later one ends at once without
    yielding: the listener would spin and freeze the gateway's event loop. The iteration calls the
    connection's ``recv()`` (as websockets documents), so this connection's ``recv`` raises
    :class:`RoomClosed` in place of ``ConnectionClosedOK``: run() ends, ``RtcDocument.synced``
    turns False and the next write reconnects.
    """
    recv = websocket.recv

    async def recv_or_raise(*args: Any, **kwargs: Any) -> Any:
        try:
            return await recv(*args, **kwargs)
        except ConnectionClosedOK as exc:
            raise RoomClosed(f"the room closed the connection ({exc})") from exc

    websocket.recv = recv_or_raise


_no_queue_logged = False


class NhNbModelClient(NbModelClient):
    """``NbModelClient`` that also applies peers' awareness, so presence works both ways, can ask
    the room to save, and ends its run when the room closes the connection normally.

    The room server only relays awareness, and clients renew theirs every 15 s; nh re-announces
    itself when a new peer appears, so a tab opened after nh connected shows nh right away.
    """

    _connection: Any = None  # the room connection, known from the first message it delivers
    _send_queue: asyncio.Queue[bytes] | None = None  # run()'s, found at that first message
    _save_requests = 0

    def request_save(self) -> bool:
        """Queue a save request (:func:`save_message`) behind the messages already queued, so the
        room has read every write nh made before it; never waits on the network. False before the
        room answered, or when run()'s queue wasn't found (logged once, at connect)."""
        queue = self._send_queue
        if queue is None:
            return False
        self._save_requests += 1
        queue.put_nowait(save_message(self._save_requests))
        return True

    def _connected(self, websocket: Any) -> None:
        """The room's first message on a connection: run() has registered its callbacks by now."""
        global _no_queue_logged
        self._connection = websocket
        end_run_on_clean_close(websocket)
        self._send_queue = send_queue(self._doc.awareness)
        if self._send_queue is None and not _no_queue_logged:
            _no_queue_logged = True
            log.warning(
                "nh can't ask the room to save: jupyter-nbmodel-client's send queue was not found "
                "(its internals changed). JupyterLab's autosave still runs."
            )

    async def _on_message(self, websocket: Any, message: bytes) -> None:
        if websocket is not self._connection:
            self._connected(websocket)
        if message and message[0] == RAW_MESSAGE:
            reply = save_reply(message)
            if reply is not None and reply.get("status") != "success":
                # warning: gateway.log keeps WARNING and up, and an unsaved write is what a
                # restart loses ("skipped": the room was loading the file; "failed": no write).
                # A save that a change to the document cancelled answers "success" too (save())
                log.warning("the room did not save %s: %s", self.path, reply.get("status"))
            return
        if message and message[0] == YMessageType.AWARENESS:
            awareness = cast(Awareness, self._doc.awareness)
            try:
                known = set(awareness.states)
                awareness.apply_awareness_update(_read_message(message[1:]), "remote")
                if set(awareness.states) - known - {awareness.client_id}:
                    awareness.set_local_state(awareness.get_local_state())
            except Exception as exc:  # a malformed awareness message must not end the session
                log.debug("ignored awareness update: %s", exc)
            return
        await super()._on_message(websocket, message)


# ---------------------------------------------------------------------------- ycell helpers (lock held)


def _py(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


def _text(value: Any) -> str:
    value = _py(value)
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


def _uid(ycell: Map) -> str | None:
    meta = _py(ycell.get("metadata"))
    nh = meta.get("nh") if isinstance(meta, dict) else None
    return nh.get("uid") if isinstance(nh, dict) else None


def _find(ycells: Array, cell_id: str) -> tuple[int, Map] | None:
    """A cell by id, else by ``metadata.nh.uid`` (ids renumbered by nbstripout keep the uid)."""
    cells = list(ycells)
    for index, ycell in enumerate(cells):
        if ycell.get("id") == cell_id:
            return index, ycell
    for index, ycell in enumerate(cells):
        if _uid(ycell) == cell_id:
            return index, ycell
    return None


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _meta_map(ycell: Map) -> Map:
    meta = ycell.get("metadata")
    if not isinstance(meta, Map):
        ycell["metadata"] = Map(dict(_py(meta) or {}))
        meta = ycell["metadata"]
    return meta


def _replace_key(target: Map, key: str, value: Any) -> None:
    if key in target:
        del target[key]
    if value is not None:
        target[key] = value


def _set_source(ycell: Map, new: str) -> None:
    """Patch the source Text with difflib opcodes, right to left, so concurrent typing elsewhere survives."""
    text = ycell.get("source")
    if not isinstance(text, Text):
        ycell["source"] = Text(new)
        return
    current = str(text)
    if current == new:
        return
    if len(current) * len(new) > 50_000_000:  # quadratic diff on huge sources: replace wholesale
        text.clear()
        text.insert(0, new)
        return
    opcodes = difflib.SequenceMatcher(None, current, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(opcodes):
        if tag == "equal":
            continue
        start = len(current[:i1].encode("utf-8"))  # pycrdt Text offsets are UTF-8 bytes
        if i2 > i1:
            del text[start : start + len(current[i1:i2].encode("utf-8"))]
        if j2 > j1:
            text.insert(start, new[j1:j2])


def _set_tags(meta: Map, add: tuple[str, ...], remove: tuple[str, ...]) -> None:
    current = [str(tag) for tag in (_py(meta.get("tags")) or [])]
    tags = [tag for tag in current if tag not in remove] + [
        tag for tag in add if tag not in current
    ]
    if tags != current:
        _replace_key(meta, "tags", tags or None)


def _apply_patch(ycell: Map, patch: CellPatch) -> None:
    if patch.source is not None:
        _set_source(ycell, safe_text(patch.source))
    if patch.drop_nh_metadata or patch.metadata is not None or patch.tags_add or patch.tags_remove:
        meta = _meta_map(ycell)
        if patch.drop_nh_metadata:
            _replace_key(meta, "nh", None)
        if patch.metadata is not None:
            _replace_key(meta, "nh", sanitize_for_doc(patch.metadata))
        if patch.tags_add or patch.tags_remove:
            _set_tags(meta, patch.tags_add, patch.tags_remove)
    if ycell.get("cell_type") != "code":
        return
    if patch.clear_outputs:
        outputs = ycell.get("outputs")
        if isinstance(outputs, Array):
            outputs.clear()
        else:
            ycell["outputs"] = Array()
    if patch.execution_count != "keep":
        ycell["execution_count"] = patch.execution_count


def _new_cell(cell: NewCell) -> dict[str, Any]:
    """Build (and validate) the nbformat cell before any lock is taken."""
    source = safe_text(cell.source)
    metadata = sanitize_for_doc(cell.metadata)
    if cell.cell_type == "code":
        return nbformat.v4.new_code_cell(source, id=cell.id, metadata=metadata)
    return nbformat.v4.new_markdown_cell(source, id=cell.id, metadata=metadata)


def _notice(text: str) -> dict[str, Any]:
    return {"output_type": "stream", "name": "stderr", "text": text}


def _youtput(output: dict[str, Any]) -> Any:
    """How jupyter_ydoc stores an output: a stream is a Map whose ``text`` is a Text (so it can grow)."""
    if output.get("output_type") == "stream":
        return Map({**output, "text": Text(_text(output.get("text")))})
    return output


def common_prefix(a: str, b: str) -> int:
    """Length of the common prefix of two (possibly megabyte-long) strings, compared in chunks."""
    limit = min(len(a), len(b))
    if a[:limit] == b[:limit]:
        return limit
    at, step = 0, 4096
    while at + step <= limit and a[at : at + step] == b[at : at + step]:
        at += step
    low, high = at, min(at + step, limit)
    while low < high:  # a[:low] == b[:low]; find the first difference in this chunk
        mid = (low + high + 1) // 2
        if a[at:mid] == b[at:mid]:
            low = mid
        else:
            high = mid - 1
    return low


def _grow_stream(yout: Any, old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Edit a stream output's Text in place: cut the changed tail, append the rest. False = replace it.

    JupyterLab applies remote stream edits only at the end of the cell's last output (it deletes
    ``n`` characters from the end, then appends), so callers use this for the last output only,
    and only with text JupyterLab adds unchanged (no ``\\r`` or ``\\b``: see ``doc_outputs``).
    Replacing the stream instead is worse when an output comes before it: JupyterLab merges the
    re-added stream into that output.
    """
    if not isinstance(yout, Map) or old.get("name") != new.get("name"):
        return False
    if old.get("output_type") != "stream" or new.get("output_type") != "stream":
        return False
    text = yout.get("text")
    if not isinstance(text, Text):
        return False
    before, after = _text(old.get("text")), _text(new.get("text"))
    if len(text) != len(before.encode("utf-8")):  # the room holds something else: replace it
        return False
    keep = common_prefix(before, after)
    start = len(before[:keep].encode("utf-8"))  # pycrdt Text offsets are UTF-8 bytes
    if start < len(text):
        del text[start:]
    if keep < len(after):
        text.insert(start, after[keep:])
    return True


# ---------------------------------------------------------------------------- the room


class RtcDocument:
    """One notebook's room connection plus every CRDT read and write nh makes on it."""

    def __init__(self, api: rest.Rest, api_path: str, label: str | None = None) -> None:
        self.api = api
        self.api_path = api_path
        self.label = label or api_path  # the project-relative path users see
        self.nb: NhNbModelClient | None = None
        self.generation = 0
        self.last_used = time.monotonic()
        self.upgraded_minor = False
        self._task: asyncio.Task[None] | None = None
        self._connecting = asyncio.Lock()
        self._summaries: dict[str, tuple[tuple[Any, ...], OutputSummary]] = {}
        self.file_id: str | None = (
            None  # the room's file id: it follows the file when it is renamed
        )
        self.checked_at = 0.0  # monotonic time the room was last matched against the path

    @property
    def synced(self) -> bool:
        return (
            self.nb is not None
            and self.nb.synced
            and self._task is not None
            and not self._task.done()
        )

    async def ensure(self) -> NhNbModelClient:
        """A synced client, (re)connecting if needed. E131/E132/E135 when the room can't be joined."""
        self.last_used = time.monotonic()
        if self.synced:
            assert self.nb is not None
            return self.nb
        async with self._connecting:
            if self.synced:
                assert self.nb is not None
                return self.nb
            await self._teardown()
            room = await self._open_room()
            nb = NhNbModelClient(
                self.api.room_url(room),
                path=self.api_path,
                username="nh-agent",
                ws_max_body_size=WS_MAX_BODY,
                close_timeout=5.0,
            )
            task = asyncio.create_task(nb.run(), name=f"nh-room:{self.api_path}")
            synced = asyncio.create_task(nb.wait_until_synced())
            done, _ = await asyncio.wait(
                {task, synced}, timeout=SYNC_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
            )
            if synced not in done:
                synced.cancel()
                cause = await self._stop(task)
                detail = f"(could not sync the room in {SYNC_TIMEOUT_S:.0f}s{': ' + cause if cause else ''})"
                raise NhError("E135", detail=scrub(detail))
            nb.set_local_state_field("user", PRESENCE)  # run() overwrote it on connect
            self.nb, self._task = nb, task
            self.file_id = str(room.get("fileId") or "") or None
            self.checked_at = time.monotonic()
            self.generation += 1
            return nb

    async def _open_room(self) -> dict[str, Any]:
        """The room for this path (PUT /api/collaboration/session). A rejected token propagates
        (``rest.Rejected``) so the backend can rediscover the server."""
        try:
            return await self.api.call(self.api.collab_session, self.api_path)
        except rest.Rejected:
            raise
        except rest.RestError as exc:
            if exc.status == 404:
                exists = await self.api.call(self.api.contents_exists, self.api_path)
                if exists:
                    raise NhError("E131") from None
                raise NhError("E132", notebook=self.label) from None
            raise NhError("E135", detail=f"({exc})") from None

    async def room_moved(self) -> bool:
        """True when the path now belongs to another file than the connected room (the notebook was
        renamed, and maybe a new file took its name). Raises like :meth:`ensure` when it is gone."""
        room = await self._open_room()
        self.checked_at = time.monotonic()
        file_id = str(room.get("fileId") or "") or None
        return self.file_id is not None and file_id != self.file_id

    @staticmethod
    async def _stop(task: asyncio.Task[None]) -> str:
        """Stop a run() task (its finally block flushes queued updates) and describe why it ended."""
        if not task.done():
            task.cancel()
        await asyncio.wait({task})
        if task.cancelled():
            return ""
        exc = task.exception()
        return f"{type(exc).__name__}: {exc}"[:300] if exc else ""

    async def _teardown(self) -> None:
        task, self._task, self.nb = self._task, None, None
        if task is not None:
            cause = await self._stop(task)
            if cause:
                log.info("room %s closed: %s", self.api_path, scrub(cause))

    async def close(self) -> None:
        await self._teardown()

    @staticmethod
    async def settle() -> None:
        """Let the sender task pick up what a write queued."""
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def save(self) -> None:
        """Ask the room to write the notebook to disk now, after one of nh's writes (design §6.13).

        jupyter-collaboration saves only after ``document_save_delay`` with no change, so while a
        cell prints (nh flushes its outputs every 200 ms) nothing reaches the file, and it doesn't
        save on shutdown; a room rebuilt after a JupyterLab restart then takes the file over its
        YStore, and whatever was never saved is gone. The request goes out after the write's
        update, through the same queue, and never waits on the network. The room saves on request
        even when its ``document_save_delay`` is None (autosave off): nh can't read that setting.

        Not a guarantee: while autosave is on, any change to the document before the room writes
        (a peer's keystroke, or nh's own next output flush on a notebook whose save takes longer
        than ``flusher.PERIOD_S``) cancels this save, and the room still answers "success"
        (jupyter-server-ydoc 3.0.4), so nothing is logged. The write then reaches the file at the
        next save that runs to its end (design §6.13, Known gaps).
        """
        nb = self.nb
        if not self.synced or nb is None:
            return
        nb.request_save()
        await self.settle()  # the sender picks up the write's update and the request

    def _client(self) -> NhNbModelClient:
        if not self.synced or self.nb is None:
            raise NhError("E135", detail="(the room disconnected; retry)")
        return self.nb

    def peers(self) -> int:
        return len(self.nb.get_connected_peers()) if self.synced and self.nb is not None else 0

    # ------------------------------------------------------------------ reads

    def cells(self, outputs: OutputsMode = "summary") -> list[CellView]:
        nb = self._client()
        with nb._lock:
            return [
                self._view(ycell, index, outputs)
                for index, ycell in enumerate(list(nb._doc.ycells))
            ]

    def _view(self, ycell: Map, index: int, mode: OutputsMode) -> CellView:
        cell_type = str(ycell.get("cell_type") or "code")
        view = CellView(
            id=str(ycell.get("id") or ""),
            index=index,
            cell_type=cell_type,
            source=_text(ycell.get("source")),
            metadata=dict(_py(ycell.get("metadata")) or {}),
        )
        if cell_type != "code":
            return view
        view.execution_count = _count(ycell.get("execution_count"))
        view.running = ycell.get("execution_state") == "running"
        if mode == "none":
            return view
        youts = ycell.get("outputs")
        size = len(youts) if isinstance(youts, Array) else len(_py(youts) or [])
        key = (view.execution_count, size, view.running)
        cached = self._summaries.get(view.id)
        if mode == "summary" and cached is not None and cached[0] == key and not view.running:
            view.summary = cached[1]
            return view
        outputs = list(_py(youts) or [])
        view.summary = output_summary(outputs)
        self._summaries[view.id] = (key, view.summary)
        if mode == "full":
            view.outputs = outputs
        return view

    def notebook_meta(self) -> dict[str, Any]:
        nb = self._client()
        with nb._lock:
            return dict(_py(nb._doc._ymeta.get("metadata")) or {})

    def nbformat(self) -> tuple[int, int]:
        nb = self._client()
        with nb._lock:
            meta = nb._doc._ymeta
            return int(meta.get("nbformat") or 4), int(meta.get("nbformat_minor") or 0)

    # ------------------------------------------------------------------ writes (one transaction each)

    def _upgrade_minor(self, nb: NhNbModelClient) -> None:
        """nbformat 4.0-4.4 files drop cell ids on save; the first nh write moves them to 4.5."""
        meta = nb._doc._ymeta
        if int(meta.get("nbformat") or 4) == 4 and int(meta.get("nbformat_minor") or 0) < 5:
            meta["nbformat_minor"] = 5
            self.upgraded_minor = True

    def insert(self, index: int, cells: list[NewCell]) -> None:
        nb = self._client()
        prepared = [nb._doc.create_ycell(_new_cell(cell)) for cell in cells]
        wanted = {cell.id for cell in cells}
        with nb._lock:
            ycells = nb._doc.ycells
            clash = wanted.intersection(ycell.get("id") for ycell in ycells)
            if clash or len(wanted) != len(cells):
                raise ValueError(f"cell id already in the notebook: {sorted(clash)}")
            total = len(nb)
            at = total if index < 0 or index > total else index
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                self._upgrade_minor(nb)
                for offset, ycell in enumerate(prepared):
                    ycells.insert(at + offset, ycell)

    def update(self, patches: list[CellPatch]) -> None:
        """Check every patch first (CellMissing/CellConflict, nothing written), then apply all at once."""
        nb = self._client()
        with nb._lock:
            ycells = nb._doc.ycells
            targets: list[tuple[Map, CellPatch]] = []
            for patch in patches:
                found = _find(ycells, patch.id)
                if found is None:
                    raise CellMissing(patch.id)
                ycell = found[1]
                if patch.base_source is not None:
                    current = _text(ycell.get("source"))
                    if normalize_source(current) != normalize_source(patch.base_source):
                        raise CellConflict(patch.id, current)
                targets.append((ycell, patch))
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                self._upgrade_minor(nb)
                for ycell, patch in targets:
                    _apply_patch(ycell, patch)

    def delete(self, ids: list[str]) -> list[CellView]:
        """Delete the cells that still exist; returns them (ids already gone are skipped)."""
        nb = self._client()
        with nb._lock:
            ycells = nb._doc.ycells
            doomed: dict[int, CellView] = {}
            for cell_id in ids:
                found = _find(ycells, cell_id)
                if found is not None and found[0] not in doomed:
                    doomed[found[0]] = self._view(found[1], found[0], "full")
            if doomed:
                with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                    self._upgrade_minor(nb)
                    for index in sorted(doomed, reverse=True):
                        del ycells[index]
        return [doomed[index] for index in sorted(doomed)]

    def set_meta(self, key: str, value: Any) -> None:
        nb = self._client()
        with nb._lock:
            ymeta = nb._doc._ymeta
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                self._upgrade_minor(nb)
                meta = ymeta.get("metadata")
                if not isinstance(meta, Map):
                    ymeta["metadata"] = Map(dict(_py(meta) or {}))
                    meta = ymeta["metadata"]
                _replace_key(meta, key, sanitize_for_doc(value))

    # ------------------------------------------------------------------ execution

    def begin_run(self, cell_id: str, expected_source: str | None = None) -> tuple[str, str]:
        """Mark a code cell running with no outputs. Returns ``(cell id, source)``.

        CellMissing if it is gone; CellConflict (nothing changed) when ``expected_source`` is given
        and the cell now holds something else, so the kernel never runs code nh didn't check.
        """
        nb = self._client()
        with nb._lock:
            found = _find(nb._doc.ycells, cell_id)
            if found is None:
                raise CellMissing(cell_id)
            ycell = found[1]
            if ycell.get("cell_type") != "code":
                raise CellMissing(cell_id)
            source = _text(ycell.get("source"))
            if expected_source is not None and normalize_source(source) != normalize_source(
                expected_source
            ):
                raise CellConflict(str(ycell.get("id")), source)
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                outputs = ycell.get("outputs")
                if isinstance(outputs, Array):
                    outputs.clear()
                else:
                    ycell["outputs"] = Array()
                ycell["execution_count"] = None
                ycell["execution_state"] = "running"
            return str(ycell.get("id")), source

    def write_outputs(
        self, cell_id: str, outputs: list[dict[str, Any]], previous: list[dict[str, Any]] | None
    ) -> WriteState:
        """Write doc-safe ``outputs`` into the cell, touching only entries that differ from ``previous``.

        A stream that is still growing (the last output written) gets only its new text appended,
        so every flush sends the new output, not the whole stream again. Any other change is
        written again with every output after it: JupyterLab appends a re-inserted output at the
        end, so replacing it alone would reorder the outputs. JupyterLab also merges an inserted
        stream into the output before it when its name matches the last stream JupyterLab added,
        even one deleted since. So when the outputs shrank (``clear_output(wait=True)``) or the
        first output written again is a stream, every output is written again: JupyterLab then
        re-adds them to an empty list.
        """
        if not self.synced or self.nb is None:
            return "unsynced"
        nb = self.nb
        with nb._lock:
            found = _find(nb._doc.ycells, cell_id)
            if found is None:
                return "missing"
            ycell = found[1]
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                youts = ycell.get("outputs")
                if not isinstance(youts, Array):
                    ycell["outputs"] = Array([_youtput(o) for o in outputs])
                    return "ok"
                start = 0  # outputs from here on are written again; 0 = all of them
                # the room holds what nh wrote last, and no output went away
                if previous is not None and len(youts) == len(previous) <= len(outputs):
                    changed = (i for i, old in enumerate(previous) if outputs[i] != old)
                    start = next(changed, len(previous))
                    last = len(previous) - 1
                    if start == last and _grow_stream(youts[last], previous[last], outputs[last]):
                        start += 1  # only the last stream changed, and it grew in place
                    elif start < len(previous) and outputs[start].get("output_type") == "stream":
                        start = 0  # JupyterLab could merge it into the output before it
                del youts[start:]
                youts.extend([_youtput(o) for o in outputs[start:]])
        return "ok"

    def end_run(
        self, cell_id: str, execution_count: int | None, notice: str | None = None
    ) -> WriteState:
        if not self.synced or self.nb is None:
            return "unsynced"
        nb = self.nb
        with nb._lock:
            found = _find(nb._doc.ycells, cell_id)
            if found is None:
                return "missing"
            ycell = found[1]
            with nb._doc._ydoc.transaction(origin=nb._changes_origin):
                if notice:
                    youts = ycell.get("outputs")
                    if isinstance(youts, Array):
                        youts.append(_youtput(_notice(notice)))
                ycell["execution_count"] = execution_count
                ycell["execution_state"] = "idle"
        return "ok"

    def stuck_cells(self, active: set[str]) -> list[str]:
        """nh cells still marked running although no run of ours is live (nh restarted mid-run)."""
        nb = self._client()
        with nb._lock:
            stuck = []
            for ycell in list(nb._doc.ycells):
                if ycell.get("execution_state") != "running" or ycell.get("cell_type") != "code":
                    continue
                meta = _py(ycell.get("metadata")) or {}
                cell_id = str(ycell.get("id"))
                if (meta.get("nh") or {}).get("role") == "code" and cell_id not in active:
                    stuck.append(cell_id)
            return stuck


def __getattr__(name: str) -> Any:
    # app.py imports RtcBackend from here; it lives in rtc_backend.py, which imports this module.
    if name == "RtcBackend":
        from .rtc_backend import RtcBackend

        return RtcBackend
    raise AttributeError(name)
