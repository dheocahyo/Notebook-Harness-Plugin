"""End to end: the MCP gateway with RtcBackend against a real JupyterLab, stamped by the real hooks.

Scenario (plan §10 e2e a): load a CSV, drop nulls, a KeyError fixed by one retry, a plot, then undo.
The saved notebook must hold one nh code cell per turn, each with a valid note and metadata, and it
must run top to bottom in a fresh kernel (nhctl fresh-run). nhctl metrics must agree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import nbformat
import pytest
from fastmcp import Client

from nh_gateway._shared.text import count_words
from nh_gateway.app import create_server
from tests.fakes.turns import PLUGIN, Turns, text

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

NB = "notebooks/01_eda.ipynb"
CSV = "region,price\n" + "\n".join(
    f"{region},{price}"
    for region, price in [
        ("north", 10.5),
        ("south", ""),
        ("north", 12.0),
        ("east", 9.5),
        ("south", 11.0),
        ("east", ""),
        ("north", 13.5),
    ]
)


def nhctl(project: Path, *args: str) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(project)),
        "NH_PYTHON": "/usr/bin/python3",
        "CLAUDE_PROJECT_DIR": str(project),
    }
    proc = subprocess.run(
        ["/bin/sh", str(PLUGIN / "bin" / "nhctl"), *args, "--json"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.stdout.strip(), proc.stderr
    return json.loads(proc.stdout)


async def test_eda_scenario(project: Path, backend, helpers, tmp_path: Path) -> None:
    (project / "harness.toml").write_text(f'version = 1\n[project]\nnotebook = "{NB}"\n')
    (project / "data").mkdir()
    (project / "data" / "sales.csv").write_text(CSV + "\n")
    # nhctl fresh-run reads the project env from env.json; the test venv has nbclient + ipykernel.
    (project / ".nh" / "state" / "env.json").write_text(
        json.dumps({"manager": "uv", "prefix": sys.prefix})
    )

    turns = Turns(project, tmp_path / "data")
    async with (
        Client(create_server(project, backend)) as client,
        helpers.observer(_api_path(project)) as user,
    ):

        async def call(tool: str, prompt: str, **args):
            result = await turns.call(client, tool, args, prompt)
            assert not result.is_error, text(result)
            return text(result)

        turns.prompt("t1")
        first = await call("nh_inspect", "t1", view="status")
        assert "collaboration" in first

        body = await call(
            "nh_add_cell",
            "t1",
            title="Load the sales data",
            notes=[
                "Reads data/sales.csv into a frame.",
                "Prints the shape so we can see its size.",
            ],
            intent="load the sales csv",
            code="import pandas as pd\n\nDATA_PATH = '../data/sales.csv'\ndf = pd.read_csv(DATA_PATH)\ndf.shape",
        )
        assert "ran ok" in body and "(7, 2)" in body
        live = await helpers.eventually(
            lambda: [c for c in helpers.cells_of(user) if c.get("metadata", {}).get("nh")]
        )
        assert {c["cell_type"] for c in live} == {"markdown", "code"}

        turns.prompt("t2")
        body = await call(
            "nh_add_cell",
            "t2",
            title="Drop rows with missing price",
            notes=[
                "Keeps rows that have a price.",
                "Price is what we analyse, so blanks are useless.",
            ],
            intent="drop rows without a price",
            code="df_clean = df.dropna(subset=['price'])\ndf_clean.shape",
        )
        assert "(5, 2)" in body

        turns.prompt("t3")
        body = await call(
            "nh_add_cell",
            "t3",
            title="Mean price by region",
            notes=["Averages price within each region.", "Sorted so the highest region is first."],
            intent="mean price per region",
            code="mean_price = df_clean.groupby('region')['prce'].mean()\nmean_price",
        )
        assert "it failed with KeyError" in body
        uid = json.loads((project / ".nh" / "state" / "last_cell.json").read_text())["cell_id"]
        body = await call(
            "nh_edit_cell",
            "t3",
            cell_id=uid,
            code="mean_price = df_clean.groupby('region')['price'].mean().sort_values(ascending=False)\nmean_price",
        )
        assert "ran ok" in body and "(retry 1 of 2)" in body

        turns.prompt("t4")
        result = await turns.call(
            client,
            "nh_add_cell",
            dict(
                title="Plot mean price by region",
                notes=[
                    "Bar chart of the averages above.",
                    "Makes the gap between regions easy to see.",
                ],
                intent="plot it",
                code="import matplotlib.pyplot as plt\n\n"
                "ax = mean_price.plot.bar(title='Mean price by region')\nax.set_ylabel('price')\nplt.show()",
            ),
            "t4",
        )
        assert not result.is_error, text(result)
        assert any(getattr(part, "type", "") == "image" for part in result.content)

        turns.prompt("t5")
        body = await call("nh_undo", "t5")
        assert 'Removed "Plot mean price by region"' in body  # after any "Kernel ≠ notebook" lead

    # The saved file: wait past the save delay, then check it on disk.
    def saved():
        data = helpers.read_disk(project / NB)
        code = [c for c in (data or {}).get("cells", []) if c["cell_type"] == "code"]
        return data if len(code) == 3 else None

    notebook = await helpers.eventually(saved, timeout=15)
    nbformat.validate(nbformat.from_dict(notebook))
    agent_code = [c for c in notebook["cells"] if c["metadata"].get("nh", {}).get("role") == "code"]
    assert [c["metadata"]["nh"]["turn_id"] for c in agent_code] == ["t1", "t2", "t3"]
    for cell in agent_code:
        nh = cell["metadata"]["nh"]
        assert {"intent", "rationale", "created_by", "turn_id", "source_sha", "uid"} <= set(nh)
        note = next(c for c in notebook["cells"] if c["id"] == nh["pair_uid"])
        lines = (
            note["source"].splitlines()
            if isinstance(note["source"], str)
            else "".join(note["source"]).splitlines()
        )
        assert lines[0].startswith("### ") and count_words(lines[0][4:]) <= 8
        bullets = [line for line in lines if line.startswith("- ")]
        assert 2 <= len(bullets) <= 5

    fresh = nhctl(project, "fresh-run", NB)
    assert fresh.get("ok") is True, fresh
    metrics = nhctl(project, "metrics", "summarize")
    assert metrics["max_cells_per_turn"] == 1, metrics
    assert metrics["cells_added"] == 4 and metrics["undo"]["undone"] == 1
    assert (
        metrics["acceptance"]["reviewed"] == 4 and metrics["acceptance"]["accepted"] == 3
    )  # the plot was undone
    assert metrics["fresh_run"]["passed"] == 1


def _api_path(project: Path) -> str:
    # The fixture's server root is the project's parent; the room path is relative to it.
    return (project / NB).relative_to(project.parent).as_posix()
