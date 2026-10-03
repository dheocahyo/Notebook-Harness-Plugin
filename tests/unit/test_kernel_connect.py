"""Dead kernel connections (design §6.13): nh checks each new kernel websocket before using it.

With ipykernel 7.3.0 behind jupyter_server 2.21.1, a kernel websocket whose first message is
sent right after the handshake is sometimes dead: the kernel doesn't run its requests until
another connection to it opens.
``open_client`` sends nothing for ``CONNECT_SETTLE_S``, and when nh's own GET says the kernel is
idle, the connection must answer a ``kernel_info_request`` (its busy or its reply), else nh
connects again, a bounded number of times, still listening on the unanswered connections; the
first to answer is used and the others are closed in a thread (never the kernel). A probe holds
``probe_lock`` while it connects, and a run that starts meanwhile has it retire its client at its
end. These tests use a stand-in client and kernel; the real stack is
``tests/integration/test_kernel_connections.py``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nh_gateway._shared.paths import Layout
from nh_gateway.backend import kernel, rest
from nh_gateway.backend.base import CellMissing, NotebookRef, ServerInfo
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.exec import probes
from nh_gateway.policy.errors import NhError

SERVER = ServerInfo(url="http://127.0.0.1:8888/", token="tok", root_dir=Path("/nowhere"))
KID = "0f2b5c1e-1111-4222-8333-944445555666"
SETTLE_S = 0.02  # design §6.13: 0 of 850 first messages died this long after the handshake
RETRY_SETTLE_S = 0.1  # a connection replacing a dead one: immediate retries died 3 of 24 times
REPLACED = "connecting again"
LATE = "answered nh's kernel_info_request late"
UNANSWERED = "none of 3 new connections answered"


class Channel:
    def __init__(self) -> None:
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()

    def put(self, msg: dict[str, Any]) -> None:
        self.messages.put(msg)

    def get_msg(self, timeout: float | None = None) -> dict[str, Any]:
        return self.messages.get(timeout=timeout)

    def get_msgs(self) -> list[dict[str, Any]]:
        found = []
        while True:
            try:
                found.append(self.messages.get_nowait())
            except queue.Empty:
                return found

    def peek(self) -> list[dict[str, Any]]:
        return list(self.messages.queue)


def message(kind: str, parent: dict[str, Any], **content: Any) -> dict[str, Any]:
    return {
        "header": {"msg_type": kind, "msg_id": uuid.uuid4().hex},
        "msg_type": kind,
        "parent_header": parent,
        "content": content,
    }


class Kernel:
    """The server and kernel behind the stand-in clients.

    ``state`` is what nh's GET of the kernel model reads (None: 404; an exception: raised), or
    ``states``, one per GET while it lasts; the first ``dead`` connections never answer anything.
    The check's answer: ``check_frames`` are the frames it gets (of busy, idle, reply),
    ``check_hold`` those held back until the next request is sent (after
    ``KernelRequest.send()``'s drain), ``slow`` the seconds before the rest arrive. ``run_delay``
    holds a run's (not a probe's) whole answer back that long. ``slow_close`` is how long a dead
    connection's stop() takes; ``noise`` puts other requests' messages on a dead connection.
    ``stuck``: a dead connection's requests are answered once another connection opens, as on
    the real stack (design §6.13), not never.
    """

    def __init__(self, state: Any = "idle", dead: int = 0) -> None:
        self.state = state
        self.states: list[Any] = []
        self.dead = dead
        self.check_frames = {"busy", "idle", "reply"}
        self.check_hold: set[str] = set()
        self.slow = 0.0
        self.run_delay = 0.0
        self.slow_close = 0.0
        self.noise = False
        self.stuck = False
        self.exists = True
        self.clients: list[FakeKernelClient] = []
        self.shutdowns = 0
        self.count = 0
        self.lock: threading.Lock | None = None  # a lock that must be held while checking
        self.lock_held: list[bool] = []
        self.on_check: Any = None  # called at each check, as it is sent

    def model(self) -> dict[str, Any] | None:
        state = self.states.pop(0) if self.states else self.state
        if isinstance(state, Exception):
            raise state
        return None if state is None else {"id": KID, "execution_state": state}


class Socket:
    """Stands in for jupyter-kernel-client's KernelWebSocketClient."""

    def __init__(self, kernel_: Kernel, dead: bool) -> None:
        self.kernel = kernel_
        self.dead = dead
        self.connection_ready = threading.Event()
        self.shell_channel = Channel()
        self.iopub_channel = Channel()
        self.sent: list[tuple[str, str, float]] = []  # (msg_type, msg_id, monotonic time)
        self.opened_at = 0.0
        self.held: list[tuple[Channel, dict[str, Any]]] = []  # (where it goes, the message)
        self.waiting: list[dict[str, Any]] = []  # a stuck connection's unanswered checks

    @property
    def channels_running(self) -> bool:
        return self.connection_ready.is_set()

    def _request(self, kind: str) -> dict[str, Any]:
        msg_id = uuid.uuid4().hex
        self.sent.append((kind, msg_id, time.monotonic()))
        for channel, msg in self.held:  # an earlier request's late messages, after the drain
            channel.put(msg)
        self.held = []
        return {"msg_id": msg_id, "msg_type": kind}

    def _frames(
        self, parent: dict[str, Any], iopub: list[dict[str, Any]], reply: dict
    ) -> list[tuple[str, Channel, dict[str, Any]]]:
        return [
            ("busy", self.iopub_channel, message("status", parent, execution_state="busy")),
            *(("output", self.iopub_channel, msg) for msg in iopub),
            ("idle", self.iopub_channel, message("status", parent, execution_state="idle")),
            ("reply", self.shell_channel, reply),
        ]

    def _deliver(self, frames: list[tuple[str, Channel, dict[str, Any]]]) -> None:
        for _name, channel, msg in frames:
            channel.put(msg)

    def _answer(
        self, parent: dict[str, Any], iopub: list[dict[str, Any]], reply: dict, delay: float = 0.0
    ) -> None:
        if self.dead:
            return
        frames = self._frames(parent, iopub, reply)
        if delay:
            threading.Timer(delay, self._deliver, args=(frames,)).start()
        else:
            self._deliver(frames)

    def _answer_check(self, parent: dict[str, Any]) -> None:
        if self.dead:
            if self.kernel.stuck:
                self.waiting.append(parent)
            return
        reply = message("kernel_info_reply", parent, status="ok", protocol_version="5.4")
        frames = [f for f in self._frames(parent, [], reply) if f[0] in self.kernel.check_frames]
        self.held.extend(
            (channel, msg) for name, channel, msg in frames if name in self.kernel.check_hold
        )
        now = [f for f in frames if f[0] not in self.kernel.check_hold]
        if self.kernel.slow:
            threading.Timer(self.kernel.slow, self._deliver, args=(now,)).start()
        else:
            self._deliver(now)

    def kernel_info(self) -> str:
        if self.kernel.lock is not None:
            self.kernel.lock_held.append(self.kernel.lock.locked())
        if self.kernel.on_check is not None:
            self.kernel.on_check()
        parent = self._request("kernel_info_request")
        self._answer_check(parent)
        if self.dead and self.kernel.noise:  # other requests' traffic, during the check too
            other = {"msg_id": uuid.uuid4().hex, "msg_type": "kernel_info_request"}
            later = [
                (self.iopub_channel, message("status", other, execution_state="busy")),
                (self.shell_channel, message("kernel_info_reply", other, status="ok")),
            ]
            threading.Timer(0.03, lambda: [ch.put(m) for ch, m in later]).start()
        return parent["msg_id"]

    def execute(self, code: str, *, silent: bool = False, **_: Any) -> str:
        parent = self._request("execute_request")
        if silent:  # a probe: its answer names its own request
            outputs = [
                message("display_data", parent, data={probes.PROBE_MIME: {"answer": parent}})
            ]
            reply = message("execute_reply", parent, status="ok")
        else:
            self.kernel.count += 1
            count = self.kernel.count
            outputs = [
                message("execute_input", parent, code=code, execution_count=count),
                message("stream", parent, name="stdout", text=f"ran {parent['msg_id']}\n"),
            ]
            reply = message("execute_reply", parent, status="ok", execution_count=count)
        self._answer(parent, outputs, reply, delay=0.0 if silent else self.kernel.run_delay)
        return parent["msg_id"]


