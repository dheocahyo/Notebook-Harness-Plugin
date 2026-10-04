"""The approved batch (design §6.3): FR-10's exception to E110, one of the two (§0).

The user asks for several plan steps ("run the next 3"), nh's question goes to them in chat
(the ask message writes nothing: E109), and their next message's yes lets that message write
up to ``min(n, [turn] max_batch)`` new cells. The record carries the request (``prev_request``),
so nothing is recorded in the ledger for the question. The batch stops at the first result
that isn't ok or has a "check this" section, and at any E12x refusal; after that, every add,
edit and re-run of the message gets E123.

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
# Their Next says to fix the code and call again: in a batch nothing is called again.
_CALL_AGAIN = frozenset({"E120", "E121", "E125"})
STOP_LINE = (
    "- The approved batch stops here, at step {step} of {total}: nh changes nothing more this "
    "message."
)
REFUSED_NEXT = (
    "Don't call again: the approved batch stops here. Tell the user what each step did, which "
    "step nh refused and why, and what you would change; then wait."
)
FULL_LINE = (
    "- The approved batch's {total} steps are written; the rest of the plan waits for the "
    "user's next message."
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


def stop(svc: Services, turn: TurnContext, state: TurnState, step: int, reason: str) -> None:
    """Stop the going batch at ``step`` (kept between 1 and its size)."""
    if not going(state):
        return
    state.batch_stop = max(1, min(int(step), int(state.batch_total or 1)))
    svc.ledger.save(state)
    _emit(svc, turn, "batch_stopped", step=state.batch_stop, total=state.batch_total, reason=reason)


def enter(
    svc: Services,
    turn: TurnContext,
    state: TurnState,
    *,
    new_cell: bool,
    verb: str = "written",
) -> None:
    """A gated write's first step, inside the lock: decide the batch, refuse after a stop
    (E123), and let a new cell in only after every earlier step ran OK (``new_cell``)."""
    decide(svc, turn, state)
    if not state.batch_total:
        return
    if state.batch_stop:
        raise stopped(state, turn, verb)
    if new_cell:
        for index, uid in enumerate(state.claims):
            if state.status.get(uid) != "ok":
                stop(svc, turn, state, index + 1, "not_ok")
                raise stopped(state, turn, verb)


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
    says to call again. Any other refusal passes unchanged."""
    if error.code not in STOP_CODES or not going(state):
        return error
    stop(svc, turn, state, step, error.code)
    head, sep, next_step = str(error).rpartition("\nNext: ")
    if not sep:
        return error
    if error.code in _CALL_AGAIN:
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


async def refused_early(svc: Services, turn: TurnContext, error: NhError) -> None:
    """``refused`` for a refusal raised before the notebook is resolved (E125, design §6.8),
    under the turn's own lock: it may also be the message's first gated call."""
    if error.code not in STOP_CODES:
        return
    async with svc.locks.hold_turn(turn):
        state = svc.ledger.get(turn.session_id, turn.prompt_id)
        decide(svc, turn, state)
        refused(svc, turn, state, error, attempt_step(state))


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
    OK before that (``report_run`` leaves the status to this while the batch goes). Returns
    ``(step, total, stopped_at)`` for the next block when this message wrote the cell, else
    None."""
    if not state.batch_total:
        return None
    state.status[uid] = status
    svc.ledger.save(state)
    if going(state) and (status != "ok" or check_this):
        at = state.claims.index(uid) + 1 if uid in state.claims else len(state.claims)
        stop(svc, turn, state, at, "check_this" if status == "ok" else status)
    if uid not in state.claims:
        return None
    return state.claims.index(uid) + 1, int(state.batch_total), state.batch_stop
