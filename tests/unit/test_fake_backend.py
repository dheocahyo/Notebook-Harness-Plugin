"""FakeBackend: the whole NotebookBackend protocol in memory, with the real probes."""

from __future__ import annotations

import asyncio

import nbformat
import pytest
import pytest_asyncio

from nh_gateway import meta
from nh_gateway.backend.base import CellConflict, CellMissing, CellPatch, NewCell
from nh_gateway.backend.fake import FakeBackend
from nh_gateway.policy.errors import NhError

pytestmark = pytest.mark.asyncio

NB = "notebooks/01_eda.ipynb"


def pair(uid: str, code: str, title: str = "Load the data") -> list[NewCell]:
    code_md = {
        "tags": [meta.TAG],
        "nh": meta.code_metadata(
            uid=uid, intent="load", bullets=["a", "b"], turn_id="t1", source=code
        ),
    }
    note = f"### {title}\n\n- a\n- b"
    note_md = {"tags": [meta.TAG], "nh": meta.note_metadata(uid=uid, turn_id="t1", source=note)}
    return [
        NewCell(
            id=meta.note_id(uid),
            cell_type="markdown",
            source=note,
            metadata=note_md,
        ),
        NewCell(id=uid, cell_type="code", source=code, metadata=code_md),
    ]


@pytest_asyncio.fixture
async def fake(tmp_path):
    backend = FakeBackend(tmp_path)
    ref = await backend.resolve_notebook(NB)
    await backend.open(ref)
    yield backend, ref
    await backend.aclose()


async def test_insert_pair_is_one_transaction_and_valid(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1\nx"))
    assert backend.transactions == [("insert", NB)]
    cells = await backend.snapshot(ref, outputs="none")
    assert [c.id for c in cells] == ["nh-0000000001-n", "nh-0000000001"]
    assert cells[1].metadata["nh"]["uid"] == "nh-0000000001"
    assert cells[0].metadata["nh"]["role"] == "note"
    nbformat.validate(nbformat.from_dict(backend.notebook(NB)))


async def test_insert_rejects_duplicate_ids_without_writing(fake):
    backend, ref = fake
    await backend.insert_cells(ref, -1, pair("nh-0000000001", "x = 1"))
    with pytest.raises(ValueError):
        await backend.insert_cells(ref, -1, pair("nh-0000000001", "y = 2"))
    assert len(await backend.snapshot(ref)) == 2
    assert len(backend.transactions) == 1


async def test_insert_index_is_clamped(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "a = 1"))
    await backend.insert_cells(ref, 99, pair("nh-0000000002", "b = 2"))
    await backend.insert_cells(ref, 2, pair("nh-0000000003", "c = 3"))
    ids = [c.id for c in await backend.snapshot(ref, outputs="none")]
    assert ids == [
        "nh-0000000001-n",
        "nh-0000000001",
        "nh-0000000003-n",
        "nh-0000000003",
        "nh-0000000002-n",
        "nh-0000000002",
    ]


async def test_update_checks_every_patch_before_writing(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.user_edit(NB, "nh-0000000001", "x = 2  # user")
    patches = [
        CellPatch(
            id="nh-0000000001-n",
            source="### New title\n\n- a\n- b",
            base_source="### Load the data\n\n- a\n- b",
        ),
        CellPatch(id="nh-0000000001", source="x = 3", base_source="x = 1"),
    ]
    with pytest.raises(CellConflict) as info:
        await backend.update_cells(ref, patches)
    assert info.value.current == "x = 2  # user"
    cells = await backend.snapshot(ref, outputs="none")
    assert cells[0].source.startswith("### Load the data")  # the note patch was not applied either
    with pytest.raises(CellMissing):
        await backend.update_cells(ref, [CellPatch(id="nh-gone", source="")])
    assert len(backend.transactions) == 1


async def test_update_by_uid_metadata_tags_and_outputs(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "print('hi')"))
    await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
    backend.notebooks[NB]["cells"][1]["id"] = "renumbered"  # nbstripout without --keep-id
    new_nh = {**backend.notebooks[NB]["cells"][1]["metadata"]["nh"], "edits": 1}
    await backend.update_cells(
        ref,
        [
            CellPatch(
                id="nh-0000000001",
                source="print('bye')\n",
                base_source="print('hi')",
                metadata=new_nh,
                tags_add=("x",),
                tags_remove=(meta.TAG,),
                clear_outputs=True,
                execution_count=None,
            )
        ],
    )
    cell = (await backend.snapshot(ref, outputs="full"))[1]
    assert cell.source == "print('bye')\n"
    assert cell.metadata["nh"]["edits"] == 1 and cell.metadata["tags"] == ["x"]
    assert cell.outputs == [] and cell.execution_count is None
    await backend.update_cells(
        ref, [CellPatch(id="renumbered", drop_nh_metadata=True, tags_remove=("x",))]
    )
    cell = (await backend.snapshot(ref, outputs="none"))[1]
    assert "nh" not in cell.metadata and "tags" not in cell.metadata


