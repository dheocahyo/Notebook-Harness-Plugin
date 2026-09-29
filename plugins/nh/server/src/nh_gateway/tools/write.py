"""nh_add_cell and nh_edit_cell: lint, write in one transaction, run, verify, report (FR-1/2/3/6/7)."""

from __future__ import annotations

import contextlib
import difflib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult

from .. import dataflow, meta, render
from .._shared import secrets
from .._shared.text import clip, count_words, render_note, split_notes, unescape_markdown
from .._shared.turn_record import WRITER_AGENT
from ..backend.base import (
    CellConflict,
    CellMissing,
    CellPatch,
    CellView,
    ExecResult,
    NewCell,
    NotebookRef,
)
from ..config import Config
from ..exec import shaping
from ..lint.lint import LintReport, lint_cell
from ..policy.errors import RETURN_TO_WORKFLOW, NhError, scrub
from ..policy.turn import TurnContext, TurnState
from ..state import write_last_cell
from .common import (
    RETRYABLE,
    RunRecord,
    Services,
    check_drift,
    code_cell_for,
    code_sources_before,
    config_lines,
    current_turn,
    drift_lead,
    effective_status,
    find_cell,
    image_contents,
    kernel_notes,
    label,
    machine_line,
    normalize_uids,
    note_for,
    note_last_cell,
    review_earlier_cells,
    safe_probe,
    text_result,
    time_left,
    title_of,
    track,
    uid_of,
    vars_args,
    wait_for,
    windows_guard,
)

# ---------------------------------------------------------------------------- shared steps


async def snapshot(svc: Services, ref: NotebookRef, outputs: str = "none") -> list[CellView]:
    cells = await svc.backend.snapshot(ref, outputs=outputs)  # type: ignore[arg-type]
    normalize_uids(cells)
    return cells


def _rebuild_claims(state: TurnState, cells: list[CellView], notebook: str) -> None:
    """After a gateway restart the ledger may be empty; the document still knows this turn's cell,
    and how often nh:cell-writer revised it."""
    if state.claims:
        return
    for cell in cells:
        nh = meta.nh_meta(cell.metadata)
        if nh.get("role") == "code" and state.prompt_id in (
            nh.get("turn_id"),
            nh.get("last_turn_id"),
        ):
            uid = nh.get("uid") or cell.id
            state.claims.append(uid)
            state.claim_notebooks[uid] = notebook
            state.kinds.setdefault(uid, "add" if nh.get("turn_id") == state.prompt_id else "edit")
            state.status.setdefault(uid, "running" if cell.running else "ok")
            revision = nh.get("revision")
            if isinstance(revision, dict) and revision.get("turn") == state.prompt_id:
                done = revision.get("n")
                if isinstance(done, int) and done > state.revisions.get(uid, 0):
                    state.revisions[uid] = done


async def _claimed(
    svc: Services, ref: NotebookRef, cells: list[CellView], state: TurnState
) -> tuple[str, CellView | None, str]:
    """This message's cell: (uid, cell, label). The cell is looked up in its own notebook, which
    the label names when it isn't ``ref``."""
    uid = state.claims[0] if state.claims else ""
    notebook = state.claim_notebooks.get(uid, ref.rel_path)
    where = ""
    if notebook != ref.rel_path:
        where = f" in {notebook}"
        try:
            cells = await snapshot(svc, await svc.resolve(notebook, activate=False))
        except NhError:
            cells = []
    cell = find_cell(cells, uid) if uid else None
    name = label(cells, cell) if cell is not None else "a cell nh wrote earlier in this message"
    return uid, cell, name + where


def refuse_if_running(svc: Services, ref: NotebookRef, cells: list[CellView]) -> None:
    """Nothing is written while an nh cell still runs in this notebook's kernel (instruction 7)."""
    record = svc.live_run(ref)
    backend_live = getattr(svc.backend, "live_run", None)
    cell_id = (
        record.cell_id if record is not None else (backend_live(ref) if backend_live else None)
    )
    if not cell_id:
        return
    cell = find_cell(cells, cell_id)
    name = label(cells, cell) if cell is not None else "an earlier nh cell"
    wait = f'call nh_run(cell_id="{cell_id}", mode="wait") to wait for it'
    if record is not None and not record.execution.started():
        raise NhError(
            "E133",
            detail=f" with another cell; {name} is queued behind it",
            next_step=(
                f"Tell the user {name} has not started: it waits behind a cell already running "
                f"in the kernel, so nothing of it has run. Write nothing now; {wait}, or they can "
                "stop the running cell in JupyterLab."
            ),
        )
    raise NhError(
        "E133",
        detail=f" running {name}",
        next_step=(
            f"Tell the user {name} is still running. Write nothing now; {wait}, or let them stop "
            "it in JupyterLab."
        ),
    )


