"""Rounds 2 and 3 (V28, V29, V31, W3, W18, W19): JupyterLab's own copy of streamed outputs, and
who stopped a cell.

``JupyterLabView`` follows JupyterLab 4.6 (CodeCellModel._onSharedModelChanged and
OutputAreaModel), checked against a real JupyterLab in the browser:

- an added stream goes through processText, which drops ``\\r`` and ``\\b``;
- a stream-text delta is applied to the LAST output as "delete n from the end, then append";
- an inserted output is always appended, and a stream named like the last stream added is merged
  into the last output instead (whatever that output is; deleting outputs keeps the name);
- a deleted output is removed at its index.
"""

from __future__ import annotations

import asyncio
import re

import pycrdt
import pytest

from nh_gateway.exec import docsafe
from nh_gateway.exec.flusher import OutputFlusher
from nh_gateway.exec.runner import error_info, stopped_by_signal
from tests.unit.test_fake_backend import pair
from tests.unit.test_runner import (
    StubApi,
    StubClient,
    _Nb,
    _runner,
    fast_watch,  # noqa: F401  (a fixture)
    stream,
)

_CONTROL = re.compile(r"[\n\b\r]")


def process_text(index: int, text: str, existing: str = "") -> tuple[str, int]:
    """JupyterLab's processText: write ``text`` into ``existing`` at ``index``, like a terminal."""
    if not _CONTROL.search(text):
        return existing[:index] + text + existing[index + len(text) :], index + len(text)
    at, pos = index, 0
    while True:
        found = _CONTROL.search(text, pos)
        chunk = text[pos : found.start() if found else len(text)]
        existing = existing[:at] + chunk + existing[at + len(chunk) :]
        at += len(chunk)
        if found is None:
            return existing, at
        pos = found.start() + 1
        char = found.group()
        if char == "\b":
            if at > 0 and existing[at - 1] != "\n":
                existing = existing[: at - 1] + existing[at + 1 :]
                at -= 1
        elif char == "\r":
            while at > 0 and existing[at - 1] != "\n":
                at -= 1
        else:
            existing += "\n"
            at = len(existing)


class JupyterLabView:
    """The outputs JupyterLab shows for one cell of the room (see the module docstring)."""

    def __init__(self, cell) -> None:
        self.outputs: list[dict] = []
        self.removed = 0  # outputs deleted by array deltas
        self._last_stream_name = ""
        self._stream_index = 0
        self.cell = cell  # pycrdt drops the subscription with the last reference to the Map
        self.subscription = cell.observe_deep(self._on)

    def _add(self, output: dict) -> None:
        output = dict(output)
        if output.get("output_type") != "stream":
            self._last_stream_name = ""
            self.outputs.append(output)
            return
        if output["name"] == self._last_stream_name and self.outputs:
            last = self.outputs[-1]  # merged, whatever the last output is
            last["text"], self._stream_index = process_text(
                self._stream_index, output["text"], last.get("text", "")
            )
            return
        output["text"], self._stream_index = process_text(0, output["text"])
        self._last_stream_name = output["name"]
        self.outputs.append(output)

    def _on(self, events) -> None:
        streams = [
            e for e in events if len(e.path) == 3 and e.path[0] == "outputs" and e.path[2] == "text"
        ]
        if streams:  # pycrdt counts UTF-8 bytes (Yjs in the browser counts UTF-16 units)
            last = self.outputs[-1]
            for op in streams[0].delta:
                if "delete" in op:
                    data = last["text"].encode()
                    last["text"] = data[: len(data) - op["delete"]].decode()
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
                    self.removed += op["delete"]
                if "insert" in op:
                    for item in op["insert"]:
                        self._add(item.to_py() if hasattr(item, "to_py") else dict(item))


