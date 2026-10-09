"""The approved batch (design §6.3): FR-10's exception to E110, one of the two (§0).

The user asks for several plan steps ("run the next 3"), nh's question goes to them in chat
(the ask message writes nothing: E109), and their next message's yes lets that message write
up to ``min(n, [turn] max_batch)`` new cells. The record carries the request (``prev_request``),
so nothing is recorded in the ledger for the question. Each step waits for the last one's
result (E133 until it is in). The batch stops at the first result that isn't ok or has a
"check this" section, at any E12x refusal and at an undo; after that, every add, edit, re-run
and undo of the message gets E123.

Everything here runs inside ``svc.locks.hold`` (E125's stop: ``svc.locks.hold_turn``), except
``reported``, which ``report_run`` calls where it already updates the turn's status.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .. import config
from .._shared import turn_record
from .._shared.turn_record import WRITER_AGENT
from ..config import Config
from ..policy.errors import RETURN_TO_WORKFLOW, NhError
from ..policy.turn import TurnContext, TurnState
from .common import Services

# Refusals that stop a going batch (plan D3: "any E12x"); E123 is the stop itself.
STOP_CODES = frozenset({"E120", "E121", "E122", "E124", "E125"})
# Their Next would have the model act again in this message: E120's and E125's fix the code and
# call again, E121's asks the user how to proceed without the batch's report.
_REPLACED_NEXT = frozenset({"E120", "E121", "E125"})
STOP_LINE = (
    "- The approved batch stops here, at step {step} of {total}: nh changes nothing more this "
    "message."
)
REFUSED_NEXT = (
    "Don't call again: the approved batch stops here. Tell the user what each step did, which "
    "step nh refused and why, and what you would change; then wait."
)
# E110 after a full batch: its own first line (the catalogue's says one cell per message).
FULL_HEAD = (
    "Not written (by design): the approved batch's {total} cells are written; the last is {cell}."
)
FULL_LINE = "- The rest of the plan waits for the user's next message."
# E110 for a writer's second cell from one nh:qa-cell run (C6a, until C6b's per-slot runs).
RUN_HEAD = (
    "Not written (by design): one new cell per nh:qa-cell run, and this run already wrote one."
)
# E133 for a step sent before the last step's result is in (a parallel call): nothing stops.
WAITING_DETAIL = " with step {step} of the approved batch, whose result isn't in yet"
WAITING_NEXT = (
    "Wait for step {step}'s result; send this call again only if that result says to go on."
)
# The end of nh_undo's next block when the undo stops a going batch.
UNDONE_NEXT = (
    "The approved batch stops here, at step {step} of {total}: say which planned steps did not run."
)


def max_batch(cfg: Config) -> int:
    return int(cfg["turn"]["max_batch"])


def cap(state: TurnState, cfg: Config) -> int:
    """E110's limit: the approved batch's size, else ``[turn] max_code_cells``."""
    return state.batch_total or int(cfg["turn"]["max_code_cells"])


def going(state: TurnState) -> bool:
    """The message has an approved batch and it hasn't stopped."""
    return bool(state.batch_total) and not state.batch_stop


def _writer(turn: TurnContext) -> bool:
    return turn.agent_type == WRITER_AGENT


def _emit(svc: Services, turn: TurnContext, event: str, **fields: Any) -> None:
    svc.events.emit(event, session_id=turn.session_id, turn_id=turn.prompt_id, **fields)


