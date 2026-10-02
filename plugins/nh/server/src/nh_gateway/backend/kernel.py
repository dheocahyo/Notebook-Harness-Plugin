"""The user's session kernel: find or create its session, connect clients, and run requests on them.

We never call ``execute_interactive``: it can't be aborted, checks for a lost connection only
between messages and hides the kernel restarts we must notice. :class:`KernelRequest` drives the
websocket client's channels directly instead. Everything here blocks; callers run it off the loop.
"""

from __future__ import annotations

from . import rest  # first: it sets NO_PROXY before websocket-client is imported

# isort: split

import codecs
import contextlib
import importlib
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from jupyter_kernel_client import JupyterKernelClient

from ..policy.errors import NhError, scrub
from .base import ServerInfo

log = logging.getLogger("nh_gateway.kernel")


def _no_check(*args: Any, **kwargs: Any) -> bool:
    raise TypeError("websocket-client's own UTF-8 check was not found")


_upstream_utf8: Callable[..., bool] = _no_check  # websocket-client's own check, once replaced
_unknown_call_logged = False
# A lead byte's second byte where it is narrower than 80-BF (RFC 3629): no overlong form, no
# surrogate, nothing past U+10FFFF.
_SECOND_BYTE = {0xE0: (0xA0, 0xBF), 0xED: (0x80, 0x9F), 0xF0: (0x90, 0xBF), 0xF4: (0x80, 0x8F)}


def _plain_text(data: Any) -> bool:
    """bytes, bytearray, str, or a plain byte memoryview: what websocket-client passes."""
    if isinstance(data, memoryview):
        return data.format == "B" and data.c_contiguous
    return isinstance(data, (bytes, bytearray, str))


def validate_utf8(data: Any, *args: Any, **kwargs: Any) -> bool:
    """websocket-client's UTF-8 check of each text frame and close reason nh's kernel clients
    receive, by the C decoder.

    Its own is a pure-Python loop over every byte unless ``wsaccel`` is installed: ~0.2 s per MB
    on the gateway's receive thread, holding the GIL (a 50 MB stream took ~10 s more than the
    kernel did; design §6.13). This answers as it does: False from the first byte that can't
    continue UTF-8 (an overlong form, a surrogate, past U+10FFFF, a stray continuation byte),
    True for valid text and for a sequence cut at the end (which ``WebSocketApp``'s decode of a
    text frame then refuses, and its close-reason decode replaces, as before). A call this
    doesn't know (other arguments or types) goes to websocket-client's own check.
    """
    global _unknown_call_logged
    if args or kwargs or not _plain_text(data):
        if not _unknown_call_logged:
            _unknown_call_logged = True
            log.warning("websocket-client called nh's UTF-8 check in a new way; its own is used")
        return _upstream_utf8(data, *args, **kwargs)
    if isinstance(data, str):  # as websocket-client's own: lone surrogates fail
        data = data.encode("utf-8", "surrogatepass")
    try:
        _, done = codecs.utf_8_decode(data, "strict", False)  # final=False: a cut end is no error
    except UnicodeDecodeError:
        return False
    tail = bytes(data[done:])  # a sequence cut at the end, at most 3 bytes: can it still be one?
    if len(tail) > 1:  # (CPython's decoder lets a cut surrogate, ED A0-BF, through)
        low, high = _SECOND_BYTE.get(tail[0], (0x80, 0xBF))
        return low <= tail[1] <= high and all(0x80 <= byte <= 0xBF for byte in tail[2:])
    return True


def install_utf8_check() -> str | None:
    """Put :func:`validate_utf8` where websocket-client's frame reader calls it: the name
    ``websocket._abnf`` imports from ``websocket._utils`` (``continuous_frame.extract`` for text
    frames, ``ABNF.validate`` for close frames). Both are private, so only when they are as
    expected; else the reason, and the gateway keeps websocket-client's own check (only slower,
    noted once by :func:`note_utf8_check`). ``tests/unit/test_kernel_utf8.py`` pins the reader.
    """
    global _upstream_utf8
    try:
        abnf = importlib.import_module("websocket._abnf")
        utils = importlib.import_module("websocket._utils")
    except Exception as exc:  # gone, renamed, or failing to import: never the gateway's import
        return f"websocket-client's modules changed ({type(exc).__name__}: {exc})"
    current = getattr(abnf, "validate_utf8", None)
    if current is validate_utf8:
        return None
    if current is None or current is not getattr(utils, "validate_utf8", None):
        return "websocket-client's frame reader no longer calls websocket._utils.validate_utf8"
    _upstream_utf8 = current
    abnf.validate_utf8 = validate_utf8  # type: ignore[attr-defined]
    return None


