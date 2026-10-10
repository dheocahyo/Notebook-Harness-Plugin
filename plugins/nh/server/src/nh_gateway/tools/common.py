"""Services shared by the nh tools, plus the helpers every write path uses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent

from .. import dataflow, meta, render
from .._shared import hosts, secrets
from .._shared.paths import Layout, atomic_write_json, read_json
from .._shared.text import normalize_title
from ..backend.base import (
    CellView,
    ExecResult,
    Execution,
    KernelStatus,
    NotebookBackend,
    NotebookRef,
)
from ..config import Config, ConfigCache
from ..exec import shaping
from ..history import HistoryStore
from ..log import EventLog
from ..policy.errors import NhError
from ..policy.turn import CURRENT_TURN, NotebookLocks, TurnContext, TurnLedger, TurnState
from ..state import DriftStore, StaleStore, same_kernel, write_last_cell

log = logging.getLogger("nh_gateway")

FINAL_STATUSES = {"ok", "error", "aborted", "timeout", "interrupted", "lost"}
# An interrupt is the user's call (they pressed stop), so it is not a retry nh may take on its own.
RETRYABLE = {"error", "timeout", "lost"}
REBUILD = "To rebuild: select the last cell that should count, then Kernel → Restart Kernel and Run Up to Selected Cell."
NEW_KERNEL = (
    "NEW kernel: earlier variables are gone; cells above may need re-running "
    "(Kernel → Restart Kernel and Run Up to Selected Cell)."
)
DRIFT_SHOWN = 6  # names a 'Kernel ≠ notebook' lead lists before "+N more"
# Lead lines of the running tool call; app._guarded puts them above a refusal too, so one-time
# news ("NEW kernel") reaches the agent even when the call that found it is then refused.
CALL_LEAD: ContextVar[list[str] | None] = ContextVar("nh_call_lead", default=None)


@dataclass
class RunRecord:
    execution: Execution
    ref: NotebookRef
    uid: str  # history/ledger key
    cell_id: str  # the cell that actually runs
    title: str | None
    session_id: str
    prompt_id: str
    code: str
    before: dict[str, Any] | None
    result: ExecResult | None = None
    reported: bool = False  # a tool call already showed the finished result
    probe_names: set[str] | None = None  # the names the self-check probes (the cell's uses | defs)


@dataclass
class Services:
    project: Path
    layout: Layout
    config_cache: ConfigCache
    backend: NotebookBackend
    ledger: TurnLedger
    locks: NotebookLocks
    history: HistoryStore
    stale: StaleStore
    drift: DriftStore
    events: EventLog
    active_notebook: str | None = None
    runs: dict[str, RunRecord] = field(default_factory=dict)  # uid -> live run
    finished: dict[str, RunRecord] = field(
        default_factory=dict
    )  # uid -> finished, not yet reported

    def config(self) -> Config:
        return self.config_cache.current()

    async def resolve(self, notebook: str | None, *, activate: bool = True) -> NotebookRef:
        """Map the tool's ``notebook`` to a reference. Only write tools make it the active one."""
        rel = notebook or self.active_notebook or self.config()["project"]["notebook"]
        if not rel:
            raise NhError("E132", notebook="(none)", candidates=_candidates(self.project))
        rel = rel.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        resolver = getattr(self.backend, "resolve_notebook", None)
        if resolver is not None:
            ref = await resolver(rel)
        else:
            path = (self.project / rel).resolve()
            if not path.is_relative_to(self.project.resolve()):
                raise NhError("E132", notebook=rel, candidates=_candidates(self.project))
            ref = NotebookRef(abs_path=path, api_path=rel, rel_path=rel)
        if activate:
            self.active_notebook = ref.rel_path
        return ref

    def live_run(self, ref: NotebookRef) -> RunRecord | None:
        for record in self.runs.values():
            if record.ref.abs_path == ref.abs_path:
                return record
        return None


def _candidates(project: Path) -> str:
    found = []
    for path in sorted(project.rglob("*.ipynb")):
        if ".ipynb_checkpoints" in path.parts or ".nh" in path.parts:
            continue
        found.append(path.relative_to(project).as_posix())
        if len(found) >= 8:
            break
    return ", ".join(found) or "none"


def current_turn() -> TurnContext:
    turn = CURRENT_TURN.get()
    if turn is None:  # the middleware sets it for every write tool
        raise NhError("E101")
    return turn


