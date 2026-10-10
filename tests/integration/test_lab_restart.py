"""a8 (design §6.13): JupyterLab restarts while the gateway is connected and one of nh's cells runs.

A JupyterLab of the test's own, an nh project in it, the gateway (MCP over ``RtcBackend``) and an
observer peer standing in for the user's tab. Message 1 adds a cell. Message 2 adds one that
prints for a minute; with a 5 s soft timeout its call comes back "still running". Then JupyterLab
gets SIGTERM: it deletes its rooms and has its kernel shut down (the kernel is gone within a
second), but with nh connected and the cell running it doesn't exit, so the test SIGKILLs it once
the kernel's connection file is gone. It starts again as "the same" server: the same root,
runtime dir, port and token, so the gateway's cached server still answers and only its room and
kernel connections have to be rebuilt. Twice:

- **same YStore** (JupyterLab restarted from the same directory): the rebuilt room loads the
  YStore, then the file where they differ;
- **fresh YStore** (``.jupyter_ystore.db`` deleted, as when JupyterLab starts from another
  directory): the room loads the file alone. Only here would nh merging its stale copy of the
  notebook into the new room show as duplicate cells; with the same YStore the room already holds
  the old items and a second merge of them changes nothing.

Checked after the restart:
- ``nh_run(mode="wait")`` on the running cell reports it lost, in nh's words.
- The gateway reconnects: message 3's cell runs, in a new kernel nh starts for the notebook.
- No duplicate cell ids, in the tab (a new observer: the old one's connection died with the
  server, as a browser tab reloads the document after a server restart) and in the saved .ipynb.
- No cell is lost either. jupyter-collaboration saves a room only after ``document_save_delay``
  with no change, never while nh's output flushes (every 200 ms) keep changing it, nor on
  shutdown; and a room rebuilt after a restart takes the file over its YStore when they differ.
  So nh asks the room to save after each of its writes and at the end of each run (design §6.13);
  before that, the cells of messages 1 and 2 were gone after the restart in most runs.
  ``test_cells_nh_wrote_are_saved_while_another_cell_streams`` checks the save itself.

``test_backend_review.py`` covers a restart with a new token or port at the backend level, with
no running cell; ``test_kernel_exec.py`` a kernel restart mid-run.
"""

from __future__ import annotations

import os
from pathlib import Path

import nbformat
import pytest
from fastmcp import Client

from nh_gateway._shared.paths import Layout
from nh_gateway.app import create_server
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from tests.fakes.turns import Turns, text

pytestmark = pytest.mark.integration

NB = "notebooks/01_eda.ipynb"


def streaming(seconds: int) -> str:
    """A cell that prints every 0.1 s, as a training loop's progress does."""
    return (
        f"import time\n\nfor step in range({seconds * 10}):\n"
        "    print(f'step {step}', flush=True)\n    time.sleep(0.1)"
    )


def cell(code: str, title: str) -> dict:
    return dict(
        title=title,
        notes=["Does the thing for the user.", "Shows the result so we can check it."],
        intent="do the thing",
        code=code,
    )


def uid_of(body: str) -> str:
    return body.split("nh: cell=")[1].split()[0]


def ids(cells: list[dict]) -> list[str]:
    return [c["id"] for c in cells]


def config(soft_timeout_s: int | None = None) -> str:
    exec_table = f"[exec]\nsoft_timeout_s = {soft_timeout_s}\n" if soft_timeout_s else ""
    return f'version = 1\n[project]\nnotebook = "{NB}"\n{exec_table}'


