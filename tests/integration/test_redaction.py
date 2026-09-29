"""Redaction against a real JupyterLab (design §6.8): the .ipynb and the RTC document keep what
the kernel printed; what the gateway hands Claude holds the marker. The discovered token is
redacted from then on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway import meta
from nh_gateway._shared import secrets
from nh_gateway.app import create_server
from tests.fakes.turns import Turns, text

pytestmark = pytest.mark.integration

NB = "notebooks/01_eda.ipynb"
PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
MARK = "[redacted:DB_PASSWORD]"
DSN = dict(
    title="Connect to the warehouse",
    notes=["Builds the warehouse connection string.", "Prints it to check the host."],
    intent="connect to the warehouse",
    code=(
        f'dsn = "postgresql://app:{PASSWORD}@db/prod"\n'
        "print(dsn)\n"
        'print("row " * 800)\n'  # past output max_chars (2000): the full copy is written
        "print(dsn)"
    ),
)


def assert_clean(body: str) -> None:
    assert PASSWORD not in body
    assert PASSWORD[:6] not in body and PASSWORD[-6:] not in body


async def test_the_notebook_stays_raw_and_the_result_is_redacted(
    lab, project: Path, backend, helpers, tmp_path: Path
) -> None:
    (project / "harness.toml").write_text(f'version = 1\n[project]\nnotebook = "{NB}"\n')
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    turns = Turns(project, tmp_path / "data")
    api_path = (project / NB).relative_to(project.parent).as_posix()
    async with (
        Client(create_server(project, backend)) as client,
        helpers.observer(api_path) as user,
    ):
        turns.prompt("t1")
        added = await turns.call(client, "nh_add_cell", DSN, "t1")
        body = text(added)
        assert not added.is_error, body
        assert MARK in body
        assert_clean(body)

        # The RTC document (the user's tab) holds the code and the output as they are.
        def raw_live() -> list[dict]:
            return [
                c
                for c in helpers.cells_of(user)
                if c.get("cell_type") == "code" and PASSWORD in json.dumps(c.get("outputs"))
            ]

        [live] = await helpers.eventually(raw_live)
        assert live["source"] == DSN["code"]

        # So does the .ipynb, once JupyterLab saved it.
        def raw_disk() -> dict | None:
            saved = helpers.read_disk(project / NB)
            return saved if saved and PASSWORD in json.dumps(saved) else None

        saved = await helpers.eventually(raw_disk, timeout=20)
        [cell] = [c for c in saved["cells"] if c["cell_type"] == "code"]
        assert "".join(cell["source"]) == DSN["code"]

        # The cell view: the marker in its source and outputs, the raw source's sha.
        view = text(
            await turns.call(client, "nh_inspect", {"view": "cell", "cell_id": cell["id"]}, "t1")
        )
        assert 'dsn = "postgresql://' + MARK + '@db/prod"' in view
        assert f"sha={meta.source_sha(DSN['code'])}" in view
        assert_clean(view)

        # The full copy under .nh/outputs holds the marker.
        copies = [p for p in (project / ".nh" / "outputs").rglob("*") if p.is_file()]
        assert copies and all(MARK in p.read_text() for p in copies)
        assert all(PASSWORD not in p.read_text() for p in copies)

        # The token nh discovered is redacted from then on: a cell that prints it, built from two
        # halves so no piece of the code holds it whole.
        assert secrets.current().redact(f"t {lab.token}") == "t [redacted:JUPYTER_TOKEN]"
        half = len(lab.token) // 2
        head, tail = lab.token[:half], lab.token[half:]
        turns.prompt("t2")
        printed = await turns.call(
            client,
            "nh_add_cell",
            dict(
                title="Print the lab token",
                notes=["Prints the lab's token.", "Checks that the reply hides it."],
                intent="print the lab token",
                code=f'print("lab token " + {head!r} + {tail!r})',
            ),
            "t2",
        )
        body = text(printed)
        assert not printed.is_error, body
        assert "lab token [redacted:JUPYTER_TOKEN]" in body and lab.token not in body
