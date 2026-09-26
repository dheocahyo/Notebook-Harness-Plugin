"""Per-tool MCP ``_meta`` (stdlib only, shared with any future boot stub)."""

from __future__ import annotations

import os
from pathlib import Path

TOOLS = ("nh_inspect", "nh_add_cell", "nh_edit_cell", "nh_run", "nh_undo")
APPROVAL_TOOLS = ("nh_add_cell", "nh_edit_cell")


def tool_meta(project: Path | None, approve_before_run: bool) -> dict[str, dict]:
    """alwaysLoad only in nh projects; the approval prompt only when harness.toml asks for it."""
    nh_project = project is not None and (project / ".nh").is_dir()
    approve = approve_before_run and os.environ.get("NH_HEADLESS") != "1"
    result: dict[str, dict] = {}
    for tool in TOOLS:
        entry: dict = {}
        if nh_project:
            entry["anthropic/alwaysLoad"] = True
        if approve and tool in APPROVAL_TOOLS:
            entry["anthropic/requiresUserInteraction"] = True
        result[tool] = entry
    return result