UTF8_CHECK_NOTE = install_utf8_check()
_utf8_noted = False


def note_utf8_check() -> None:
    """Log once, at the first kernel connection (gateway.log is open by then), when
    :func:`install_utf8_check` left websocket-client's own check in place."""
    global _utf8_noted
    if UTF8_CHECK_NOTE and not _utf8_noted:
        _utf8_noted = True
        log.warning(
            "nh's fast UTF-8 check for kernel messages is not installed (%s): kernel output of "
            "many MB is received more slowly",
            UTF8_CHECK_NOTE,
        )


PumpState = Literal["idle", "timeout", "abort", "lost"]
UUID_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RESTART_STATES = {"restarting", "dead"}
RESTART_GRACE_S = 2.0
_BACKSPACE = re.compile(r"[^\n]\x08")
POLL_S = 0.25


# ---------------------------------------------------------------------------- outputs


def _process_cr(text: str) -> str:
    """Apply backspaces and carriage returns like JupyterLab (a trailing ``\r`` waits for more text)."""
    if "\r" not in text and "\b" not in text:
        return text
    shorter = text
    while True:
        text, shorter = shorter, _BACKSPACE.sub("", shorter)
        if len(shorter) == len(text):
            break
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        trailing = line.endswith("\r")
        segments = line.rstrip("\r").split("\r")
        kept = segments[0]
        for segment in segments[1:]:
            kept = segment + kept[len(segment) :]
        lines.append(kept + ("\r" if trailing else ""))
    return "\n".join(lines)


def output_hook(outputs: list[dict[str, Any]], message: dict[str, Any]) -> bool:
    """Apply one iopub message to nbformat ``outputs`` the way JupyterLab does. True if they changed.

    Unlike ``jupyter_kernel_client.output_hook``, consecutive stream messages with the same name
    are merged into one output (and ``\\r`` progress bars collapse), and ``clear_output(wait=True)``
    waits for the next output.
    """
    kind = message["header"]["msg_type"]
    content = message.get("content") or {}
    if kind == "clear_output":
        if content.get("wait"):
            outputs.append({"output_type": "_pending_clear"})
            return False
        changed = bool(outputs)
        outputs.clear()
        return changed
    if kind == "update_display_data":
        display_id = (content.get("transient") or {}).get("display_id")
        changed = False
        for output in outputs:
            if display_id and (output.get("transient") or {}).get("display_id") == display_id:
                output["data"] = content.get("data") or {}
                output["metadata"] = content.get("metadata") or {}
                changed = True
        return changed
    if kind == "stream":
        new = {
            "output_type": "stream",
            "name": content.get("name") or "stdout",
            "text": content.get("text") or "",
        }
    elif kind in ("display_data", "execute_result"):
        new = {
            "output_type": kind,
            "data": content.get("data") or {},
            "metadata": content.get("metadata") or {},
        }
        if kind == "execute_result":
            new["execution_count"] = content.get("execution_count")
        if content.get("transient"):
            new["transient"] = content["transient"]
    elif kind == "error":
        new = {
            "output_type": "error",
            "ename": content.get("ename") or "",
            "evalue": content.get("evalue") or "",
            "traceback": list(content.get("traceback") or []),
        }
    else:
        return False
    if outputs and outputs[-1].get("output_type") == "_pending_clear":
        outputs.clear()
    last = outputs[-1] if outputs else None
    if (
        kind == "stream"
        and last is not None
        and last.get("output_type") == "stream"
        and last["name"] == new["name"]
    ):
        last["text"] = _process_cr(last["text"] + new["text"])
    else:
        if kind == "stream":
            new["text"] = _process_cr(new["text"])
        outputs.append(new)
    return True


# ---------------------------------------------------------------------------- requests


