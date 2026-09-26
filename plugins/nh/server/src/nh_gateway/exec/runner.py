"""Run one cell in the user's kernel (plan §4.4 "Execute").

The request loop runs on a daemon thread (a run can outlive the gateway's patience; a daemon
thread never blocks exit) and hands every message to the event loop with
``call_soon_threadsafe``; all document writes happen on the loop via :class:`OutputFlusher`.
A REST poll every 2 s notices a kernel that died, restarted or forgot our request. REST saying
"idle" alone proves little (a huge result may still be on its way to us), so a run counts as
lost that way only after ``IDLE_LOST_S`` of idle with no message for our request and no
execute_reply. The soft timeout is the caller's business: :meth:`Runner.start` returns at once
with an execution whose future resolves when the cell finishes, however long that takes (up to
the hard timeout).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..backend import rest
from ..backend.base import ErrorInfo, ExecResult, ExecStatus
from ..backend.kernel import KernelRequest
from ..policy.errors import scrub
from .flusher import OutputFlusher, OutputTarget

log = logging.getLogger("nh_gateway.runner")

T = TypeVar("T")
POLL_S = 2.0
IDLE_LOST_S = 15.0  # REST idle this long, with nothing for our request: the kernel forgot it
REPLIED_LOST_S = 60.0  # the same after our execute_reply arrived (only the final idle is missing)
INTERRUPT_SETTLE_S = 10.0
USER_INTERRUPT_NOTE = "interrupted from JupyterLab"
REPLY_WAIT_S = 5.0
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CELL_LINE = re.compile(r"Cell In\[\d*\], line (\d+)")
_ANY_LINE = re.compile(r"\bline (\d+)")
_CHAINED = re.compile(
    r"^(?:During handling of the above exception|The above exception was the direct cause)", re.M
)
_SIGINT = re.compile(r"^KeyboardInterrupt:?\s*$", re.M)  # SIGINT's KeyboardInterrupt: no message
_ARROW = re.compile(r"^-+>\s*\d+\s(.*)$")  # IPython's "----> 5 code" marks a frame's current line
_RAISES_IT = re.compile(r"\s*raise\s+KeyboardInterrupt\b")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def error_info(
    outputs: list[dict[str, Any]], reply: dict[str, Any] | None = None
) -> ErrorInfo | None:
    """The first error output (or the error reply) with an ANSI-free traceback and the cell's line."""
    source = next((o for o in outputs if o.get("output_type") == "error"), None)
    if source is None and reply and reply.get("status") == "error":
        source = reply
    if source is None:
        return None
    traceback = strip_ansi("\n".join(str(line) for line in source.get("traceback") or []))
    matches = _CELL_LINE.findall(traceback) or _ANY_LINE.findall(traceback)
    return ErrorInfo(
        ename=str(source.get("ename") or "Error"),
        evalue=strip_ansi(str(source.get("evalue") or "")),
        traceback=traceback,
        line=int(matches[-1]) if matches else None,
    )


def stopped_by_signal(error: ErrorInfo) -> bool:
    """Whether a KeyboardInterrupt is a stop (SIGINT) rather than one the cell's code raised.

    SIGINT's KeyboardInterrupt has no message. A cell that catches the stop and raises again with
    a message still shows SIGINT's exception earlier in the chained traceback. The code's own bare
    ``raise KeyboardInterrupt`` has no message either, but its last frame is that line in a cell.
    """
    if error.ename != "KeyboardInterrupt":
        return False
    *earlier, last = _CHAINED.split(error.traceback)
    if any(_SIGINT.search(block) for block in earlier):
        return True
    return not error.evalue.strip() and not _raised_in_a_cell(last)


def _raised_in_a_cell(traceback: str) -> bool:
    """Whether the traceback's last frame is a ``raise KeyboardInterrupt`` line in a cell."""
    in_cell, current = False, ""
    for line in traceback.splitlines():
        if line.startswith(("Cell In[", "File ")):  # a frame starts: a cell's or a module's
            in_cell, current = line.startswith("Cell In["), ""
        elif arrow := _ARROW.match(line):
            current = arrow.group(1)
    return in_cell and bool(_RAISES_IT.match(current))


