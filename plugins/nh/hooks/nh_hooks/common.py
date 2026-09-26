"""Hook I/O and settings shared by the nh hooks (stdlib only, Python >= 3.9)."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from nh_gateway._shared import tomlread
from nh_gateway._shared.paths import Layout

PLUGIN_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..")
)

Payload = dict[str, Any]


def read_payload() -> Payload:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def emit(output: Payload | None) -> None:
    """Print a hook result; ASCII-only JSON is safe whatever the locale."""
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
        sys.stdout.flush()


def deny(reason: str) -> Payload:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def context(event: str, text: str) -> Payload:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def settings(layout: Layout) -> dict[str, Any]:
    """harness.toml as a dict; missing or unreadable means defaults everywhere."""
    data = tomlread.load(layout.harness_toml)
    return data if isinstance(data, dict) else {}


def setting(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    table = data.get(section)
    if not isinstance(table, dict):
        return default
    value = table.get(key, default)
    return value if isinstance(value, type(default)) else default


def text_field(payload: Payload, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""