def _lint_failure(
    svc: Services, state: TurnState, report: LintReport, turn: TurnContext
) -> NhError:
    state.lint_rejects += 1
    svc.ledger.save(state)
    rules = [issue.rule for issue in report.errors]
    svc.events.emit(
        "cell_rejected", session_id=turn.session_id, turn_id=turn.prompt_id, reason=rules
    )
    lines = [f"- {issue.rule}: {issue.message} Fix: {issue.fix}" for issue in report.errors]
    next_step = None
    if "L009" in rules:
        next_step = (
            "Don't call again yet: ask the user whether to install the package. After a yes, run "
            "`uv add <package>` (or `conda install`) with Bash, then write the cell without the install."
        )
    elif "L002" in rules:
        next_step = (
            "Keep only the first step in this cell (no separators), call again, and propose the rest "
            "as a numbered list in your reply."
        )
    return NhError("E120", detail="\n".join(lines), next_step=next_step)


def diff_lines(old: str, new: str, limit: int = 6) -> str:
    diff = [
        line
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))
    ]
    if not diff:
        return ""
    shown = diff[:limit]
    more = f"\n  … {len(diff) - limit} more changed lines" if len(diff) > limit else ""
    return "\n" + "\n".join(f"  {line}" for line in shown) + more


def nh_last_source(svc: Services, uid: str, ref: NotebookRef) -> str | None:
    """The code nh last left in the cell: its newest write that is still in place, or what the
    oldest of the undos since then restored (undos go newest first). None without history."""
    ops = [op for op in svc.history.ops_for(uid) if op.notebook == ref.rel_path]
    kept = len(ops)
    while kept and ops[kept - 1].undone:
        kept -= 1
    if kept < len(ops):
        return (ops[kept].before or {}).get("source")
    return ops[-1].after_source if ops else None


def user_change(svc: Services, uid: str, ref: NotebookRef, current: str) -> str:
    """The user's edit as a diff against the code nh last left in the cell (history keeps it)."""
    last = nh_last_source(svc, uid, ref)
    if last is not None:
        return diff_lines(last, current)
    head = "\n".join(f"  {line}" for line in current.splitlines()[:6])
    return f"\nThe cell now reads:\n{head}" if head else ""


def nh_wrote(svc: Services, uid: str, ref: NotebookRef, current: str) -> bool:
    """``current`` is the code nh last left in the cell: whatever sha the agent sent, the user
    changed nothing since."""
    last = nh_last_source(svc, uid, ref)
    return last is not None and meta.source_sha(last) == meta.source_sha(current)


def is_writer(turn: TurnContext) -> bool:
    """The call comes from nh:cell-writer inside an nh:qa-cell run (the gate checked the run)."""
    return turn.agent_type == WRITER_AGENT


def next_for(turn: TurnContext, main: str | None = None) -> str | None:
    """A refusal's Next line: ``main`` for the main conversation (None keeps the catalogue's);
    nh:cell-writer, which can't ask the user, returns the refusal to the workflow."""
    return RETURN_TO_WORKFLOW if is_writer(turn) else main


async def refuse_other_run(
    svc: Services, ref: NotebookRef, cells: list[CellView], state: TurnState, turn: TurnContext
) -> None:
    """The first nh:qa-cell run whose writer wrote owns this message's cell: a second run of the
    same message (launched in parallel) changes nothing, not even that cell."""
    if turn.run_id and state.writer_run and state.writer_run != turn.run_id:
        _, _, name = await _claimed(svc, ref, cells, state)
        raise NhError(
            "E110",
            "- Another nh:qa-cell run owns this message's cell.",
            cell=name,
            next_step=RETURN_TO_WORKFLOW,
        )


def refuse_user_code(
    svc: Services, uid: str, ref: NotebookRef, target: CellView, name: str
) -> None:
    """nh:cell-writer runs in the background, where nobody can confirm overwriting the user's
    typing: its base_sha counts for nothing, and a cell the user wrote (E144) or changed since
    nh wrote it (E141) goes back to the workflow."""
    if not meta.is_agent_authored(target.metadata):
        raise NhError(
            "E144",
            "- nh:cell-writer never changes a cell the user wrote.",
            cell=name,
            next_step=RETURN_TO_WORKFLOW,
        )
    known_sha = meta.nh_meta(target.metadata).get("source_sha")
    current = target.source
    if known_sha and known_sha != meta.source_sha(current) and not nh_wrote(svc, uid, ref, current):
        raise NhError(
            "E141",
            cell=name,
            diff=user_change(svc, uid, ref, current),
            next_step=RETURN_TO_WORKFLOW,
        )


def nothing_to_revise(name: str, status: str | None) -> NhError:
    """E112 for nh:cell-writer on its cell when the last run is neither OK (a revision) nor a
    failure to retry: aborted, never reported, or unknown. Such edits would count nowhere."""
    error = NhError(
        "E112",
        f"- nh's status for it: {status or 'none'}.",
        cell=name,
        next_step=RETURN_TO_WORKFLOW,
    )
    # The catalogue's first line says the cell ran OK, which it didn't.
    _, rest = str(error).split("\n", 1)
    head = f"Not written: {name} has no OK run to revise and no failed run to retry."
    error.args = (f"{scrub(head)}\n{rest}",)
    return error


