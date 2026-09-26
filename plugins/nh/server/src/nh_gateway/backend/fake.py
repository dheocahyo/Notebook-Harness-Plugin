"""In-memory ``NotebookBackend`` for tests and ``NH_BACKEND=fake``: no JupyterLab, the real protocol.

Notebooks are nbformat dicts. Code runs with ``exec`` in one persistent namespace that plays the
kernel: ``print`` becomes stream outputs, ``display()`` becomes ``display_data``, a trailing
expression becomes ``execute_result`` and exceptions become IPython-style ``error`` outputs.
Probes run the real probe sources against that namespace. Every write is one "transaction"
(recorded in :attr:`FakeBackend.transactions`) and checks ids and base sources first, like
``RtcBackend``.

Knobs: ``kernel_busy`` (runs queue, probes refuse), ``fail_open`` (True or an error code),
``exec_delay_s`` (each run sleeps first; interruptible), ``python_version`` (what probes report).
Simulated user actions: :meth:`FakeBackend.user_edit`, ``user_delete``, ``user_insert``,
``user_interrupt`` (the stop button) and :meth:`FakeBackend.restart_kernel` (which, like Jupyter,
keeps the kernel id but changes ``incarnation``).
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import copy
import re
import sys
import time
import types
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import nbformat

from ..exec import probes
from ..exec.docsafe import output_summary, safe_text, sanitize_for_doc
from ..exec.runner import USER_INTERRUPT_NOTE, error_info, stopped_by_signal
from ..meta import normalize_source
from ..policy.errors import NhError
from .base import (
    CellConflict,
    CellMissing,
    CellPatch,
    CellView,
    ExecResult,
    KernelStatus,
    NewCell,
    NotebookRef,
    OutputsMode,
)
from .kernel import output_hook

_MAGIC = re.compile(r"^(\s*)[%!]")
_RULE = "-" * 75


class _Stream:
    """A file-like object that turns writes into merged stream outputs."""

    def __init__(self, outputs: list[dict[str, Any]], name: str) -> None:
        self.outputs = outputs
        self.name = name

    def write(self, text: str) -> int:
        if text:
            output_hook(
                self.outputs,
                {"header": {"msg_type": "stream"}, "content": {"name": self.name, "text": text}},
            )
        return len(text)

    def flush(self) -> None:
        return None


def _bundle(value: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"text/plain": repr(value)}
    html = getattr(value, "_repr_html_", None)
    if callable(html):
        try:
            rendered = html()
        except Exception:
            rendered = None
        if isinstance(rendered, str):
            data["text/html"] = rendered
    return data


def _mask_magics(code: str) -> str:
    """IPython line magics and shell escapes become ``pass`` (the fake kernel can't run them)."""
    if code.lstrip().startswith("%%"):
        return ""
    return "\n".join(
        _MAGIC.sub(r"\1pass  # ", line) if _MAGIC.match(line) else line
        for line in code.splitlines()
    )


class FakeExecution:
    """The ``Execution`` handle for a fake run. ``started_at`` is ``time.monotonic()`` at submission."""

    def __init__(self, cell_id: str, future: asyncio.Future[ExecResult]) -> None:
        self.cell_id = cell_id
        self.started_at = time.monotonic()
        self.outputs: list[dict[str, Any]] = []
        self.interrupt = asyncio.Event()
        self.by_user = False  # the interrupt came from JupyterLab's stop button, not from nh
        self.task: asyncio.Task[None] | None = None
        self._future = future
        self._started = False

    @property
    def future(self) -> asyncio.Future[ExecResult]:
        return self._future

    def started(self) -> bool:
        return self._started

    def partial_outputs(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.outputs)


class FakeBackend:
    def __init__(
        self,
        project: Path | None = None,
        *,
        kernel_busy: bool = False,
        fail_open: bool | str = False,
        exec_delay_s: float = 0.0,
        python_version: tuple[int, int] = (3, 12),
    ) -> None:
        self.project = project
        self.kernel_busy = kernel_busy
        self.fail_open = fail_open
        self.exec_delay_s = exec_delay_s
        self.python_version = python_version
        self.notebooks: dict[str, dict[str, Any]] = {}
        self.transactions: list[tuple[str, str]] = []  # (operation, notebook) per write
        self._runs: dict[str, FakeExecution] = {}
        self.kernel_id = str(uuid.uuid4())
        self.restart_kernel()

    # ------------------------------------------------------------------ the fake kernel

    def restart_kernel(self, *, new_id: bool = False) -> None:
        """A restarted kernel: empty namespace, execution count back to 0, a new ``incarnation``.

        Like Jupyter, a restart keeps the kernel id; ``new_id`` simulates a different kernel
        (a new session) instead.
        """
        if new_id:
            self.kernel_id = str(uuid.uuid4())
        self.incarnation = uuid.uuid4().hex
        self.execution_count = 0
        self._current: list[dict[str, Any]] = []
        self._shell = types.SimpleNamespace(user_ns={}, user_ns_hidden={}, execution_count=1)
        self._builtins = dict(
            builtins.__dict__, display=self._display, get_ipython=lambda: self._shell
        )
        initial = {
            "__name__": "__main__",
            "__builtins__": self._builtins,
            "In": [""],
            "Out": {},
            "get_ipython": lambda: self._shell,
            "exit": None,
            "quit": None,
        }
        self._shell.user_ns.update(initial)
        self._shell.user_ns_hidden.update(initial)

    @property
    def namespace(self) -> dict[str, Any]:
        return self._shell.user_ns

    def _display(self, *objs: Any, raw: bool = False, **_kwargs: Any) -> None:
        for obj in objs:
            data = obj if raw and isinstance(obj, dict) else _bundle(obj)
            self._current.append({"output_type": "display_data", "data": data, "metadata": {}})

    def _traceback(self, exc: BaseException, filename: str, source: str, count: int) -> list[str]:
        lines = source.splitlines()
        if isinstance(exc, SyntaxError):
            line = exc.lineno or 1
            text = lines[line - 1] if 0 < line <= len(lines) else ""
            return [
                f"  Cell In[{count}], line {line}",
                f"    {text}",
                "    ^",
                f"{type(exc).__name__}: {exc.msg}",
            ]
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == filename:
                frames.append(tb.tb_lineno)
            tb = tb.tb_next
        line = frames[-1] if frames else 1
        text = lines[line - 1] if 0 < line <= len(lines) else ""
        return [
            _RULE,
            f"{type(exc).__name__}{' ' * 33}Traceback (most recent call last)",
            f"Cell In[{count}], line {line}",
            f"----> {line} {text}",
            "",
            f"{type(exc).__name__}: {exc}",
        ]

    def _execute(self, code: str) -> tuple[list[dict[str, Any]], int, str]:
        """Run code like IPython would; returns ``(outputs, execution_count, reply status)``."""
        self.execution_count += 1
        count = self.execution_count
        self._shell.execution_count = count + 1  # like IPython: the count the next cell will get
        outputs: list[dict[str, Any]] = []
        self._current = outputs
        filename = f"<nh-fake-cell-{count}>"
        namespace = self.namespace
        namespace["In"].append(code)
        status = "ok"
        try:
            tree = ast.parse(_mask_magics(code), filename=filename)
            last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
            with (
                redirect_stdout(_Stream(outputs, "stdout")),
                redirect_stderr(_Stream(outputs, "stderr")),
            ):
                exec(compile(tree, filename, "exec"), namespace)
                value = None
                if isinstance(last, ast.Expr):
                    value = eval(compile(ast.Expression(last.value), filename, "eval"), namespace)
            if value is not None:
                outputs.append(
                    {
                        "output_type": "execute_result",
                        "execution_count": count,
                        "data": _bundle(value),
                        "metadata": {},
                    }
                )
                namespace["Out"][count] = namespace["_"] = value
        except (
            Exception,
            SystemExit,
            KeyboardInterrupt,
        ) as exc:  # IPython shows these as errors too
            status = "error"
            outputs.append(
                {
                    "output_type": "error",
                    "ename": type(exc).__name__,
                    "evalue": str(exc),
                    "traceback": self._traceback(exc, filename, code, count),
                }
            )
        finally:
            self._current = []
        return outputs, count, status

    # ------------------------------------------------------------------ notebooks

    def _rel(self, ref: NotebookRef) -> str:
        return ref.rel_path

    def _nb(self, ref: NotebookRef) -> dict[str, Any]:
        if self.fail_open:
            raise NhError(self.fail_open if isinstance(self.fail_open, str) else "E130")
        rel = self._rel(ref)
        if rel not in self.notebooks:
            if ref.abs_path.is_file():
                with open(ref.abs_path, encoding="utf-8") as handle:
                    self.notebooks[rel] = nbformat.read(handle, as_version=4)
            else:
                self.notebooks[rel] = nbformat.v4.new_notebook()
            for cell in self.notebooks[rel]["cells"]:
                cell.setdefault("id", uuid.uuid4().hex[:8])
        return self.notebooks[rel]

    def _commit(self, operation: str, ref: NotebookRef) -> None:
        notebook = self._nb(ref)
        if notebook.get("nbformat") == 4 and int(notebook.get("nbformat_minor") or 0) < 5:
            notebook["nbformat_minor"] = 5
        self.transactions.append((operation, self._rel(ref)))

    @staticmethod
    def _find(cells: list[dict[str, Any]], cell_id: str) -> int | None:
        for index, cell in enumerate(cells):
            if cell.get("id") == cell_id:
                return index
        for index, cell in enumerate(cells):
            if ((cell.get("metadata") or {}).get("nh") or {}).get("uid") == cell_id:
                return index
        return None

    @staticmethod
    def _view(cell: dict[str, Any], index: int, mode: OutputsMode) -> CellView:
        view = CellView(
            id=str(cell.get("id") or ""),
            index=index,
            cell_type=cell["cell_type"],
            source=str(cell.get("source") or ""),
            metadata=copy.deepcopy(dict(cell.get("metadata") or {})),
        )
        if view.cell_type == "code":
            view.execution_count = cell.get("execution_count")
            view.running = cell.get("execution_state") == "running"
            outputs = copy.deepcopy(list(cell.get("outputs") or []))
            if mode != "none":
                view.summary = output_summary(outputs)
            if mode == "full":
                view.outputs = outputs
        return view

    def notebook(self, rel: str) -> dict[str, Any]:
        """The notebook as it would be saved (nbformat dict, without runtime-only keys)."""
        notebook = copy.deepcopy(self.notebooks[rel])
        for cell in notebook["cells"]:
            cell.pop("execution_state", None)
        return notebook

    # Simulated user actions in JupyterLab (for tests).

    def user_edit(self, rel: str, cell_id: str, source: str) -> None:
        cells = self.notebooks[rel]["cells"]
        index = self._find(cells, cell_id)
        if index is None:
            raise CellMissing(cell_id)
        cells[index]["source"] = source

    def user_delete(self, rel: str, cell_id: str) -> None:
        cells = self.notebooks[rel]["cells"]
        index = self._find(cells, cell_id)
        if index is not None:
            del cells[index]

    def user_insert(self, rel: str, index: int, source: str, cell_type: str = "code") -> str:
        make = nbformat.v4.new_code_cell if cell_type == "code" else nbformat.v4.new_markdown_cell
        cell = make(source, id=uuid.uuid4().hex[:8])
        self.notebooks.setdefault(rel, nbformat.v4.new_notebook())["cells"].insert(index, cell)
        return cell["id"]

    def user_interrupt(self) -> bool:
        """The user presses stop in JupyterLab: interrupts whatever nh cell is running now.

        False when nothing of nh's is running (a queued run belongs behind someone else's cell).
        """
        execution = self._live_run()
        if execution is None or not execution.started():
            return False
        execution.by_user = True
        execution.interrupt.set()
        return True

    # ------------------------------------------------------------------ NotebookBackend

    async def resolve_notebook(self, rel: str | None) -> NotebookRef:
        rel = (rel or "notebooks/01_eda.ipynb").replace("\\", "/")
        if not rel.endswith(".ipynb") or rel.startswith("/") or ".." in rel.split("/"):
            raise NhError(
                "E132", notebook=rel, candidates=", ".join(sorted(self.notebooks)) or "none"
            )
        base = self.project or Path.cwd()
        return NotebookRef(abs_path=(base / rel).resolve(), api_path=rel, rel_path=rel)

    async def open(self, ref: NotebookRef) -> None:
        self._nb(ref)

    async def close(self, ref: NotebookRef) -> None:
        return None

    async def aclose(self) -> None:
        for execution in self._runs.values():
            if execution.task is not None and not execution.task.done():
                execution.task.cancel()

    async def snapshot(
        self, ref: NotebookRef, *, outputs: OutputsMode = "summary"
    ) -> list[CellView]:
        return [
            self._view(cell, index, outputs) for index, cell in enumerate(self._nb(ref)["cells"])
        ]

    async def notebook_meta(self, ref: NotebookRef) -> dict[str, Any]:
        return copy.deepcopy(dict(self._nb(ref).get("metadata") or {}))

    async def set_notebook_meta(self, ref: NotebookRef, key: str, value: Any) -> None:
        self._nb(ref).setdefault("metadata", {})[key] = sanitize_for_doc(value)
        self._commit("set_meta", ref)

    async def insert_cells(self, ref: NotebookRef, index: int, cells: list[NewCell]) -> None:
        notebook = self._nb(ref)
        existing = {cell.get("id") for cell in notebook["cells"]}
        wanted = [cell.id for cell in cells]
        if existing.intersection(wanted) or len(set(wanted)) != len(wanted):
            raise ValueError(
                f"cell id already in the notebook: {sorted(existing.intersection(wanted))}"
            )
        built = []
        for cell in cells:
            make = (
                nbformat.v4.new_code_cell
                if cell.cell_type == "code"
                else nbformat.v4.new_markdown_cell
            )
            built.append(
                make(safe_text(cell.source), id=cell.id, metadata=sanitize_for_doc(cell.metadata))
            )
        total = len(notebook["cells"])
        at = total if index < 0 or index > total else index
        notebook["cells"][at:at] = built
        self._commit("insert", ref)

    async def update_cells(self, ref: NotebookRef, patches: list[CellPatch]) -> None:
        cells = self._nb(ref)["cells"]
        targets = []
        for patch in patches:
            index = self._find(cells, patch.id)
            if index is None:
                raise CellMissing(patch.id)
            current = str(cells[index].get("source") or "")
            if patch.base_source is not None and normalize_source(current) != normalize_source(
                patch.base_source
            ):
                raise CellConflict(patch.id, current)
            targets.append((cells[index], patch))
        for cell, patch in targets:
            self._apply(cell, patch)
        self._commit("update", ref)

    @staticmethod
    def _apply(cell: dict[str, Any], patch: CellPatch) -> None:
        if patch.source is not None:
            cell["source"] = safe_text(patch.source)
        metadata = cell.setdefault("metadata", {})
        if patch.drop_nh_metadata:
            metadata.pop("nh", None)
        if patch.metadata is not None:
            metadata["nh"] = sanitize_for_doc(patch.metadata)
        if patch.tags_add or patch.tags_remove:
            current = list(metadata.get("tags") or [])
            tags = [t for t in current if t not in patch.tags_remove] + [
                t for t in patch.tags_add if t not in current
            ]
            if tags:
                metadata["tags"] = tags
            else:
                metadata.pop("tags", None)
        if cell["cell_type"] == "code":
            if patch.clear_outputs:
                cell["outputs"] = []
            if patch.execution_count != "keep":
                cell["execution_count"] = patch.execution_count

    async def delete_cells(self, ref: NotebookRef, ids: list[str]) -> list[CellView]:
        cells = self._nb(ref)["cells"]
        doomed: dict[int, CellView] = {}
        for cell_id in ids:
            index = self._find(cells, cell_id)
            if index is not None and index not in doomed:
                doomed[index] = self._view(cells[index], index, "full")
        if doomed:
            for index in sorted(doomed, reverse=True):
                del cells[index]
            self._commit("delete", ref)
        return [doomed[index] for index in sorted(doomed)]

    def _live_run(self) -> FakeExecution | None:
        return next((run for run in self._runs.values() if not run.future.done()), None)

    def live_run(self, ref: NotebookRef) -> str | None:
        """The cell nh is running in this notebook's kernel (the fake has one kernel for all)."""
        execution = self._live_run()
        return execution.cell_id if execution is not None else None

    async def kernel_status(self, ref: NotebookRef, *, create: bool = True) -> KernelStatus:
        self._nb(ref)
        state = "busy" if self.kernel_busy or self._live_run() is not None else "idle"
        return KernelStatus(
            kernel_id=self.kernel_id,
            name="python3",
            execution_state=state,
            language="python",
            python_version=self.python_version,
            prefix=sys.prefix,
            executable=sys.executable,
            incarnation=self.incarnation,
        )

    async def start_execution(
        self,
        ref: NotebookRef,
        cell_id: str,
        *,
        hard_timeout: float,
        expected_source: str | None = None,
    ) -> FakeExecution:
        cells = self._nb(ref)["cells"]
        if self._live_run() is not None:
            raise NhError("E133", detail=" (nh is still running another cell in this notebook)")
        index = self._find(cells, cell_id)
        if index is None or cells[index]["cell_type"] != "code":
            raise NhError("E140", cell_id=cell_id)
        cell = cells[index]
        current = str(cell.get("source") or "")
        if expected_source is not None and normalize_source(current) != normalize_source(
            expected_source
        ):
            raise CellConflict(str(cell["id"]), current)
        cell.update(outputs=[], execution_count=None, execution_state="running")
        execution = FakeExecution(str(cell["id"]), asyncio.get_running_loop().create_future())
        self._runs[self._rel(ref)] = execution
        execution.task = asyncio.create_task(self._run(ref, cell, execution, float(hard_timeout)))
        return execution

    async def _run(
        self, ref: NotebookRef, cell: dict[str, Any], execution: FakeExecution, hard: float
    ) -> None:
        began = time.monotonic()
        status, note, count = "ok", "", None
        try:
            while self.kernel_busy:
                if time.monotonic() - began > hard:
                    status, note = (
                        "timeout",
                        "it never started: the kernel was busy with another cell",
                    )
                    return
                await asyncio.sleep(0.02)
            execution._started = True
            began = time.monotonic()
            if self.exec_delay_s > 0:
                try:
                    await asyncio.wait_for(
                        execution.interrupt.wait(), timeout=min(self.exec_delay_s, hard)
                    )
                    stopped = "interrupted"
                except TimeoutError:
                    stopped = "timeout" if self.exec_delay_s > hard else ""
                if stopped:
                    self.execution_count += 1
                    count = self.execution_count
                    execution.outputs.append(
                        {
                            "output_type": "error",
                            "ename": "KeyboardInterrupt",
                            "evalue": "",
                            "traceback": [_RULE, "KeyboardInterrupt", f"Cell In[{count}], line 1"],
                        }
                    )
                    status = stopped
                    if stopped == "timeout":
                        note = "interrupted at the time limit"
                    elif execution.by_user:
                        note = USER_INTERRUPT_NOTE
                    return
            outputs, count, reply = self._execute(str(cell.get("source") or ""))
            execution.outputs = outputs
            status = "error" if reply == "error" else "ok"
            error = error_info(outputs)
            if error is not None and stopped_by_signal(error):
                status, note = "interrupted", USER_INTERRUPT_NOTE  # like the real runner
        except asyncio.CancelledError:
            status, note = "lost", "nh stopped while this cell was running"
            raise
        finally:
            cell["outputs"] = sanitize_for_doc(copy.deepcopy(execution.outputs))
            cell["execution_count"] = count
            cell["execution_state"] = "idle"
            result = ExecResult(
                status=status,
                execution_count=count,
                outputs=copy.deepcopy(execution.outputs),
                seconds=time.monotonic() - began,
                error=error_info(execution.outputs),
                note=note,
            )
            if not execution.future.done():
                execution.future.set_result(result)

    async def interrupt(self, ref: NotebookRef) -> None:
        execution = self._runs.get(self._rel(ref))
        if execution is None or execution.future.done():
            return
        if not execution.started():
            raise NhError(
                "E133",
                detail=" with another cell; nh's cell is still queued and was not interrupted",
            )
        execution.interrupt.set()

    async def probe(
        self, ref: NotebookRef, name: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self._nb(ref)
        if name not in probes.PROBES:
            return {"error": f"ValueError: unknown probe {name!r}"}
        if self.kernel_busy or self._live_run() is not None:
            return {"error": "KernelBusy: the kernel is running a cell"}
        captured: list[dict[str, Any]] = []
        saved, self._current = self._current, captured
        try:
            scope: dict[str, Any] = {"__builtins__": self._builtins}
            for library in probes.LIBRARIES.get(name, ()):
                scope["_A"] = {**args, "_as_library": True}
                exec(probes.probe_source(library), scope)
            scope["_A"] = copy.deepcopy(args)
            exec(probes.probe_source(name), scope)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._current = saved
        payload = probes.extract_payload(captured)
        if name == "attach" and "python" in payload:
            payload["python"] = list(self.python_version)
            payload["pid"], payload["started"] = 0, self.incarnation  # the fake kernel "process"
        return payload

    async def fresh_run(self, ref: NotebookRef) -> dict[str, Any]:
        raise NotImplementedError("fresh runs are `nhctl fresh-run` in v0.1")

    async def describe(self, ref: NotebookRef | None) -> dict[str, str]:
        return {
            "backend": "fake (in memory, NH_BACKEND=fake)",
            "kernel": f"{self.kernel_id[:8]}, python {'.'.join(map(str, self.python_version))}",
        }