async def test_delete_returns_what_it_deleted(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.user_delete(NB, "nh-0000000001-n")
    deleted = await backend.delete_cells(ref, ["nh-0000000001-n", "nh-0000000001"])
    assert [c.id for c in deleted] == ["nh-0000000001"]
    assert deleted[0].metadata["nh"]["uid"] == "nh-0000000001"
    assert await backend.snapshot(ref) == []
    assert await backend.delete_cells(ref, ["nh-0000000001"]) == []


async def test_run_outputs_and_execution_count(fake):
    backend, ref = fake
    code = "import sys\nprint('a')\nprint('b')\nprint('err', file=sys.stderr)\ndisplay({'k': 1})\nvalue = 41\nvalue + 1"
    await backend.insert_cells(ref, 0, pair("nh-0000000001", code))
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    result = await execution.future
    assert execution.started()
    assert result.status == "ok" and result.execution_count == 1 and result.error is None
    kinds = [(o["output_type"], o.get("name")) for o in result.outputs]
    assert kinds == [
        ("stream", "stdout"),
        ("stream", "stderr"),
        ("display_data", None),
        ("execute_result", None),
    ]
    assert result.outputs[0]["text"] == "a\nb\n"
    assert result.outputs[-1]["data"]["text/plain"] == "42"
    cell = (await backend.snapshot(ref, outputs="full"))[1]
    assert cell.execution_count == 1 and not cell.running and cell.summary.count == 4
    second = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
    assert second.execution_count == 2


async def test_error_cell(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = {}\ny = 1\nx['prce']"))
    result = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
    assert result.status == "error"
    assert (
        result.error.ename == "KeyError"
        and result.error.evalue == "'prce'"
        and result.error.line == 3
    )
    await backend.insert_cells(ref, -1, pair("nh-0000000002", "def broken(:\n  pass"))
    result = await (await backend.start_execution(ref, "nh-0000000002", hard_timeout=10)).future
    assert (
        result.status == "error" and result.error.ename == "SyntaxError" and result.error.line == 1
    )


async def test_magics_are_skipped(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "%matplotlib inline\n!ls\nx = 1\nx"))
    result = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
    assert result.status == "ok" and result.outputs[-1]["data"]["text/plain"] == "1"


async def test_start_execution_refusals(fake):
    backend, ref = fake
    with pytest.raises(NhError) as info:
        await backend.start_execution(ref, "nh-missing", hard_timeout=10)
    assert info.value.code == "E140"
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    with pytest.raises(NhError) as info:
        await backend.start_execution(ref, "nh-0000000001-n", hard_timeout=10)
    assert info.value.code == "E140"
    backend.exec_delay_s = 5
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    with pytest.raises(NhError) as info:
        await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    assert info.value.code == "E133"
    await asyncio.sleep(0.05)
    await backend.interrupt(ref)
    await execution.future


async def test_probes_see_the_namespace_and_bind_nothing(fake):
    backend, ref = fake
    backend.python_version = (3, 10)
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "rows = [1, 2, 3]\nlabel = 'x'"))
    await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)).future
    before = dict(backend.namespace)
    payload = await backend.probe(
        ref, "vars", {"names": None, "max_vars": 40, "budget_s": 0.8}, 2.0
    )
    assert payload["vars"]["rows"] == {"kind": "container", "type": "list", "len": 3}
    assert payload["vars"]["label"]["repr"] == "'x'"
    assert set(payload["vars"]) == {"rows", "label"}
    var = await backend.probe(ref, "var", {"name": "rows", "rows": 2}, 2.0)
    assert var["text"] == "[1, 2, 3]"
    attach = await backend.probe(ref, "attach", {}, 2.0)
    assert (
        attach["python"] == [3, 10] and attach["exec_count"] == 2
    )  # IPython's count for the next cell
    assert backend.namespace.keys() == before.keys()
    assert (await backend.probe(ref, "nope", {}, 2.0))["error"].startswith("ValueError")
    status = await backend.kernel_status(ref)
    assert status.python_version == (3, 10) and status.execution_state == "idle"


async def test_busy_kernel_queues_runs_and_refuses_probes(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.kernel_busy = True
    assert (await backend.probe(ref, "vars", {}, 1.0))["error"].startswith("KernelBusy")
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    await asyncio.sleep(0.05)
    assert not execution.started() and (await backend.snapshot(ref))[1].running
    with pytest.raises(NhError) as info:
        await backend.interrupt(ref)
    assert info.value.code == "E133"
    backend.kernel_busy = False
    result = await execution.future
    assert execution.started() and result.status == "ok"


async def test_queued_run_times_out_without_interrupting(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.kernel_busy = True
    result = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=0.1)).future
    assert result.status == "timeout" and "never started" in result.note and result.outputs == []


