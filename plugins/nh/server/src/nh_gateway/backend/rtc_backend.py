"""``RtcBackend``: the ``NotebookBackend`` for a live JupyterLab (plan §4.4).

It discovers the project's server once per process (again after the server stops answering or
rejects nh's token, e.g. JupyterLab restarted: the call that notices retries once against the
server found then), keeps one room connection per notebook (``rtc.RtcDocument``, re-matched to
the path so a renamed notebook is noticed), attaches to the notebook's session kernel before
every run or probe, and falls back to reading the file from disk for read-only views when no
server is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from .._shared import secrets
from .._shared.paths import Layout, atomic_write_json, read_json, safe_name
from ..config import ConfigCache
from ..exec import probes
from ..exec.runner import RtcExecution, Runner, in_daemon_thread
from ..policy.errors import NhError
from . import discovery, kernel, rest
from .base import (
    CellMissing,
    CellPatch,
    CellView,
    Execution,
    KernelStatus,
    NewCell,
    NotebookRef,
    OutputsMode,
)
from .diskread import DiskReadBackend
from .rtc import RtcDocument

log = logging.getLogger("nh_gateway.backend")

T = TypeVar("T")
JANITOR_S = 30.0
KERNEL_IDLE_S = 120.0
ROOM_IDLE_S = 1800.0
ROOM_CHECK_S = 2.0  # how often a connected room is re-matched to its path (renames)
RECORD_GRACE_S = 5.0  # a new running record may be ahead of its cell's mark in the room
READ_ONLY_FALLBACK = {"E130", "E131"}
STUCK_NOTICE = "[nh] nh restarted while this cell was running; its later output was not saved.\n"
GONE = "(the JupyterLab nh was using stopped answering)"
REJECTED = "(the server rejected nh's token)"


class _ServerLost(NhError):
    """The server stopped answering or no longer accepts nh's token (it restarted). The public
    method that hit it rediscovers the server and retries once; a second failure is E130."""

    def __init__(self, detail: str) -> None:
        super().__init__("E130", detail=detail)


def kernel_candidates(notebook_spec: str, configured: str) -> list[str]:
    """Kernelspecs to try for a new session: ``[jupyter].kernel_name`` first when set, then the
    notebook's own kernelspec, then python3 (empty names and repeats are dropped later)."""
    return [configured, notebook_spec, "python3"]


def _pid_alive(pid: Any) -> bool:
    return discovery.pid_alive(pid)


def _fresh(record: dict[str, Any]) -> bool:
    """A running record written in the last few seconds."""
    started = record.get("started")
    return isinstance(started, (int, float)) and time.time() - started < RECORD_GRACE_S


@dataclass
class _Run:
    runner: Runner
    execution: RtcExecution
    kernel_id: str

    @property
    def live(self) -> bool:
        return not self.execution.future.done()


def _version_major(version: Any) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return None


