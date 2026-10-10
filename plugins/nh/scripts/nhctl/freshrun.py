"""nhctl fresh-run: does the notebook run top to bottom in a fresh kernel? (FR-11)

A copy (``.nh/tmp/fresh-<ts>-<pid>.ipynb``) runs under the project env's nbclient, with
relative paths resolved against the original notebook's folder. The original is only
ever read. Each run is logged as a ``fresh_run`` event in ``.nh/log.jsonl``.

``--review`` is /nh:review's run (design §6.10): a copy (``.nh/tmp/review-<ts>-<pid>.ipynb``)
runs past errors, one MARKER line per cell (spike V7), and the analysis
(``python -m nh_gateway.review`` in nh's runtime venv, spike V8) writes the report to
``.nh/reviews/<ts>-<stem>.md``. Neither mode gives the kernel a Jupyter token, and neither ever
touches the live kernel or writes the notebook. Both runners write their MARKER lines to a pipe of
their own, never to the stdout the kernel's output shares.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import posixpath
import selectors
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import common
import lab
from common import NhctlError, Result

from nh_gateway._shared import paths, secrets, text
from nh_gateway._shared.scaffold import core

KEEP_COPIES = 5
KEEP_REVIEW_COPIES = 3  # /nh:review's copies in .nh/tmp (design §6.10)
MARKER = "NH-FRESH-RUN "
REVIEW_MARKER = "NH-REVIEW-"  # + 16 random hex digits and a space, per run (design §6.10)
EVALUE_CHARS = 500  # of the failing cell's error message, shown after redaction
# What the runner hands back raw: well past the cut, so a secret across it is redacted whole
# (design §6.8). The runner can't redact: it runs in the project env, without nh's code.
EVALUE_RAW_CHARS = 8000
# The kernel runs the notebook's code: it gets no Jupyter token (design §6.10).
TOKEN_DROP = lab.TOKEN_VARS + ("NH_JUPYTER_TOKEN",)
CELL_TIMEOUT_S = 600  # plain fresh-run's per-cell default
# The review's per-cell default: a third of --timeout, kept within these (design §6.10), so a
# hanging cell is interrupted and the run goes on (V7's interrupt_on_timeout).
REVIEW_CELL_TIMEOUT_S = (30, 600)
DRAIN_S = 2.0  # how long the record pipe is read to its end after the runner exits
RUNNER_GRACE_S = 5.0  # plain fresh-run: SIGTERM, then SIGKILL after this
KERNEL_GRACE_S = 1.0  # the kernel's group and the strays: SIGTERM, then SIGKILL after this
ANALYSIS_TIMEOUT_S = 120
TAIL_LINES = 15  # of the runner's other output, for D151
TAIL_BYTES = 64 * 1024
RECORD_MAX = 1024 * 1024  # a longer record line is dropped (a cell line is at most ~50 KB)

# Runs inside the project env: argv = copy, working dir, per-cell timeout, kernel name
# (harness.toml [jupyter].kernel_name; "" = the notebook's kernelspec), the record pipe's fd.
_RUNNER = r"""
import json, os, sys
records = os.fdopen(int(sys.argv[5]), "w", encoding="utf-8")
import nbformat
from jupyter_client.kernelspec import NoSuchKernel
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError

copy, cwd, cell_timeout, configured = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
nb = nbformat.read(copy, as_version=4)
kernel = configured or (nb.metadata.get("kernelspec") or {}).get("name") or "python3"
state = {"index": None}
result = {"ok": False, "kernel": kernel}

def run(name):
    client = NotebookClient(nb, timeout=cell_timeout, kernel_name=name, allow_errors=False,
                            resources={"metadata": {"path": cwd}})
    client.on_cell_start = lambda cell, cell_index: state.update(index=cell_index)
    client.km = client.create_kernel_manager()
    client.km.connection_file = os.path.join(os.environ["JUPYTER_RUNTIME_DIR"], "kernel.json")
    client.execute()

try:
    try:
        run(kernel)
    except NoSuchKernel:
        if kernel == "python3":
            raise
        result["kernel"] = "python3"
        run("python3")
    result["ok"] = True
except CellExecutionError as exc:
    result.update(ename=exc.ename, evalue=str(exc.evalue))
except (CellTimeoutError, DeadKernelError, NoSuchKernel) as exc:
    result.update(ename=type(exc).__name__, evalue=str(exc))
if not result["ok"]:
    result["failing_index"] = state["index"]
    result["evalue"] = result.get("evalue", "")[:__EVALUE_RAW_CHARS__]
