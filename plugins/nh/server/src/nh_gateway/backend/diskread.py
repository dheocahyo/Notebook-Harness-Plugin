"""Read-only view of a notebook file for when no JupyterLab is running: outline and metadata only.

It never writes the file (a live room would overwrite it anyway); every write, run or probe
refuses with E130 so the agent asks the user to start the project's JupyterLab.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import nbformat

from ..config import ConfigCache
from ..exec.docsafe import output_summary
from ..policy.errors import NhError
from . import discovery
from .base import CellPatch, CellView, KernelStatus, NewCell, NotebookRef, OutputsMode

_NO_SERVER = (
    "Reading the notebook file from disk: nh can only show its outline until JupyterLab runs."
)


def _read(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return nbformat.read(handle, as_version=4)
    except FileNotFoundError:
        raise NhError("E132", notebook=path.name) from None
    except (OSError, ValueError) as exc:  # nbformat raises NotJSONError (a ValueError) on bad files
        raise NhError("E136", detail=f" (the file can't be read: {exc})"[:200]) from None


def cell_views(notebook: dict[str, Any], mode: OutputsMode) -> list[CellView]:
    views = []
    for index, cell in enumerate(notebook.get("cells") or []):
        source = cell.get("source") or ""
        view = CellView(
            id=str(cell.get("id") or f"cell-{index}"),
            index=index,
            cell_type=cell.get("cell_type", "code"),
            source="".join(source) if isinstance(source, list) else str(source),
            metadata=dict(cell.get("metadata") or {}),
        )
        if view.cell_type == "code":
            view.execution_count = cell.get("execution_count")
            outputs = [dict(output) for output in cell.get("outputs") or []]
            if mode != "none":
                view.summary = output_summary(outputs)
            if mode == "full":
                view.outputs = outputs
        views.append(view)
    return views


class DiskReadBackend:
    """``NotebookBackend`` over the ``.ipynb`` file: reads only."""

    def __init__(self, project: Path, cfg_cache: ConfigCache | None = None) -> None:
        self.project = project
        self.cfg_cache = cfg_cache or ConfigCache(project)

    def _refuse(self) -> NhError:
        return NhError("E130", detail=_NO_SERVER)

    async def resolve_notebook(self, rel: str | None) -> NotebookRef:
        return discovery.resolve_notebook(self.project, None, rel, self.cfg_cache.current())

    async def open(self, ref: NotebookRef) -> None:
        await asyncio.to_thread(_read, ref.abs_path)

    async def close(self, ref: NotebookRef) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def snapshot(
        self, ref: NotebookRef, *, outputs: OutputsMode = "summary"
    ) -> list[CellView]:
        return cell_views(await asyncio.to_thread(_read, ref.abs_path), outputs)

    async def notebook_meta(self, ref: NotebookRef) -> dict[str, Any]:
        return dict((await asyncio.to_thread(_read, ref.abs_path)).get("metadata") or {})

    async def set_notebook_meta(self, ref: NotebookRef, key: str, value: Any) -> None:
        raise self._refuse()

    async def insert_cells(self, ref: NotebookRef, index: int, cells: list[NewCell]) -> None:
        raise self._refuse()

    async def update_cells(self, ref: NotebookRef, patches: list[CellPatch]) -> None:
        raise self._refuse()

    async def delete_cells(self, ref: NotebookRef, ids: list[str]) -> list[CellView]:
        raise self._refuse()

    async def kernel_status(self, ref: NotebookRef, *, create: bool = True) -> KernelStatus:
        raise self._refuse()

    async def start_execution(
        self,
        ref: NotebookRef,
        cell_id: str,
        *,
        hard_timeout: float,
        expected_source: str | None = None,
    ) -> Any:
        raise self._refuse()

    def live_run(self, ref: NotebookRef) -> str | None:
        return None

    async def interrupt(self, ref: NotebookRef) -> None:
        raise self._refuse()

    async def probe(
        self, ref: NotebookRef, name: str, args: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        raise self._refuse()

    async def fresh_run(self, ref: NotebookRef) -> dict[str, Any]:
        raise NotImplementedError("fresh runs are `nhctl fresh-run` in v0.1")

    async def describe(self, ref: NotebookRef | None) -> dict[str, str]:
        return {
            "backend": "disk (read-only)",
            "server": "no JupyterLab found; run `nhctl lab start`",
        }