def decide(svc: Services, turn: TurnContext, state: TurnState) -> None:
    """The message's batch, decided once by its first gated call: ``min(n, max_batch)`` when
    its record answers yes to the previous message's batch request, no question of that
    message is pending (first ask wins: one yes answers one question) and someone can answer
    (not headless); else 0."""
    if state.batch_total is not None:
        return
    record = turn_record.read(svc.layout, turn.session_id)
    n = turn_record.approved_batch(record, turn.prompt_id)
    total, reason = 0, None
    if n is not None and record is not None:
        pending = svc.ledger.pending(turn.session_id)
        if config.headless():
            reason = "headless"
        elif pending is not None and pending["turn_id"] == record.get("prev_turn_id"):
            reason = "pending"
        else:
            total = min(n, max_batch(svc.config()))
    state.batch_total = total
    svc.ledger.save(state)
    if total:
        _emit(svc, turn, "batch_granted", n=n, total=total)
    elif reason:
        _emit(svc, turn, "batch_not_granted", n=n, reason=reason)


def stopped(state: TurnState, turn: TurnContext, verb: str = "written") -> NhError:
    """E123: the batch stopped earlier in this message."""
    return NhError(
        "E123",
        verb=verb,
        step=f"{state.batch_stop} of {state.batch_total}",
        next_step=RETURN_TO_WORKFLOW if _writer(turn) else None,
    )


def refuse_after_stop(state: TurnState, turn: TurnContext, verb: str = "written") -> None:
    """E123 once the message's batch has stopped (nh_undo's check; ``enter`` for the others)."""
    if state.batch_total and state.batch_stop:
        raise stopped(state, turn, verb)


def stop(svc: Services, turn: TurnContext, state: TurnState, step: int, reason: str) -> None:
    """Stop the going batch at ``step`` (kept between 1 and its size)."""
    if not going(state):
        return
    state.batch_stop = max(1, min(int(step), int(state.batch_total or 1)))
    svc.ledger.save(state)
    _emit(svc, turn, "batch_stopped", step=state.batch_stop, total=state.batch_total, reason=reason)


def waiting(turn: TurnContext, step: int) -> NhError:
    """E133 for a call sent while step ``step``'s result isn't in yet: nothing is written and
    the batch goes on; that result decides whether the call may come again."""
    return NhError(
        "E133",
        detail=WAITING_DETAIL.format(step=step),
        next_step=RETURN_TO_WORKFLOW if _writer(turn) else WAITING_NEXT.format(step=step),
    )


def enter(
    svc: Services,
    turn: TurnContext,
    state: TurnState,
    *,
    new_cell: bool,
    verb: str = "written",
) -> None:
    """A gated write's first step, inside the lock: decide the batch, refuse after a stop
    (E123), wait for a step whose result isn't in yet (E133), and let a new cell in only after
    every earlier step ran OK (``new_cell``; a full batch's next one goes on to E110).

    A going batch's claimed cell reads ``running`` until ``reported`` has its status and
    "check this" (``record_finish`` leaves it alone), so a call sent in parallel can't get
    ahead of a step's check."""
    decide(svc, turn, state)
    if not state.batch_total:
        return
    refuse_after_stop(state, turn, verb)
    if new_cell and len(state.claims) >= state.batch_total:
        return
    for index, uid in enumerate(state.claims):
        if state.status.get(uid) == "running":
            raise waiting(turn, index + 1)
    if new_cell:
        for index, uid in enumerate(state.claims):
            if state.status.get(uid) != "ok":
                stop(svc, turn, state, index + 1, "not_ok")
                raise stopped(state, turn, verb)


def full(state: TurnState, turn: TurnContext, cell: str) -> NhError:
    """E110 after the batch's last cell: the batch's own first line, then E110's Next."""
    return NhError(
        "E110",
        FULL_LINE,
        head=FULL_HEAD.format(total=state.batch_total, cell="{cell}"),
        cell=cell,
        next_step=RETURN_TO_WORKFLOW if _writer(turn) else None,
    )


def refuse_second_cell(state: TurnState, turn: TurnContext) -> None:
    """In a batch, one nh:qa-cell run writes one cell, as the cap of 1 kept it before (C6a;
    C6b's per-slot runs replace this). Another run is ``refuse_other_run``'s."""
    if state.batch_total and _writer(turn) and turn.run_id and state.writer_run == turn.run_id:
        raise NhError("E110", head=RUN_HEAD, next_step=RETURN_TO_WORKFLOW)