@pytest.mark.parametrize("ystore", ["same", "fresh"], ids=["same-ystore", "fresh-ystore"])
async def test_a_jupyterlab_restart_mid_run_reports_the_cell_lost_and_reconnects(
    helpers, tmp_path: Path, monkeypatch, ystore: str
) -> None:
    base = Path(os.path.realpath(tmp_path / "lab"))
    first = helpers.start_lab(base)
    second = None
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(first.runtime))
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN", "JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    project = first.root / "proj"
    (project / ".nh" / "state").mkdir(parents=True)
    helpers.new_notebook(project / NB, [nbformat.v4.new_markdown_cell("# Sales EDA", id="title")])
    # Message 1 has the default soft timeout: its call also starts the kernel and connects nh.
    (project / "harness.toml").write_text(config())
    api_path = (project / NB).relative_to(first.root).as_posix()
    turns = Turns(project, tmp_path / "data")
    backend = RtcBackend(Layout(project), ConfigCache(project))
    try:
        async with Client(create_server(project, backend)) as client:

            async def call(tool: str, prompt_id: str, **args) -> str:
                result = await turns.call(client, tool, args, prompt_id)
                assert not result.is_error, text(result)
                return text(result)

            async with helpers.observe(first, api_path) as tab:
                turns.prompt("t1")
                body = await call("nh_add_cell", "t1", **cell("total = 10\ntotal", "Set a total"))
                assert "ran ok" in body, body
                (project / "harness.toml").write_text(config(soft_timeout_s=5))
                turns.prompt("t2")
                body = await call("nh_add_cell", "t2", **cell(streaming(60), "Train for a minute"))
                assert "still running after" in body, body
                running = uid_of(body)

                def printing() -> bool:
                    found = [c for c in helpers.cells_of(tab) if c["id"] == running]
                    return bool(
                        found and found[0]["execution_state"] == "running" and found[0]["outputs"]
                    )

                await helpers.eventually(printing, timeout=15)
                before = ids(helpers.cells_of(tab))
                assert len(before) == len(set(before)) == 5, before

                # JupyterLab restarts as the same server; its kernel is shut down with it.
                port = int(first.url.rsplit(":", 1)[1].strip("/"))
                helpers.stop(first.proc, runtime=first.runtime)
                if ystore == "fresh":
                    stores = list(base.glob(".jupyter_ystore.db*"))
                    assert stores, sorted(p.name for p in base.iterdir())
                    for store in stores:
                        store.unlink()
                second = helpers.start_lab(
                    base, root=first.root, runtime=first.runtime, port=port, token=first.token
                )

            # The running cell is reported lost, in nh's words.
            body = await call("nh_run", "t2", cell_id=running, mode="wait")
            lines = body.splitlines()
            assert "the kernel went away while it ran" in body, body
            assert any(line.startswith(f"nh: cell={running} ") for line in lines), body
            assert "The kernel restarted, died or disconnected while" in body, body

            # The gateway reconnected: the next message's cell runs in a new kernel.
            turns.prompt("t3")
            body = await call("nh_add_cell", "t3", **cell("count = 3\ncount", "Set a count"))
            assert "ran ok" in body, body
            added = uid_of(body)

            # No duplicate cell ids: in the user's tab after the restart, and on disk.
            async with helpers.observe(second, api_path) as tab:

                def settled() -> list[dict] | None:
                    cells = helpers.cells_of(tab)
                    return cells if added in ids(cells) else None

                cells = await helpers.eventually(settled, timeout=15)
                after = ids(cells)
                assert len(after) == len(set(after)) == 7, after
                assert after[:5] == before, after
                [lost] = [c for c in cells if c["id"] == running]
                assert lost["execution_state"] == "idle", lost

            def saved() -> dict | None:
                disk = helpers.read_disk(project / NB)
                return disk if disk and added in ids(disk["cells"]) else None

            disk = await helpers.eventually(saved, timeout=20)
            nbformat.validate(nbformat.from_dict(disk))
            assert ids(disk["cells"]) == after, ids(disk["cells"])
    finally:
        await backend.aclose()
        helpers.stop(first.proc)
        if second is not None:
            helpers.stop(second.proc)


async def test_cells_nh_wrote_are_saved_while_another_cell_streams(
    project: Path, backend, helpers, tmp_path: Path
) -> None:
    """The root cause a8 found: while a cell prints, the room's autosave never fires, so nh asks
    for the save itself. Both messages' cells are in the .ipynb while the second one still runs."""
    (project / "harness.toml").write_text(config(soft_timeout_s=5))
    turns = Turns(project, tmp_path / "data")
    async with Client(create_server(project, backend)) as client:

        async def call(tool: str, prompt_id: str, **args) -> str:
            result = await turns.call(client, tool, args, prompt_id)
            assert not result.is_error, text(result)
            return text(result)

        turns.prompt("t1")
        first = uid_of(await call("nh_add_cell", "t1", **cell("total = 10\ntotal", "Set a total")))
        turns.prompt("t2")
        body = await call("nh_add_cell", "t2", **cell(streaming(30), "Train for a while"))
        assert "still running after" in body, body
        running = uid_of(body)

        def on_disk() -> list[str] | None:
            disk = helpers.read_disk(project / NB)
            found = ids(disk["cells"]) if disk else []
            return found if {first, running} <= set(found) else None

        try:
            found = await helpers.eventually(on_disk, timeout=5)
            assert found == ["title", f"{first}-n", first, f"{running}-n", running], found
        finally:
            await call("nh_run", "t2", cell_id=running, mode="interrupt")