result["executed"] = sum(1 for c in nb.cells if c.cell_type == "code" and c.get("execution_count"))
nbformat.write(nb, copy)
records.write(__MARKER__ + json.dumps(result) + "\n")
records.flush()
""".replace("__MARKER__", repr(MARKER)).replace("__EVALUE_RAW_CHARS__", str(EVALUE_RAW_CHARS))


# /nh:review's runner (design §6.10, spike V7), in the project env: argv = copy, working dir,
# per-cell timeout, kernel name ("" = the notebook's kernelspec), the run's marker, the indexes
# to skip (JSON), the record pipe's fd. It starts the kernel itself so its `start` line names the
# kernel's pid and group before anything runs, then runs cell by cell past errors, flushing one
# MARKER line per event to the record pipe, which the kernel never holds. Before the kernel shuts
# down it stops what the cells started outside the kernel's group. It never writes the copy back.
_REVIEW_RUNNER = r"""
import json, os, signal, subprocess, sys, time
copy, cwd, cell_timeout, configured, marker, skip, record_fd = sys.argv[1:8]
skip, cell_timeout = set(json.loads(skip)), int(cell_timeout)
out = os.fdopen(int(record_fd), "w", encoding="utf-8")
import nbformat
from jupyter_client.kernelspec import NoSuchKernel
from nbclient import NotebookClient
from nbclient.exceptions import DeadKernelError

def emit(**fields):
    out.write(marker + json.dumps(fields) + "\n")
    out.flush()  # explicitly: -I ignores PYTHONUNBUFFERED, and a kill must find the lines (V7)

def strays(root, group):
    # the descendants of root outside its group: a cell's own-session child (ps: Linux, macOS)
    try:
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True,
                              text=True, timeout=10).stdout.splitlines()
    except Exception:
        return []
    children = {}
    for row in rows:
        fields = row.split()
        if len(fields) == 3 and all(f.isdigit() for f in fields):
            children.setdefault(int(fields[1]), []).append((int(fields[0]), int(fields[2])))
    found, todo, seen = [], [root], {root}
    while todo:
        for child, child_group in children.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                todo.append(child)
                if child_group != group and child > 1:
                    found.append(child)
    return found

