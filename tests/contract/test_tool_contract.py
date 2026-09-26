"""Everything that names an nh tool must agree with the server's real tool list."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "nh"
sys.path.insert(0, str(REPO / "scripts"))

import dump_tools  # noqa: E402

from nh_gateway._shared.tool_defaults import TOOL_DEFAULTS  # noqa: E402

NAMES = {"nh_inspect", "nh_add_cell", "nh_edit_cell", "nh_run", "nh_undo"}


@pytest.fixture(scope="module")
def built() -> tuple[dict, dict]:
    return dump_tools.build()


def test_snapshots_are_current(built: tuple[dict, dict]) -> None:
    snapshot, evals = built
    assert json.loads(dump_tools.SNAPSHOT.read_text()) == snapshot, "run scripts/dump_tools.py"
    assert json.loads(dump_tools.EVAL_TOOLS.read_text()) == evals, "run scripts/dump_tools.py"


def test_defaults_table_matches_schemas(built: tuple[dict, dict]) -> None:
    snapshot, _ = built
    for tool in snapshot["tools"]:
        props = tool["inputSchema"]["properties"]
        declared = {name: spec["default"] for name, spec in props.items() if "default" in spec}
        assert declared == TOOL_DEFAULTS[tool["name"]], tool["name"]


def test_approval_only_on_writes(built: tuple[dict, dict]) -> None:
    snapshot, _ = built
    flagged = {
        n
        for n, m in snapshot["meta"]["approve_on"].items()
        if m.get("anthropic/requiresUserInteraction")
    }
    assert flagged == {"nh_add_cell", "nh_edit_cell"}
    assert not any(
        m.get("anthropic/requiresUserInteraction") for m in snapshot["meta"]["approve_off"].values()
    )


def test_hook_matchers_cover_the_write_tools() -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    stamp = next(
        g["matcher"]
        for g in hooks
        if "pre-tool" in g["hooks"][0]["args"] and "nh" in g["hooks"][0]["args"]
    )
    for name in NAMES:
        full = f"mcp__plugin_nh_nh__{name}"
        assert bool(re.fullmatch(stamp, full)) == (name != "nh_inspect"), name


def test_every_tool_name_mentioned_exists() -> None:
    mentioned: set[str] = set()
    for folder in (
        PLUGIN / "skills",
        PLUGIN / "evals",
        PLUGIN / "hooks",
        REPO / "docs",
        PLUGIN / "README.md",
        REPO / "README.md",
    ):
        files = (
            [folder]
            if folder.is_file()
            else [
                p
                for p in folder.rglob("*")
                if p.is_file() and p.suffix in {".md", ".py", ".json", ".yaml", ".sh"}
            ]
        )
        for path in files:
            mentioned |= set(
                re.findall(
                    r"\bnh_(?:inspect|add_cell|edit_cell|run|undo|[a-z_]+)\b(?=\()",
                    path.read_text(errors="replace"),
                )
            )
            mentioned |= set(
                re.findall(r"mcp__plugin_nh_nh__(nh_[a-z_]+)", path.read_text(errors="replace"))
            )
    tools = {
        m
        for m in mentioned
        if m.startswith("nh_") and not m.startswith("nh_gateway") and not m.startswith("nh_hooks")
    }
    assert tools <= NAMES, tools - NAMES
