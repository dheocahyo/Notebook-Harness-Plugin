"""FR-12 cell approvals (design §6.4): a cell the lint asks about waits for the user's yes.

The gate runs inside ``svc.locks.hold``. A cell with asks is refused with E122 and its question
recorded as the session's pending question (a sha256 key, never text); the user's yes in the
next message lets exactly that call through once.
"""

from __future__ import annotations

from .. import config
from .._shared import secrets, turn_record
from ..lint.lint import Issue
from ..policy.errors import RETURN_TO_WORKFLOW, NhError
from ..policy.turn import TurnContext, grant, pending_key
from .common import Services

KIND = "cell"
ASK_NEXT = "Ask the user, then stop: '{question}'. After a yes, send the same call again."
WAITING_LINE = (
    "- nh is already waiting for the user's answer to this message's first question; it asks "
    "one at a time."
)
WAITING_NEXT = (
    "Ask the user only nh's first question of this message, then stop; write nothing more "
    "until they answer."
)
HELD_LINE = (
    "- The user's yes in this message is for the other cell nh asked about, and it covers only "
    "that exact call."
)
HELD_NEXT = (
    "Send the call the user said yes to first, exactly as before (same tool, cell and code); "
    "propose this cell in your reply instead."
)
HEADLESS_NEXT = (
    "No one can answer here (NH_HEADLESS=1): write nothing, and tell the user this cell needs "
    "their yes in an interactive session: '{question}'"
)
WRITER_QUESTION = "- The main conversation asks the user: '{question}'"
WRITER_HEADLESS = (
    "- No one can answer here (NH_HEADLESS=1); the cell needs the user's yes in an interactive "
    "session: '{question}'"
)


def normalized(code: str) -> str:
    """The code as the key reads it: ``\\n`` line endings, no trailing whitespace on a line, no
    blank lines at the start or end. Every other character counts."""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def cell_key(notebook: str, target: str, code: str) -> str:
    """The pending key of "this exact cell": the notebook, ``add`` or ``edit:<uid>``, and the
    normalised code. Title, notes, intent and position don't count."""
    return pending_key(f"{KIND}\n{notebook}\n{target}\n{normalized(code)}")


def question(asks: list[Issue]) -> str:
    """One question for the user, naming what the cell does, redacted (design §6.8). Only an
    ask rule asks (``config.ASK_RULES``), and each has its clause; one without (a hand-built
    ``Config``) still reads as a sentence."""
    clauses: list[str] = []
    for issue in asks:
        clause = issue.question or f"trips nh's rule {issue.rule} ({issue.message.rstrip('.')})"
        if clause not in clauses:
            clauses.append(clause)
    text = f"This cell {'; it also '.join(clauses)}. Run it as it is?"
    return secrets.current().redact(text)


def _refusal(asks: list[Issue], extra: str, next_step: str) -> NhError:
    lines = [f"- {issue.rule}: {issue.message}" for issue in asks]
    if extra:
        lines.append(extra)
    return NhError("E122", "\n".join(lines), next_step=next_step)


def gate_cell(svc: Services, turn: TurnContext, key: str, asks: list[Issue]) -> None:
    """Let the call through when the user granted this exact cell; otherwise ask (E122).

    Must run inside ``svc.locks.hold``: the grant check, its use and the pending write are one
    step per message (design §6.4)."""
    if not asks:
        return
    ask = question(asks)
    rules = [issue.rule for issue in asks]
    writer = turn.agent_type == turn_record.WRITER_AGENT

    def refuse(outcome: str) -> NhError:
        svc.events.emit(
            "cell_asked",
            session_id=turn.session_id,
            turn_id=turn.prompt_id,
            rules=rules,
            outcome=outcome,
        )
        if outcome == "headless":
            extra = WRITER_HEADLESS if writer else ""
            main = HEADLESS_NEXT
        elif outcome == "waiting":
            extra, main = WAITING_LINE, WAITING_NEXT
        elif outcome == "held":
            extra, main = HELD_LINE, HELD_NEXT
        else:
            extra = WRITER_QUESTION if writer else ""
            main = ASK_NEXT
        return _refusal(
            asks,
            extra.format(question=ask),
            RETURN_TO_WORKFLOW if writer else main.format(question=ask),
        )

    if config.headless():  # no yes can arrive: fail closed, and leave the ledger alone
        raise refuse("headless")
    session = turn.session_id
    record = turn_record.read(svc.layout, session)
    if svc.ledger.use_grant(session, record, turn.prompt_id, KIND, key):
        svc.events.emit("cell_granted", session_id=session, turn_id=turn.prompt_id, rules=rules)
        return
    current = svc.ledger.pending(session)
    if current is not None and current["turn_id"] == turn.prompt_id:
        same = current["kind"] == KIND and current["key"] == key
        raise refuse("repeated" if same else "waiting")
    if (
        current is not None
        and grant(current, record, turn.prompt_id, current["kind"], current["key"])[0]
    ):
        raise refuse("held")  # this message's yes is for the call it approved: keep it
    # set_pending refuses only a second question of this message, caught just above under the
    # same lock, so it records this one.
    svc.ledger.set_pending(session, KIND, key, turn.prompt_id)
    raise refuse("asked")


def wrote(svc: Services, turn: TurnContext) -> None:
    """A cell this message wrote supersedes this message's question (design §6.4)."""
    svc.ledger.clear_pending(turn.session_id, turn.prompt_id)