def send(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass

nb = nbformat.read(copy, as_version=4)
kernel = configured or (nb.metadata.get("kernelspec") or {}).get("name") or "python3"
replies = {}

def started(name):
    client = NotebookClient(nb, timeout=int(cell_timeout), kernel_name=name, allow_errors=True,
                            interrupt_on_timeout=True, resources={"metadata": {"path": cwd}})
    client.on_cell_executed = lambda cell, cell_index, execute_reply: replies.update(
        {cell_index: execute_reply})
    client.km = client.create_kernel_manager()
    # in nhctl's private runtime dir, not a temp file a killed runner would leave behind
    client.km.connection_file = os.path.join(os.environ["JUPYTER_RUNTIME_DIR"], "kernel.json")
    client.start_new_kernel()
    return client

try:
    client = started(kernel)
except NoSuchKernel:
    if kernel == "python3":
        raise
    kernel = "python3"
    client = started(kernel)
km = client.km
pid = getattr(getattr(km, "provisioner", None), "pid", None)
if pid is None:
    pid = getattr(getattr(km, "kernel", None), "pid", None)
try:
    pgid = os.getpgid(pid)
except Exception:
    pgid = None
emit(t="start", kernel=kernel, pid=pid, pgid=pgid)
dead, left = False, []
with client.setup_kernel():
    for index, cell in enumerate(nb.cells):
        if cell.cell_type != "code" or not cell.source.strip():
            continue
        if index in skip or dead:
            emit(t="cell", i=index, status="not_run",
                 reason="flagged" if index in skip else "kernel_died")
            continue
        emit(t="cell_start", i=index)
        began = time.monotonic()
        status, ename, evalue = "ok", "", ""
        try:
            client.execute_cell(cell, index, execution_count=client.code_cells_executed + 1)
        except DeadKernelError as exc:
            status, ename, evalue, dead = "error", "DeadKernelError", str(exc), True
        else:
            content = (replies.get(index) or {}).get("content") or {}
            errors = [o for o in cell.get("outputs", []) if o.get("output_type") == "error"]
            if content.get("status") == "error":
                status, ename, evalue = "error", content.get("ename", ""), content.get("evalue", "")
            elif errors:
                status, ename, evalue = "error", errors[0].get("ename", ""), errors[0].get("evalue", "")
        took = time.monotonic() - began
        if ename == "KeyboardInterrupt" and took >= cell_timeout:  # interrupt_on_timeout's
            evalue = "stopped by the review's %d s limit per cell" % cell_timeout
        emit(t="cell", i=index, status=status, ename=str(ename),
             evalue=str(evalue)[:__EVALUE_RAW_CHARS__], ms=int(took * 1000))
    if isinstance(pid, int) and isinstance(pgid, int):
        left = strays(pid, pgid)
        send(left, signal.SIGTERM)
send(left, signal.SIGKILL)  # the kernel is down: what is still there
emit(t="done")
""".replace("__EVALUE_RAW_CHARS__", str(EVALUE_RAW_CHARS))


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    fresh = sub.add_parser(
        "fresh-run", parents=[common_opts], help="run a copy of a notebook in a fresh kernel"
    )
    fresh.add_argument("notebook", nargs="?", help="default: harness.toml's notebook")
    fresh.add_argument(
        "--cell-timeout",
        type=_whole_seconds,
        default=None,
        metavar="N",
        help=f"seconds per cell (default {CELL_TIMEOUT_S}; with --review, a third of --timeout, "
        f"{REVIEW_CELL_TIMEOUT_S[0]} to {REVIEW_CELL_TIMEOUT_S[1]})",
    )
    fresh.add_argument(
        "--timeout", type=_seconds, default=1800.0, metavar="S", help="seconds for the whole run"
    )
    fresh.add_argument(
        "--review",
        action="store_true",
        help="/nh:review: run past errors and report failing cells, hidden state, long cells "
        "and the intents to .nh/reviews/",
    )
    flagged = fresh.add_mutually_exclusive_group()
    flagged.add_argument(
        "--yes",
        metavar="DIGEST",
        help="--review: also run the cells it asked about; DIGEST is the one its question gave",
    )
    flagged.add_argument(
        "--skip-flagged",
        action="store_true",
        help="--review: run the rest and report the cells it would ask about as not run",
    )
    fresh.set_defaults(func=cmd_fresh_run)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"{text!r} isn't a number of seconds above 0")
    return value


def _whole_seconds(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number of seconds above 0")
    return value


def _review_cell_timeout(timeout: float) -> int:
    """The review's per-cell default: a third of its deadline, within REVIEW_CELL_TIMEOUT_S."""
    low, high = REVIEW_CELL_TIMEOUT_S
    return int(min(high, max(low, timeout / 3)))


def _nh(cell: object) -> dict:
    meta = cell.get("metadata") if isinstance(cell, dict) else None
    value = meta.get("nh") if isinstance(meta, dict) else None
    return value if isinstance(value, dict) else {}


def cell_label(cells: list, index: int) -> str:
    """'"<note title>" [n]', falling back to the first code line; redacted before the cut."""
    cell = cells[index]
    uid = _nh(cell).get("uid")
    title = ""
    above = cells[index - 1] if index > 0 else {}
    note = _nh(above)
    is_note = above.get("cell_type") == "markdown" and note.get("role") == "note"
    if is_note and note.get("pair_uid") in (None, uid):
        first = core.cell_source(above).strip().splitlines()
        title = text.normalize_title(first[0]) if first else ""
    if not title:
        lines = [ln.strip() for ln in core.cell_source(cell).splitlines() if ln.strip()]
        title = secrets.current().redact(lines[0]) if lines else "an empty cell"
        title = title if len(title) <= 40 else title[:39] + "…"
    else:
        title = secrets.current().redact(title)
    count = cell.get("execution_count")
    return f'"{title}"' + (f" [{count}]" if isinstance(count, int) else "")


def failing_report(result: dict, cells: list) -> dict | None:
    """The failing cell of a runner ``result``, None when it ran through: its error and label
    are redacted before their cuts (design §6.8)."""
    if result.get("ok"):
        return None
    index = result.get("failing_index")
    redactor = secrets.current()
    ename = redactor.redact(str(result.get("ename") or ""))
    evalue = redactor.redact_head(str(result.get("evalue") or ""), EVALUE_CHARS)
    failing = {"index": index, "ename": ename, "evalue": evalue[:EVALUE_CHARS]}
    if isinstance(index, int) and 0 <= index < len(cells):
        failing["label"] = cell_label(cells, index)
        failing["uid"] = _nh(cells[index]).get("uid")
    return failing


def _prune(tmp: Path, pattern: str = "fresh-*.ipynb", keep: int = KEEP_COPIES) -> None:
    copies = sorted(tmp.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[keep:]:
        old.unlink(missing_ok=True)


def _runner_env(prefix: Path, runtime_dir: Path) -> dict:
    """The runner's environment (design §6.10): the project env first on PATH, no Jupyter token
    (the kernel runs the notebook's code), and a private runtime dir for its connection file."""
    return common.env_with(
        prefix,
        drop=TOKEN_DROP,
        JUPYTER_PREFER_ENV_PATH="1",
        JUPYTER_RUNTIME_DIR=str(runtime_dir),
    )


# ---------------------------------------------------------------------- the run


class Isolated(NamedTuple):
    """What ``_run_isolated`` read: the record lines up to the end or the deadline, whether the
    deadline came first, the runner's exit code (None if it couldn't be reaped), and the last
    lines of its other output (stdout and stderr, which the kernel's output shares)."""

    out: str
    timed_out: bool
    code: int | None
    tail: str


class _Pipes:
    """The runner's two pipes, read without blocking (design §6.10). The record pipe: its lines
    that start with the marker are kept, and the kernel's ``(pid, pgid)`` is noted from the
    ``start`` line as it arrives. The other output: only its last lines are kept, for D151."""

    def __init__(self, proc: subprocess.Popen, record_fd: int, marker: str) -> None:
        assert proc.stdout is not None
        self.proc = proc
        self.record_fd = record_fd
        self.output_fd = proc.stdout.fileno()
        self.marker = marker.encode("utf-8")
        self.selector = selectors.DefaultSelector()
        for fd in (record_fd, self.output_fd):
            os.set_blocking(fd, False)
            self.selector.register(fd, selectors.EVENT_READ)
        self.open = {record_fd, self.output_fd}
        self.partial = {record_fd: b"", self.output_fd: b""}
        self.overflow = False  # the record line being read is past RECORD_MAX: dropped
        self.records: list[str] = []
        self.lines: deque[str] = deque(maxlen=TAIL_LINES)
        self.kernel: tuple[int, int | None] | None = None

    def read(self, wait: float) -> None:
        if not self.open:
            time.sleep(max(0.0, min(wait, 0.05)))
            return
        for key, _ in self.selector.select(max(0.0, wait)):
            self._take(int(key.fd))

    def take(self) -> None:
        """What both pipes hold now, without waiting."""
        for fd in list(self.open):
            self._take(fd)

    def _take(self, fd: int) -> None:
        while fd in self.open:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                chunk = b""
            if not chunk:
                self.open.discard(fd)
                self.selector.unregister(fd)
                return
            if fd == self.record_fd:
                self._records(chunk)
            else:
                self._output(chunk)

    def _records(self, chunk: bytes) -> None:
        parts = (self.partial[self.record_fd] + chunk).split(b"\n")
        rest = parts.pop()
        for line in parts:
            if self.overflow:
                self.overflow = False
            elif line.startswith(self.marker):
                self._record(line.decode("utf-8", "replace"))
        if self.overflow or len(rest) > RECORD_MAX:
            rest, self.overflow = b"", True
        self.partial[self.record_fd] = rest

    def _record(self, line: str) -> None:
        self.records.append(line)
        if self.kernel is not None:
            return
        record = _record(line[len(self.marker.decode("utf-8")) :])
        pid = (record or {}).get("pid")
        if record and record.get("t") == "start" and _int(pid):
            pgid = record.get("pgid")
            self.kernel = (pid, pgid if _int(pgid) else None)

    def _output(self, chunk: bytes) -> None:
        parts = (self.partial[self.output_fd] + chunk).split(b"\n")
        rest = parts.pop()
        for line in parts[-TAIL_LINES:]:
            self.lines.append(line[-(TAIL_BYTES // TAIL_LINES) :].decode("utf-8", "replace"))
        self.partial[self.output_fd] = rest[-TAIL_BYTES:]

    def until_exit(self, deadline: float) -> bool:
        """Read until the runner exits (True) or the deadline passes (False)."""
        while self.proc.poll() is None:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.read(min(left, 0.25))
        return True

    def finish(self, seconds: float) -> None:
        """After the runner's exit: the record pipe to its end (it closes with the runner; at
        most ``seconds``), then what the other pipe holds now. A process the notebook started
        may hold that one open: it isn't waited for."""
        end = time.monotonic() + seconds
        while self.record_fd in self.open and time.monotonic() < end:
            self.read(min(end - time.monotonic(), 0.25))
        self.take()

    def out(self) -> str:
        return "\n".join(self.records)

    def tail(self) -> str:
        last = self.partial[self.output_fd].decode("utf-8", "replace")
        return "\n".join([*self.lines, last] if last.strip() else self.lines)

    def close(self) -> None:
        self.selector.close()
        with contextlib.suppress(OSError):
            os.close(self.record_fd)
        assert self.proc.stdout is not None
        self.proc.stdout.close()


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _record(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, sig: int, group: bool) -> None:
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _descendants(root: int) -> list[tuple[int, int]]:
    """``(pid, pgid)`` of every process below ``root`` (``ps``: Linux and macOS); [] if ps fails."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[tuple[int, int]]] = {}
    for row in out.splitlines():
        fields = row.split()
        if len(fields) == 3 and all(field.isdigit() for field in fields):
            pid, ppid, pgid = (int(field) for field in fields)
            children.setdefault(ppid, []).append((pid, pgid))
    found: list[tuple[int, int]] = []
    todo, seen = [root], {root}
    while todo:
        for pid, pgid in children.get(todo.pop(), []):
            if pid not in seen:
                seen.add(pid)
                todo.append(pid)
                found.append((pid, pgid))
    return found


def _strays(proc: subprocess.Popen, kernel: tuple[int, int | None] | None) -> list[int]:
    """The runner's descendants outside its group and the kernel's (design §6.10): a cell's
    own-session child, or the kernel itself before its ``start`` line. Listed before any signal:
    they can only be found through their parents."""
    groups = {proc.pid}
    if kernel is not None and kernel[1] is not None:
        groups.add(kernel[1])
    mine = (0, 1, os.getpid())
    return [pid for pid, pgid in _descendants(proc.pid) if pgid not in groups and pid not in mine]


def _stop_kernel(
    kernel: tuple[int, int | None] | None, runner_pgid: int, strays: list[int] | None = None
) -> None:
    """SIGTERM, then SIGKILL after KERNEL_GRACE_S, to the kernel's group (V7: it has its own
    session, so the runner's group misses it; its group also holds what its cells started in it)
    and to each stray. Only a session leader's group (pgid == pid) that is neither init's,
    nhctl's nor the runner's is signalled as a group; otherwise only that pid."""
    targets: list[tuple[int, bool]] = []
    if kernel is not None:
        pid, pgid = kernel
        if pid > 1 and pid != os.getpid():
            group = pgid == pid and pgid not in (0, 1, os.getpgrp(), runner_pgid)
            targets.append((pid, group))
    targets += [(pid, False) for pid in strays or () if pid > 1 and pid != os.getpid()]

    def alive() -> list[tuple[int, bool]]:
        return [(p, g) for p, g in targets if (_group_alive(p) if g else common.pid_alive(p))]

    left = alive()
    if not left:
        return
    for pid, group in left:
        _signal(pid, signal.SIGTERM, group)
    end = time.monotonic() + KERNEL_GRACE_S
    while left and time.monotonic() < end:
        time.sleep(0.05)
        left = alive()
    for pid, group in left:
        _signal(pid, signal.SIGKILL, group)


def _stop(proc: subprocess.Popen, kernel: tuple[int, int | None] | None) -> None:
    """At the deadline (design §6.10, V7): the strays listed first, then the runner's group, so
    it starts no new cell (SIGKILL when the kernel's group is known; else SIGTERM, whose nbclient
    handler stops the kernel, then SIGKILL), then the kernel's group and the strays."""
    strays = _strays(proc, kernel)
    if kernel is not None:
        _signal(proc.pid, signal.SIGKILL, True)
    else:
        _signal(proc.pid, signal.SIGTERM, True)
        if not _reap(proc, RUNNER_GRACE_S):
            _signal(proc.pid, signal.SIGKILL, True)
    _reap(proc, 5)
    _stop_kernel(kernel, proc.pid, strays)


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)  # nhctl stopped from outside: as Ctrl-C


def _interrupted(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@contextlib.contextmanager
def _handling(handler: Any, signals: tuple[int, ...]) -> Iterator[None]:
    """``handler`` for ``signals`` inside the block (main thread only), the old ones after."""
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in signals:
            previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            if old is not None:
                signal.signal(sig, old)


def _quietly(stop: Callable[[], None]) -> None:
    """A stop that a second Ctrl-C, SIGTERM or SIGHUP can't cut short."""
    with _handling(signal.SIG_IGN, (signal.SIGINT, *STOP_SIGNALS)):
        stop()


def _run_isolated(cmd: list[str], cwd: Path, env: dict, timeout: float, marker: str) -> Isolated:
    """Run the runner in its own session, with the record pipe's write end as its last argument,
    and read both its pipes as they come. At the deadline the record lines read so far are kept,
    the runner, the kernel and the strays are stopped (``_stop``), and the pipes are closed: what
    comes after is dropped. nhctl stopped meanwhile (Ctrl-C, SIGTERM, SIGHUP) stops them the same
    way, then raises KeyboardInterrupt (D198)."""
    with _handling(_interrupted, STOP_SIGNALS):
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                [*cmd, str(write_fd)], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=(write_fd,),
            )  # fmt: skip
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)  # the runner holds it now: the pipe ends when the runner does
        pipes: _Pipes | None = None
        stopped = False
        try:
            pipes = _Pipes(proc, read_fd, marker)
            if not pipes.until_exit(time.monotonic() + timeout):
                pipes.take()  # what the runner flushed before the deadline
                kept, kernel = pipes.out(), pipes.kernel
                stopped = True
                _quietly(lambda: _stop(proc, kernel))
                return Isolated(kept, True, proc.returncode, pipes.tail())
            pipes.finish(DRAIN_S)
            stopped, kernel = True, pipes.kernel
            _quietly(lambda: _stop_kernel(kernel, proc.pid))  # what its cells left in its group
            return Isolated(pipes.out(), False, proc.returncode, pipes.tail())
        finally:
            if not stopped:  # interrupted (Ctrl-C, SIGTERM, SIGHUP) or failed while reading
                known = pipes.kernel if pipes is not None else None
                _quietly(lambda: _stop(proc, known))
            if pipes is not None:
                pipes.close()
            else:
                os.close(read_fd)
                assert proc.stdout is not None
                proc.stdout.close()


def parse_run(out: str, marker: str) -> dict:
    """The review runner's MARKER lines (design §6.10): every other line is skipped, and so is a
    marker line that isn't a JSON object of a known kind. Each error's name and value are
    redacted here, before the run goes anywhere (the runner can't: design §6.8)."""
    run: dict = {"kernel": None, "cells": [], "running": None, "done": False}
    cells: dict[int, dict] = {}
    redactor = secrets.current()
    for line in out.splitlines():
        if not line.startswith(marker):
            continue
        record = _record(line[len(marker) :])
        if record is None:
            continue
        kind, index = record.get("t"), record.get("i")
        if kind == "start" and isinstance(record.get("kernel"), str):
            run["kernel"] = record["kernel"]
        elif kind == "cell_start" and _int(index):
            run["running"] = index
        elif kind == "cell" and _int(index) and record.get("status") in ("ok", "error", "not_run"):
            entry: dict = {"i": index, "status": record["status"]}
            entry["ms"] = record.get("ms") if _int(record.get("ms")) else None
            if record["status"] == "error":
                entry["ename"] = redactor.redact(str(record.get("ename") or ""))
                evalue = redactor.redact_head(str(record.get("evalue") or ""), EVALUE_CHARS)
                entry["evalue"] = evalue[:EVALUE_CHARS]
            if record["status"] == "not_run":
                entry["reason"] = str(record.get("reason") or "")
            cells[index] = entry
            if run["running"] == index:
                run["running"] = None
        elif kind == "done":
            run["done"] = True
    run["cells"] = [cells[i] for i in sorted(cells)]
    return run


def _output_tail(out: str) -> str:
    lines = out.strip().splitlines()
    return common.scrub("\n".join(lines[-TAIL_LINES:]))


# ----------------------------------------------------------------- the analysis


def _analysis(python: Path, data: Path, project: Path, mode: str, request: dict) -> dict:
    """``python -m nh_gateway.review <mode>`` in the runtime venv, launched as libexec/nh-mcp
    launches the gateway (V8): ``-s``, nh's source on PYTHONPATH, bytecode under the data dir;
    and ``-P``, so the project (its cwd) isn't on sys.path: a project's ``random.py`` is never
    imported here. It keeps the Jupyter tokens: it runs nh's code, and its Redactor needs their
    values."""
    env = common.env_with(
        drop=("UV_PYTHON", "VIRTUAL_ENV", "PYTHONHOME"),
        PYTHONPATH=str(common.ROOT / "server" / "src"),
        PYTHONPYCACHEPREFIX=str(data / "pycache"),
    )
    cmd = [str(python), "-s", "-P", "-m", "nh_gateway.review", mode]
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(request).encode("utf-8"), cwd=str(project), env=env,
            capture_output=True, timeout=ANALYSIS_TIMEOUT_S, check=False,
        )  # fmt: skip
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = 124, b"", f"timed out after {ANALYSIS_TIMEOUT_S}s".encode()
    except OSError as exc:
        code, stdout, stderr = 126, b"", str(exc).encode()
    answer = _record(stdout.decode("utf-8", "replace").strip()) if code == 0 else None
    if answer is None:
        raise NhctlError(
            "D199",
            f"The review's analysis failed (exit {code}).",
            "Run nhctl runtime sync, then rerun; NHCTL_DEBUG=1 shows more.",
            data={"output_tail": _output_tail(stderr.decode("utf-8", "replace"))},
        )
    return answer


# --------------------------------------------------------------------- commands


def cmd_fresh_run(args: argparse.Namespace) -> Result:
    if (args.yes is not None or args.skip_flagged) and not args.review:
        raise NhctlError(
            "D100", "--yes and --skip-flagged go with --review.", "See: nhctl fresh-run --help", 2
        )
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    original = common.resolve_notebook(project, args.notebook)
    shown = common.rel(project, original)
    if original.suffix != ".ipynb" or not original.is_file():
        raise NhctlError("D150", f"No notebook at {shown}.", "Pass the notebook's path.")
    nb = paths.read_json(original, None)
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise NhctlError("D150", f"{shown} is not a readable notebook.")
    prefix = common.env_prefix(layout)
    if args.review:
        return _review(args, project, layout, original, shown, prefix)
    return _plain(args, project, layout, original, shown, prefix, nb)


def _private_runtime(layout: paths.Layout, stamp: str) -> Path:
    """A fresh ``.nh/tmp/rt-<ts>-<pid>`` for the runner (design §6.10). One whose nhctl is gone
    (a SIGKILLed run's) is removed first."""
    for old in layout.tmp.glob("rt-*-*"):
        pid = old.name.rsplit("-", 1)[1]
        if pid.isdigit() and not common.pid_alive(int(pid)):
            shutil.rmtree(old, ignore_errors=True)
    path = layout.tmp / f"rt-{stamp}-{os.getpid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _plain(
    args: argparse.Namespace,
    project: Path,
    layout: paths.Layout,
    original: Path,
    shown: str,
    prefix: Path,
    nb: dict,
) -> Result:
    layout.tmp.mkdir(parents=True, exist_ok=True)
    stamp = common.now_stamp()
    copy = layout.tmp / f"fresh-{stamp}-{os.getpid()}.ipynb"
    shutil.copyfile(original, copy)
    runtime_dir = _private_runtime(layout, stamp)
    cmd = [str(prefix / "bin" / "python"), "-I", "-c", _RUNNER]
    cmd += [str(copy), str(original.parent), str(args.cell_timeout or CELL_TIMEOUT_S)]
    cmd.append(common.configured_kernel(project))
    started = time.monotonic()
    try:
        run = _run_isolated(
            cmd, original.parent, _runner_env(prefix, runtime_dir), args.timeout, MARKER
        )
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
    ms = int((time.monotonic() - started) * 1000)
    _prune(layout.tmp)
    if run.timed_out:
        raise NhctlError(
            "D152",
            f"The fresh run took longer than {args.timeout:g}s and was stopped.",
            "Rerun with a larger --timeout, or check for a cell that waits forever.",
        )

    lines = [ln for ln in run.out.splitlines() if ln.startswith(MARKER)]
    if not lines:
        raise _unfinished("The fresh run", run)
    result = json.loads(lines[-1][len(MARKER) :])
    cells = nb["cells"]
    code_cells = sum(1 for c in cells if isinstance(c, dict) and c.get("cell_type") == "code")
    failing = failing_report(result, cells)
    common.emit_event(
        layout, "fresh_run", nb=shown, ok=bool(result.get("ok")),
        failing_cell_uid=(failing or {}).get("uid"), n_cells=code_cells, ms=ms, via="nbclient",
    )  # fmt: skip
    data = {
        "ok": bool(result.get("ok")),
        "notebook": shown,
        "copy": common.rel(project, copy),
        "kernel": result.get("kernel"),
        "code_cells": code_cells,
        "executed": result.get("executed"),
        "ms": ms,
        "failing": failing,
    }
    if data["ok"]:
        return Result(data, f"Fresh run OK: {shown} ran top to bottom ({code_cells} code cells).")
    assert failing is not None
    where = failing.get("label") or "kernel startup"
    reason = f"{failing['ename']}: {failing['evalue']}".rstrip(": ")
    return Result(data, f"Fresh run FAILED in {shown} at {where}: {reason}", 1)


def _unfinished(what: str, run: Isolated) -> NhctlError:
    """D151: the runner ended before the deadline without its records."""
    fix = "Check that the project env has jupyterlab (it brings nbclient): nhctl env sync."
    if "No module named" in run.tail:
        fix = "The project env lacks nbclient/ipykernel; run nhctl env sync."
    return NhctlError(
        "D151",
        f"{what} didn't finish (exit {run.code}).",
        fix,
        data={"output_tail": _output_tail(run.tail)},
    )


# Rule keys, as D154's message names them (the skill words them for the user: design §6.10).
def _flagged_text(flagged: list) -> str:
    parts = [f"{f.get('label')} ({', '.join(f.get('rules') or [])})" for f in flagged]
    return "; ".join(parts)


def _notebook_dir(project: Path, notebook: Path) -> str:
    """The notebook's folder relative to the project, as L013 takes it: "" for the root or a
    notebook outside the project (design §6.10)."""
    folder = posixpath.normpath(os.path.relpath(notebook.parent, project).replace(os.sep, "/"))
    return "" if folder in (".", "..") or folder.startswith("../") else folder


def _code_cells(copy: Path) -> list[int]:
    """The indexes of the copy's code cells with source: what the review runner runs or reports
    as not run (it skips the others, as nbclient does), each with its ``cell`` line."""
    nb = paths.read_json(copy, None)
    cells = nb.get("cells") if isinstance(nb, dict) else None
    found = []
    for index, cell in enumerate(cells if isinstance(cells, list) else []):
        if isinstance(cell, dict) and cell.get("cell_type") == "code":
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else str(source or "")
            if source.strip():
                found.append(index)
    return found


def _review(
    args: argparse.Namespace,
    project: Path,
    layout: paths.Layout,
    original: Path,
    shown: str,
    prefix: Path,
) -> Result:
    data_dir = common.plugin_data_dir(getattr(args, "plugin_data", None))
    python = common.runtime_python(data_dir, "the review can't analyse the notebook")
    layout.tmp.mkdir(parents=True, exist_ok=True)
    stamp = common.now_stamp()
    copy = layout.tmp / f"review-{stamp}-{os.getpid()}.ipynb"
    # after the check that it is a notebook, every step reads the copy
    shutil.copyfile(original, copy)
    _prune(layout.tmp, "review-*.ipynb", KEEP_REVIEW_COPIES)
    request = {
        "project": str(project),
        "copy": str(copy),
        "notebook_dir": _notebook_dir(project, original),
    }
    answer = _analysis(python, data_dir, project, "flag", request)
    flagged = answer.get("flagged")
    flagged = [f for f in flagged if isinstance(f, dict)] if isinstance(flagged, list) else []
    digest = str(answer.get("digest") or "")
    if flagged and not (args.skip_flagged or (digest and args.yes == digest)):
        copy.unlink(missing_ok=True)  # nothing ran
        changed = "The cells to ask about changed since that yes. " if args.yes is not None else ""
        raise NhctlError(
            "D154",
            f"{changed}{len(flagged)} cell(s) would do more than compute in the review's kernel: "
            f"{_flagged_text(flagged)}. Nothing ran.",
            f"Ask the user, then rerun with --yes {digest} to run them too, or --skip-flagged "
            "to run the rest.",
            2,
            data={"notebook": shown, "flagged": flagged, "digest": digest},
        )
    skip = [f["index"] for f in flagged if _int(f.get("index"))] if args.skip_flagged else []

    marker = f"{REVIEW_MARKER}{os.urandom(8).hex()} "
    runtime_dir = _private_runtime(layout, stamp)
    cell_timeout = args.cell_timeout or _review_cell_timeout(args.timeout)
    cmd = [str(prefix / "bin" / "python"), "-I", "-c", _REVIEW_RUNNER, str(copy)]
    cmd += [str(original.parent), str(cell_timeout)]
    cmd += [common.configured_kernel(project), marker, json.dumps(skip)]
    started = time.monotonic()
    try:
        isolated = _run_isolated(
            cmd, original.parent, _runner_env(prefix, runtime_dir), args.timeout, marker
        )
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
    ms = int((time.monotonic() - started) * 1000)
    run = parse_run(isolated.out, marker)
    reported = {cell["i"] for cell in run["cells"]}
    missing = [index for index in _code_cells(copy) if index not in reported]
    # Partial only when a code cell has no line: a deadline after the last one cut the kernel's
    # shutdown short, nothing the report needs (design §6.10).
    partial = isolated.timed_out and bool(missing)
    if missing and not isolated.timed_out:
        raise _unfinished("The review run", isolated)

    request = dict(
        request, notebook=shown, run=run, flagged=flagged, skipped=skip, timed_out=partial,
        timeout_s=args.timeout, ms=ms, when=time.strftime("%Y-%m-%d %H:%M", time.localtime()),
    )  # fmt: skip
    answer = _analysis(python, data_dir, project, "report", request)
    report, markdown = answer.get("report"), answer.get("markdown")
    if not isinstance(report, dict) or not isinstance(markdown, str):
        raise NhctlError("D199", "The review's analysis gave no report.", "Rerun the review.")
    path = layout.reviews / f"{stamp}-{original.stem}.md"
    common.write_keeping_mode(path, common.scrub(markdown))  # redacted again (design §6.8)
    shown_path = common.rel(project, path)
    hidden = report.get("hidden_state") or {}
    ran = report.get("ran") or {}
    common.emit_event(
        layout, "review", nb=shown, ok=bool(report.get("ok")), complete=not partial,
        failing=ran.get("error"), not_run=ran.get("not_run"),
        hidden_state=sum(len(v) for v in hidden.values() if isinstance(v, list)),
        n_cells=report.get("code_cells"), ms=ms, via="nbclient",
    )  # fmt: skip
    data = dict(report, report=shown_path)
    text_out = f"{markdown.rstrip()}\n\nReport: {shown_path}"
    if not partial:
        return Result(data, text_out)
    stopped = report.get("stopped_at")
    if isinstance(stopped, dict):
        where = f"while {stopped.get('label')} was running; the report covers the cells before it"
        fix = "Rerun with a larger --timeout, or check that cell for a wait that never ends."
    else:  # the kernel was still starting, or the deadline fell between two cells
        where = (
            "before any cell ran" if not (ran.get("ok") or ran.get("error")) else "between cells"
        )
        where += "; the report covers the cells that finished"
        fix = "Rerun with a larger --timeout."
    error = {
        "code": "D153",
        "message": f"The review stopped after {args.timeout:g}s {where}.",
        "fix": fix,
    }
    data["error"] = error
    return Result(data, f"{text_out}\nerror: {error['message']}\nfix: {error['fix']}", 1)
