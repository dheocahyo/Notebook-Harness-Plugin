"""Per-turn budgets and notebook locks (plan §4.3), and the pending question (design §6.1)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

from filelock import FileLock, Timeout

from .._shared.paths import Layout, atomic_write_json, read_json
from .errors import NhError

KEEP_TURNS = 5
LEDGER_VERSION = 2
PENDING_KINDS = ("cell", "rerun")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TurnContext:
    session_id: str
    prompt_id: str  # the human turn (a background task's alias resolves to it)
    agent_id: str | None = None
    agent_type: str | None = None  # nh:cell-writer inside the nh:qa-cell workflow, else None
    run_id: str | None = None  # the writer's nh:qa-cell run
    permission_mode: str | None = None
    deadline: float = 0.0  # time.monotonic() by which the call must return


CURRENT_TURN: ContextVar[TurnContext | None] = ContextVar("nh_current_turn", default=None)


@dataclass
class TurnState:
    session_id: str
    prompt_id: str
    opened_at: float
    claims: list[str] = field(default_factory=list)  # uids of code cells this turn wrote or re-ran
    claim_notebooks: dict[str, str] = field(default_factory=dict)  # uid -> notebook it is in
    kinds: dict[str, str] = field(default_factory=dict)  # uid -> add | edit | rerun
    status: dict[str, str] = field(default_factory=dict)  # uid -> last execution status
    retries: dict[str, int] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)  # uid -> nh:cell-writer's revisions
    writer_run: str | None = None  # the nh:qa-cell run that wrote first; it owns the turn's cell
    waits: int = 0
    undos: int = 0
    lint_rejects: int = 0
    notebook: str | None = None
    reviewed: bool = False  # earlier nh cells were checked for user edits (acceptance metric)
    # "Already deleted" undo: the cells it suggested undoing next (nh_undo() in the next message),
    # and the notebook they are in.
    undo_next: list[str] = field(default_factory=list)
    undo_next_notebook: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TurnState:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def pending_key(text: str) -> str:
    """A pending question's key: the sha256 hex of ``text`` (never the text itself)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_pending(value: Any) -> dict[str, Any] | None:
    """``value`` rebuilt as ``{"kind", "key", "turn_id", "ts"}``, or None for any other shape."""
    if not isinstance(value, dict):
        return None
    kind, key, turn_id, ts = (value.get(k) for k in ("kind", "key", "turn_id", "ts"))
    if kind not in PENDING_KINDS or not isinstance(key, str) or not _SHA256_HEX.fullmatch(key):
        return None
    if not isinstance(turn_id, str) or not turn_id:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return {"kind": kind, "key": key, "turn_id": turn_id, "ts": float(ts)}