def _insert_index(cells: list[CellView], after_cell_id: str | None) -> tuple[int, CellView | None]:
    if after_cell_id is None:
        return len(cells), (cells[-1] if cells else None)
    anchor = find_cell(cells, after_cell_id)
    if anchor is None:
        raise NhError("E140", cell_id=after_cell_id)
    anchor = code_cell_for(cells, anchor)
    return anchor.index + 1, anchor


async def mark_downstream(
    svc: Services,
    ref: NotebookRef,
    cells: list[CellView],
    start: int,
    tainted: set[str],
    *,
    by: str,
    turn: TurnContext,
    reason: str,
) -> list[CellView]:
    later = [c for c in cells[start:] if c.cell_type == "code"]
    if not later or not tainted:
        return []
    ids = dataflow.downstream([(c.id, c.source) for c in later], 0, set(tainted))
    affected = [c for c in later if c.id in ids]
    if not affected:
        return []
    clear_count = svc.config()["stale"]["mark"] == "clear-count"
    if clear_count:  # show it in JupyterLab too: an empty [ ] prompt
        try:
            await svc.backend.update_cells(
                ref, [CellPatch(id=c.id, execution_count=None) for c in affected]
            )
        except (CellConflict, CellMissing):
            clear_count = False
    svc.stale.mark(
        ref.rel_path,
        [uid_of(c) for c in affected],
        reason=reason,
        by=by,
        turn_id=turn.prompt_id,
        exec_counts={uid_of(c): (None if clear_count else c.execution_count) for c in affected},
    )
    return affected


def backend_notices(svc: Services, ref: NotebookRef) -> list[str]:
    take = getattr(svc.backend, "take_notices", None)
    return list(take(ref)) if take is not None else []


def record_history(svc: Services, **kwargs: Any) -> list[str]:
    """History powers undo; a failed write must not fail the turn, but the user should know."""
    try:
        svc.history.record(**kwargs)
    except OSError as exc:
        svc.events.emit("history_failed", error=str(exc))
        return [
            f"Undo is unavailable for this cell (nh couldn't save its history: {exc.strerror or exc})."
        ]
    return []


def _hint_lines(report: LintReport) -> list[str]:
    return [f"- {issue.message} {issue.fix}".rstrip() for issue in report.hints[:5]]


def _where(svc: Services, ref: NotebookRef, base: str) -> str:
    default = svc.config()["project"]["notebook"]
    return base if not default or ref.rel_path == default else f"{base} in {ref.rel_path}"


async def probe_before(svc: Services, ref: NotebookRef, names: set[str]) -> dict[str, Any] | None:
    """The before-state for the self-check. First the 'Kernel ≠ notebook' names get their own
    check, so names gone from the kernel (restart, del, never bound) or bound again by a re-run
    stop being reported, and none of them is left out by the cut to ``max_vars``."""
    await check_drift(svc, ref)
    return await safe_probe(svc, ref, "vars", vars_args(svc.config(), set(names)))


# ---------------------------------------------------------------------------- execution + report


async def start_run(
    svc: Services,
    *,
    ref: NotebookRef,
    turn: TurnContext,
    state: TurnState,
    uid: str,
    cell_id: str,
    title: str | None,
    code: str,
    before: dict[str, Any] | None,
    rollback: Callable[[], Awaitable[None]] | None = None,
    history: dict[str, Any] | None = None,
) -> RunRecord:
    """Start the cell. If it can't start, undo the write (``rollback``) so no unrun cell is left
    and the turn's cell isn't used up; if the user typed into it first, don't run their text.

    That write stays, so it goes into the undo history (``history``: ``record_history``'s
    arguments) and last_cell.json says it didn't run.
    """
    cfg = svc.config()
    try:
        execution = await svc.backend.start_execution(
            ref, cell_id, hard_timeout=float(cfg["exec"]["hard_timeout_s"]), expected_source=code
        )
    except CellConflict as exc:
        state.status[uid] = "conflict"
        svc.ledger.save(state)
        if history is not None:
            record_history(svc, **history)
        write_last_cell(
            svc.layout,
            session_id=turn.session_id,
            notebook=ref.rel_path,
            cell_id=uid,
            title=title,
            exec=None,
            status="conflict",
            turn_id=turn.prompt_id,
            retries_left=max(0, int(cfg["turn"]["max_retries"]) - state.retries.get(uid, 0)),
            finished_at=time.time(),
        )
        raise NhError(
            "E141",
            verb="run",
            cell=render.cell_label(title, None, code),
            diff=diff_lines(code, exc.current),
            next_step=next_for(
                turn,
                "The user typed into the cell before it ran, so nh left their text and ran "
                "nothing. Show them the change and ask how to continue.",
            ),
        ) from exc
    except NhError:
        if rollback is not None:
            await rollback()
        raise
    defs, uses, _ = dataflow.defs_uses(code)
    record = RunRecord(
        execution=execution,
        ref=ref,
        uid=uid,
        cell_id=cell_id,
        title=title,
        session_id=turn.session_id,
        prompt_id=turn.prompt_id,
        code=code,
        before=before,
        probe_names=defs | (uses - {dataflow.EVERYTHING}),  # for nh_run(mode="wait") later
    )
    track(svc, record)
    return record