async def _room() -> tuple:
    from nh_gateway.backend.rtc import RtcDocument

    doc = RtcDocument(api=None, api_path="nb.ipynb")  # type: ignore[arg-type]
    nb = _Nb()
    doc.nb = nb  # type: ignore[assignment]
    doc._task = asyncio.create_task(asyncio.sleep(3600))
    doc.generation = 1
    replica = pycrdt.Doc()
    nb._doc._ydoc.observe(lambda event: replica.apply_update(event.update))
    replica.apply_update(nb._doc._ydoc.get_update())
    view = JupyterLabView(replica.get("cells", type=pycrdt.Array)[0])
    return doc, replica, view


def _room_outputs(replica) -> list[dict]:
    return replica.get("cells", type=pycrdt.Array)[0].to_py()["outputs"]


def _display(text: str, kind: str = "display_data", display_id: str = "d") -> dict:
    return {
        "header": {"msg_type": kind},
        "content": {
            "data": {"text/plain": text},
            "metadata": {},
            "transient": {"display_id": display_id},
        },
    }


LOSS = [stream(f"loss={v:.3f}\r") for v in (0.512, 0.518, 0.519, 0.527)]
SCENARIOS = {
    "loss_cr": LOSS,
    "epoch_loop": [
        *[stream(f"epoch {i} loss={1 / (i + 1):.3f}\r") for i in range(8)],
        stream("\n"),
        stream("done\n"),
    ],
    "display_then_cr": [_display("'a display'"), *LOSS, stream("\nend\n")],
    "stdout_then_tqdm": [
        stream("loading data\n"),
        *[stream(f"\r{i:3d}%|{'#' * (i // 5):20s}|", "stderr") for i in range(0, 101, 5)],
        stream("\n", "stderr"),
        stream("trained\n"),
    ],
    "backspace": [
        stream("\b"),
        *[stream(part) for i in range(5) for part in (str(i), "\b")],
        stream("!\n"),
    ],
    "wide_chars": [
        stream("é中😀 step 1\r"),
        stream("é中😀 step 22\r"),
        stream("é😀\r"),
        stream("\n😀 done\n"),
    ],
}


