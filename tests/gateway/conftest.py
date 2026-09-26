from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from tests.fakes.turns import Turns

NOTEBOOK = "notebooks/eda.ipynb"


class Harness:
    def __init__(self, project: Path, backend: FakeBackend, client: Client, turns: Turns) -> None:
        self.project, self.backend, self.client, self.turns = project, backend, client, turns

    async def call(self, tool: str, prompt_id: str, **args):
        args = {k: v for k, v in args.items() if v is not None}
        return await self.turns.call(self.client, tool, args, prompt_id)

    def cells(self) -> list[dict]:
        if NOTEBOOK not in self.backend.notebooks:
            return []
        return self.backend.notebook(NOTEBOOK)["cells"]


def make_project(tmp_path: Path, harness_toml: str = "") -> Path:
    project = tmp_path / "proj"
    (project / ".nh").mkdir(parents=True)
    (project / "notebooks").mkdir()
    (project / "harness.toml").write_text(
        f'version = 1\n[project]\nnotebook = "{NOTEBOOK}"\n{harness_toml}'
    )
    return project


@pytest.fixture
async def nh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("NH_PROJECT_DIR", str(project))
    backend = FakeBackend(project)
    server = create_server(project, backend)
    async with Client(server) as client:
        yield Harness(project, backend, client, Turns(project, tmp_path / "data"))