async def test_interrupt_and_hard_timeout(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.exec_delay_s = 5
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    await asyncio.sleep(0.05)
    assert execution.started() and not execution.future.done()
    assert (await backend.kernel_status(ref)).execution_state == "busy"
    await backend.interrupt(ref)
    result = await execution.future
    assert result.status == "interrupted" and result.error.ename == "KeyboardInterrupt"
    result = await (await backend.start_execution(ref, "nh-0000000001", hard_timeout=0.1)).future
    assert result.status == "timeout" and result.error.ename == "KeyboardInterrupt"
    await backend.interrupt(ref)  # nothing running: no-op


async def test_fail_open_and_restart(tmp_path):
    backend = FakeBackend(tmp_path, fail_open="E131")
    ref = await backend.resolve_notebook(NB)
    with pytest.raises(NhError) as info:
        await backend.open(ref)
    assert info.value.code == "E131"
    backend.fail_open = True
    with pytest.raises(NhError) as info:
        await backend.snapshot(ref)
    assert info.value.code == "E130"
    backend.fail_open = False
    old, before = backend.kernel_id, (await backend.kernel_status(ref)).incarnation
    backend.namespace["x"] = 1
    backend.restart_kernel()
    status = await backend.kernel_status(ref)
    assert "x" not in backend.namespace and backend.execution_count == 0
    assert status.kernel_id == old, "like Jupyter, a restart keeps the kernel id"
    assert status.incarnation and status.incarnation != before
    backend.restart_kernel(new_id=True)
    assert backend.kernel_id != old
    with pytest.raises(NhError):
        await backend.resolve_notebook("../outside.ipynb")


async def test_loads_disk_notebook_and_upgrades_nbformat_on_first_write(tmp_path):
    path = tmp_path / NB
    path.parent.mkdir(parents=True)
    old = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("a = 1")])
    old.nbformat_minor = 4
    del old.cells[0]["id"]
    path.write_text(nbformat.writes(old, version=4))
    backend = FakeBackend(tmp_path)
    ref = await backend.resolve_notebook(NB)
    assert [c.source for c in await backend.snapshot(ref)] == ["a = 1"]
    await backend.set_notebook_meta(ref, "nh", {"v": 1, "goal": "eda"})
    assert backend.notebook(NB)["nbformat_minor"] == 5
    assert (await backend.notebook_meta(ref))["nh"] == {"v": 1, "goal": "eda"}
    nbformat.validate(nbformat.from_dict(backend.notebook(NB)))


async def test_aclose_mid_run_resolves_the_run_as_lost(tmp_path):
    backend = FakeBackend(tmp_path, exec_delay_s=5)
    ref = await backend.resolve_notebook(NB)
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    await asyncio.sleep(0.05)
    await backend.aclose()
    result = await execution.future
    assert result.status == "lost"
    assert not (await backend.snapshot(ref))[1].running


async def test_start_execution_checks_the_expected_source(fake):
    """Finding 69: the kernel never runs code other than what nh checked."""
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    backend.user_edit(NB, "nh-0000000001", "user_was_here = 1\nx = 1")
    with pytest.raises(CellConflict) as info:
        await backend.start_execution(
            ref, "nh-0000000001", hard_timeout=10, expected_source="x = 1"
        )
    assert info.value.current == "user_was_here = 1\nx = 1"
    cell = (await backend.snapshot(ref))[1]
    assert not cell.running and "user_was_here" not in backend.namespace
    result = await (
        await backend.start_execution(
            ref, "nh-0000000001", hard_timeout=10, expected_source="user_was_here = 1\nx = 1\n"
        )
    ).future
    assert result.status == "ok"  # compared after normalize_source


async def test_live_run_names_the_running_cell(fake):
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    assert backend.live_run(ref) is None
    backend.exec_delay_s = 5
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    assert backend.live_run(ref) == "nh-0000000001"
    await asyncio.sleep(0.05)
    await backend.interrupt(ref)
    await execution.future
    assert backend.live_run(ref) is None


async def test_user_interrupt_is_interrupted_not_an_error(fake):
    """Finding 19: the stop button in JupyterLab ends the run as 'interrupted', with a note."""
    backend, ref = fake
    await backend.insert_cells(ref, 0, pair("nh-0000000001", "x = 1"))
    assert backend.user_interrupt() is False  # nothing running
    backend.exec_delay_s = 5
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    await asyncio.sleep(0.05)
    assert backend.user_interrupt() is True
    result = await execution.future
    assert result.status == "interrupted" and result.note == "interrupted from JupyterLab"
    assert result.error is not None and result.error.ename == "KeyboardInterrupt"
    execution = await backend.start_execution(ref, "nh-0000000001", hard_timeout=10)
    await asyncio.sleep(0.05)
    await backend.interrupt(ref)  # nh's own interrupt: no note
    result = await execution.future
    assert result.status == "interrupted" and result.note == ""
    backend.exec_delay_s = 0
    await backend.insert_cells(ref, -1, pair("nh-0000000002", "raise KeyboardInterrupt"))
    result = await (await backend.start_execution(ref, "nh-0000000002", hard_timeout=10)).future
    assert result.status == "error"  # the code raised it (W3), nobody pressed stop