def test_doc_streams_hold_the_text_jupyterlab_shows():
    """V28: no \\r or \\b in stream text written to the notebook; other outputs are untouched."""
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "epoch 7 loss=0.125\r"},
        {"output_type": "stream", "name": "stderr", "text": "\b100%\n"},
        {"output_type": "display_data", "data": {"text/plain": "a\rb"}, "metadata": {}},
    ]
    saved = docsafe.doc_outputs(outputs)
    assert [o.get("text") for o in saved] == ["epoch 7 loss=0.125", "100%\n", None]
    assert saved[2]["data"]["text/plain"] == "a\rb"
    assert outputs[0]["text"] == "epoch 7 loss=0.125\r"  # the input is not changed


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_progress_lines_show_the_same_text_in_jupyterlab(name):
    """V28: in-place stream edits leave JupyterLab's copy equal to the notebook after every flush."""
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for message in SCENARIOS[name]:
            flusher.apply(message)
            assert flusher.flush()
            assert view.outputs == _room_outputs(replica), message
        await flusher.close()
        assert view.outputs == _room_outputs(replica)
        assert view.removed == 0, "streams grow in place; no output is replaced"
        if name == "loss_cr":
            assert view.outputs == [
                {"output_type": "stream", "name": "stdout", "text": "loss=0.527"}
            ]
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_the_view_shows_the_garbled_numbers_seen_in_the_browser(monkeypatch):
    """The view model reproduces V28 as the browser showed it when the doc keeps the \\r."""
    monkeypatch.setattr(docsafe, "_as_shown", lambda output: output)
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for message in LOSS:
            flusher.apply(message)
            assert flusher.flush()
        assert _room_outputs(replica)[0]["text"] == "loss=0.527\r"
        assert view.outputs[0]["text"] == "loss=0.27\r"
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_final_flush_never_replaces_an_earlier_stream(monkeypatch):
    """V29: a capped stream that is no longer last keeps its tail, so JupyterLab keeps the order."""
    monkeypatch.setattr(docsafe, "STREAM_KEEP_CHARS", 1000)
    monkeypatch.setattr(docsafe, "STREAM_SLACK_CHARS", 500)
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for message in (stream("x" * 1199 + "\n"), stream("y" * 299 + "\n")):  # inside the slack
            flusher.apply(message)
            assert flusher.flush()
        flusher.apply(stream("after\n", "stderr"))
        assert flusher.flush()
        before = view.removed
        await flusher.close()
        saved = _room_outputs(replica)
        assert [o["name"] for o in saved] == ["stdout", "stderr"]
        assert view.outputs == saved and view.removed == before
        assert saved[0]["text"].startswith("[nh: 200 earlier characters")  # the running start
        assert saved[0]["text"].endswith("y" * 299 + "\n")
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_final_flush_still_trims_the_last_stream_exactly(monkeypatch):
    monkeypatch.setattr(docsafe, "STREAM_KEEP_CHARS", 1000)
    monkeypatch.setattr(docsafe, "STREAM_SLACK_CHARS", 500)
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        flusher.apply(_display("'first'"))
        for message in (stream("x" * 1199 + "\n"), stream("y" * 299 + "\n")):
            flusher.apply(message)
            assert flusher.flush()
        await flusher.close()
        saved = _room_outputs(replica)
        assert saved[1]["text"].startswith("[nh: 500 earlier characters")
        assert view.outputs == saved and view.removed == 0
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps",
    [
        [  # a progress display updated above newer output
            _display("'v0'"),
            stream("after the display\n"),
            _display("'v1'", "update_display_data"),
            stream("end\n"),
            stream("warn\n", "stderr"),
            _display("'v2'", "update_display_data"),
        ],
        [  # clear_output(wait=True), then fewer outputs
            stream("a\n"),
            _display("'x'"),
            {"header": {"msg_type": "clear_output"}, "content": {"wait": True}},
            stream("b\n"),
        ],
    ],
    ids=["display_update", "clear_wait"],
)
async def test_an_earlier_output_that_changes_keeps_its_place(steps):
    """V29: JupyterLab appends a re-inserted output, so nh rewrites from the changed one on."""
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for message in steps:
            flusher.apply(message)
            assert flusher.flush()
            assert view.outputs == _room_outputs(replica), message
        await flusher.close()
        assert view.outputs == _room_outputs(replica)
    finally:
        doc._task.cancel()


CLEAR_WAIT = {"header": {"msg_type": "clear_output"}, "content": {"wait": True}}
# Loops that start with clear_output(wait=True) and write the same first output every time.
# Each inner list arrives between two flushes.
CLEAR_WAIT_LOOPS = {
    "header_then_stderr": [  # print('Training...'), then a stderr progress line
        group
        for e in range(3)
        for group in (
            [CLEAR_WAIT, stream("Training...\n")],
            [stream(f"\repoch {e} 100%", "stderr")],
        )
    ],
    "display_then_print": [  # display(Markdown('**title**')), then print(f'step {i}')
        group
        for i in range(3)
        for group in ([CLEAR_WAIT, _display("'title'")], [stream(f"step {i}\n")])
    ],
    "display_then_three_streams": [  # display, then stdout, stderr, stdout, all in one flush
        [
            CLEAR_WAIT,
            _display("'hdr'"),
            stream(f"loss {i}\n"),
            stream("w\n", "stderr"),
            stream(f"end {i}\n"),
        ]
        for i in range(3)
    ],
}

CLEAR_WAIT_SHOWN = {  # the stream texts JupyterLab and the notebook end with
    "header_then_stderr": ["Training...\n", "epoch 2 100%"],
    "display_then_print": [None, "step 2\n"],
    "display_then_three_streams": [None, "loss 2\n", "w\n", "end 2\n"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CLEAR_WAIT_LOOPS))
