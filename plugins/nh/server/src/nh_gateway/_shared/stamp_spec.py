"""Turn stamps: how a PreToolUse hook tells the gateway which user prompt a call belongs to.

The hook writes ``.nh/state/stamps/<key>--<tool_use_id>.json``; the gateway
recomputes ``key`` from the arguments it receives and claims the file.
Both sides import this module, so the key function cannot drift.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .tool_defaults import TOOL_DEFAULTS

TOOL_PREFIX = "mcp__plugin_nh_nh__"
STAMP_VERSION = 1


def bare_tool_name(name: str) -> str:
    return name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name


def canonical_args(tool: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Drop nulls and values equal to the tool's documented default."""
    defaults = TOOL_DEFAULTS.get(tool, {})
    result = {}
    for key, value in (args or {}).items():
        if value is None:
            continue
        if key in defaults and defaults[key] == value:
            continue
        result[key] = value
    return result


def stamp_key(tool: str, args: dict[str, Any] | None) -> str:
    tool = bare_tool_name(tool)
    raw = json.dumps(
        {"tool": tool, "input": canonical_args(tool, args)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def stamp_filename(key: str, tool_use_id: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (tool_use_id or "none"))
    return f"{key}--{safe_id[:80]}.json"