class KernelRequest:
    """One execute_request on a websocket kernel client, pumped by the calling thread."""

    def __init__(
        self,
        client: Any,
        code: str,
        *,
        silent: bool = False,
        on_message: Callable[[dict[str, Any]], Any] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.code = code
        self.silent = silent
        self.on_message = on_message
        self.on_started = on_started
        self.msg_id: str | None = None
        self.sent_at = 0.0
        self.started_at: float | None = None
        self.execution_count: int | None = None
        self.restarted_at: float | None = None
        self.lost_reason: str | None = None
        self.last_message_at: float | None = None  # monotonic time of our latest iopub message
        self.reply_content: dict[str, Any] | None = None  # our execute_reply, once it arrived

    @property
    def started(self) -> bool:
        return self.started_at is not None

    def send(self) -> None:
        for channel in (self.client.iopub_channel, self.client.shell_channel):
            channel.get_msgs()  # drop what arrived while nobody was listening
        self.msg_id = self.client.execute(
            self.code,
            silent=self.silent,
            store_history=not self.silent,
            allow_stdin=False,
            stop_on_error=False,
        )
        self.sent_at = time.monotonic()

    def pump(
        self, deadline: Callable[[], float], abort: threading.Event | None = None
    ) -> PumpState:
        """Handle iopub messages until our ``status: idle`` ("idle"), the deadline, an abort or a lost kernel."""
        while True:
            if not self.client.connection_ready.is_set():
                self.lost_reason = "the connection to the kernel was lost"
                return "lost"
            if abort is not None and abort.is_set():
                return "abort"
            now = time.monotonic()
            if self.restarted_at is not None and now - self.restarted_at > RESTART_GRACE_S:
                self.lost_reason = "the kernel was restarted"
                return "lost"
            if now >= deadline():
                return "timeout"
            try:
                msg = self.client.iopub_channel.get_msg(timeout=POLL_S)
            except queue.Empty:
                self._poll_reply()
                continue
            if self._is_restart(msg):
                self.restarted_at = self.restarted_at or time.monotonic()
                continue
            if (msg.get("parent_header") or {}).get("msg_id") != self.msg_id:
                continue
            self.last_message_at = time.monotonic()
            kind = msg["header"]["msg_type"]
            content = msg.get("content") or {}
            if not self.started and (
                kind == "execute_input"
                or (kind == "status" and content.get("execution_state") == "busy")
            ):
                self.started_at = time.monotonic()
                self.restarted_at = None  # queued across a restart: the new kernel is running it
                if self.on_started is not None:
                    self.on_started()
            if kind == "execute_input" and content.get("execution_count") is not None:
                self.execution_count = int(content["execution_count"])
            if self.on_message is not None:
                self.on_message(msg)
            if kind == "status" and content.get("execution_state") == "idle":
                if self.restarted_at is not None:
                    self.lost_reason = "the kernel was restarted"
                    return "lost"
                return "idle"

    @staticmethod
    def _is_restart(msg: dict[str, Any]) -> bool:
        """A restart announced on iopub: the old kernel's shutdown reply, or the server's restarting/dead."""
        kind = msg["header"]["msg_type"]
        if kind == "shutdown_reply":
            return True
        parent_kind = (msg.get("parent_header") or {}).get("msg_type")
        state = (msg.get("content") or {}).get("execution_state")
        return kind == "status" and not parent_kind and state in RESTART_STATES

    @property
    def replied(self) -> bool:
        """The kernel sent our execute_reply: it finished the request (iopub may still be catching up)."""
        return self.reply_content is not None

    def _poll_reply(self) -> None:
        """Pick up our execute_reply without waiting (the shell channel is read only by this thread)."""
        if self.reply_content is not None:
            return
        while True:
            try:
                msg = self.client.shell_channel.get_msg(timeout=0)
            except queue.Empty:
                return
            if (msg.get("parent_header") or {}).get("msg_id") == self.msg_id:
                self.reply_content = msg.get("content") or {}
                return

    def reply(self, timeout: float) -> dict[str, Any] | None:
        """The shell reply content for our request, or None if it doesn't arrive in time."""
        if self.reply_content is not None:
            return self.reply_content
        until = time.monotonic() + timeout
        while time.monotonic() < until and self.client.connection_ready.is_set():
            try:
                msg = self.client.shell_channel.get_msg(timeout=POLL_S)
            except queue.Empty:
                continue
            if (msg.get("parent_header") or {}).get("msg_id") == self.msg_id:
                self.reply_content = msg.get("content") or {}
                return self.reply_content
        return None


# ---------------------------------------------------------------------------- sessions


def _choose(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Several sessions for one path: take the kernel most clients are connected to."""
    return max(sessions, key=lambda s: int(((s.get("kernel") or {}).get("connections")) or 0))


def _for_path(sessions: list[dict[str, Any]], api_path: str) -> list[dict[str, Any]]:
    return [
        s for s in sessions if s.get("path") == api_path and s.get("type", "notebook") == "notebook"
    ]


def _pending_uuid(sessions: list[dict[str, Any]], api_path: str) -> bool:
    """JupyterLab first creates a session at ``<dir>/<uuid>`` and only then renames it to the notebook."""
    folder, _, name = api_path.rpartition("/")
    for session in sessions:
        path = str(session.get("path") or "")
        s_folder, _, s_name = path.rpartition("/")
        if (
            s_folder == folder
            and UUID_NAME.match(s_name)
            and session.get("name") in (None, "", name)
        ):
            return True
    return False


def attach_session(
    api: rest.Rest,
    api_path: str,
    kernel_names: list[str],
    *,
    create: bool = True,
    uuid_wait_s: float = 5.0,
) -> tuple[dict[str, Any], bool]:
    """Find the notebook's session, or create one (unless ``create`` is False: E134).

    Returns ``(session, created)``.
    """
    sessions = api.sessions()
    until = time.monotonic() + uuid_wait_s
    while (
        not _for_path(sessions, api_path)
        and _pending_uuid(sessions, api_path)
        and time.monotonic() < until
    ):
        time.sleep(0.25)
        sessions = api.sessions()
    found = _for_path(sessions, api_path)
    if found:
        return _choose(found), False
    if not create:
        raise NhError("E134", detail=" (no kernel is running for this notebook yet)")
    names = [name for i, name in enumerate(kernel_names) if name and name not in kernel_names[:i]]
    last: rest.RestError | None = None
    for name in names or [None]:
        try:
            api.create_session(api_path, name)
            break
        except rest.RestError as exc:
            if exc.status != 501:  # 501 = that kernelspec isn't installed; try the next one
                raise
            last = exc
    else:
        raise NhError("E134", detail=f" (no installed kernel matches {', '.join(names)}: {last})")
    found = _for_path(api.sessions(), api_path)  # re-read: JupyterLab may have raced us to it
    if not found:
        raise NhError("E134", detail=" (JupyterLab did not keep the new kernel session)")
    return _choose(found), True


# ---------------------------------------------------------------------------- clients


def open_client(server: ServerInfo, kernel_id: str, timeout: float = 10.0) -> JupyterKernelClient:
    """Connect a websocket client to an existing kernel. Never starts or owns a kernel."""
    note_utf8_check()
    try:
        kc = JupyterKernelClient(
            server_url=server.url.rstrip("/"),
            token=server.token,
            kernel_id=kernel_id,
            username="nh-agent",
            client_kwargs={"timeout": timeout},
        )
    except Exception as exc:  # requests errors from the constructor's GET /api/kernels/<id>
        raise NhError(
            "E134", detail=f" (can't reach the kernel: {scrub(str(exc))[:200]})"
        ) from None
    if not kc.has_kernel:
        raise NhError("E134", detail=" (the kernel no longer exists)")
    kc.start()  # with a kernel attached this only opens the channels
    if not kc._manager.client.channels_running:
        close_client(kc)
        raise NhError("E134", detail=" (couldn't open the kernel's websocket)")
    return kc


def close_client(kc: JupyterKernelClient | None) -> None:
    """Disconnect without ever shutting the user's kernel down."""
    if kc is None:
        return
    with contextlib.suppress(Exception):
        kc.stop(shutdown_kernel=False)


def close_client_later(kc: JupyterKernelClient | None) -> None:
    if kc is not None:
        threading.Thread(
            target=close_client, args=(kc,), name="nh-kernel-close", daemon=True
        ).start()


@dataclass
class KernelHandle:
    """Our two clients for one kernel: one runs cells, the other probes (so a probe never waits on a run)."""

    kernel_id: str
    name: str = ""
    exec_kc: JupyterKernelClient | None = None
    probe_kc: JupyterKernelClient | None = None
    probe_lock: threading.Lock = field(default_factory=threading.Lock)
    connect_lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)
    python: tuple[int, int] | None = None
    prefix: str | None = None
    executable: str | None = None
    incarnation: str | None = None  # the kernel process (pid and start), from the attach probe

    def client(self, role: Literal["exec", "probe"], server: ServerInfo) -> Any:
        """The connected websocket client for ``role``, reconnecting if its socket dropped. Blocking."""
        self.last_used = time.monotonic()
        with self.connect_lock:
            kc = self.exec_kc if role == "exec" else self.probe_kc
            if kc is None or not kc._manager.client.channels_running:
                close_client_later(kc)
                kc = open_client(server, self.kernel_id)
                if role == "exec":
                    self.exec_kc = kc
                else:
                    self.probe_kc = kc
            return kc._manager.client

    def retire(self, role: Literal["exec", "probe"] | None = None) -> None:
        """Drop a client (both when ``role`` is None); the next use connects a fresh one."""
        if role in (None, "exec"):
            close_client_later(self.exec_kc)
            self.exec_kc = None
        if role in (None, "probe"):
            close_client_later(self.probe_kc)
            self.probe_kc = None