async def test_clear_output_wait_loops_show_the_same_outputs_in_jupyterlab(name):
    """W19: JupyterLab keeps the name of a deleted stream and merges the next one into the output
    before it, so outputs that shrank, or a rewrite starting at a stream, rewrite every output."""
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for group in CLEAR_WAIT_LOOPS[name]:
            for message in group:
                flusher.apply(message)
            assert flusher.flush()
            assert view.outputs == _room_outputs(replica), group
        await flusher.close()
        assert view.outputs == _room_outputs(replica)
        assert [o.get("text") for o in view.outputs] == CLEAR_WAIT_SHOWN[name]
    finally:
        doc._task.cancel()


@pytest.mark.asyncio
async def test_plain_streaming_still_grows_in_place():
    """W19: the rewrite rules leave the common case alone: one stream, only appended to."""
    doc, replica, view = await _room()
    try:
        flusher = OutputFlusher(doc, "c1")
        for i in range(20):
            flusher.apply(stream(f"line {i}\n"))
            assert flusher.flush()
        await flusher.close()
        assert view.outputs == _room_outputs(replica) and len(view.outputs) == 1
        assert view.removed == 0, "the stream was never re-inserted"
    finally:
        doc._task.cancel()


# ---------------------------------------------------------------------------- V31: who stopped it


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_watch")
async def test_keyboard_interrupt_raised_by_the_code_is_an_error():
    client, api = StubClient(), StubApi("busy")
    execution = _runner(client, api).start()
    client.iopub("status", execution_state="busy")
    error = {"ename": "KeyboardInterrupt", "evalue": "from the code", "traceback": []}
    client.iopub("error", **error)
    client.iopub("status", execution_state="idle")
    client.reply("error", **error)
    result = await asyncio.wait_for(execution.future, 5)
    assert result.status == "error" and result.note == ""
    assert result.error is not None and result.error.evalue == "from the code"


# IPython 9's traceback for a bare `raise KeyboardInterrupt` in a cell, colours included
BARE_RAISE = [
    "\x1b[31m" + "-" * 75 + "\x1b[39m",
    "\x1b[31mKeyboardInterrupt\x1b[39m                         Traceback (most recent call last)",
    "\x1b[36mCell\x1b[39m\x1b[36m \x1b[39m\x1b[32mIn[1]\x1b[39m\x1b[32m, line 1\x1b[39m\n"
    "\x1b[32m----> \x1b[39m\x1b[32m1\x1b[39m \x1b[38;5;28;01mraise\x1b[39;00m KeyboardInterrupt\n",
    "\x1b[31mKeyboardInterrupt\x1b[39m: ",
]
# The user stops `time.sleep(40)`; the cell catches it and raises KeyboardInterrupt('checkpoint
# saved'). IPython shows the frame's current line for SIGINT's exception too.
STOP_RAISED_AGAIN = [
    "KeyboardInterrupt                         Traceback (most recent call last)",
    "Cell In[3], line 5\n      3     time.sleep(40)\n      4 except KeyboardInterrupt:\n"
    "----> 5     raise KeyboardInterrupt('checkpoint saved')\n",
    "KeyboardInterrupt: ",
    "\nDuring handling of the above exception, another exception occurred:\n",
    "KeyboardInterrupt                         Traceback (most recent call last)",
    "Cell In[3], line 5\n      4 except KeyboardInterrupt:\n"
    "----> 5     raise KeyboardInterrupt('checkpoint saved')\n",
    "KeyboardInterrupt: checkpoint saved",
]


