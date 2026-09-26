"""Round-2 fix: waiting on a long cell lists the variables it made (verification item V24)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.fakes.turns import text
from tests.gateway.test_gateway import LOAD
from tests.gateway.test_review_fixes import code_cells, harness_with


async def test_wait_lists_new_variables(tmp_path: Path) -> None:
    h, client = await harness_with(tmp_path, "[exec]\nsoft_timeout_s = 5\n", exec_delay_s=6)
    try:
        h.turns.prompt("p1")
        code = "slope = 2.5\nintercept = 1.0\nslope"
        first = await h.call("nh_add_cell", "p1", **dict(LOAD, code=code))
        assert "still running" in text(first)
        await asyncio.sleep(1.5)
        uid = code_cells(h)[0]["id"]
        waited = text(await h.call("nh_run", "p1", cell_id=uid, mode="wait"))
        assert "ran ok" in waited
        assert "slope: new float 2.5" in waited and "intercept: new float 1.0" in waited
    finally:
        await client.__aexit__(None, None, None)
