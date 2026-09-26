"""Undo history (FR-6): append-only JSONL per cell in .nh/history/<uid>.jsonl, plus _ops.jsonl.

Every change nh makes to a cell is an op. Same-turn retries fold into the turn's op (plan §4.7):
undoing an insert that needed retries deletes the cell, and undoing an edit restores the source
from before the turn. Records are only appended; the newest record of an op wins, and an undo
record marks its op as undone. Several gateway processes may append to the same files.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import NoneType
from typing import Any, Literal

from . import meta
from ._shared.paths import Layout, append_jsonl, safe_name

HISTORY_VERSION = 1
OP_KINDS = ("insert", "edit")
INDEX_NAME = "_ops.jsonl"

# Every record starts with these two keys, so a reader can find a record glued to a torn line.
_RECORD_START = re.compile(r'\{"v":\d+,"kind":"')
_DECODER = json.JSONDecoder()


@dataclass
class Op:
    op_id: str
    op: Literal["insert", "edit"]
    ts: float  # when the op was created; folding keeps it
    notebook: str
    session_id: str
    turn_id: str
    uid: str
    note_uid: str | None
    index: int
    before: dict | None  # edit: {"source","nh","tags","note_source","note_nh","execution_count"}
    after_sha: str  # source_sha after the last attempt in the turn
    defs: list[str]  # names bound by any attempt: the kernel may still hold all of them
    attempts: int
    undone: bool = False
    title: str | None = None  # the note title when nh wrote the cell (names it after a delete)
    after_source: str | None = None  # the code nh last wrote (to show the user's changes as a diff)


_OP_TYPES: dict[str, type | tuple[type, ...]] = {
    "op_id": str,
    "op": str,
    "ts": (int, float),
    "notebook": str,
    "session_id": str,
    "turn_id": str,
    "uid": str,
    "note_uid": (str, NoneType),
    "index": int,
    "before": (dict, NoneType),
    "after_sha": str,
    "defs": list,
    "attempts": int,
}


class HistoryStore:
    def __init__(self, layout: Layout) -> None:
        self.dir = layout.history
        self.index_path = self.dir / INDEX_NAME

    def record(
        self,
        *,
        op: str,
        notebook: str,
        session_id: str,
        turn_id: str,
        uid: str,
        note_uid: str | None,
        index: int,
        before: dict | None,
        after_source: str,
        defs: set[str],
        title: str | None = None,
    ) -> Op:
        """Record a write that landed in the notebook.

        A second write to ``uid`` in the same turn folds into the turn's op: the op id, kind and
        ``before`` stay, while ``after_sha``, ``defs``, ``index`` and ``attempts`` are updated.
        Raises OSError when the history can't be written (the caller warns that undo is off).
        """
        if op not in OP_KINDS:
            raise ValueError(f"unknown history op {op!r}")
        kind: Literal["insert", "edit"] = "edit" if op == "edit" else "insert"
        if (kind == "edit") != (before is not None):
            raise ValueError("an edit records the cell's previous state; an insert records none")
        after_sha = meta.source_sha(after_source)
        current = next(
            (
                o
                for o in reversed(self._load(uid))
                if o.notebook == notebook and o.turn_id == turn_id and not o.undone
            ),
            None,
        )
        if current is not None:
            folded = replace(
                current,
                index=index,
                note_uid=current.note_uid or note_uid,
                after_sha=after_sha,
                defs=sorted(set(current.defs) | set(defs)),
                attempts=current.attempts + 1,
                title=title or current.title,
                after_source=after_source,
            )
            self._append_op(folded)
            return folded

        now = time.time()
        new = Op(
            op_id=_new_op_id(now),
            op=kind,
            ts=now,
            notebook=notebook,
            session_id=session_id,
            turn_id=turn_id,
            uid=uid,
            note_uid=note_uid,
            index=index,
            before=before,
            after_sha=after_sha,
            defs=sorted(defs),
            attempts=1,
            title=title,
            after_source=after_source,
        )
        self._append_op(new)
        append_jsonl(
            self.index_path,
            {
                "v": HISTORY_VERSION,
                "kind": "index",
                "op_id": new.op_id,
                "op": new.op,
                "uid": uid,
                "notebook": notebook,
                "session_id": session_id,
                "turn_id": turn_id,
                "ts": now,
            },
        )
        return new

    def last_ops(self, notebook: str, session_id: str, turn_ids: list[str]) -> list[Op]:
        """Ops of this notebook and session from ``turn_ids`` that are not undone, newest first."""
        wanted = set(turn_ids)
        position: dict[str, int] = {}
        uids: dict[str, None] = {}
        for rec in _read_records(self.index_path):
            uid, turn_id, op_id = rec.get("uid"), rec.get("turn_id"), rec.get("op_id")
            if (
                rec.get("kind") == "index"
                and rec.get("notebook") == notebook
                and rec.get("session_id") == session_id
                and isinstance(turn_id, str)
                and turn_id in wanted
                and isinstance(uid, str)
                and isinstance(op_id, str)
            ):
                position.setdefault(op_id, len(position))
                uids[uid] = None
        found = [
            op
            for uid in uids
            for op in self._load(uid)
            if op.notebook == notebook
            and op.session_id == session_id
            and op.turn_id in wanted
            and not op.undone
        ]
        found.sort(key=lambda op: (op.ts, position.get(op.op_id, -1)), reverse=True)
        return found

    def ops_for(self, uid: str) -> list[Op]:
        """Every op recorded for ``uid`` in creation order (oldest first), undone ones included.

        Copying a cell between notebooks copies its uid, so check ``Op.notebook``.
        """
        return self._load(uid)

    def mark_undone(self, op: Op, *, turn_id: str) -> None:
        """Record that ``op`` was undone in ``turn_id``; it then never folds or undoes again."""
        append_jsonl(
            self._file(op.uid),
            {
                "v": HISTORY_VERSION,
                "kind": "undo",
                "of": op.op_id,
                "uid": op.uid,
                "notebook": op.notebook,
                "turn_id": turn_id,
                "ts": time.time(),
            },
        )
        op.undone = True

    def _file(self, uid: str) -> Path:
        return self.dir / f"{safe_name(uid)}.jsonl"

    def _append_op(self, op: Op) -> None:
        record = {"v": HISTORY_VERSION, "kind": "op", **asdict(op)}
        del record["undone"]  # derived from undo records
        append_jsonl(self._file(op.uid), record)

    def _load(self, uid: str) -> list[Op]:
        """Ops of ``uid`` in creation order, each as its newest record says."""
        ops: dict[str, Op] = {}
        undone: set[str] = set()
        for rec in _read_records(self._file(uid)):
            if rec.get("uid") != uid:  # safe_name() can map two uids to one file
                continue
            kind = rec.get("kind")
            if kind == "op":
                op = _op_from(rec)
                if op is not None:
                    ops[op.op_id] = op  # an update keeps the op's first position
            elif kind == "undo" and isinstance(rec.get("of"), str):
                undone.add(rec["of"])
        for op in ops.values():
            op.undone = op.op_id in undone
        return list(ops.values())


def _new_op_id(ts: float) -> str:
    return f"op-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(ts))}-{secrets.token_hex(4)}"


def _op_from(rec: dict[str, Any]) -> Op | None:
    if any(not isinstance(rec.get(key), kind) for key, kind in _OP_TYPES.items()):
        return None
    if rec["op"] not in OP_KINDS:
        return None
    fields: dict[str, Any] = {key: rec.get(key) for key in _OP_TYPES}
    for optional in ("title", "after_source"):
        value = rec.get(optional)
        fields[optional] = value if isinstance(value, str) else None
    fields["ts"] = float(rec["ts"])
    fields["defs"] = [name for name in rec["defs"] if isinstance(name, str)]
    return Op(**fields)


def _read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Records of a JSONL file. Corrupt lines are skipped; a missing file has none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    for line in text.split("\n"):  # not splitlines(): U+2028 inside a string is not a newline
        yield from _decode(line)


def _decode(line: str) -> Iterator[dict[str, Any]]:
    """Records in one line: normally one, more if a failed write left a line without its newline."""
    pos = 0
    while (match := _RECORD_START.search(line, pos)) is not None:
        try:
            value, pos = _DECODER.raw_decode(line, match.start())
        except ValueError:
            pos = match.start() + 1
            continue
        if isinstance(value, dict):
            yield value