def time_left(turn: TurnContext | None, cfg: Config) -> float:
    soft = float(cfg["exec"]["soft_timeout_s"])
    soft = max(5.0, min(soft, 110.0))
    if turn is None or not turn.deadline:
        return soft
    return max(1.0, min(soft, turn.deadline - time.monotonic() - 3.0))


# ---------------------------------------------------------------------------- cells


def find_cell(cells: list[CellView], cell_id: str) -> CellView | None:
    for cell in cells:
        if cell.id == cell_id:
            return cell
    for cell in cells:  # ids renumbered (nbstripout): fall back to the stable uid
        if meta.nh_meta(cell.metadata).get("uid") == cell_id:
            return cell
    return None


def code_cell_for(cells: list[CellView], cell: CellView) -> CellView:
    """A note cell stands for the code cell it describes."""
    if meta.is_note(cell.metadata):
        pair = meta.nh_meta(cell.metadata).get("pair_uid")
        target = find_cell(cells, pair) if pair else None
        if target is not None:
            return target
        if cell.index + 1 < len(cells) and cells[cell.index + 1].cell_type == "code":
            return cells[cell.index + 1]
    return cell


def note_for(cells: list[CellView], code: CellView) -> CellView | None:
    uid = meta.nh_meta(code.metadata).get("uid") or code.id
    pair = meta.nh_meta(code.metadata).get("pair_uid")
    if pair:
        found = find_cell(cells, pair)
        if found is not None:
            return found
    if code.index > 0:
        prev = cells[code.index - 1]
        if meta.is_note(prev.metadata) and meta.nh_meta(prev.metadata).get("pair_uid") in (
            uid,
            code.id,
        ):
            return prev
    return None


def uid_of(cell: CellView) -> str:
    return meta.nh_meta(cell.metadata).get("uid") or cell.id


def title_of(cells: list[CellView], cell: CellView) -> str | None:
    note = note_for(cells, cell)
    if note is not None:
        first = note.source.strip().splitlines()[0] if note.source.strip() else ""
        return normalize_title(first) or None
    return None


def label(cells: list[CellView], cell: CellView) -> str:
    return render.cell_label(title_of(cells, cell), cell.execution_count, cell.source)


def code_sources_before(cells: list[CellView], index: int) -> list[str]:
    return [c.source for c in cells[:index] if c.cell_type == "code"]


# ---------------------------------------------------------------------------- execution


async def wait_for(execution: Execution, timeout: float, ctx: Context | None) -> ExecResult | None:
    """Wait for an execution, reporting progress every 5 s. None means still running."""
    fut = execution.future
    started = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return None
        done, _ = await asyncio.wait({fut}, timeout=min(5.0, remaining))
        if done:
            return fut.result()
        if ctx is not None:
            # Best effort: without a progressToken from the client this is a no-op.
            with contextlib.suppress(Exception):
                await ctx.report_progress(progress=time.monotonic() - started, total=timeout)


