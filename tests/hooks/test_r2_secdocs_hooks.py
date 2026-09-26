"""Round 2 (V15): the per-message reminder names every status the gateway writes in plain words."""

from __future__ import annotations

import json

import pytest
from hookenv import Sandbox, needs_system_python

pytestmark = needs_system_python


def reminder_for(sandbox: Sandbox, status: str) -> str:
    record = {
        "v": 1,
        "session_id": "sess-1",
        "notebook": "notebooks/01_eda.ipynb",
        "cell_id": "nh-4f2a91c07b",
        "title": "Load raw data and check schema",
        "exec": None,
        "status": status,
        "turn_id": "prompt-0",
        "retries_left": 2,
        "finished_at": None,
    }
    path = sandbox.nh / "state" / "last_cell.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    payload = sandbox.payload("UserPromptSubmit", prompt="go")
    return sandbox.run("UserPromptSubmit", payload).context


@pytest.mark.parametrize(
    ("status", "says"),
    [
        ("queued", "QUEUED behind a running cell; add nothing."),
        ("interrupted", "INTERRUPTED (stopped early; ask before re-running or changing it)."),
        ("deleted", "deleted by the user in JupyterLab (a no; don't re-add it)."),
        ("conflict", "not run (the user typed into it first; ask before running it)."),
        ("lost", "stopped (the kernel restarted or went away)."),
    ],
)
def test_reminder_explains_the_newer_statuses(sandbox: Sandbox, status: str, says: str) -> None:
    text = reminder_for(sandbox, status)
    assert f'Last cell: "Load raw data and check schema" in 01_eda.ipynb: {says}' in text
    assert f": {status}." not in text  # never the bare machine word