@pytest.mark.parametrize(
    ("evalue", "traceback", "stopped"),
    [
        ("", ["Cell In[2], line 2\n----> 2 time.sleep(40)\n", "KeyboardInterrupt: "], True),
        ("", [], True),
        ("", BARE_RAISE, False),
        (
            "",
            [
                "Cell In[7], line 4\n----> 4 stop()\n",
                "Cell In[7], line 2, in stop()\n----> 2     raise KeyboardInterrupt\n",
                "KeyboardInterrupt: ",
            ],
            False,
        ),
        (
            "",
            [
                "Cell In[9], line 5\n----> 5 time.sleep(40)\n",
                "File ~/lib/signals.py:8, in handler(sig, frame)\n---> 8     raise KeyboardInterrupt\n",
                "KeyboardInterrupt: ",
            ],
            True,
        ),
        (
            "",
            [
                "Cell In[4], line 6\n      5     print('saved')\n----> 6     raise\n",
                "KeyboardInterrupt: ",
            ],
            True,
        ),
        ("checkpoint saved", STOP_RAISED_AGAIN, True),
        (
            "from the code",
            ["Cell In[6], line 1\n----> 1 raise KeyboardInterrupt('from the code')\n"],
            False,
        ),
    ],
    ids=[
        "stop_during_sleep",
        "no_traceback",
        "bare_raise_in_the_cell",
        "raised_by_a_notebook_function",
        "raised_by_a_modules_signal_handler",
        "stop_caught_and_raised_again",
        "stop_raised_again_with_a_message",
        "raised_with_a_message",
    ],
)
def test_a_keyboard_interrupt_is_a_stop_unless_the_cell_raised_it(evalue, traceback, stopped):
    """W3, W18: SIGINT's exception has no message; the code's own has a message or a raise line."""
    output = {"output_type": "error", "ename": "KeyboardInterrupt", "evalue": evalue}
    error = error_info([{**output, "traceback": traceback}])
    assert error is not None and stopped_by_signal(error) is stopped


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_watch")
@pytest.mark.parametrize(
    ("evalue", "traceback", "status", "note"),
    [
        ("", BARE_RAISE, "error", ""),
        ("checkpoint saved", STOP_RAISED_AGAIN, "interrupted", "interrupted from JupyterLab"),
    ],
    ids=["bare_raise_in_the_cell", "stop_raised_again_with_a_message"],
)
async def test_the_runner_tells_the_users_stop_from_the_codes_keyboard_interrupt(
    evalue, traceback, status, note
):
    client, api = StubClient(), StubApi("busy")
    execution = _runner(client, api).start()
    client.iopub("status", execution_state="busy")
    error = {"ename": "KeyboardInterrupt", "evalue": evalue, "traceback": traceback}
    client.iopub("error", **error)
    client.iopub("status", execution_state="idle")
    client.reply("error", **error)
    result = await asyncio.wait_for(execution.future, 5)
    assert (result.status, result.note) == (status, note)
    assert api.interrupts == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_watch")
async def test_after_nh_interrupts_any_keyboard_interrupt_is_the_stop():
    """Code that catches nh's interrupt and raises its own KeyboardInterrupt was still stopped."""
    client, api = StubClient(), StubApi("busy")
    runner = _runner(client, api)
    execution = runner.start()
    client.iopub("status", execution_state="busy")
    await asyncio.sleep(0.1)
    await runner.interrupt()
    error = {"ename": "KeyboardInterrupt", "evalue": "cleanup interrupted", "traceback": []}
    client.iopub("error", **error)
    client.iopub("status", execution_state="idle")
    client.reply("error", **error)
    result = await asyncio.wait_for(execution.future, 5)
    assert result.status == "interrupted" and result.note == ""


@pytest.mark.asyncio
async def test_fake_backend_classifies_keyboard_interrupt_like_the_runner(tmp_path):
    from nh_gateway.backend.fake import FakeBackend

    backend = FakeBackend(tmp_path)
    ref = await backend.resolve_notebook("notebooks/01_eda.ipynb")
    await backend.open(ref)
    try:
        code = "raise KeyboardInterrupt('from the code')"
        await backend.insert_cells(ref, -1, pair("nh-0000000001", code))
        result = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
        assert result.status == "error" and result.note == ""
        await backend.insert_cells(ref, -1, pair("nh-0000000002", "raise KeyboardInterrupt"))
        result = await (await backend.start_execution(ref, "nh-0000000002", hard_timeout=10)).future
        assert result.status == "error" and result.note == ""  # its last frame is the raise line
    finally:
        await backend.aclose()