async def report_run(
    svc: Services,
    ctx: Context | None,
    record: RunRecord,
    *,
    turn: TurnContext,
    state: TurnState,
    verb: str,
    where: str,
    cells: list[CellView],
    stale: list[CellView] | None = None,
    report: LintReport | None = None,
    probe_names: set[str] | None = None,
    started_at: float | None = None,
    note_text: str | None = None,
    bullets: list[str] | None = None,
    event: str | None = None,
    lead: list[str] | None = None,
    kernel_lines: list[str] | None = None,
    extra_notices: list[str] | None = None,
    revision: int | None = None,
) -> ToolResult:
    """Wait for a run (up to the soft timeout), verify it, and build the tool result.

    nh:cell-writer's result (``nh_run`` waits included) is for the nh:qa-cell workflow: its
    machine line counts revisions and its next block returns the result there.
    """
    cfg = svc.config()
    writer = is_writer(turn)
    uid, ref, code = record.uid, record.ref, record.code
    harness_ms = int((time.monotonic() - started_at) * 1000) if started_at else None
    result = await _wait(svc, record.execution, turn, cfg, ctx)
    status = effective_status(result, record.execution)
    state.status[uid] = status
    svc.ledger.save(state)
    exec_count = result.execution_count if result else None
    note_last_cell(svc, record, status, exec_count, finished=result is not None)
    if result is not None:
        record.reported = True
        svc.finished.pop(uid, None)

    after = None
    if result is not None and status in ("ok", "error") and probe_names is not None:
        after = await safe_probe(svc, ref, "vars", vars_args(cfg, probe_names))

    outputs = result.outputs if result is not None else record.execution.partial_outputs()
    shaped = shaping.shape_outputs(
        outputs,
        max_chars=int(cfg["output"]["max_chars"]),
        max_images=int(cfg["output"]["max_images"]),
        image_max_px=int(cfg["output"]["image_max_px"]),
        save_dir=svc.layout.outputs,
    )
    before = record.before
    selfcheck, check_this = (
        render.selfcheck(before, after, code=code) if (before and after) else ([], [])
    )
    cell_name = render.cell_label(record.title, exec_count, code)
    retries_left = max(0, int(cfg["turn"]["max_retries"]) - state.retries.get(uid, 0))
    waits_left = max(0, int(cfg["turn"]["max_waits"]) - state.waits)

    first = _first_line(verb, cell_name, where, status, result, shaped, selfcheck, record)
    out = render.Result(first, machine_line(uid, exec_count, state, cfg, writer=writer))
    _lead(out, list(lead or []) + drift_lead(svc, ref))
    out.section("kernel", list(kernel_lines or []))
    out.section("check this", check_this)
    out.section("output", shaped.text)
    if shaped.error is not None and status == "error":
        out.section("error", _error_lines(shaped.error, code))
    out.section("self-check", selfcheck)
    out.section("readability hints (advisory)", _hint_lines(report) if report else [])
    out.section("stale", [f"Now outdated: {label(cells, c)}" for c in (stale or [])[:8]])
    notices = backend_notices(svc, ref) + list(extra_notices or [])
    if result is not None and result.note and status not in ("lost", "deleted"):
        notices.append(result.note)
    out.section("notices", notices)
    out.section("config", config_lines(cfg))
    out.section(
        "next",
        render.next_block(
            status,
            retries_left=retries_left,
            waits_left=waits_left,
            cell=cell_name,
            audience="writer" if writer else "main",
        ),
    )

    if event:
        svc.events.emit(
            event,
            session_id=turn.session_id,
            turn_id=turn.prompt_id,
            notebook=ref.rel_path,
            cell_uid=uid,
            note_words=count_words(note_text or ""),
            bullet_words=[count_words(b) for b in bullets or []],
            code_lines=report.code_lines if report else None,
            comment_lines=report.comment_lines if report else None,
            hints=[issue.rule for issue in report.hints] if report else [],
            exec={
                "status": status,
                "ms": int((result.seconds if result else 0) * 1000),
                "exec_count": exec_count,
            },
            harness_ms=harness_ms,
            source_sha=meta.source_sha(code),
            agent=turn.agent_type,
            revision=revision,
        )
    return text_result(out.text(), image_contents(shaped), status=status, cell_id=uid)


def _lead(out: render.Result, lines: list[str]) -> None:
    if not lines:
        return
    lead = getattr(out, "lead", None)
    if lead is not None:
        lead(lines)
    else:  # older render: fold into the kernel section
        out.section("kernel", lines)


async def _wait(
    svc: Services, execution: Any, turn: TurnContext, cfg: Config, ctx: Context | None
) -> ExecResult | None:
    return await wait_for(execution, time_left(turn, cfg), ctx)


