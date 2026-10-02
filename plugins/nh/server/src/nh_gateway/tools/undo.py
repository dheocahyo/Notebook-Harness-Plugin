"""nh_undo: remove a cell nh added, or restore the version before nh's edit (FR-6)."""

from __future__ import annotations

import time
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult

from .. import meta, render
from ..backend.base import CellConflict, CellMissing, CellPatch, NotebookRef
from ..history import Op
from ..policy.errors import NhError
from ..policy.turn import KEEP_TURNS, TurnState
from ..state import write_last_cell
from .common import (
    Services,
    code_cell_for,
    config_lines,
    current_turn,
    drift_args,
    drift_lead,
    find_cell,
    kernel_notes,
    label,
    last_count,
    machine_line,
    note_for,
    review_earlier_cells,
    safe_probe,
    text_result,
    title_of,
    uid_of,
    windows_guard,
)
from .write import _rebuild_claims, _where, diff_lines, mark_downstream, snapshot, user_change


async def _leftovers(
    svc: Services, ref: NotebookRef, defs: list[str], name: str, verb: str = "undone"
) -> dict[str, str]:
    """Names the undone cell bound that the kernel really still holds (a failed line binds nothing)."""
    names = {n for n in defs if n != "*"}
    if not names:
        return {}
    payload = await safe_probe(svc, ref, "vars", drift_args(svc.config(), names))  # no max_vars cut
    if payload is None:  # can't tell: don't claim anything
        return {}
    present = set((payload.get("vars") or {}).keys()) & names
    name = name.removeprefix("the ")  # 'the undone cell `x = 1` [2]', not 'the undone the cell'
    return {n: f"still holds results from the {verb} {name}" for n in sorted(present)}


def _identity(kernel: Any) -> str:
    return f"{kernel.kernel_id}:{kernel.incarnation or ''}" if kernel is not None else ""


