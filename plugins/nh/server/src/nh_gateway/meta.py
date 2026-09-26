"""Cell ids, source hashes and the metadata nh writes (FR-7)."""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
from typing import Any

META_VERSION = 1
TAG = "nh-agent"
NOTE_SUFFIX = "-n"


def new_code_id(existing: set[str]) -> str:
    while True:
        cid = "nh-" + secrets.token_hex(5)
        if cid not in existing and cid + NOTE_SUFFIX not in existing:
            return cid


def note_id(code_id: str) -> str:
    return code_id + NOTE_SUFFIX


def normalize_source(source: str) -> str:
    return source.replace("\r\n", "\n").rstrip("\n")


def source_sha(source: str) -> str:
    return hashlib.sha256(normalize_source(source).encode("utf-8", "surrogatepass")).hexdigest()[
        :16
    ]


def code_metadata(
    *,
    uid: str,
    intent: str,
    bullets: list[str],
    turn_id: str,
    source: str,
    host: str = "claude-code",
    agent: str | None = None,
) -> dict[str, Any]:
    """``agent``: the subagent that wrote the cell (nh:cell-writer), kept only when set."""
    data: dict[str, Any] = {
        "v": META_VERSION,
        "role": "code",
        "uid": uid,
        "pair_uid": note_id(uid),
        "intent": intent,
        "rationale": list(bullets),
        "created_by": "agent",
        "host": host,
        "turn_id": turn_id,
        "created": _dt.date.today().isoformat(),
        "source_sha": source_sha(source),
        "edits": 0,
    }
    if agent:
        data["agent"] = agent
    return data


def note_metadata(*, uid: str, turn_id: str, source: str) -> dict[str, Any]:
    return {
        "v": META_VERSION,
        "role": "note",
        "uid": note_id(uid),
        "pair_uid": uid,
        "turn_id": turn_id,
        "source_sha": source_sha(source),  # detects the user's own edits to the note
    }


def nh_meta(metadata: dict[str, Any] | None) -> dict[str, Any]:
    value = (metadata or {}).get("nh")
    return value if isinstance(value, dict) else {}


def is_agent_code(metadata: dict[str, Any] | None) -> bool:
    return nh_meta(metadata).get("role") == "code"


def is_note(metadata: dict[str, Any] | None) -> bool:
    return nh_meta(metadata).get("role") == "note"


def is_agent_authored(metadata: dict[str, Any] | None) -> bool:
    """nh wrote this code cell (a user's cell that nh later edited stays the user's)."""
    nh = nh_meta(metadata)
    return nh.get("role") == "code" and nh.get("created_by") != "human"


def note_edited(metadata: dict[str, Any] | None, source: str) -> bool:
    """The user changed a note nh wrote (notes written before v0.1's sha are trusted)."""
    sha = nh_meta(metadata).get("source_sha")
    return bool(sha) and sha != source_sha(source)
