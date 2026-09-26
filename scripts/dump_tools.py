"""Regenerate the tool contract files from the live server definition.

    uv run --project plugins/nh/server python scripts/dump_tools.py

Writes plugins/nh/server/tools.snapshot.json (names, schemas, descriptions, _meta for approval
on/off) and plugins/nh/evals/mocks/nh/_tools.json (what eval mocks advertise). CI fails when
either is stale (tests/contract/test_tool_contract.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "nh" / "server" / "src"))

from fastmcp import Client  # noqa: E402

from nh_gateway.app import create_server  # noqa: E402
from nh_gateway.backend.fake import FakeBackend  # noqa: E402

SNAPSHOT = REPO / "plugins" / "nh" / "server" / "tools.snapshot.json"
EVAL_TOOLS = REPO / "plugins" / "nh" / "evals" / "mocks" / "nh" / "_tools.json"


async def _tools(approve: bool) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / ".nh").mkdir()
        (project / "harness.toml").write_text(
            f"[approval]\napprove_before_run = {str(approve).lower()}\n"
        )
        async with Client(create_server(project, FakeBackend(project))) as client:
            listed = await client.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "meta": {k: v for k, v in (tool.meta or {}).items() if k != "fastmcp"},
        }
        for tool in listed
    ]


def build() -> tuple[dict, dict]:
    off, on = asyncio.run(_tools(False)), asyncio.run(_tools(True))
    snapshot = {
        "tools": [{k: v for k, v in tool.items() if k != "meta"} for tool in off],
        "meta": {
            "approve_off": {t["name"]: t["meta"] for t in off},
            "approve_on": {t["name"]: t["meta"] for t in on},
        },
    }
    evals = {"tools": [{k: v for k, v in tool.items() if k != "meta"} for tool in off]}
    return snapshot, evals


def render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    snapshot, evals = build()
    SNAPSHOT.write_text(render(snapshot))
    EVAL_TOOLS.write_text(render(evals))
    print(f"wrote {SNAPSHOT.relative_to(REPO)} and {EVAL_TOOLS.relative_to(REPO)}")