class FakeKernelClient:
    """Stands in for jupyter-kernel-client's JupyterKernelClient."""

    def __init__(self, kernel_: Kernel, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.kernel = kernel_
        self.has_kernel = kernel_.exists
        self.socket = Socket(kernel_, dead=len(kernel_.clients) < kernel_.dead)
        if self.socket.dead and kernel_.noise:  # an earlier request's late reply and status
            stale = {"msg_id": uuid.uuid4().hex, "msg_type": "kernel_info_request"}
            self.socket.iopub_channel.put(message("status", stale, execution_state="idle"))
            self.socket.shell_channel.put(message("kernel_info_reply", stale, status="ok"))
        self._manager = SimpleNamespace(client=self.socket, shutdown_kernel=self.shutdown_kernel)
        self.stops: list[Any] = []
        self.stopped = threading.Event()
        kernel_.clients.append(self)

    def start(self) -> None:
        self.socket.opened_at = time.monotonic()
        self.socket.connection_ready.set()
        for other in self.kernel.clients:  # a new connection unsticks the stuck ones
            if other is not self and other.socket.dead and self.kernel.stuck:
                other.socket.dead = False
                for parent in other.socket.waiting:
                    other.socket._answer_check(parent)
                other.socket.waiting = []

    def stop(self, shutdown_kernel: Any = None, **_: Any) -> None:
        if self.socket.dead and self.kernel.slow_close:
            time.sleep(self.kernel.slow_close)  # a dead connection's close can take ~10 s
        self.stops.append(shutdown_kernel)
        self.socket.connection_ready.clear()
        if shutdown_kernel:
            self.shutdown_kernel()
        self.stopped.set()

    def shutdown_kernel(self, *_: Any, **__: Any) -> None:
        self.kernel.shutdowns += 1


@pytest.fixture
def fake(monkeypatch) -> Kernel:
    """A kernel nh's GET reads as idle, every connection alive; the check's timeout short."""
    kernel_ = Kernel()
    monkeypatch.setattr(kernel, "JupyterKernelClient", lambda **kw: FakeKernelClient(kernel_, **kw))
    monkeypatch.setattr(rest.Rest, "kernel", lambda self, kernel_id: kernel_.model())
    monkeypatch.setattr(kernel, "CHECK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(kernel, "_unanswered_logged", False)
    return kernel_


def sent(client: FakeKernelClient) -> list[str]:
    return [kind for kind, _msg_id, _at in client.socket.sent]


def closed(client: FakeKernelClient) -> list[Any]:
    """How the client was closed, once ``close_client_later``'s thread ran."""
    assert client.stopped.wait(5), "the client was never closed"
    return client.stops


def records(caplog, level: int, text: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level and text in r.getMessage()]


def test_a_live_connection_is_checked_once_and_used(fake: Kernel, caplog) -> None:
    caplog.set_level(logging.INFO, logger="nh_gateway")
    kc = kernel.open_client(SERVER, KID)
    assert fake.clients == [kc]
    assert sent(kc) == ["kernel_info_request"]
    assert kc.stops == [] and caplog.records == []


def test_a_dead_first_connection_is_closed_and_replaced(fake: Kernel, caplog) -> None:
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.dead = 1
    kc = kernel.open_client(SERVER, KID)
    first, second = fake.clients
    assert kc is second and sent(first) == sent(second) == ["kernel_info_request"]
    assert first.socket.sent[0][2] - first.socket.opened_at >= SETTLE_S
    assert second.socket.sent[0][2] - second.socket.opened_at >= RETRY_SETTLE_S
    assert RETRY_SETTLE_S == kernel.RETRY_SETTLE_S
    assert closed(first) == [False] and second.stops == []
    assert fake.shutdowns == 0
    [info] = records(caplog, logging.INFO, REPLACED)
    assert KID in info.getMessage() and "attempt 2 of 3" in info.getMessage()
    assert records(caplog, logging.WARNING, "") == []


@pytest.mark.parametrize(
    "frames", [{"busy"}, {"reply"}, {"busy", "idle"}], ids=["busy-only", "reply-only", "status"]
)
def test_either_the_busy_or_the_reply_passes_the_check(
    fake: Kernel, frames: set[str], caplog
) -> None:
    """A dead connection gets neither; one frame is enough, whichever comes first (the other
    can wait ~40 ms behind it on a real server: design §6.13, Nagle)."""
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.check_frames = frames
    kc = kernel.open_client(SERVER, KID)
    assert fake.clients == [kc] and kc.stops == []
    assert records(caplog, logging.INFO, REPLACED) == []


def test_the_check_ends_at_its_first_frame(fake: Kernel) -> None:
    """The reply 0.3 s after the busy: the check takes the busy and doesn't wait for it."""
    fake.check_frames = {"busy"}
    began = time.monotonic()
    kc = kernel.open_client(SERVER, KID)
    took = time.monotonic() - began
    assert sent(kc) == ["kernel_info_request"] and took < SETTLE_S + 0.15


def test_a_dead_connection_with_other_requests_traffic_is_still_replaced(
    fake: Kernel, caplog
) -> None:
    """Its queues hold another request's late reply and status, and more arrive during the
    check: none of them answers the check."""
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.dead, fake.noise = 1, True
    kc = kernel.open_client(SERVER, KID)
    first, second = fake.clients
    assert kc is second and closed(first) == [False]
    assert len(records(caplog, logging.INFO, REPLACED)) == 1


def test_a_late_answer_on_an_earlier_connection_is_used(fake: Kernel, caplog, monkeypatch) -> None:
    """An idle but slow kernel: the first connection's answer comes after its window, while the
    second's check waits. nh uses the first, closes the second, and connects no more. The window
    is 1 s here, so the time taken tells taking the answer when it comes (~1.2 s) from waiting
    out the second window (2 windows and the retry's settle, ~2.1 s) on a slow runner too: with
    0.2 s windows that was 0.37 s against 0.52 s, and macOS CI took 0.56 s."""
    monkeypatch.setattr(kernel, "CHECK_TIMEOUT_S", 1.0)
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.slow = kernel.CHECK_TIMEOUT_S + 0.15  # inside the second connection's window
    began = time.monotonic()
    kc = kernel.open_client(SERVER, KID)
    took = time.monotonic() - began
    first, second = fake.clients
    assert kc is first and first.stops == [] and first.socket.connection_ready.is_set()
    assert closed(second) == [False] and fake.shutdowns == 0
    assert sent(first) == sent(second) == ["kernel_info_request"]
    assert took < 1.5 * kernel.CHECK_TIMEOUT_S + RETRY_SETTLE_S
    assert len(records(caplog, logging.INFO, REPLACED)) == 1
    [late] = records(caplog, logging.INFO, LATE)
    assert "nh uses it and closes the newer one" in late.getMessage()
    assert records(caplog, logging.WARNING, "") == []
    # The first connection works, and its late check reaches nothing that follows.
    answer = probes.run_probe(kc._manager.client, "attach", {}, 2.0)
    [_, (_, probe_id, _)] = kc.socket.sent
    assert answer == {"answer": {"msg_id": probe_id, "msg_type": "execute_request"}}


def test_a_stuck_connection_that_answers_once_another_opens_is_used(fake: Kernel, caplog) -> None:
    """The real stack's case (design §6.13): the first connection answers its check a few ms
    after nh opens the second. nh uses the first, which then works, and closes the second."""
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.dead, fake.stuck = 1, True
    kc = kernel.open_client(SERVER, KID)
    first, second = fake.clients
    assert kc is first and first.stops == [] and closed(second) == [False]
    assert len(records(caplog, logging.INFO, REPLACED)) == 1
    assert len(records(caplog, logging.INFO, LATE)) == 1
    assert records(caplog, logging.WARNING, "") == []
    answer = probes.run_probe(kc._manager.client, "attach", {}, 2.0)
    assert "answer" in answer and fake.shutdowns == 0


def test_the_state_is_read_again_for_each_connection(fake: Kernel, caplog) -> None:
    """The kernel turns busy after the first check went unanswered: the replacement is used
    unchecked, as a busy kernel's connection always is, and nothing is logged as silent."""
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.dead, fake.states = 99, ["idle", "busy"]
    kc = kernel.open_client(SERVER, KID)
    first, second = fake.clients
    assert kc is second and sent(first) == ["kernel_info_request"] and sent(second) == []
    assert closed(first) == [False] and second.stops == []
    assert records(caplog, logging.WARNING, "") == []


def test_a_dead_connection_is_closed_off_the_callers_thread(fake: Kernel) -> None:
    """A dead connection's close can take ~10 s (design §6.13): open_client doesn't wait for it."""
    fake.dead, fake.slow_close = 1, 2.0
    began = time.monotonic()
    kc = kernel.open_client(SERVER, KID)
    took = time.monotonic() - began
    first, second = fake.clients
    assert kc is second and took < 1.0
    assert first.stops == []  # its close is still running
    assert closed(first) == [False] and fake.shutdowns == 0


def test_a_reconnect_that_fails_raises_e134_after_closing_the_first(
    fake: Kernel, monkeypatch
) -> None:
    fake.dead = 1
    fake.on_check = lambda: setattr(fake, "exists", False)  # the kernel goes during the check
    with pytest.raises(NhError) as gone:
        kernel.open_client(SERVER, KID)
    assert gone.value.code == "E134" and "no longer exists" in str(gone.value)
    first, _second = fake.clients
    assert closed(first) == [False] and fake.shutdowns == 0

    import requests

    fake.clients.clear()
    fake.exists, fake.dead, fake.on_check = True, 1, None
    real = kernel.JupyterKernelClient

    def unreachable(**kw: Any) -> FakeKernelClient:
        if fake.clients:
            raise requests.ConnectionError("Connection refused")
        return real(**kw)

    monkeypatch.setattr(kernel, "JupyterKernelClient", unreachable)
    with pytest.raises(NhError) as unreached:
        kernel.open_client(SERVER, KID)
    assert unreached.value.code == "E134" and "can't reach the kernel" in str(unreached.value)
    [first] = fake.clients
    assert closed(first) == [False] and fake.shutdowns == 0


@pytest.mark.parametrize(
    "state",
    [
        "busy",
        "starting",
        "restarting",
        "unknown",
        None,
        rest.RestError(500, "boom"),
        rest.ServerGone("gone"),
    ],
    ids=["busy", "starting", "restarting", "unknown", "404", "rest-error", "server-gone"],
)
def test_a_kernel_that_is_not_idle_is_not_checked(fake: Kernel, state: Any, caplog) -> None:
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.state, fake.dead = state, 1  # a check would fail and replace it
    kc = kernel.open_client(SERVER, KID)
    assert fake.clients == [kc] and sent(kc) == [] and kc.stops == []
    assert records(caplog, logging.INFO, REPLACED) == []


@pytest.mark.parametrize("state", ["idle", "busy"])
def test_nothing_is_sent_before_the_settle(fake: Kernel, state: str) -> None:
    fake.state = state
    kc = kernel.open_client(SERVER, KID)
    returned = time.monotonic()
    first_send = kc.socket.sent[0][2] if kc.socket.sent else returned
    assert first_send - kc.socket.opened_at >= SETTLE_S == kernel.CONNECT_SETTLE_S


def test_the_attempts_are_bounded_and_the_last_client_is_kept(fake: Kernel, caplog) -> None:
    caplog.set_level(logging.INFO, logger="nh_gateway")
    fake.dead = 99
    kc = kernel.open_client(SERVER, KID)
    assert len(fake.clients) == kernel.CONNECT_ATTEMPTS == 3
    *dropped, kept = fake.clients
    assert kc is kept and kept.stops == [] and kept.socket.connection_ready.is_set()
    assert [closed(client) for client in dropped] == [[False], [False]]
    assert len(records(caplog, logging.INFO, REPLACED)) == 2
    [warning] = records(caplog, logging.WARNING, UNANSWERED)
    assert KID in warning.getMessage() and "uses the last one" in warning.getMessage()
    assert "or a kernel too slow to answer" in warning.getMessage()
    # Used as before the check: a probe on it waits for its timeout.
    assert "TimeoutError" in probes.run_probe(kc._manager.client, "attach", {}, 0.3)["error"]
    # The next connect is checked again; the warning is not repeated.
    kernel.open_client(SERVER, KID)
    assert len(fake.clients) == 6
    assert len(records(caplog, logging.INFO, REPLACED)) == 4
    assert len(records(caplog, logging.WARNING, "")) == 1
    assert fake.shutdowns == 0


CHECK_TRAFFIC = {
    # name: (the check's frames held until the next request, what the check leaves queued)
    "drained": (set(), {"iopub": ["idle"], "shell": ["kernel_info_reply"]}),
    "late-status": ({"busy", "idle"}, {"iopub": [], "shell": []}),
    "late-reply": ({"idle", "reply"}, {"iopub": [], "shell": []}),
}


@pytest.mark.parametrize("first", ["probe", "run"])
@pytest.mark.parametrize("traffic", list(CHECK_TRAFFIC))
def test_the_check_reaches_no_following_probe_or_run(
    fake: Kernel, traffic: str, first: str
) -> None:
    """The check's other frames, queued or arriving after the next request's drain, reach
    neither a probe nor a run. The run's own answer comes after one poll, so its pump reads the
    shell channel (``_poll_reply``) while a late check reply may be there."""
    hold, left = CHECK_TRAFFIC[traffic]
    fake.check_hold, fake.run_delay = hold, kernel.POLL_S + 0.1
    kc = kernel.open_client(SERVER, KID)
    client = kc._manager.client
    [(_, check_id, _)] = client.sent
    queued = {
        "iopub": [m["msg_type"] for m in client.iopub_channel.peek()],
        "shell": [m["msg_type"] for m in client.shell_channel.peek()],
    }
    assert queued == {k: ["status" if v == "idle" else v for v in vs] for k, vs in left.items()}
    assert all(m["parent_header"]["msg_id"] == check_id for _, m in client.held)
    assert len(client.held) == len(hold)

    def probe() -> None:
        answer = probes.run_probe(client, "attach", {}, 2.0)
        kind, probe_id, _ = client.sent[-1]
        assert kind == "execute_request" and answer == {
            "answer": {"msg_id": probe_id, "msg_type": kind}
        }

    def run() -> None:
        seen: list[dict[str, Any]] = []
        request = kernel.KernelRequest(client, "x = 1", on_message=seen.append)
        request.send()
        assert request.pump(lambda: time.monotonic() + 3.0) == "idle"
        assert {m["parent_header"]["msg_id"] for m in seen} == {request.msg_id}
        assert [m["msg_type"] for m in seen] == ["status", "execute_input", "stream", "status"]
        assert request.reply_content in (None, {"status": "ok", "execution_count": fake.count})
        assert request.execution_count == fake.count
        assert (request.reply(1.0) or {}).get("execution_count") == fake.count

    for step in (probe, run) if first == "probe" else (run, probe):
        step()
    assert client.held == []


def test_a_connection_that_closes_during_the_check_is_silent(fake: Kernel) -> None:
    socket = Socket(fake, dead=True)
    socket.connection_ready.set()
    threading.Timer(0.05, socket.connection_ready.clear).start()
    began = time.monotonic()
    assert kernel.first_answer([(socket, kernel.ask(socket))], 5.0) is None
    assert time.monotonic() - began < 1.0

    class Closed(Socket):
        def kernel_info(self) -> str:
            raise ConnectionError("socket is already closed")

    closed_socket = Closed(fake, dead=False)
    closed_socket.connection_ready.set()
    assert kernel.ask(closed_socket) is None
    assert kernel.first_answer([(closed_socket, None)], 0.1) is None


def test_a_failed_connect_still_raises_e134(fake: Kernel, monkeypatch) -> None:
    fake.exists = False
    with pytest.raises(NhError) as gone:
        kernel.open_client(SERVER, KID)
    assert gone.value.code == "E134" and "no longer exists" in str(gone.value)
    assert all(sent(client) == [] for client in fake.clients)

    fake.exists = True
    monkeypatch.setattr(FakeKernelClient, "start", lambda self: None)  # the websocket never opens
    with pytest.raises(NhError) as unopened:
        kernel.open_client(SERVER, KID)
    assert unopened.value.code == "E134" and "websocket" in str(unopened.value)
    assert fake.clients[-1].stops == [False] and fake.shutdowns == 0


def test_closing_never_shuts_the_kernel_down(fake: Kernel) -> None:
    kc = kernel.open_client(SERVER, KID)
    kernel.close_client(kc)
    assert kc.stops == [False]
    handle = kernel.KernelHandle(KID)
    handle.client("exec", SERVER)
    handle.client("probe", SERVER)
    exec_kc, probe_kc = handle.exec_kc, handle.probe_kc
    handle.retire()
    assert closed(exec_kc) == [False] and closed(probe_kc) == [False]
    assert fake.shutdowns == 0


def test_the_handle_connects_through_the_check_under_its_lock(fake: Kernel) -> None:
    handle = kernel.KernelHandle(KID)
    fake.lock, fake.dead = handle.connect_lock, 1
    client = handle.client("probe", SERVER)
    first, second = fake.clients
    assert client is second.socket and handle.probe_kc is second
    assert closed(first) == [False]
    assert handle.client("probe", SERVER) is client  # connected: no new connection, no check
    second.socket.connection_ready.clear()  # the socket dropped
    again = handle.client("probe", SERVER)
    assert again is fake.clients[2].socket and sent(fake.clients[2]) == ["kernel_info_request"]
    assert closed(second) == [False]
    assert fake.lock_held == [True, True, True]


# ---------------------------------------------------------------------------- the probe client


@pytest.fixture
def backend(tmp_path: Path) -> RtcBackend:
    project = tmp_path / "proj"
    (project / ".nh" / "state" / "running").mkdir(parents=True)
    return RtcBackend(Layout(project), ConfigCache(project))


def probe_api(interrupts: list[str] | None = None) -> Any:
    return SimpleNamespace(server=SERVER, interrupt=lambda kid: (interrupts or []).append(kid))


async def test_a_probe_holds_its_lock_while_it_connects(fake: Kernel, backend) -> None:
    """A run starting while the probe client connects (now up to the check's time) sees the
    probe: ``start_execution`` reads ``probe_lock``."""
    handle = kernel.KernelHandle(KID)
    fake.lock, fake.dead = handle.probe_lock, 1
    answer = await backend._probe(handle, probe_api(), "attach", {}, 2.0)
    first, second = fake.clients
    assert fake.lock_held == [True, True]  # both checks ran under the probe's lock
    probe_id = second.socket.sent[-1][1]
    assert answer == {"answer": {"msg_id": probe_id, "msg_type": "execute_request"}}
    assert handle.probe_kc is second and not handle.probe_lock.locked()
    assert closed(first) == [False]


async def test_a_probe_waits_for_another_probe_before_it_connects(fake: Kernel, backend) -> None:
    handle = kernel.KernelHandle(KID)
    handle.probe_lock.acquire()
    try:
        answer = await backend._probe(handle, probe_api(), "attach", {}, 0.1)
    finally:
        handle.probe_lock.release()
    assert answer == {"error": "KernelBusy: another probe is still running"}
    assert fake.clients == []


async def test_a_run_starting_during_a_probe_has_it_retire_its_client(
    fake: Kernel, backend
) -> None:
    handle = kernel.KernelHandle(KID)
    fake.on_check = lambda: setattr(handle, "retire_probe", True)  # as start_execution does
    answer = await backend._probe(handle, probe_api(), "attach", {}, 2.0)
    [client] = fake.clients
    assert "answer" in answer
    assert handle.probe_kc is None and handle.retire_probe is False
    assert closed(client) == [False] and fake.shutdowns == 0
    fake.on_check = None
    await backend._probe(handle, probe_api(), "attach", {}, 2.0)  # the next one connects anew
    assert len(fake.clients) == 2 and handle.probe_kc is fake.clients[1]
    assert fake.clients[1].stops == []


class StubDoc:
    """Enough of an RtcDocument for start_execution to reach the cell lookup."""

    api = SimpleNamespace(server=SERVER)

    async def ensure(self) -> None:
        return None

    def begin_run(self, cell_id: str, expected: str | None = None) -> tuple[str, str]:
        raise CellMissing(cell_id)


@pytest.mark.parametrize("probing", [False, True], ids=["no-probe", "probe-in-progress"])
async def test_a_run_retires_the_probe_client_or_leaves_it_to_the_probe(
    fake: Kernel, backend, monkeypatch, probing: bool
) -> None:
    handle = kernel.KernelHandle(KID)
    handle.client("probe", SERVER)
    probe_kc = handle.probe_kc

    async def prepared(_prepare: Any) -> tuple[StubDoc, kernel.KernelHandle]:
        return StubDoc(), handle

    monkeypatch.setattr(backend, "_retry", prepared)
    ref = NotebookRef(
        abs_path=SERVER.root_dir / "nb.ipynb", api_path="nb.ipynb", rel_path="nb.ipynb"
    )
    if probing:
        handle.probe_lock.acquire()
    try:
        with pytest.raises(NhError) as missing:
            await backend.start_execution(ref, "c1", hard_timeout=10)
    finally:
        if probing:
            handle.probe_lock.release()
    assert missing.value.code == "E140"  # stopped right after the connect, as wanted here
    assert handle.exec_kc is fake.clients[-1]
    if probing:  # the probe still uses its client: it retires it when it ends
        assert handle.retire_probe is True and handle.probe_kc is probe_kc
        assert probe_kc is not None and probe_kc.stops == []
    else:
        assert handle.retire_probe is False and handle.probe_kc is None
        assert closed(probe_kc) == [False]