def _error_lines(err: Any, code: str) -> list[str]:
    lines = [render.error_summary(err.ename, err.evalue, 200)]
    source = code.splitlines()
    if err.line and 1 <= err.line <= len(source):
        failing = secrets.current().redact(source[err.line - 1].strip())
        lines.append(f"failing code: {failing}")
    return lines


def refuse_redacted(code: str) -> None:
    """E125: code holding nh's marker would put the marker, not the secret, in the notebook
    (design §6.8). The raw value is never restored: Claude never saw it."""
    if secrets.MARKER in code:
        raise NhError("E125")


def _first_line(
    verb: str,
    cell_name: str,
    where: str,
    status: str,
    result: ExecResult | None,
    shaped: shaping.Shaped,
    selfcheck: list[str],
    record: RunRecord,
) -> str:
    head = f"{verb} {cell_name}{where}"
    if result is None:
        waited = time.monotonic() - getattr(record.execution, "started_at", time.monotonic())
        if status == "queued":
            return (
                f"{head}; not started yet: it is queued behind a cell already running in the kernel "
                f"({waited:.0f}s so far)."
            )
        return (
            f"{head}; still running after {waited:.0f}s (outputs keep streaming into the notebook)."
        )
    if status == "ok":
        extra = render.headline(selfcheck)
        extra = f"; {extra}" if extra else ""
        return f"{head}; ran ok in {result.seconds:.1f}s{extra}."
    if status == "error":
        err = shaped.error or result.error
        what = render.error_summary(err.ename, err.evalue, 100) if err else "an error"
        end = "" if what.endswith(("…", ".")) else "."  # '…' marks a cut message
        return f"{head}; it failed with {what}{end}"
    if status == "aborted":
        return f"{head}; nothing ran because the cell ahead of it in the kernel queue failed."
    if status == "timeout":
        return f"{head}; stopped after the time limit ({result.seconds:.0f}s)."
    if status == "interrupted":
        who = "the user stopped it" if "JupyterLab" in (result.note or "") else "interrupted"
        return f"{head}; {who} after {result.seconds:.1f}s."
    if status == "deleted":
        return f"{head}; the user deleted the cell in JupyterLab while it ran."
    return f"{head}; the kernel went away while it ran ({result.note or 'restart or shutdown'})."


async def e110(
    svc: Services,
    ref: NotebookRef,
    cells: list[CellView],
    state: TurnState,
    cfg: Config,
    turn: TurnContext | None = None,
) -> NhError:
    """A second cell was asked for; if this message's cell failed, point at fixing it instead.
    nh:cell-writer (``turn``) returns the refusal to the workflow either way."""
    uid, cell, name = await _claimed(svc, ref, cells, state)
    if turn is not None and is_writer(turn):
        return NhError("E110", cell=name, next_step=RETURN_TO_WORKFLOW)
    left = int(cfg["turn"]["max_retries"]) - state.retries.get(uid, 0)
    if state.status.get(uid) in RETRYABLE and left > 0 and cell is not None:
        notebook = state.claim_notebooks.get(uid, ref.rel_path)
        where = f', notebook="{notebook}"' if notebook != ref.rel_path else ""
        return NhError(
            "E110",
            cell=name,
            next_step=f'This message\'s cell failed. Fix it with nh_edit_cell(cell_id="{cell.id}"{where}) '
            f"on that same cell ({left} {'retry' if left == 1 else 'retries'} left), not with a new cell.",
        )
    return NhError("E110", cell=name)


# ---------------------------------------------------------------------------- nh_add_cell


