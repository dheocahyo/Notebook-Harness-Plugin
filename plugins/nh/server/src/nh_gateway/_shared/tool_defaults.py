"""Default values of every nh tool parameter.

The PreToolUse hook and the gateway both drop arguments equal to these defaults
before hashing, so a model that spells out a default and one that omits it
produce the same stamp key. tests/contract checks this table against tools/list.
"""

from __future__ import annotations

TOOL_DEFAULTS = {
    "nh_inspect": {
        "view": "overview",
        "name": None,
        "cell_id": None,
        "rows": None,
        "notebook": None,
    },
    "nh_add_cell": {"after_cell_id": None, "notebook": None},
    "nh_edit_cell": {
        "base_sha": None,
        "title": None,
        "notes": None,
        "intent": None,
        "notebook": None,
    },
    "nh_run": {"mode": "run", "notebook": None},
    "nh_undo": {"cell_id": None, "force": False, "notebook": None},
}

WRITE_TOOLS = ("nh_add_cell", "nh_edit_cell", "nh_run", "nh_undo")