def attempt_step(state: TurnState, uid: str | None = None) -> int:
    """The step a call would write: its target's place among this message's cells, else the
    next one."""
    if uid is not None and uid in state.claims:
        return state.claims.index(uid) + 1
    return len(state.claims) + 1


def refused(
    svc: Services, turn: TurnContext, state: TurnState, error: NhError, step: int
) -> NhError:
    """An E12x refusal in a going batch stops it at ``step`` and says so; its Next no longer
    has the model act again. Any other refusal passes unchanged."""
    if error.code not in STOP_CODES or not going(state):
        return error
    stop(svc, turn, state, step, error.code)
    head, sep, next_step = str(error).rpartition("\nNext: ")
    if not sep:
        return error
    if error.code in _REPLACED_NEXT:
        next_step = RETURN_TO_WORKFLOW if _writer(turn) else REFUSED_NEXT
    line = STOP_LINE.format(step=state.batch_stop, total=state.batch_total)
    error.args = (f"{head}\n{line}{sep}{next_step}",)
    return error


@dataclass
class Attempt:
    """What a gated write is about to change: ``uid``, its target cell once known (an edit or
    re-run); None for a new cell."""

    uid: str | None = None


@contextlib.asynccontextmanager
async def stops(svc: Services, turn: TurnContext) -> AsyncIterator[Attempt]:
    """Around a gated write's checks, inside its lock: an E12x refusal raised in them stops
    the message's going batch (``refused``) at the step the call would have written."""
    attempt = Attempt()
    try:
        yield attempt
    except NhError as exc:
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        refused(svc, turn, state, exc, attempt_step(state, attempt.uid))
        raise


async def refused_early(
    svc: Services, turn: TurnContext, error: NhError, uid: str | None = None
) -> NhError:
    """The refusal to raise for one found before the notebook is resolved (E125, design
    §6.8), under the turn's own lock: it may be the message's first gated call (it decides the
    batch), it stops a going batch (``refused``; ``uid``: an edit's target, as named, when it is
    one of this message's cells), and after a stop it is E123 like every other call."""
    if error.code not in STOP_CODES:
        return error
    async with svc.locks.hold_turn(turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        decide(svc, turn, state)
        if state.batch_total and state.batch_stop:
            return stopped(state, turn)
        return refused(svc, turn, state, error, attempt_step(state, uid))


def undone(svc: Services, turn: TurnContext, state: TurnState, uid: str) -> str:
    """An undo in a going batch stops it at the undone cell's step (the next one, for a cell
    this message didn't write): the steps after it would build on a notebook without it.
    Returns the sentence for the undo's next block, or ""."""
    if not going(state):
        return ""
    stop(svc, turn, state, attempt_step(state, uid), "undo")
    return UNDONE_NEXT.format(step=state.batch_stop, total=state.batch_total)


def reported(
    svc: Services,
    turn: TurnContext,
    state: TurnState,
    uid: str,
    status: str,
    check_this: list[str],
) -> tuple[int, int, int] | None:
    """A result in a message with a batch: the cell's status and, in a going batch, its stop
    are set together, once its "check this" is known, so a parallel call never sees the step
    OK before that (``report_run`` and ``record_finish`` leave the status to this while the
    batch goes). Returns ``(step, total, stopped_at)`` for the next block: ``step`` is the
    cell's place among this message's cells, 0 for one it didn't write (an earlier message's
    cell it waited for), which gets the plain block while the batch goes (None)."""
    if not state.batch_total:
        return None
    state.status[uid] = status
    svc.ledger.save(state)
    step = state.claims.index(uid) + 1 if uid in state.claims else 0
    if going(state) and (status != "ok" or check_this):
        stop(
            svc, turn, state, step or len(state.claims), "check_this" if status == "ok" else status
        )
    if not step and not state.batch_stop:
        return None
    return step, int(state.batch_total), state.batch_stop