async def add_cell(
    svc: Services,
    ctx: Context | None,
    *,
    title: str,
    notes: list[str] | str,
    intent: str,
    code: str,
    after_cell_id: str | None = None,
    notebook: str | None = None,
) -> ToolResult:
    windows_guard()
    started = time.monotonic()
    turn = current_turn()
    refuse_redacted(code)
    cfg = svc.config()
    ref = await svc.resolve(notebook)
    kernel = await svc.backend.kernel_status(ref)
    lead, kernel_lines = kernel_notes(svc, ref, kernel)

    async with svc.locks.hold(ref.rel_path, turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        state.notebook = ref.rel_path
        cells = await snapshot(svc, ref)
        _rebuild_claims(state, cells, ref.rel_path)
        review_earlier_cells(svc, ref, cells, state)
        writer = is_writer(turn)
        if writer:
            await refuse_other_run(svc, ref, cells, state, turn)
        if len(state.claims) >= int(cfg["turn"]["max_code_cells"]):
            raise await e110(svc, ref, cells, state, cfg, turn)
        refuse_if_running(svc, ref, cells)
        if state.lint_rejects >= int(cfg["turn"]["max_lint_rejects"]):
            raise NhError("E121")

        index, anchor = _insert_index(cells, after_cell_id)
        names_above = dataflow.defined_names(code_sources_before(cells, index))
        report = lint_cell(
            code,
            title=title,
            notes=notes,
            intent=intent,
            cfg=cfg,
            require_note=True,
            require_intent=True,
            kernel_python=kernel.python_version,
            names_above=names_above,
        )
        if report.errors:
            raise _lint_failure(svc, state, report, turn)

        probe_names = set(report.uses) | set(report.defs)
        before = await probe_before(svc, ref, probe_names)

        existing = {c.id for c in cells} | {uid_of(c) for c in cells}
        uid = meta.new_code_id(existing)
        note_uid = meta.note_id(uid)
        bullets = report.bullets or split_notes(notes)
        clean_title = report.title or title
        note_text = render_note(clean_title, bullets, int(cfg["markdown"]["heading_level"]))
        code_md = {
            "tags": [meta.TAG],
            "nh": meta.code_metadata(
                uid=uid,
                intent=intent.strip(),
                bullets=bullets,
                turn_id=turn.prompt_id,
                source=code,
                agent=turn.agent_type if writer else None,
            ),
        }
        note_md = {
            "tags": [meta.TAG],
            "nh": meta.note_metadata(uid=uid, turn_id=turn.prompt_id, source=note_text),
        }
        await svc.backend.insert_cells(
            ref,
            index,
            [
                NewCell(id=note_uid, cell_type="markdown", source=note_text, metadata=note_md),
                NewCell(id=uid, cell_type="code", source=code, metadata=code_md),
            ],
        )
        state.claims.append(uid)
        state.claim_notebooks[uid] = ref.rel_path
        state.kinds[uid] = "add"
        state.status[uid] = "running"
        owner = state.writer_run
        if writer:
            state.writer_run = turn.run_id
        svc.ledger.save(state)

        async def rollback() -> None:
            await svc.backend.delete_cells(ref, [note_uid, uid])
            state.claims.remove(uid)
            state.status.pop(uid, None)
            state.writer_run = owner
            svc.ledger.save(state)

        history = dict(
            op="insert",
            notebook=ref.rel_path,
            session_id=turn.session_id,
            turn_id=turn.prompt_id,
            uid=uid,
            note_uid=note_uid,
            index=index,
            before=None,
            after_source=code,
            defs=set(report.defs),
            title=clean_title,
        )
        record = await start_run(
            svc,
            ref=ref,
            turn=turn,
            state=state,
            uid=uid,
            cell_id=uid,
            title=clean_title,
            code=code,
            before=before,
            rollback=rollback,
            history=history,
        )
        history_notes = record_history(svc, **history)
        stale = await mark_downstream(
            svc, ref, cells, index, set(report.defs), by=uid, turn=turn, reason="upstream-edit"
        )
        svc.drift.clear(ref.rel_path, set(report.defs))

    if index >= len(cells):
        base = " at the bottom" + (f", below {label(cells, anchor)}" if anchor is not None else "")
    else:
        base = f" below {label(cells, anchor)}" if anchor is not None else " at the top"
    return await report_run(
        svc,
        ctx,
        record,
        turn=turn,
        state=state,
        verb="Added",
        where=_where(svc, ref, base),
        cells=cells,
        stale=stale,
        report=report,
        probe_names=probe_names,
        started_at=started,
        note_text=note_text,
        bullets=bullets,
        event="cell_added",
        lead=lead,
        kernel_lines=kernel_lines,
        extra_notices=history_notes,
    )


# ---------------------------------------------------------------------------- nh_edit_cell


async def edit_cell(
    svc: Services,
    ctx: Context | None,
    *,
    cell_id: str,
    code: str,
    base_sha: str | None = None,
    title: str | None = None,
    notes: list[str] | str | None = None,
    intent: str | None = None,
    notebook: str | None = None,
) -> ToolResult:
    windows_guard()
    started = time.monotonic()
    turn = current_turn()
    refuse_redacted(code)
    writer = is_writer(turn)
    if writer:
        base_sha = None  # refuse_user_code: nobody in the background can confirm the user's change
    cfg = svc.config()
    ref = await svc.resolve(notebook)
    kernel = await svc.backend.kernel_status(ref)
    lead, kernel_lines = kernel_notes(svc, ref, kernel)

    async with svc.locks.hold(ref.rel_path, turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        state.notebook = ref.rel_path
        cells = await snapshot(svc, ref)
        _rebuild_claims(state, cells, ref.rel_path)
        review_earlier_cells(svc, ref, cells, state)
        if writer:
            await refuse_other_run(svc, ref, cells, state, turn)
        found = find_cell(cells, cell_id)
        if found is None:
            raise NhError("E140", cell_id=cell_id)
        target = code_cell_for(cells, found)
        target_label = label(cells, target)
        if target.cell_type != "code":
            raise NhError("E145", cell=target_label)
        refuse_if_running(svc, ref, cells)
        if target.running:
            raise NhError("E133", detail=f" running {target_label}")
        uid = uid_of(target)
        if writer:
            refuse_user_code(svc, uid, ref, target, target_label)

        retry = False
        revision = False  # nh:cell-writer changing its OK cell from QA findings
        prior, owner = state.status.get(uid), state.writer_run
        if state.claims:
            if uid not in state.claims:
                _, _, claimed = await _claimed(svc, ref, cells, state)
                raise NhError("E113", cell=claimed, other=target_label)
            status = state.status.get(uid)
            if status == "ok":
                if not writer:
                    raise NhError("E112", cell=target_label)
                done, most = state.revisions.get(uid, 0), int(cfg["turn"]["max_revisions"])
                if done >= most:
                    raise NhError(
                        "E112",
                        f"- No revisions left ({done} of {most} used; [turn] max_revisions).",
                        cell=target_label,
                        next_step=RETURN_TO_WORKFLOW,
                    )
                revision = True
            if status == "interrupted":
                raise NhError("E117", cell=target_label)
            if status == "conflict":
                raise NhError("E118", cell=target_label)
            if status in RETRYABLE:
                if state.retries.get(uid, 0) >= int(cfg["turn"]["max_retries"]):
                    raise NhError("E111", cell=target_label, retries=str(state.retries.get(uid, 0)))
                retry = True
            elif writer and not revision:
                raise nothing_to_revise(target_label, status)
        if state.lint_rejects >= int(cfg["turn"]["max_lint_rejects"]):
            raise NhError("E121")

        current = target.source
        nh = meta.nh_meta(target.metadata)
        is_agent = meta.is_agent_authored(target.metadata)
        # A sha that doesn't match is no user change while the cell holds what nh last wrote.
        known_sha = base_sha or (nh.get("source_sha") if is_agent else None)
        if not base_sha and not is_agent:
            raise NhError("E144", cell=target_label)
        changed = known_sha and known_sha != meta.source_sha(current)
        if changed and not nh_wrote(svc, uid, ref, current):
            raise NhError("E141", cell=target_label, diff=user_change(svc, uid, ref, current))

        note = note_for(cells, target) if is_agent else None
        wants_note = note is not None and (title is not None or notes is not None)
        note_title = title
        note_bullets: list[str] | str | None = notes
        if wants_note and note is not None:
            if meta.note_edited(note.metadata, note.source):
                raise NhError(
                    "E141",
                    cell=f"the note above {target_label}",
                    diff="\n" + "\n".join(f"  {line}" for line in note.source.splitlines()[:6]),
                    next_step=next_for(
                        turn,
                        "Ask the user; keep their wording in notes= or leave the note alone "
                        "(omit title and notes).",
                    ),
                )
            if note_title is None:  # only notes given: keep the title
                note_title = title_of(cells, target)
            if note_bullets is None:  # only a title given: keep the bullets
                note_bullets = list(nh.get("rationale") or []) or [
                    line[2:] for line in note.source.splitlines() if line.startswith("- ")
                ]
        names_above = dataflow.defined_names(code_sources_before(cells, target.index))
        report = lint_cell(
            code,
            title=note_title if wants_note else None,
            notes=note_bullets if wants_note else None,
            intent=intent,
            cfg=cfg,
            require_note=wants_note,
            require_intent=not nh.get("intent"),
            kernel_python=kernel.python_version,
            names_above=names_above,
        )
        if report.errors:
            raise _lint_failure(svc, state, report, turn)

        probe_names = set(report.uses) | set(report.defs)
        before_probe = await probe_before(svc, ref, probe_names)
        old_defs, _, _ = dataflow.defs_uses(current)
        history_before = {
            "source": current,
            "nh": nh or None,
            "tags": list((target.metadata or {}).get("tags") or []),
            "note_source": note.source if note else None,
            "note_nh": meta.nh_meta(note.metadata) if note else None,
            "execution_count": target.execution_count,
        }
        bullets = report.bullets if wants_note else list(nh.get("rationale") or [])
        if nh:
            new_nh = dict(nh)
        else:
            new_nh = {
                "v": meta.META_VERSION,
                "role": "code",
                "uid": target.id,
                "created_by": "human",
                "rationale": [],
            }
        new_nh.update(
            {
                "source_sha": meta.source_sha(code),
                "edits": int(nh.get("edits", 0)) + 1,
                "last_turn_id": turn.prompt_id,
            }
        )
        if not is_agent:
            new_nh["edited_by"] = "agent"
        if intent and intent.strip():
            if not new_nh.get("intent"):
                new_nh["intent"] = intent.strip()
            elif intent.strip() != new_nh["intent"]:  # keep the original ask; add this one
                new_nh["edit_intents"] = [*list(new_nh.get("edit_intents") or []), intent.strip()]
        if wants_note:
            new_nh["rationale"] = bullets
        if writer:
            new_nh["agent"] = turn.agent_type
        if revision:
            new_nh["revision"] = {"turn": turn.prompt_id, "n": state.revisions.get(uid, 0) + 1}
        # Outputs and [n] stay until the run starts (it clears them), so a rollback keeps them.
        patches = [
            CellPatch(
                id=target.id,
                source=code,
                base_source=current,
                metadata=new_nh,
                tags_add=(meta.TAG,) if is_agent else (),
            )
        ]
        note_text = None
        title_now = title_of(cells, target)
        if wants_note and note is not None:
            title_now = report.title or note_title or title_now or ""
            note_text = render_note(title_now, bullets, int(cfg["markdown"]["heading_level"]))
            note_nh = dict(meta.nh_meta(note.metadata))
            note_nh.update(source_sha=meta.source_sha(note_text), last_turn_id=turn.prompt_id)
            patches.append(
                CellPatch(id=note.id, source=note_text, base_source=note.source, metadata=note_nh)
            )
        try:
            await svc.backend.update_cells(ref, patches)
        except CellConflict as exc:
            if note is not None and exc.cell_id == note.id:
                raise NhError(
                    "E141",
                    cell=f"the note above {target_label}",
                    diff=diff_lines(note.source, exc.current),
                    next_step=next_for(
                        turn, "Ask the user; keep their wording in notes= or leave the note alone."
                    ),
                ) from exc
            raise NhError(
                "E141",
                cell=target_label,
                diff=diff_lines(current, exc.current),
                next_step=next_for(turn),
            ) from exc
        except CellMissing as exc:
            raise NhError("E140", cell_id=exc.cell_id) from exc

        newly_claimed = uid not in state.claims
        if newly_claimed:
            state.claims.append(uid)
            state.claim_notebooks[uid] = ref.rel_path
            state.kinds[uid] = "edit"
        if retry:
            state.retries[uid] = state.retries.get(uid, 0) + 1
        if revision:
            state.revisions[uid] = state.revisions.get(uid, 0) + 1
        if writer:
            state.writer_run = turn.run_id
        state.status[uid] = "running"
        svc.ledger.save(state)

        async def rollback() -> None:
            restore = [
                CellPatch(
                    id=target.id,
                    source=current,
                    base_source=code,
                    metadata=nh or None,
                    drop_nh_metadata=not nh,
                )
            ]
            if note_text is not None and note is not None:
                restore.append(
                    CellPatch(
                        id=note.id,
                        source=note.source,
                        base_source=note_text,
                        metadata=meta.nh_meta(note.metadata),
                    )
                )
            # If the user changed it meanwhile, leave their version.
            with contextlib.suppress(CellConflict, CellMissing):
                await svc.backend.update_cells(ref, restore)
            if newly_claimed:
                state.claims.remove(uid)
            if retry:
                state.retries[uid] -= 1
            if revision:
                state.revisions[uid] -= 1
            state.writer_run = owner
            if writer and prior is not None:  # its OK (or failed) run still stands
                state.status[uid] = prior
            else:
                state.status.pop(uid, None)
            svc.ledger.save(state)

        history = dict(
            op="edit",
            notebook=ref.rel_path,
            session_id=turn.session_id,
            turn_id=turn.prompt_id,
            uid=uid,
            note_uid=meta.nh_meta(note.metadata).get("uid") if note else None,
            index=target.index,
            before=history_before,
            after_source=code,
            defs=set(report.defs),
            title=title_now,
        )
        record = await start_run(
            svc,
            ref=ref,
            turn=turn,
            state=state,
            uid=uid,
            cell_id=target.id,
            title=title_now,
            code=code,
            before=before_probe,
            rollback=rollback,
            history=history,
        )
        history_notes = record_history(svc, **history)
        svc.stale.clear(ref.rel_path, [uid])
        stale = await mark_downstream(
            svc,
            ref,
            cells,
            target.index + 1,
            set(old_defs) | set(report.defs),
            by=uid,
            turn=turn,
            reason="upstream-edit",
        )
        svc.drift.clear(ref.rel_path, set(report.defs))

    notices = list(history_notes)
    if note is not None and not wants_note and not retry:  # a retry's note is still the step
        old_bullets = list(nh.get("rationale") or []) or [
            unescape_markdown(line[2:])
            for line in note.source.splitlines()
            if line.startswith("- ")
        ]
        if old_bullets:
            notices.append(
                f'Note unchanged ("{clip(old_bullets[0], 80)}"). Check it still describes the code; '
                "pass notes= to update it."
            )
    retry_note = (
        f" (retry {state.retries.get(uid, 0)} of {cfg['turn']['max_retries']})" if retry else ""
    )
    if revision:
        retry_note = f" (revision {state.revisions.get(uid, 0)} of {cfg['turn']['max_revisions']})"
    return await report_run(
        svc,
        ctx,
        record,
        turn=turn,
        state=state,
        verb="Updated",
        where=_where(svc, ref, retry_note),
        cells=cells,
        stale=stale,
        report=report,
        probe_names=probe_names,
        started_at=started,
        note_text=note_text,
        bullets=bullets if wants_note else [],
        event="cell_edited",
        lead=lead,
        kernel_lines=kernel_lines,
        extra_notices=notices,
        revision=state.revisions.get(uid) if revision else None,
    )