async def safe_probe(
    svc: Services, ref: NotebookRef, name: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    cfg = svc.config()
    try:
        payload = await svc.backend.probe(
            ref, name, args, timeout=float(cfg["exec"]["probe_budget_s"]) + 1.0
        )
    except NhError:
        return None
    except Exception as exc:  # a failed probe never fails the turn
        log.debug("probe %s failed: %s", name, exc)
        return None
    if not isinstance(payload, dict) or "error" in payload:
        return None
    return payload


def vars_args(cfg: Config, names: set[str] | None) -> dict[str, Any]:
    return {
        "names": sorted(names)[: int(cfg["inspect"]["max_vars"])] if names is not None else None,
        "max_vars": int(cfg["inspect"]["max_vars"]),
        "budget_s": float(cfg["exec"]["probe_budget_s"]),
        "max_cells": 20_000_000,
    }


def effective_status(result: ExecResult | None, execution: Execution) -> str:
    """The status tools report: 'queued' (not started yet), 'deleted' (the user removed the cell
    while it ran) and the runner's own statuses."""
    if result is None:
        return "running" if execution.started() else "queued"
    if "deleted" in (result.note or ""):
        return "deleted"
    return result.status


def note_last_cell(
    svc: Services, record: RunRecord, status: str, exec_count: int | None, finished: bool
) -> None:
    state = svc.ledger.get(record.session_id, record.prompt_id)
    retries_left = max(
        0, int(svc.config()["turn"]["max_retries"]) - state.retries.get(record.uid, 0)
    )
    write_last_cell(
        svc.layout,
        session_id=record.session_id,
        notebook=record.ref.rel_path,
        cell_id=record.uid,
        title=record.title,
        exec=exec_count,
        status=status,
        turn_id=record.prompt_id,
        retries_left=retries_left,
        finished_at=time.time() if finished else None,
    )


def record_finish(svc: Services, record: RunRecord, result: ExecResult) -> None:
    """Bookkeeping when a run ends, whether or not a tool call is still waiting for it.

    A going approved batch's step keeps reading ``running`` here: its report sets the status
    together with the stop once its "check this" is known (``batch.reported``, design §6.3),
    so a call sent in parallel can't see the step OK before its check."""
    record.result = result
    status = effective_status(result, record.execution)
    state = svc.ledger.get(record.session_id, record.prompt_id)
    batch_going = bool(state.batch_total) and not state.batch_stop
    if not (batch_going and record.uid in state.claims):
        state.status[record.uid] = status
        svc.ledger.save(state)
    note_last_cell(svc, record, status, result.execution_count, finished=True)
    svc.runs.pop(record.uid, None)
    if not record.reported:
        svc.finished[record.uid] = record  # nh_run(mode="wait") may still report it


def track(svc: Services, record: RunRecord) -> None:
    svc.finished.pop(record.uid, None)
    svc.runs[record.uid] = record

    def _done(fut: asyncio.Future[ExecResult]) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            log.error("execution of %s failed: %s", record.uid, exc)
            return
        record_finish(svc, record, fut.result())

    record.execution.future.add_done_callback(_done)


def image_contents(shaped: shaping.Shaped) -> list[ImageContent]:
    import base64

    return [
        ImageContent(
            type="image", data=base64.b64encode(img.data).decode("ascii"), mime_type=img.mime
        )
        for img in shaped.images
    ]


def text_result(
    text: str,
    images: list[ImageContent] | None = None,
    *,
    status: str | None = None,
    cell_id: str | None = None,
) -> ToolResult:
    """Every tool result's text passes the installed redactor here, the last safety net
    (design §6.8): each site has already redacted before cutting; this catches the rest."""
    content: list[Any] = [TextContent(type="text", text=secrets.current().redact(text))]
    content.extend(images or [])
    meta: dict[str, Any] = {"nh/v": 1}
    if status is not None:
        meta["nh/status"] = status
    if cell_id is not None:
        meta["nh/cell_id"] = cell_id
    return ToolResult(content=content, meta=meta)


def status_word(result: ExecResult | None) -> str:
    if result is None:
        return "running"
    return result.status


def machine_line(
    uid: str, exec_count: int | None, state: TurnState, cfg: Config, *, writer: bool = False
) -> str:
    """``writer``: nh:cell-writer's results also count its revisions (the workflow reads them).
    An approved batch (design §6.3) counts against its own size: ``turn=k/N batch``."""
    turn = cfg["turn"]
    limit = f"{state.batch_total} batch" if state.batch_total else str(turn["max_code_cells"])
    line = (
        f"nh: cell={uid} exec={exec_count if exec_count is not None else '-'} "
        f"turn={len(state.claims)}/{limit} "
        f"retries={state.retries.get(uid, 0)}/{turn['max_retries']} "
        f"waits={state.waits}/{turn['max_waits']} undos={state.undos}/{turn['max_undos']}"
    )
    if writer:
        line += f" revisions={state.revisions.get(uid, 0)}/{turn['max_revisions']}"
    return line


def config_lines(cfg: Config, layout: Layout | None = None) -> list[str]:
    """``harness.toml``'s problems, then the approved hosts list's (design §6.4)."""
    found = [f"harness.toml: {problem}" for problem in cfg.problems]
    return found + (hosts.problems(layout.approved_hosts) if layout is not None else [])


def windows_guard() -> None:
    if sys.platform == "win32":
        raise NhError("E137")


def review_earlier_cells(
    svc: Services, ref: NotebookRef, cells: list[CellView], state: TurnState
) -> None:
    """Acceptance metric (PRD): at a new message, did the user keep nh's earlier cells as written?

    Each nh cell is reviewed once: ``unedited`` when its source still matches what nh wrote,
    ``deleted`` when it is gone without an nh undo.
    """
    if state.reviewed:
        return
    state.reviewed = True
    path = svc.layout.state / "reviewed.json"
    seen_all = read_json(path, default={}) or {}
    seen = set(seen_all.get(ref.rel_path, []))
    present: set[str] = set()
    for cell in cells:
        nh = meta.nh_meta(cell.metadata)
        if nh.get("role") != "code" or nh.get("created_by") == "human":
            continue
        uid = nh.get("uid") or cell.id
        present.add(uid)
        if uid in seen or state.prompt_id in (nh.get("turn_id"), nh.get("last_turn_id")):
            continue
        seen.add(uid)
        svc.events.emit(
            "cell_review",
            session_id=state.session_id,
            turn_id=state.prompt_id,
            notebook=ref.rel_path,
            cell_uid=uid,
            deleted=False,
            unedited=nh.get("source_sha") == meta.source_sha(cell.source),
        )
    earlier = [
        t for t in svc.ledger.recent_turn_ids(state.session_id, limit=5) if t != state.prompt_id
    ]
    for op in svc.history.last_ops(ref.rel_path, state.session_id, earlier) if earlier else []:
        if op.op == "insert" and op.uid not in present and op.uid not in seen:
            seen.add(op.uid)
            svc.events.emit(
                "cell_review",
                session_id=state.session_id,
                turn_id=state.prompt_id,
                notebook=ref.rel_path,
                cell_uid=op.uid,
                deleted=True,
                unedited=False,
            )
    seen_all[ref.rel_path] = sorted(seen)
    try:
        atomic_write_json(path, seen_all)
    except OSError:
        log.warning("could not save review state")


# ---------------------------------------------------------------------------- copies, kernel, drift


def normalize_uids(cells: list[CellView]) -> None:
    """A JupyterLab copy/paste keeps metadata.nh (and its uid) but gets a new id.

    In this snapshot the copy becomes its own cell: its uid is its id, it points at no note and
    counts as the user's (they made it). The original, whose id is the uid, keeps everything.
    """
    ids = {cell.id for cell in cells}
    seen: set[str] = set()
    for cell in cells:
        nh = meta.nh_meta(cell.metadata)
        uid = nh.get("uid")
        if not uid or uid == cell.id:
            if uid:
                seen.add(uid)
            continue
        if uid in ids or uid in seen:  # the original is present (or an earlier copy claimed it)
            copy = dict(nh)
            copy["copied_from"] = uid
            copy["uid"] = cell.id
            copy.pop("pair_uid", None)
            if copy.get("role") == "note":
                copy = {}
            else:
                copy["created_by"] = "human"
            cell.metadata = {**cell.metadata, "nh": copy}
        else:
            seen.add(uid)


async def look_at_kernel(svc: Services, ref: NotebookRef) -> KernelStatus | None:
    """The notebook's kernel for a read-only view: never starts one, never fails the view."""
    try:
        return await svc.backend.kernel_status(ref, create=False)
    except NhError:
        return None
    except Exception as exc:  # a look at the kernel never fails the view
        log.debug("kernel look failed: %s", exc)
        return None


def kernel_notes(svc: Services, ref: NotebookRef, kernel: Any) -> tuple[list[str], list[str]]:
    """(lead lines, kernel-section lines) after comparing this kernel with the last one nh saw.

    A new kernel process (restart, change, shutdown) empties the namespace, so any
    'Kernel ≠ notebook' warning for the notebook no longer applies. An unknown incarnation (nh
    couldn't look inside a busy kernel) is no news: only a new kernel id counts then, and the
    identity nh saw before is kept. The lead also goes to ``CALL_LEAD``, so a refusal carries it.
    """
    lead: list[str] = []
    section: list[str] = []
    path = svc.layout.state / "kernels.json"
    seen = read_json(path, default={})
    seen = seen if isinstance(seen, dict) else {}
    identity = f"{kernel.kernel_id}:{kernel.incarnation or ''}"
    previous = seen.get(ref.rel_path)
    new = bool(previous) and not same_kernel(previous, identity)
    if new:
        svc.drift.clear(ref.rel_path, None)
        lead.append(NEW_KERNEL)
        pending = CALL_LEAD.get()
        if pending is not None:
            pending.extend(lead)
    if previous != identity and (not previous or new or kernel.incarnation):
        seen[ref.rel_path] = identity
        try:
            atomic_write_json(path, seen)
        except OSError:
            log.warning("could not save kernel identity")
    env = read_json(svc.layout.env_json)
    env_prefix = env.get("prefix") if isinstance(env, dict) else None
    if env_prefix and kernel.prefix:
        try:
            inside = Path(kernel.prefix).resolve().is_relative_to(Path(env_prefix).resolve())
        except OSError:
            inside = True
        if not inside:
            section.append(
                f"The kernel runs from {kernel.prefix}, not the project env ({env_prefix}); "
                "packages may differ from what the project installs."
            )
    return lead, section


def drift_names(svc: Services, ref: NotebookRef) -> set[str]:
    drift = svc.drift.get(ref.rel_path) or {}
    return set((drift.get("names") or {}).keys())


def last_count(cells: list[CellView]) -> int:
    """The notebook's highest execution count: a cell run from now on shows a higher one."""
    counts = [
        c.execution_count
        for c in cells
        if c.cell_type == "code" and isinstance(c.execution_count, int)
    ]
    return max(counts, default=0)


def drift_args(cfg: Config, names: set[str]) -> dict[str, Any]:
    """``vars_args`` for the drift names alone: none of them is cut by ``max_vars``."""
    return {**vars_args(cfg, None), "names": sorted(names), "max_vars": max(1, len(names))}


def prune_drift(
    svc: Services, ref: NotebookRef, payload: dict[str, Any] | None, asked: set[str]
) -> None:
    """Drop drift names a vars probe asked about and didn't find (restart, del, never bound).

    ``asked`` must be the names the probe was sent. A truncated payload (time budget) says
    nothing about the names it didn't reach, so it prunes nothing.
    """
    if payload is None or not asked or payload.get("truncated"):
        return
    present = set((payload.get("vars") or {}).keys())
    gone = asked - present
    if gone:
        svc.drift.clear(ref.rel_path, gone)


def clear_rerun_drift(svc: Services, ref: NotebookRef, cells: list[CellView]) -> None:
    """Drop drift names a cell run since the undo binds afresh (the user re-ran it in JupyterLab):
    the kernel's value comes from the notebook again.

    Only a fresh binding counts: ``df = df.reset_index()`` or ``count += 1`` builds on the undone
    value, and a run that failed may not have reached the binding. ``cells`` needs summaries.
    """
    drift = svc.drift.get(ref.rel_path) or {}
    names = drift.get("names") or {}
    since = {
        name: count
        for name, count in (drift.get("exec_counts") or {}).items()
        if name in names and isinstance(count, int)
    }
    if not since:
        return
    rerun: set[str] = set()
    for cell in cells:
        count = cell.execution_count
        if cell.cell_type != "code" or not isinstance(count, int):
            continue
        failed = cell.summary is None or bool(cell.summary.error)
        waiting = {name for name, before in since.items() if count > before} - rerun
        if waiting and not failed:
            flow = dataflow.analyze(cell.source)
            rerun |= waiting & (set(flow.fresh) - set(flow.uses))
    if rerun:
        svc.drift.clear(ref.rel_path, rerun)


async def check_drift(
    svc: Services, ref: NotebookRef, cells: list[CellView] | None = None, *, probe: bool = True
) -> None:
    """Forget 'Kernel ≠ notebook' names that no longer hold undone results: a cell run since
    binds them again, or (``probe``) the kernel no longer has them. Costs nothing without drift."""
    if not drift_names(svc, ref):
        return
    if cells is None:
        try:
            cells = await svc.backend.snapshot(ref, outputs="summary")
        except NhError:
            cells = []
    clear_rerun_drift(svc, ref, cells)
    names = drift_names(svc, ref)
    if probe and names:
        payload = await safe_probe(svc, ref, "vars", drift_args(svc.config(), names))
        prune_drift(svc, ref, payload, names)


def drift_lead(svc: Services, ref: NotebookRef) -> list[str]:
    """'Kernel ≠ notebook: `a`, `b` still hold results from the undone "X" [2]; …': names grouped
    by why, the newest first, at most ``DRIFT_SHOWN`` of them."""
    drift = svc.drift.get(ref.rel_path) or {}
    names = drift.get("names") or {}
    if not names:
        return []
    groups: dict[str, list[str]] = {}
    for name, why in reversed(list(names.items())):  # newest first; a group keeps its order
        groups.setdefault(str(why), []).insert(0, str(name))
    parts: list[str] = []
    shown = 0
    for why, group in groups.items():
        group = group[: DRIFT_SHOWN - shown]
        if not group:
            break
        shown += len(group)
        if len(group) > 1 and why.startswith("still holds "):
            why = "still hold " + why.removeprefix("still holds ")
        parts.append(f"{', '.join(f'`{name}`' for name in group)} {why}")
    if len(names) > shown:
        parts.append(f"+{len(names) - shown} more")
    return [f"Kernel ≠ notebook: {'; '.join(parts)}. {REBUILD}"]