def in_daemon_thread(
    fn: Callable[..., T], *args: Any, name: str = "nh-worker"
) -> asyncio.Future[T]:
    """Run blocking ``fn`` on a daemon thread; the returned future resolves on the calling loop."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def resolve(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def target() -> None:
        try:
            result, error = fn(*args), None
        except BaseException as exc:  # hand every failure to the awaiting coroutine
            result, error = None, exc
        with contextlib.suppress(RuntimeError):  # the loop closed while we worked
            loop.call_soon_threadsafe(resolve, result, error)

    threading.Thread(target=target, name=name, daemon=True).start()
    return future


@dataclass
class Outcome:
    """What the worker thread saw."""

    state: str = "lost"  # idle | timeout | abort | lost
    reply: dict[str, Any] | None = None
    started: bool = False
    started_at: float | None = None
    sent_at: float = 0.0
    execution_count: int | None = None
    timed_out: bool = False
    reason: str | None = None


class RtcExecution:
    """The :class:`~nh_gateway.backend.base.Execution` handle for a cell nh runs.

    ``started_at`` is ``time.monotonic()`` when nh submitted the cell.
    """

    def __init__(
        self, cell_id: str, future: asyncio.Future[ExecResult], flusher: OutputFlusher
    ) -> None:
        self.cell_id = cell_id
        self.started_at = time.monotonic()
        self._future = future
        self._flusher = flusher
        self._started = False

    @property
    def future(self) -> asyncio.Future[ExecResult]:
        return self._future

    def started(self) -> bool:
        """True once the kernel began running this cell (not merely queued behind another)."""
        return self._started

    def partial_outputs(self) -> list[dict[str, Any]]:
        return self._flusher.snapshot()


class RunTarget(OutputTarget, Protocol):
    """The document operations a run needs besides writing outputs."""

    def end_run(
        self, cell_id: str, execution_count: int | None, notice: str | None = None
    ) -> str: ...


class Runner:
    """Owns one execution: the worker thread, the output flusher and the lost-kernel watcher."""

    def __init__(
        self,
        *,
        doc: RunTarget,
        cell_id: str,
        code: str,
        client: Any,
        api: rest.Rest,
        kernel_id: str,
        hard_timeout: float,
        on_done: Callable[[ExecResult], None] | None = None,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self.doc = doc
        self.cell_id = cell_id
        self.code = code
        self.client = client
        self.api = api
        self.kernel_id = kernel_id
        self.hard_timeout = max(1.0, float(hard_timeout))
        self.on_done = on_done
        self.on_lost = on_lost
        self.flusher = OutputFlusher(doc, cell_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._abort = threading.Event()
        self._abort_reason: str | None = None
        self._idle = threading.Event()
        self._interrupted = False
        self._watcher: asyncio.Task[None] | None = None
        self._request: KernelRequest | None = None
        self._sent_at: float | None = None
        self.execution: RtcExecution | None = None

    def start(self) -> RtcExecution:
        self._loop = asyncio.get_running_loop()
        self.execution = RtcExecution(self.cell_id, self._loop.create_future(), self.flusher)
        self.flusher.start()
        self._watcher = asyncio.create_task(self._watch(), name=f"nh-watch:{self.cell_id}")
        threading.Thread(target=self._work, name=f"nh-run:{self.cell_id}", daemon=True).start()
        return self.execution

    def abort(self, reason: str) -> None:
        """Stop following the cell (the kernel may keep running it); the run ends as ``lost``."""
        if self._abort_reason is None:
            self._abort_reason = reason
        self._abort.set()

    async def interrupt(self) -> None:
        """Interrupt the kernel. Only call this once :meth:`RtcExecution.started` is True."""
        self._interrupted = True
        await self.api.call(self.api.interrupt, self.kernel_id)

    # ------------------------------------------------------------------ worker thread

    def _post(self, fn: Callable[..., Any], *args: Any) -> None:
        assert self._loop is not None
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop is closed: nobody is listening any more
            self.abort("nh stopped")

    def _mark_started(self) -> None:
        if self.execution is not None:
            self.execution._started = True

    def _work(self) -> None:
        outcome = Outcome()
        try:
            request = KernelRequest(
                self.client,
                self.code,
                on_message=lambda msg: self._post(self.flusher.apply, msg),
                on_started=lambda: self._post(self._mark_started),
            )
            self._request = request
            request.send()
            self._sent_at = outcome.sent_at = request.sent_at
            state = request.pump(
                lambda: (request.started_at or request.sent_at) + self.hard_timeout, self._abort
            )
            if state == "timeout":
                outcome.timed_out = True
                if (
                    request.started
                ):  # never interrupt a kernel that is still busy with someone else's cell
                    with contextlib.suppress(Exception):
                        self.api.interrupt(self.kernel_id)
                    settle = time.monotonic() + INTERRUPT_SETTLE_S
                    state = request.pump(lambda: settle, self._abort)
            if state == "idle":
                self._idle.set()
                outcome.reply = request.reply(REPLY_WAIT_S)
            outcome.state = state
            outcome.started, outcome.started_at = request.started, request.started_at
            outcome.execution_count = request.execution_count
            outcome.reason = request.lost_reason
        except Exception as exc:
            outcome.state = "lost"
            outcome.reason = scrub(f"nh lost track of the kernel ({type(exc).__name__}: {exc})")[
                :300
            ]
        if outcome.state == "abort":
            outcome.reason = self._abort_reason
        self._post(self._finish, outcome)

    # ------------------------------------------------------------------ event loop

    def _quiet_for(self, now: float) -> float:
        """Seconds since anything arrived for our request (or since it was sent)."""
        request = self._request
        last = (request.last_message_at if request is not None else None) or self._sent_at
        return now - last if last is not None else 0.0

    async def _watch(self) -> None:
        """Poll the kernel over REST; a run is lost on 404, dead, (re)starting, or a long quiet idle."""
        idle_since: float | None = None
        while not self._idle.is_set():
            await asyncio.sleep(POLL_S)
            if self._idle.is_set():
                return
            try:
                model = await self.api.call(self.api.kernel, self.kernel_id)
            except rest.ServerGone:
                self.abort("the connection to JupyterLab was lost")
                return
            except rest.RestError:
                continue
            state = None if model is None else model.get("execution_state")
            now = time.monotonic()
            if model is None:
                self.abort("the kernel was shut down")
            elif state == "dead":
                self.abort("the kernel died")
            elif state in ("restarting", "starting"):
                self.abort("the kernel was restarted")
            elif state == "idle" and not self._idle.is_set():
                idle_since = idle_since or now
                request = self._request
                window = REPLIED_LOST_S if request is not None and request.replied else IDLE_LOST_S
                if now - idle_since >= window and self._quiet_for(now) >= window:
                    self.abort("the kernel stopped running this cell (it was probably restarted)")
            else:
                idle_since = None
            if self._abort.is_set():
                return

    def _finish(self, outcome: Outcome) -> None:
        asyncio.ensure_future(self._complete(outcome))

    async def _complete(self, outcome: Outcome) -> None:
        assert self.execution is not None
        future = self.execution.future
        try:
            if self._watcher is not None:
                self._watcher.cancel()
            await self.flusher.close()
            result = self._result(outcome)
            if outcome.state in ("lost", "abort") and self.on_lost is not None:
                self.on_lost()
            notice = None
            if result.status == "lost":
                notice = f"[nh] {result.note}; output after this point was not saved.\n"
            elif result.status == "timeout" and not outcome.started:
                notice = "[nh] this cell never started: the kernel was busy with another cell the whole time.\n"
            state = self.doc.end_run(self.cell_id, result.execution_count, notice)
            if state == "unsynced":
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.doc.ensure(), 10.0)
                    self.doc.end_run(self.cell_id, result.execution_count, notice)
            elif state == "missing" or self.flusher.missing:
                result.note = "; ".join(
                    filter(None, [result.note, "the cell was deleted in JupyterLab while it ran"])
                )
        except Exception as exc:  # the run must always resolve
            log.exception("finishing the run of %s failed", self.cell_id)
            result = ExecResult(
                status="lost",
                execution_count=None,
                outputs=self.flusher.snapshot(),
                seconds=0.0,
                note=scrub(f"nh failed to finish the run ({type(exc).__name__})"),
            )
        if self.on_done is not None:
            with contextlib.suppress(Exception):
                self.on_done(result)
        if not future.done():
            future.set_result(result)

    def _result(self, outcome: Outcome) -> ExecResult:
        outputs = self.flusher.snapshot()
        begin = outcome.started_at or outcome.sent_at or self.execution.started_at  # type: ignore[union-attr]
        seconds = max(0.0, time.monotonic() - begin)
        reply = outcome.reply or {}
        count = (
            reply.get("execution_count") if isinstance(reply.get("execution_count"), int) else None
        )
        count = count if count is not None else outcome.execution_count
        error = error_info(outputs, outcome.reply)
        status: ExecStatus
        note = ""
        if outcome.state in ("lost", "abort"):
            status, note = "lost", outcome.reason or "the kernel went away"
        elif outcome.timed_out:
            status = "timeout"
            note = (
                "interrupted at the time limit"
                if outcome.started
                else "it never started: the kernel was busy with another cell, which nh did not interrupt"
            )
        elif reply.get("status") == "aborted":
            status, note, error = "aborted", "a cell ahead of it in the kernel queue failed", None
        elif reply.get("status") == "error" or error is not None:
            # a KeyboardInterrupt after nh's interrupt, or from SIGINT, is a stop, not a failure
            if error is not None and (
                (self._interrupted and error.ename == "KeyboardInterrupt")
                or stopped_by_signal(error)
            ):
                status = "interrupted"  # whoever pressed stop, the cell didn't fail
                if not self._interrupted:
                    note = USER_INTERRUPT_NOTE
            else:
                status = "error"
        else:
            status = "ok"
            if outcome.reply is None:
                note = "the kernel sent no reply"
        return ExecResult(
            status=status,
            execution_count=count,
            outputs=outputs,
            seconds=seconds,
            error=error,
            note=note,
        )