def grant(
    pending: dict[str, Any] | None,
    record: dict[str, Any] | None,
    turn_id: str | None,
    kind: str,
    key: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Whether the user's reply grants the pending question to this call, and what stays
    pending (design §6.1). Pure: ``record`` is the session's turn record and ``turn_id`` the
    call's canonical turn. A grant is used once; any reply but a yes to it drops it."""
    if pending is None:
        return False, None
    if turn_id == pending["turn_id"]:
        return False, pending  # asked this turn: the user hasn't replied yet
    if record is None or not turn_id or turn_id != record.get("turn_id"):
        return False, pending
    if record.get("answer") != "yes" or pending["turn_id"] != record.get("prev_turn_id"):
        return False, None
    if pending["kind"] == kind and pending["key"] == key:
        return True, None
    return False, pending


class TurnLedger:
    """Turn states per session, persisted to .nh/state/ledger/<session>.json (last 5 turns),
    with the session's pending question (``{"v": 2, "turns": {...}, "pending": ...}``)."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self._sessions: dict[str, dict[str, TurnState]] = {}
        self._pending: dict[str, dict[str, Any] | None] = {}

    def _load(self, session_id: str) -> dict[str, TurnState]:
        if session_id not in self._sessions:
            raw = read_json(self.layout.ledger_file(session_id), default={})
            raw = raw if isinstance(raw, dict) else {}
            stored = raw.get("turns")
            turns = {}
            for prompt_id, data in (stored if isinstance(stored, dict) else {}).items():
                try:
                    turns[prompt_id] = TurnState.from_dict(data)
                except (TypeError, AttributeError):
                    continue
            self._sessions[session_id] = turns
            # v1 has no pending question.
            v2 = raw.get("v") == LEDGER_VERSION
            self._pending[session_id] = valid_pending(raw.get("pending")) if v2 else None
        return self._sessions[session_id]

    def _write(self, session_id: str) -> None:
        """Persist the session, keeping only its KEEP_TURNS newest turns (every write, so a
        pending-question write can't grow the file past them either)."""
        keep = sorted(self._load(session_id).values(), key=lambda t: t.opened_at, reverse=True)
        turns = self._sessions[session_id] = {t.prompt_id: t for t in keep[:KEEP_TURNS]}
        atomic_write_json(
            self.layout.ledger_file(session_id),
            {
                "v": LEDGER_VERSION,
                "turns": {prompt_id: asdict(t) for prompt_id, t in turns.items()},
                "pending": self._pending.get(session_id),
            },
        )

    def get(self, session_id: str, prompt_id: str) -> TurnState:
        turns = self._load(session_id)
        if prompt_id not in turns:
            turns[prompt_id] = TurnState(
                session_id=session_id, prompt_id=prompt_id, opened_at=time.time()
            )
        return turns[prompt_id]

    def recent_turn_ids(self, session_id: str, limit: int = 3) -> list[str]:
        turns = self._load(session_id)
        ordered = sorted(turns.values(), key=lambda t: t.opened_at, reverse=True)
        return [t.prompt_id for t in ordered[:limit]]

    def save(self, state: TurnState) -> None:
        self._load(state.session_id)[state.prompt_id] = state
        self._write(state.session_id)

    def pending(self, session_id: str) -> dict[str, Any] | None:
        """The session's pending question (a copy), or None."""
        self._load(session_id)
        current = self._pending.get(session_id)
        return dict(current) if current else None

    def set_pending(
        self, session_id: str, kind: str, key: str, turn_id: str, now: float | None = None
    ) -> bool:
        """Record the question a turn asked. The first ask of a turn wins (False: kept); a
        pending question of an earlier turn is replaced."""
        entry = valid_pending(
            {
                "kind": kind,
                "key": key,
                "turn_id": turn_id,
                "ts": time.time() if now is None else now,
            }
        )
        if entry is None:
            raise ValueError(f"not a pending question: {kind!r} {key!r} {turn_id!r}")
        current = self.pending(session_id)
        if current is not None and current["turn_id"] == turn_id:
            return False
        self._pending[session_id] = entry
        self._write(session_id)
        return True

    def clear_pending(self, session_id: str, turn_id: str | None = None) -> None:
        """Drop the pending question (only the one ``turn_id`` asked, when given)."""
        current = self.pending(session_id)
        if current is None or (turn_id is not None and current["turn_id"] != turn_id):
            return
        self._pending[session_id] = None
        self._write(session_id)

    def use_grant(
        self,
        session_id: str,
        record: dict[str, Any] | None,
        turn_id: str | None,
        kind: str,
        key: str,
    ) -> bool:
        """Apply ``grant()`` to the session's pending question and save what stays pending."""
        current = self.pending(session_id)
        granted, after = grant(current, record, turn_id, kind, key)
        if after != current:
            self._pending[session_id] = after
            self._write(session_id)
        return granted


class NotebookLocks:
    """An asyncio lock per notebook, plus a cross-process file lock for the critical section."""

    def __init__(self, layout: Layout, timeout_s: float = 5.0) -> None:
        self.layout = layout
        self.timeout_s = timeout_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._files: dict[str, FileLock] = {}
        self._turn_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _file_lock(self, notebook_key: str) -> FileLock:
        # Not thread-local: it is acquired in a worker thread and released on the event loop.
        if notebook_key not in self._files:
            self.layout.locks.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(notebook_key.encode("utf-8")).hexdigest()[:12]
            self._files[notebook_key] = FileLock(
                str(self.layout.locks / f"{digest}.lock"), thread_local=False
            )
        return self._files[notebook_key]

    @contextlib.asynccontextmanager
    async def hold(self, notebook_key: str, turn: TurnContext | None = None) -> AsyncIterator[None]:
        """Serialize a write path: first per turn (the one-cell budget spans notebooks), then per
        notebook, in this process and across processes (file lock)."""
        turn_lock = self._turn_lock(turn) if turn is not None else None
        async with contextlib.AsyncExitStack() as stack:
            if turn_lock is not None:
                await stack.enter_async_context(turn_lock)
            await stack.enter_async_context(self._locks.setdefault(notebook_key, asyncio.Lock()))
            flock = self._file_lock(notebook_key)
            await self._acquire(flock)
            try:
                yield
            finally:
                flock.release()

    def _turn_lock(self, turn: TurnContext) -> asyncio.Lock:
        key = (turn.session_id, turn.prompt_id)
        if key not in self._turn_locks:
            if len(self._turn_locks) > 64:  # old turns never write again
                for stale in [k for k, lock in self._turn_locks.items() if not lock.locked()][:32]:
                    del self._turn_locks[stale]
            self._turn_locks[key] = asyncio.Lock()
        return self._turn_locks[key]

    async def _acquire(self, flock: FileLock) -> None:
        """Poll instead of blocking a thread, so a cancelled call can never leave the lock held."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                flock.acquire(timeout=0)
                return
            except Timeout:
                if time.monotonic() >= deadline:
                    raise NhError(
                        "E133", detail=" (another nh session is writing to this notebook)"
                    ) from None
                await asyncio.sleep(0.05)