class RtcBackend:
    def __init__(self, layout: Layout, cfg_cache: ConfigCache) -> None:
        self.layout = layout
        self.cfg_cache = cfg_cache
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nh-rest")
        self._api: rest.Rest | None = None
        self._server_lock: asyncio.Lock | None = None
        self._docs: dict[str, RtcDocument] = {}
        self._checked: set[str] = set()
        self._kernels: dict[str, kernel.KernelHandle] = {}
        self._notebook_kernel: dict[str, str] = {}
        self._runs: dict[str, _Run] = {}
        self._languages: dict[str, str] | None = None
        self._collab: str | None = None
        self._notices: dict[str, list[str]] = {}
        self._disk = DiskReadBackend(layout.project, cfg_cache)
        self._janitor: asyncio.Task[None] | None = None
        self._saves: set[asyncio.Future[None]] = set()  # save requests after runs, kept alive

    async def _retry(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op``; when the server went away or changed its token, rediscover it and run it once more.

        Only steps before any write reach the server, so a retry never repeats a write.
        """
        try:
            return await op()
        except _ServerLost as exc:
            log.info("JupyterLab changed under nh; rediscovering: %s", str(exc).splitlines()[-2:])
        return await op()

    # ------------------------------------------------------------------ server

    @staticmethod
    def _key(ref: NotebookRef) -> str:
        return str(ref.abs_path)

    def _note(self, ref: NotebookRef, text: str) -> None:
        notes = self._notices.setdefault(self._key(ref), [])
        if text not in notes:
            notes.append(text)

    def take_notices(self, ref: NotebookRef) -> list[str]:
        """One-time notes for the user (new kernel session, nbformat upgrade, reset cells). Clears them."""
        return self._notices.pop(self._key(ref), [])

    def _env_gate(self) -> None:
        """Hard gate from ``nhctl env``: the project's JupyterLab needs jupyter-collaboration >= 5."""
        env = read_json(self.layout.env_json)
        if not isinstance(env, dict):
            return
        major = _version_major(env.get("jupyter_collaboration"))
        if major is not None and major < 5:
            raise NhError(
                "E131",
                detail=f"(the project env has jupyter-collaboration {env['jupyter_collaboration']})",
            )

    async def _server(self) -> rest.Rest:
        if self._api is not None:
            return self._api
        if self._server_lock is None:
            self._server_lock = asyncio.Lock()
        async with self._server_lock:
            if self._api is None:
                cfg = self.cfg_cache.current()
                info = await asyncio.get_running_loop().run_in_executor(
                    self._pool, partial(discovery.discover, self.layout.project, cfg)
                )
                secrets.add_value("JUPYTER_TOKEN", info.token)  # redacted from now on (§6.8)
                self._env_gate()
                self._api = rest.Rest(info, self._pool)
                if self._janitor is None or self._janitor.done():
                    self._janitor = asyncio.create_task(self._janitor_loop(), name="nh-janitor")
        return self._api

    async def _server_gone(self, detail: str = GONE) -> NhError:
        """The server stopped answering or rejected the token: forget it and everything connected to it."""
        self._api = None
        self._checked.clear()
        docs, self._docs = list(self._docs.values()), {}
        for doc in docs:
            with contextlib.suppress(Exception):
                await doc.close()
        for handle in self._kernels.values():
            handle.retire()
        self._kernels.clear()
        self._notebook_kernel.clear()
        self._languages = None
        return _ServerLost(detail)

    async def _call(self, api: rest.Rest, fn: Any, *args: Any) -> Any:
        try:
            return await api.call(fn, *args)
        except rest.ServerGone:
            raise await self._server_gone() from None
        except rest.Rejected:
            raise await self._server_gone(REJECTED) from None

    def _api_path(self, ref: NotebookRef, api: rest.Rest) -> str:
        root = api.server.root_dir
        path = Path(os.path.realpath(ref.abs_path))
        if path != root and root not in path.parents:
            raise NhError(
                "E130",
                detail=f"The JupyterLab at {api.server.url} serves {root}, which doesn't "
                f"contain {ref.rel_path}.",
            )
        return path.relative_to(root).as_posix()

    async def resolve_notebook(self, rel: str | None) -> NotebookRef:
        cfg = self.cfg_cache.current()
        try:
            api = await self._server()
        except NhError as exc:
            if exc.code not in READ_ONLY_FALLBACK:
                raise
            return discovery.resolve_notebook(self.layout.project, None, rel, cfg)
        return discovery.resolve_notebook(self.layout.project, api.server, rel, cfg)

    # ------------------------------------------------------------------ documents

    async def _doc(self, ref: NotebookRef) -> RtcDocument:
        api = await self._server()
        key = self._key(ref)
        doc = self._docs.get(key)
        if doc is None or doc.api is not api:
            doc = self._docs[key] = RtcDocument(api, self._api_path(ref, api), label=ref.rel_path)
        try:
            await doc.ensure()
            if time.monotonic() - doc.checked_at > ROOM_CHECK_S and await doc.room_moved():
                doc = await self._replaced(ref, doc)
        except rest.ServerGone:
            raise await self._server_gone() from None
        except rest.Rejected:
            raise await self._server_gone(REJECTED) from None
        if key not in self._checked:
            await self._first_open(ref, doc)
            self._checked.add(key)
        await self._reset_stuck(ref, doc)
        return doc

    async def _replaced(self, ref: NotebookRef, doc: RtcDocument) -> RtcDocument:
        """The path now names another file (the notebook was renamed in JupyterLab, maybe a new one
        took its name): drop the old room and this notebook's kernel, and connect to the file there."""
        key = self._key(ref)
        run = self._runs.get(key)
        if run is not None and run.live and run.runner.doc is doc:
            # nh's running cell is in the renamed notebook: its run keeps that room until it ends
            self._runs[f"{key}\0renamed:{id(run)}"] = self._runs.pop(key)
            run.execution.future.add_done_callback(lambda _done: asyncio.ensure_future(doc.close()))
        else:
            await doc.close()
        self._checked.discard(key)
        self._notebook_kernel.pop(key, None)
        self._note(
            ref,
            f"{ref.rel_path} is now a different file (the notebook that was there was renamed or "
            "replaced in JupyterLab); nh works on the file at this path.",
        )
        fresh = self._docs[key] = RtcDocument(doc.api, doc.api_path, label=ref.rel_path)
        await fresh.ensure()
        return fresh

    def _run_elsewhere(self, cell_id: str) -> bool:
        """Whether another live nh process is running this cell (its ``running/<cell>.json``).

        A record left by a process that died is removed.
        """
        path = self._running_file(cell_id)
        record = read_json(path)
        if not isinstance(record, dict):
            return False
        pid = record.get("pid")
        if pid == os.getpid():
            return False
        if _pid_alive(pid):
            return True
        with contextlib.suppress(OSError):
            path.unlink()
        return False

    async def _reset_stuck(self, ref: NotebookRef, doc: RtcDocument) -> None:
        """nh cells still marked running with no run behind them (nh restarted mid-run): once the
        kernel is idle, mark them stopped. Checked on every use, so a busy kernel only delays it."""
        live = {run.runner.cell_id for run in self._runs.values() if run.live}
        stuck = [cell for cell in doc.stuck_cells(live) if not self._run_elsewhere(cell)]
        if not stuck or await self._session_busy(doc):
            return
        await asyncio.sleep(0.5)  # idle twice: the user may have just pressed run on it
        if await self._session_busy(doc):
            return
        live = {run.runner.cell_id for run in self._runs.values() if run.live}
        stuck = [cell for cell in doc.stuck_cells(live) if not self._run_elsewhere(cell)]
        for cell_id in stuck:
            doc.end_run(cell_id, None, STUCK_NOTICE)
            with contextlib.suppress(OSError):
                self._running_file(cell_id).unlink()
        if stuck:
            await doc.save()
            self._note(
                ref,
                f"{len(stuck)} nh cell(s) were still marked running from an earlier nh session; "
                "nh marked them stopped.",
            )

    async def _first_open(self, ref: NotebookRef, doc: RtcDocument) -> None:
        """Once per notebook: another server editing it (E138), format and language (E136)."""
        await asyncio.get_running_loop().run_in_executor(
            self._pool, partial(discovery.refuse_other_servers, doc.api.server, ref.abs_path)
        )
        ignored = discovery.config_url_problem(self.cfg_cache.current())
        if ignored:
            self._note(ref, ignored)
        major, _minor = doc.nbformat()
        if major != 4:
            raise NhError("E136", detail=f" (this notebook is nbformat {major})")
        meta = doc.notebook_meta()
        language = str(
            (meta.get("language_info") or {}).get("name")
            or (meta.get("kernelspec") or {}).get("language")
            or ""
        ).lower()
        if language and language != "python":
            raise NhError("E136", detail=f" (this notebook's language is {language})")

    async def _session_busy(self, doc: RtcDocument) -> bool:
        """Whether the notebook's kernel is busy right now (someone may be running an nh cell in the UI)."""
        try:
            sessions = await doc.api.call(doc.api.sessions)
        except (rest.ServerGone, rest.RestError):
            return True
        for session in sessions:
            if (
                session.get("path") == doc.api_path
                and (session.get("kernel") or {}).get("execution_state") == "busy"
            ):
                return True
        return False

    async def open(self, ref: NotebookRef) -> None:
        await self._retry(lambda: self._doc(ref))

    async def close(self, ref: NotebookRef) -> None:
        doc = self._docs.pop(self._key(ref), None)
        self._checked.discard(self._key(ref))
        if doc is not None:
            await doc.close()

    async def snapshot(
        self, ref: NotebookRef, *, outputs: OutputsMode = "summary"
    ) -> list[CellView]:
        try:
            doc = await self._retry(lambda: self._doc(ref))
        except NhError as exc:
            if exc.code in READ_ONLY_FALLBACK:
                return await self._disk.snapshot(ref, outputs=outputs)
            raise
        return doc.cells(outputs)

    async def notebook_meta(self, ref: NotebookRef) -> dict[str, Any]:
        try:
            doc = await self._retry(lambda: self._doc(ref))
        except NhError as exc:
            if exc.code in READ_ONLY_FALLBACK:
                return await self._disk.notebook_meta(ref)
            raise
        return doc.notebook_meta()

    def _after_write(self, ref: NotebookRef, doc: RtcDocument) -> None:
        if doc.upgraded_minor:
            doc.upgraded_minor = False
            self._note(
                ref,
                "The notebook was upgraded to nbformat 4.5 so its cells keep stable ids "
                "(a one-time change in git).",
            )

    async def set_notebook_meta(self, ref: NotebookRef, key: str, value: Any) -> None:
        doc = await self._retry(lambda: self._doc(ref))
        doc.set_meta(key, value)
        self._after_write(ref, doc)
        await doc.save()

    async def insert_cells(self, ref: NotebookRef, index: int, cells: list[NewCell]) -> None:
        doc = await self._retry(lambda: self._doc(ref))
        doc.insert(index, cells)
        self._after_write(ref, doc)
        await doc.save()

    async def update_cells(self, ref: NotebookRef, patches: list[CellPatch]) -> None:
        doc = await self._retry(lambda: self._doc(ref))
        doc.update(patches)
        self._after_write(ref, doc)
        await doc.save()

    async def delete_cells(self, ref: NotebookRef, ids: list[str]) -> list[CellView]:
        doc = await self._retry(lambda: self._doc(ref))
        deleted = doc.delete(ids)
        self._after_write(ref, doc)
        await doc.save()
        return deleted

    # ------------------------------------------------------------------ kernel

    async def _language(self, api: rest.Rest, name: str) -> str | None:
        if self._languages is None:
            try:
                specs = (await self._call(api, api.kernelspecs)).get("kernelspecs") or {}
            except rest.RestError:
                return None
            self._languages = {
                spec_name: str(((spec or {}).get("spec") or {}).get("language") or "").lower()
                for spec_name, spec in specs.items()
            }
        return self._languages.get(name) or None

    async def _attach(
        self, ref: NotebookRef, doc: RtcDocument, *, create: bool = True
    ) -> tuple[kernel.KernelHandle, dict[str, Any]]:
        """The notebook's session kernel; with ``create``, start a session if it has none."""
        cfg = self.cfg_cache.current()
        spec = str((doc.notebook_meta().get("kernelspec") or {}).get("name") or "")
        names = kernel_candidates(spec, str(cfg["jupyter"].get("kernel_name") or ""))
        attach = partial(kernel.attach_session, create=create)
        try:
            session, created = await self._call(doc.api, attach, doc.api, doc.api_path, names)
        except rest.RestError as exc:
            raise NhError("E134", detail=f" ({exc})") from None
        model = session.get("kernel")
        if not model:
            raise NhError("E134", detail=" (the notebook has no kernel selected)")
        if model.get("execution_state") == "dead":
            raise NhError("E134", detail=" (the kernel has died)")
        language = await self._language(doc.api, str(model.get("name") or ""))
        if language and language != "python":
            raise NhError("E136", detail=f" (the notebook's kernel runs {language})")
        kernel_id = str(model["id"])
        key = self._key(ref)
        previous = self._notebook_kernel.get(key)
        if previous and previous != kernel_id:
            shared = any(
                kid == previous for other, kid in self._notebook_kernel.items() if other != key
            )
            old = None if shared else self._kernels.pop(previous, None)
            if old is not None:
                old.retire()
        self._notebook_kernel[key] = kernel_id
        handle = self._kernels.get(kernel_id)
        if handle is None:
            handle = self._kernels[kernel_id] = kernel.KernelHandle(
                kernel_id, name=str(model.get("name") or "")
            )
        if created:
            self._note(
                ref,
                "nh started a kernel for this notebook; open the notebook in JupyterLab to watch.",
            )
        return handle, model

    def _running_on(self, kernel_id: str) -> _Run | None:
        return next(
            (run for run in self._runs.values() if run.live and run.kernel_id == kernel_id), None
        )

    async def _busy(self, api: rest.Rest, kernel_id: str, model: dict[str, Any]) -> bool:
        """REST says busy twice, 0.5 s apart (JupyterLab's completion requests flip it briefly)."""
        if model.get("execution_state") != "busy":
            return False
        await asyncio.sleep(0.5)
        try:
            again = await self._call(api, api.kernel, kernel_id)
        except rest.RestError:
            return True
        return again is None or again.get("execution_state") == "busy"

    async def _probe(
        self,
        handle: kernel.KernelHandle,
        api: rest.Rest,
        name: str,
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            client = handle.client("probe", api.server)
            return probes.run_probe(
                client,
                name,
                args,
                timeout,
                lock=handle.probe_lock,
                interrupt=lambda: api.interrupt(handle.kernel_id),  # only ever stops nh's own probe
            )

        return await in_daemon_thread(work, name="nh-probe")

    @staticmethod
    def _apply_attach(handle: kernel.KernelHandle, payload: dict[str, Any]) -> None:
        python = payload.get("python")
        if isinstance(python, list) and len(python) >= 2:
            handle.python = (int(python[0]), int(python[1]))
            handle.prefix = payload.get("prefix")
            handle.executable = payload.get("executable")
        pid, started = payload.get("pid"), payload.get("started")
        if pid is not None and started is not None:
            handle.incarnation = f"{pid}@{started}"

    async def kernel_status(self, ref: NotebookRef, *, create: bool = True) -> KernelStatus:
        """The notebook's session kernel (``create=False``: never start one).

        ``incarnation`` comes from the attach probe, run every time the kernel is free: a restart
        keeps the kernel id, only the process tells. While the kernel is busy it stays what this
        process saw last, or None (unknown, which is not a new kernel).
        """

        async def attach() -> tuple[kernel.KernelHandle, dict[str, Any], RtcDocument]:
            doc = await self._doc(ref)
            handle, model = await self._attach(ref, doc, create=create)
            return handle, model, doc

        handle, model, doc = await self._retry(attach)
        state = str(model.get("execution_state") or "unknown")
        busy = self._running_on(handle.kernel_id) is not None or await self._busy(
            doc.api, handle.kernel_id, model
        )
        if busy:
            state = "busy"
        else:
            state = "idle" if state == "busy" else state  # it was a blip (a completion request)
            self._apply_attach(handle, await self._probe(handle, doc.api, "attach", {}, 3.0))
        return KernelStatus(
            kernel_id=handle.kernel_id,
            name=handle.name,
            execution_state=state,
            language="python",
            python_version=handle.python,
            prefix=handle.prefix,
            executable=handle.executable,
            incarnation=handle.incarnation,
        )

    async def probe(
        self, ref: NotebookRef, name: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if name not in probes.PROBES:
            return {"error": f"ValueError: unknown probe {name!r}"}

        async def attach() -> tuple[kernel.KernelHandle, dict[str, Any], RtcDocument]:
            doc = await self._doc(ref)
            handle, model = await self._attach(ref, doc, create=False)  # a look never starts one
            return handle, model, doc

        handle, model, doc = await self._retry(attach)
        if self._running_on(handle.kernel_id) is not None:
            return {"error": "KernelBusy: nh is running a cell in this kernel"}
        if await self._busy(doc.api, handle.kernel_id, model):
            return {"error": "KernelBusy: the kernel is running another cell"}
        return await self._probe(handle, doc.api, name, args, timeout)

    # ------------------------------------------------------------------ execution

    def _running_file(self, cell_id: str) -> Path:
        return self.layout.running / f"{safe_name(cell_id)}.json"

    def _run_done(self, cell_id: str, doc: RtcDocument) -> None:
        """A run ended: drop its record and have the room save its outputs (design §6.13)."""
        with contextlib.suppress(OSError):
            self._running_file(cell_id).unlink()
        task = asyncio.ensure_future(doc.save())
        self._saves.add(task)
        task.add_done_callback(self._saves.discard)

    def _marked_running(self, ref: NotebookRef) -> set[str] | None:
        """Ids of the cells the notebook marks running; None when nh has no synced copy of it."""
        doc = self._docs.get(self._key(ref))
        if doc is None or not doc.synced:
            return None
        try:
            return {cell.id for cell in doc.cells("none") if cell.running}
        except NhError:
            return None

    def live_run(self, ref: NotebookRef) -> str | None:
        """The cell nh is running in this notebook's kernel: this process's run, else one another
        live nh process recorded for this notebook in ``.nh/state/running``.

        Another process's record counts only while the notebook marks its cell running (or for
        a few seconds after it was written, while that mark travels through the room). A record
        whose process is gone, or whose cell no longer runs (its pid now belongs to some other
        program), is removed.
        """
        key = self._key(ref)
        run = self._runs.get(key)
        if run is None or not run.live:
            kernel_id = self._notebook_kernel.get(key)
            run = self._running_on(kernel_id) if kernel_id else None
        if run is not None:
            return run.runner.cell_id
        try:
            records = sorted(self.layout.running.glob("*.json"))
        except OSError:
            return None
        marked = self._marked_running(ref)
        for path in records:
            record = read_json(path)
            if (
                not isinstance(record, dict)
                or record.get("notebook") != ref.rel_path
                or record.get("pid") == os.getpid()
            ):
                continue
            cell_id = str(record.get("cell_id") or "")
            if _pid_alive(record.get("pid")) and (
                marked is None or cell_id in marked or _fresh(record)
            ):
                return cell_id or None
            with contextlib.suppress(OSError):
                path.unlink()
        return None

    async def start_execution(
        self,
        ref: NotebookRef,
        cell_id: str,
        *,
        hard_timeout: float,
        expected_source: str | None = None,
    ) -> Execution:
        key = self._key(ref)

        async def prepare() -> tuple[RtcDocument, kernel.KernelHandle]:
            doc = await self._doc(ref)
            current = self._runs.get(key)
            if current is not None and current.live:
                raise NhError("E133", detail=" (nh is still running another cell in this notebook)")
            handle, _model = await self._attach(ref, doc)
            if self._running_on(handle.kernel_id) is not None:
                raise NhError("E133", detail=" (nh is still running a cell in this kernel)")
            return doc, handle

        doc, handle = await self._retry(prepare)
        if not handle.probe_lock.locked():
            handle.retire("probe")  # its socket would decode every message of this run again
        client = await in_daemon_thread(handle.client, "exec", doc.api.server, name="nh-connect")
        await doc.ensure()  # the room may have dropped while the kernel client connected
        try:
            resolved, source = doc.begin_run(cell_id, expected_source)
        except CellMissing:
            raise NhError("E140", cell_id=cell_id) from None
        await doc.settle()
        runner = Runner(
            doc=doc,
            cell_id=resolved,
            code=source,
            client=client,
            api=doc.api,
            kernel_id=handle.kernel_id,
            hard_timeout=hard_timeout,
            on_done=lambda _result: self._run_done(resolved, doc),
            on_lost=lambda: handle.retire("exec"),
        )
        execution = runner.start()
        self._runs[key] = _Run(runner=runner, execution=execution, kernel_id=handle.kernel_id)
        with contextlib.suppress(OSError):
            atomic_write_json(
                self._running_file(resolved),
                {
                    "cell_id": resolved,
                    "notebook": ref.rel_path,
                    "kernel_id": handle.kernel_id,
                    "started": time.time(),
                    "pid": os.getpid(),
                },
            )
        return execution

    async def interrupt(self, ref: NotebookRef) -> None:
        """Interrupt nh's running cell. Refuses (E133) while it is still queued behind someone else's cell."""
        run = self._runs.get(self._key(ref))
        if run is None or not run.live:
            return
        if not run.execution.started():
            raise NhError(
                "E133",
                detail=" with another cell; nh's cell is still queued and was not interrupted",
            )
        try:
            await run.runner.interrupt()
        except rest.ServerGone:
            raise await self._server_gone() from None
        except rest.Rejected:
            raise await self._server_gone(REJECTED) from None

    async def fresh_run(self, ref: NotebookRef) -> dict[str, Any]:
        raise NotImplementedError("fresh runs are `nhctl fresh-run` in v0.1")

    # ------------------------------------------------------------------ status and lifecycle

    async def _collab_version(self, api: rest.Rest) -> str:
        if self._collab is None:
            env = read_json(self.layout.env_json)
            version = env.get("jupyter_collaboration") if isinstance(env, dict) else None
            if not version:
                try:
                    extensions = await asyncio.wait_for(api.call(api.extensions), 6.0)
                    version = next(
                        (
                            ext.get("installed_version")
                            for ext in extensions
                            if "collaboration" in str(ext.get("name", ""))
                        ),
                        None,
                    )
                except Exception:  # advisory only
                    version = None
            self._collab = str(version) if version else "unknown"
        return self._collab

    async def describe(self, ref: NotebookRef | None) -> dict[str, str]:
        """Lines for ``nh_inspect(view="status")``. No tokens."""
        ignored = discovery.config_url_problem(self.cfg_cache.current())
        try:
            api = await self._server()
        except NhError as exc:
            lines = str(exc).splitlines()
            reasons = [
                line for line in lines[2:] if line.strip() and not line.startswith("Next:")
            ]  # the lines between "nh: E130" and "Next:" say why
            failed = {
                "backend": "rtc",
                "server": lines[0] if lines else "unavailable",
                **({"reason": " ".join(reasons)} if reasons else {}),
                "fallback": "reading the notebook file from disk (outline only)",
            }
            return {**failed, "config": ignored} if ignored else failed
        server = api.server
        info = {
            "backend": "rtc",
            "server": f"{server.url} (jupyter_server {server.version or '?'}, pid {server.pid or '?'}, "
            f"root {server.root_dir})",
            "collaboration": await self._collab_version(api),
        }
        if ignored:
            info["config"] = ignored
        if ref is not None:
            doc = self._docs.get(self._key(ref))
            if doc is not None and doc.synced:
                info["room"] = (
                    f"synced, {doc.peers()} other client(s), reconnects {max(0, doc.generation - 1)}"
                )
            else:
                info["room"] = "not connected yet"
            notes = self._notices.get(self._key(ref))
            if notes:
                info["notes"] = " ".join(notes)
        return info

    async def _janitor_loop(self) -> None:
        """Close idle kernel clients (they queue every message of the user's kernel) and idle rooms."""
        while True:
            await asyncio.sleep(JANITOR_S)
            now = time.monotonic()
            busy = {run.kernel_id for run in self._runs.values() if run.live}
            for kernel_id, handle in self._kernels.items():
                idle = now - handle.last_used > KERNEL_IDLE_S
                if idle and kernel_id not in busy and not handle.probe_lock.locked():
                    handle.retire()
            running_docs = {key for key, run in self._runs.items() if run.live}
            for key, doc in list(self._docs.items()):
                if key not in running_docs and doc.synced and now - doc.last_used > ROOM_IDLE_S:
                    await doc.close()

    async def aclose(self) -> None:
        """Stop following running cells (marking them in the notebook), close rooms and clients."""
        live = [run for run in self._runs.values() if run.live]
        for run in live:
            run.runner.abort("nh stopped while this cell was running")
        if live:
            await asyncio.wait([run.execution.future for run in live], timeout=15.0)
        if self._janitor is not None:
            self._janitor.cancel()
        for doc in list(self._docs.values()):
            with contextlib.suppress(Exception):
                await doc.close()
        self._docs.clear()
        for handle in self._kernels.values():
            handle.retire()
        self._kernels.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
