"""Per-turn budgets and notebook locks (plan §4.3)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

from filelock import FileLock, Timeout

from .._shared.paths import Layout, atomic_write_json, read_json
from .errors import NhError

KEEP_TURNS = 5


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


class TurnLedger:
    """Turn states per session, persisted to .nh/state/ledger/<session>.json (last 5 turns)."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self._sessions: dict[str, dict[str, TurnState]] = {}

    def _load(self, session_id: str) -> dict[str, TurnState]:
        if session_id not in self._sessions:
            raw = read_json(self.layout.ledger_file(session_id), default={}) or {}
            turns = {}
            for prompt_id, data in (raw.get("turns") or {}).items():
                try:
                    turns[prompt_id] = TurnState.from_dict(data)
                except TypeError:
                    continue
            self._sessions[session_id] = turns
        return self._sessions[session_id]

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
        turns = self._load(state.session_id)
        turns[state.prompt_id] = state
        keep = sorted(turns.values(), key=lambda t: t.opened_at, reverse=True)[:KEEP_TURNS]
        self._sessions[state.session_id] = {t.prompt_id: t for t in keep}
        atomic_write_json(
            self.layout.ledger_file(state.session_id),
            {"v": 1, "turns": {t.prompt_id: asdict(t) for t in keep}},
        )


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