async def undo(
    svc: Services,
    ctx: Context | None,
    *,
    cell_id: str | None = None,
    force: bool = False,
    notebook: str | None = None,
) -> ToolResult:
    windows_guard()
    turn = current_turn()
    cfg = svc.config()
    if not cell_id and notebook is None:  # the user saying yes to an offer made in another notebook
        offering = _offer(svc, turn, svc.ledger.get(turn.session_id, turn.prompt_id))
        notebook = offering.undo_next_notebook if offering is not None else None
    ref = await svc.resolve(notebook)
    kernel = None
    lead: list[str] = []
    kernel_lines: list[str] = []
    try:
        kernel = await svc.backend.kernel_status(ref)
        lead, kernel_lines = kernel_notes(svc, ref, kernel)
    except NhError:
        pass  # undo edits the notebook; it doesn't need the kernel

    async with svc.locks.hold(ref.rel_path, turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        if state.undos >= int(cfg["turn"]["max_undos"]):
            raise NhError("E115")
        cells = await snapshot(svc, ref)
        _rebuild_claims(state, cells, ref.rel_path)
        review_earlier_cells(svc, ref, cells, state)

        offering = _offer(svc, turn, state) if not cell_id else None
        offered = _offered_ops(svc, ref, offering)
        if offered:
            ops = offered
        elif cell_id:
            found = find_cell(cells, cell_id)
            uid = uid_of(code_cell_for(cells, found)) if found is not None else cell_id
            ops = [
                op
                for op in reversed(svc.history.ops_for(uid))
                if not op.undone and op.notebook == ref.rel_path
            ]
        else:
            turns = svc.ledger.recent_turn_ids(turn.session_id, limit=3)
            if turn.prompt_id not in turns:
                turns.insert(0, turn.prompt_id)
            ops = svc.history.last_ops(ref.rel_path, turn.session_id, turns)
        if not ops:
            raise NhError("E143")
        op = ops[0]
        target = find_cell(cells, op.uid)
        if target is None:
            rest = [other for other in ops[1:] if other.uid != op.uid]
            if not rest:  # the step before may be older than the three-message window
                remembered = svc.ledger.recent_turn_ids(turn.session_id, limit=KEEP_TURNS)
                older = svc.history.last_ops(ref.rel_path, turn.session_id, remembered)
                rest = [other for other in older if other.uid != op.uid]
            return await _already_gone(svc, ref, cells, op, rest, turn, state, lead, kernel)

        name = label(cells, target)
        backend_live = getattr(svc.backend, "live_run", None)
        if (
            target.running
            or op.uid in svc.runs
            or (backend_live and backend_live(ref) == target.id)
        ):
            raise NhError("E133", detail=f" running {name}")
        if meta.source_sha(target.source) != op.after_sha and not force:
            change = diff_lines(op.after_source, target.source) if op.after_source else ""
            raise NhError(
                "E141",
                verb="undone",
                cell=name,
                diff=change or user_change(svc, op.uid, ref, target.source),
            )

        title = title_of(cells, target)
        note = note_for(cells, target)
        note_kept = note is not None and meta.note_edited(note.metadata, note.source)
        before = op.before or {}
        # A title- or notes-only edit resent the same code: undoing it changes no code, so
        # nothing in the kernel or below it is out of date.
        code_changed = op.op == "insert" or before.get("source", "") != target.source
        try:
            if op.op == "insert":
                ids = [target.id] if note is None or note_kept else [note.id, target.id]
                await svc.backend.delete_cells(ref, ids)
                action = f"Removed {name}" + (
                    " and its note" if note is not None and not note_kept else ""
                )
            else:
                patches = [
                    CellPatch(
                        id=target.id,
                        source=before.get("source", ""),
                        base_source=None if force else target.source,
                        metadata=before.get("nh"),
                        drop_nh_metadata=before.get("nh") is None,
                        tags_remove=() if meta.TAG in (before.get("tags") or []) else (meta.TAG,),
                        clear_outputs=code_changed,
                        execution_count=None if code_changed else target.execution_count,
                    )
                ]
                if note is not None and not note_kept and before.get("note_source") is not None:
                    patches.append(
                        CellPatch(
                            id=note.id,
                            source=before["note_source"],
                            base_source=note.source,
                            metadata=before.get("note_nh"),
                        )
                    )
                await svc.backend.update_cells(ref, patches)
                if code_changed:
                    action = f"Restored the previous version of {name} (not re-run)"
                elif note_kept:  # the note was all this edit changed, and the user rewrote it
                    action = (
                        f"Nothing to restore for {name}: its code was unchanged, and the user "
                        "had rewritten the note above it, so nh kept theirs"
                    )
                else:
                    action = (
                        f"Restored the previous title and notes of {name}; its code was unchanged"
                    )
        except CellConflict as exc:
            raise NhError(
                "E141", verb="undone", cell=name, diff=diff_lines(target.source, exc.current)
            ) from exc
        except CellMissing as exc:
            raise NhError("E140", cell_id=exc.cell_id) from exc

        svc.history.mark_undone(op, turn_id=turn.prompt_id)
        state.undos += 1
        svc.ledger.save(state)
        if offered and offering is not None:  # the offer is used up
            offering.undo_next = []
            svc.ledger.save(offering)

        stale_cells = []
        leftover: dict[str, str] = {}
        if code_changed:
            defs = set(op.defs)
            stale_cells = await mark_downstream(
                svc, ref, cells, target.index + 1, defs, by=op.uid, turn=turn, reason="undo"
            )
            if op.op == "edit":  # restored but not re-run: its count is now empty
                svc.stale.mark(
                    ref.rel_path,
                    [op.uid],
                    reason="undo",
                    by=op.uid,
                    turn_id=turn.prompt_id,
                    exec_counts={op.uid: None},
                )
            leftover = await _leftovers(svc, ref, op.defs, name)
            if leftover:
                svc.drift.add(
                    ref.rel_path, _identity(kernel), leftover, exec_count=last_count(cells)
                )
        write_last_cell(
            svc.layout,
            session_id=turn.session_id,
            notebook=ref.rel_path,
            cell_id=op.uid,
            title=title,
            exec=None,
            status="undone",
            turn_id=turn.prompt_id,
            retries_left=0,
            finished_at=time.time(),
        )
        svc.events.emit(
            "cell_undone",
            session_id=turn.session_id,
            turn_id=turn.prompt_id,
            notebook=ref.rel_path,
            cell_uid=op.uid,
            op=op.op,
            stale_marked=[uid_of(c) for c in stale_cells],
        )

    out = render.Result(
        f"{action}. The code is kept in .nh/history.", machine_line(op.uid, None, state, cfg)
    )
    lead_lines = lead + drift_lead(svc, ref)
    if lead_lines:
        (getattr(out, "lead", None) or (lambda lines: out.section("kernel", lines)))(lead_lines)
    out.section("kernel", kernel_lines)
    out.section("stale", [f"Now outdated: {label(cells, c)}" for c in stale_cells[:8]])
    notices = (
        ["Kept the note above it: the user had edited it."] if note_kept and code_changed else []
    )
    out.section("notices", notices)
    out.section("config", config_lines(cfg, svc.layout))
    kernel_part = "which variables still hold the old results, " if leftover else ""
    out.section(
        "next",
        f"Tell the user, in plain words: what was undone, {kernel_part}and which cells are "
        "now outdated. Offer to redo the step differently. Wait.",
    )
    return text_result(out.text(), status="undone", cell_id=op.uid)


def _offer(svc: Services, turn: Any, state: TurnState) -> TurnState | None:
    """The turn whose "already deleted" undo offered to undo the step before: this message or
    the one before it (the user saying yes). Older offers have expired."""
    if state.undo_next:
        return state
    recent = svc.ledger.recent_turn_ids(turn.session_id, limit=2)
    previous = [prompt_id for prompt_id in recent if prompt_id != turn.prompt_id]
    if previous:
        offering = svc.ledger.get(turn.session_id, previous[0])
        if offering.undo_next:
            return offering
    return None


def _offered_ops(svc: Services, ref: NotebookRef, offering: TurnState | None) -> list[Op]:
    if offering is None:
        return []
    return [
        op
        for uid in offering.undo_next
        for op in reversed(svc.history.ops_for(uid))
        if not op.undone and op.notebook == ref.rel_path
    ][:1]


async def _already_gone(
    svc: Services,
    ref: NotebookRef,
    cells: list,
    op: Op,
    rest: list[Op],
    turn: Any,
    state: TurnState,
    lead: list[str],
    kernel: Any,
) -> ToolResult:
    """The user deleted nh's cell in JupyterLab (a way to reject it). Nothing to undo, but its
    variables may still be in the kernel, and the next undo should move on to the step before."""
    name = f'"{op.title}"' if op.title else render.cell_label(None, None, op.after_source or "")
    for earlier in svc.history.ops_for(op.uid):  # every step of it is gone, not only the newest
        if not earlier.undone and earlier.notebook == ref.rel_path:
            svc.history.mark_undone(earlier, turn_id=turn.prompt_id)
    leftover = await _leftovers(svc, ref, op.defs, name, verb="deleted")
    if leftover:
        svc.drift.add(ref.rel_path, _identity(kernel), leftover, exec_count=last_count(cells))
    state.undo_next = [rest[0].uid] if rest else []
    state.undo_next_notebook = ref.rel_path if rest else None
    svc.ledger.save(state)
    write_last_cell(
        svc.layout,
        session_id=turn.session_id,
        notebook=ref.rel_path,
        cell_id=op.uid,
        title=op.title,
        exec=None,
        status="deleted",
        turn_id=turn.prompt_id,
        retries_left=0,
        finished_at=time.time(),
    )
    if rest:
        previous = find_cell(cells, rest[0].uid)
        prev_name = (
            label(cells, previous)
            if previous is not None
            else (f'"{rest[0].title}"' if rest[0].title else "the step before it")
        )
        next_step = (
            f"Tell the user {name} is already gone. Ask whether to undo {prev_name} next; "
            "if they say yes, call nh_undo() again. Wait."
        )
    else:
        next_step = f"Tell the user {name} is already gone; there is nothing else to undo. Wait."
    where = _where(svc, ref, "")
    lines = [
        f"Nothing undone: {name}{where} was already deleted in JupyterLab.",
        f"nh: E142 cell={op.uid}",
    ]
    lines = lead + drift_lead(svc, ref) + lines
    lines += ["--- next ---", next_step]
    return text_result("\n".join(lines), status="already-gone", cell_id=op.uid)
