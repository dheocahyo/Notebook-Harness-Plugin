"""The notebook backend protocol. RtcBackend talks to JupyterLab; FakeBackend runs in memory for tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

OutputsMode = Literal["none", "summary", "full"]
ExecStatus = Literal["ok", "error", "aborted", "running", "timeout", "interrupted", "lost"]


@dataclass(frozen=True)
class ServerInfo:
    url: str
    token: str | None
    root_dir: Path
    pid: int | None = None
    version: str | None = None


@dataclass(frozen=True)
class NotebookRef:
    abs_path: Path
    api_path: str  # POSIX path relative to the server root_dir
    rel_path: str  # POSIX path relative to the project (what the user and agent see)


@dataclass
class OutputSummary:
    count: int = 0
    types: list[str] = field(default_factory=list)
    error: str | None = None  # "KeyError: 'prce'"
    text_head: str = ""
    images: int = 0


@dataclass
class CellView:
    id: str
    index: int
    cell_type: str
    source: str
    metadata: dict[str, Any]
    execution_count: int | None = None
    running: bool = False
    outputs: list[dict[str, Any]] | None = None  # only with outputs="full"
    summary: OutputSummary | None = None  # with outputs="summary" or "full"


@dataclass
class NewCell:
    id: str
    cell_type: Literal["code", "markdown"]
    source: str
    metadata: dict[str, Any]


@dataclass
class CellPatch:
    id: str
    source: str | None = None
    base_source: str | None = None  # expected current source; mismatch => CellConflict
    metadata: dict[str, Any] | None = None  # full replacement of metadata["nh"]; None = unchanged
    drop_nh_metadata: bool = False
    tags_add: tuple[str, ...] = ()
    tags_remove: tuple[str, ...] = ()
    clear_outputs: bool = False
    execution_count: int | None | Literal["keep"] = "keep"


@dataclass
class ErrorInfo:
    ename: str
    evalue: str
    traceback: str  # ANSI-stripped, newline-joined
    line: int | None = None


@dataclass
class ExecResult:
    status: ExecStatus
    execution_count: int | None
    outputs: list[dict[str, Any]]
    seconds: float
    error: ErrorInfo | None = None
    note: str = ""


@dataclass
class KernelStatus:
    kernel_id: str
    name: str
    execution_state: str
    language: str | None = None
    python_version: tuple[int, int] | None = None
    prefix: str | None = None
    executable: str | None = None
    # Changes whenever the kernel process restarts (Jupyter keeps kernel_id across restarts).
    incarnation: str | None = None


class Execution(Protocol):
    cell_id: str
    started_at: float

    @property
    def future(self) -> asyncio.Future[ExecResult]: ...

    def started(self) -> bool: ...

    def partial_outputs(self) -> list[dict[str, Any]]: ...


class CellConflict(Exception):
    """The cell's current source differs from the expected base (user edited it)."""

    def __init__(self, cell_id: str, current: str) -> None:
        super().__init__(cell_id)
        self.cell_id = cell_id
        self.current = current


class CellMissing(Exception):
    def __init__(self, cell_id: str) -> None:
        super().__init__(cell_id)
        self.cell_id = cell_id


class NotebookBackend(Protocol):
    async def resolve_notebook(self, rel: str | None) -> NotebookRef:
        """Map a project-relative notebook path to a reference (E130/E132 on failure)."""
        ...

    async def describe(self, ref: NotebookRef | None) -> dict[str, str]:
        """Connection facts for nh_inspect(view="status"). Never includes tokens."""
        ...

    def take_notices(self, ref: NotebookRef) -> list[str]:
        """One-time notes for the user (new kernel session, nbformat upgrade). Clears them."""
        ...

    async def open(self, ref: NotebookRef) -> None: ...

    async def close(self, ref: NotebookRef) -> None: ...

    async def aclose(self) -> None: ...

    async def snapshot(
        self, ref: NotebookRef, *, outputs: OutputsMode = "summary"
    ) -> list[CellView]: ...

    async def notebook_meta(self, ref: NotebookRef) -> dict[str, Any]: ...

    async def set_notebook_meta(self, ref: NotebookRef, key: str, value: Any) -> None: ...

    async def insert_cells(self, ref: NotebookRef, index: int, cells: list[NewCell]) -> None:
        """Insert all cells at ``index`` in ONE transaction (index -1 = append)."""
        ...

    async def update_cells(self, ref: NotebookRef, patches: list[CellPatch]) -> None:
        """Apply all patches in ONE transaction; raise CellConflict/CellMissing without writing."""
        ...

    async def delete_cells(self, ref: NotebookRef, ids: list[str]) -> list[CellView]:
        """Delete in ONE transaction; returns the deleted cells (with metadata)."""
        ...

    async def kernel_status(self, ref: NotebookRef, *, create: bool = True) -> KernelStatus:
        """The notebook's kernel. ``create=False`` (a read-only look) never starts one."""
        ...

    async def start_execution(
        self,
        ref: NotebookRef,
        cell_id: str,
        *,
        hard_timeout: float,
        expected_source: str | None = None,
    ) -> Execution:
        """Run a code cell. With ``expected_source``, raise CellConflict (without running) when the
        cell's current source differs after ``meta.normalize_source`` (the user typed into it)."""
        ...

    def live_run(self, ref: NotebookRef) -> str | None:
        """Id of the cell nh is currently running in this notebook's kernel, or None."""
        ...

    async def interrupt(self, ref: NotebookRef) -> None: ...

    async def probe(
        self, ref: NotebookRef, name: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Run a read-only kernel probe (see nh_gateway/probes). Returns its JSON payload."""
        ...

    async def fresh_run(self, ref: NotebookRef) -> dict[str, Any]:
        """Optional (FR-11 seam). v0.1 backends may raise NotImplementedError."""
        ...
