"""What the gateway's config, nhctl and the hooks share about harness.toml's shape.

Python 3.9, stdlib only: the hooks and nhctl run on the system Python (design §6.9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Top-level sections accepted and ignored: reserved for later versions. "preset" left it in
# v0.2 (design §6.9); config.py and nhctl's doctor both read this one definition.
RESERVED_SECTIONS = frozenset({"guardrails", "secrets", "libraries", "comprehension"})

PRESET_LEVELS = ("junior", "senior")
DEFAULT_LEVEL = "junior"
# The keys each preset sets above the packaged defaults and below the user's own keys.
PRESET_OVERLAY: dict[str, dict[str, dict[str, Any]]] = {
    "junior": {"lint": {"comment_ratio": 8}},
    "senior": {"lint": {"comment_ratio": 16}},
}


def preset_level(data: Mapping[str, Any]) -> tuple[str, bool]:
    """``(level, valid)`` from a parsed harness.toml: no ``[preset] level`` is junior and valid;
    any value but "junior" or "senior" (a number, "", "Senior", a ``preset`` that isn't a table)
    reads as junior and isn't valid."""
    if "preset" not in data:
        return DEFAULT_LEVEL, True
    table = data["preset"]
    if not isinstance(table, dict):
        return DEFAULT_LEVEL, False
    if "level" not in table:
        return DEFAULT_LEVEL, True
    value = table["level"]
    if isinstance(value, str) and value in PRESET_LEVELS:
        return value, True
    return DEFAULT_LEVEL, False


def comment_ratio(data: Mapping[str, Any]) -> tuple[Any, str]:
    """``(ratio, set_by)``: the ``[lint] comment_ratio`` the gateway's config uses with no NH_*
    variable. An explicit number in harness.toml (an int or float, not a bool, as
    ``config._merge`` takes it) wins over the preset's."""
    lint = data.get("lint")
    value = lint.get("comment_ratio") if isinstance(lint, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value, "harness.toml"
    level, _ = preset_level(data)
    return PRESET_OVERLAY[level]["lint"]["comment_ratio"], "preset"
