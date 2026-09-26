"""nh_run: re-run one existing cell (uses the turn's action), wait for a running one, or interrupt it."""

from __future__ import annotations

import asyncio
import contextlib
import time

from fastmcp import Context
from fastmcp.tools import ToolResult

from .. import dataflow, render
from ..policy.errors import NhError
from .common import (
    RETRYABLE,
    RunRecord,
    Services,
    code_cell_for,
    current_turn,
    find_cell,
    kernel_notes,
    label,
    review_earlier_cells,
    text_result,
    title_of,
    uid_of,
    windows_guard,
)
from .write import (
    _rebuild_claims,
    is_writer,
    probe_before,
    refuse_if_running,
    report_run,
    snapshot,
    start_run,
)


async def run_cell(
    svc: Services,
    ctx: Context | None,
    *,
    cell_id: str,
    mode: str = "run",
    notebook: str | None = None,
) -> ToolResult:
    windows_guard()
    if mode == "wait":
        return await _wait(svc, ctx, cell_id=cell_id, notebook=notebook)
    if mode == "interrupt":
        return await _interrupt(svc, ctx, cell_id=cell_id, notebook=notebook)
    if mode != "run":
        raise NhError("E120", detail=f"- mode must be run, wait or interrupt (got {mode!r}).")

    started = time.monotonic()
    turn = current_turn()
    cfg = svc.config()
    ref = await svc.resolve(notebook)
    kernel = await svc.backend.kernel_status(ref)
    lead, kernel_lines = kernel_notes(svc, ref, kernel)
    async with svc.locks.hold(ref.rel_path, turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        cells = await snapshot(svc, ref)
        _rebuild_claims(state, cells, ref.rel_path)
        review_earlier_cells(svc, ref, cells, state)
        found = find_cell(cells, cell_id)
        if found is None:
            raise NhError("E140", cell_id=cell_id)
        target = code_cell_for(cells, found)
        name = label(cells, target)
        if target.cell_type != "code":
            raise NhError("E145", cell=name)
        refuse_if_running(svc, ref, cells)
        if target.running:
            raise NhError("E133", detail=f" running {name}")
        uid = uid_of(target)
        retry = False
        if state.claims:
            if uid not in state.claims:
                raise NhError("E114")
            status = state.status.get(uid)
            if status == "ok":
                raise NhError("E112", cell=name)
            if status == "interrupted":
                raise NhError("E117", verb="run", cell=name)
            if status == "conflict":
                raise NhError("E118", verb="run", cell=name)
            if status in RETRYABLE:
                if state.retries.get(uid, 0) >= int(cfg["turn"]["max_retries"]):
                    raise NhError("E111", cell=name, retries=str(state.retries.get(uid, 0)))
                retry = True
        newly_claimed = uid not in state.claims
        if newly_claimed:
            state.claims.append(uid)
            state.claim_notebooks[uid] = ref.rel_path
            state.kinds[uid] = "rerun"
        if retry:
            state.retries[uid] = state.retries.get(uid, 0) + 1
        state.status[uid] = "running"
        svc.ledger.save(state)

        defs, uses, _ = dataflow.defs_uses(target.source)
        names = set(defs) | {u for u in uses if u != "*"}
        before = await probe_before(svc, ref, names)
        title = title_of(cells, target)

        async def rollback() -> None:
            if newly_claimed:
                state.claims.remove(uid)
            if retry:
                state.retries[uid] -= 1
            state.status.pop(uid, None)
            svc.ledger.save(state)

        record = await start_run(
            svc,
            ref=ref,
            turn=turn,
            state=state,
            uid=uid,
            cell_id=target.id,
            title=title,
            code=target.source,
            before=before,
            rollback=rollback,
        )
        svc.stale.clear(ref.rel_path, [uid])
        svc.drift.clear(ref.rel_path, set(defs))

    return await report_run(
        svc,
        ctx,
        record,
        turn=turn,
        state=state,
        verb="Re-ran",
        where="",
        cells=cells,
        probe_names=names,
        started_at=started,
        event="cell_rerun",
        lead=lead,
        kernel_lines=kernel_lines,
    )


def _find_record(svc: Services, cells: list, cell_id: str) -> RunRecord | None:
    for pool in (svc.runs, svc.finished):
        if cell_id in pool:
            return pool[cell_id]
    found = find_cell(cells, cell_id)
    if found is not None:
        uid = uid_of(code_cell_for(cells, found))
        for pool in (svc.runs, svc.finished):
            if uid in pool:
                return pool[uid]
    return None


async def _wait(
    svc: Services, ctx: Context | None, *, cell_id: str, notebook: str | None
) -> ToolResult:
    turn = current_turn()
    cfg = svc.config()
    ref = await svc.resolve(notebook, activate=False)
    state = svc.ledger.get(turn.session_id, turn.prompt_id)
    cells = await snapshot(svc, ref, "summary")
    record = _find_record(svc, cells, cell_id)
    if record is None:
        found = find_cell(cells, cell_id)
        name = label(cells, code_cell_for(cells, found)) if found else "That cell"
        summary = found.summary if found else None
        status = f"failed with {summary.error}" if summary and summary.error else "finished"
        after = (
            "then return your last result for it to the workflow as your final answer."
            if is_writer(turn)
            else "report it, and wait for the user."
        )
        return text_result(
            f"Nothing is running: {name} {status}; nh has no unreported result for it.\n"
            f"nh: wait cell={cell_id}\n--- next ---\n"
            f'Call nh_inspect(view="cell") to read its output, {after}'
        )
    if record.uid in svc.runs:  # only waiting on a live run counts against the budget
        if state.waits >= int(cfg["turn"]["max_waits"]):
            raise NhError("E116")
        state.waits += 1
        svc.ledger.save(state)
    names = record.probe_names
    if names is None and record.before:
        names = set((record.before.get("vars") or {}).keys())
    return await report_run(
        svc,
        ctx,
        record,
        turn=turn,
        state=state,
        verb="Waited for",
        where="",
        cells=cells,
        probe_names=names,
    )


async def _interrupt(
    svc: Services, ctx: Context | None, *, cell_id: str, notebook: str | None
) -> ToolResult:
    turn = current_turn()
    ref = await svc.resolve(notebook, activate=False)
    state = svc.ledger.get(turn.session_id, turn.prompt_id)
    cells = await snapshot(svc, ref)
    record = _find_record(svc, cells, cell_id)
    if record is None or record.uid not in svc.runs:
        return text_result(
            "Nothing to interrupt: no nh cell is running.\nnh: interrupt none\n--- next ---\n"
            "Tell the user and wait."
        )
    name = render.cell_label(record.title, None, record.code)
    if not record.execution.started():
        return text_result(
            f"Not interrupted: {name} hasn't started yet (it is queued behind another cell in the kernel).\n"
            f"nh: interrupt cell={record.uid} started=false\n--- next ---\n"
            "Tell the user nh won't interrupt a cell it didn't start; they can stop the running cell in JupyterLab."
        )
    await svc.backend.interrupt(ref)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(record.execution.future), timeout=5.0)
    return await report_run(
        svc, ctx, record, turn=turn, state=state, verb="Interrupted", where="", cells=cells
    )
