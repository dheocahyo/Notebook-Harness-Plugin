"""Without a running JupyterLab: DiskReadBackend serves the outline from the file, and RtcBackend
falls back to it for read-only calls while every write refuses with E130."""

from __future__ import annotations

import nbformat
import pytest
import pytest_asyncio

from nh_gateway._shared.paths import Layout
from nh_gateway.backend.base import NewCell
from nh_gateway.backend.diskread import DiskReadBackend
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.policy.errors import NhError

pytestmark = pytest.mark.asyncio
NB = "notebooks/01_eda.ipynb"


@pytest.fixture
def project(tmp_path, monkeypatch):
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no real runtime dirs
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "proj"
    (root / ".nh" / "state").mkdir(parents=True)
    (root / "notebooks").mkdir()
    cell = nbformat.v4.new_code_cell(
        "x = 1", id="c1", metadata={"nh": {"role": "code", "uid": "c1"}}
    )
    cell.outputs = [nbformat.v4.new_output("error", ename="KeyError", evalue="'a'", traceback=[])]
    cell.execution_count = 3
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell("# t", id="t"), cell])
    notebook.metadata["nh"] = {"v": 1}
    (root / NB).write_text(nbformat.writes(notebook))
    return root


async def test_disk_backend_reads_and_refuses_writes(project):
    backend = DiskReadBackend(project)
    ref = await backend.resolve_notebook(NB)
    cells = await backend.snapshot(ref, outputs="full")
    assert [c.id for c in cells] == ["t", "c1"]
    assert cells[1].execution_count == 3 and cells[1].summary.error == "KeyError: 'a'"
    assert cells[1].outputs[0]["ename"] == "KeyError"
    assert (await backend.notebook_meta(ref))["nh"] == {"v": 1}
    with pytest.raises(NhError) as info:
        await backend.insert_cells(
            ref, 0, [NewCell(id="n", cell_type="code", source="", metadata={})]
        )
    assert info.value.code == "E130"
    with pytest.raises(NhError):
        await backend.kernel_status(ref)


@pytest_asyncio.fixture
async def rtc(project):
    backend = RtcBackend(Layout(project), ConfigCache(project))
    yield backend
    await backend.aclose()


async def test_rtc_backend_falls_back_to_the_file_for_reads(rtc: RtcBackend):
    ref = await rtc.resolve_notebook(NB)
    assert ref.api_path == NB and ref.rel_path == NB
    assert [c.id for c in await rtc.snapshot(ref)] == ["t", "c1"]
    assert (await rtc.notebook_meta(ref))["nh"] == {"v": 1}
    for call in (
        rtc.kernel_status(ref),
        rtc.probe(ref, "vars", {}, 1.0),
        rtc.insert_cells(ref, -1, [NewCell(id="n", cell_type="code", source="", metadata={})]),
    ):
        with pytest.raises(NhError) as info:
            await call
        assert info.value.code == "E130" and "nhctl lab start" in str(info.value)
    status = await rtc.describe(ref)
    assert "outline only" in status["fallback"]


async def test_env_gate_refuses_old_collaboration(rtc: RtcBackend, project, monkeypatch):
    (project / ".nh" / "state" / "env.json").write_text('{"jupyter_collaboration": "4.0.2"}')
    monkeypatch.setattr("nh_gateway.backend.discovery.discover", lambda *a, **k: None)
    ref = await rtc.resolve_notebook(NB)  # reads still fall back to the file
    with pytest.raises(NhError) as info:
        await rtc.insert_cells(ref, -1, [])
    assert info.value.code == "E131"
