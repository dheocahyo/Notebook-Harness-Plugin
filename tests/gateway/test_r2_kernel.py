"""Round-2 regressions for the kernel group (FakeBackend): restart detection from any view, the
one-time NEW-kernel lead on refusals, unknown incarnations, and the checks that keep or drop
'Kernel ≠ notebook' names (a user re-run, the max_vars cut, a truncated probe)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastmcp import Client

from nh_gateway.app import create_server
from nh_gateway.backend.fake import FakeBackend
from tests.fakes.turns import Turns, text
from tests.gateway.conftest import NOTEBOOK, Harness, make_project
from tests.gateway.test_gateway import DROP, LOAD

TOTAL = dict(LOAD, title="Set a total", code="total = 10\ntotal")


def code_cells(h: Harness) -> list[dict]:
    return [c for c in h.cells() if c["cell_type"] == "code"]


def state_json(h: Harness, name: str) -> dict:
    path = h.project / ".nh" / "state" / name
    return json.loads(path.read_text()) if path.exists() else {}


def reminder(h: Harness, prompt_id: str) -> str:
    return h.turns.prompt(prompt_id)["hookSpecificOutput"]["additionalContext"]


def user_run(backend: FakeBackend, cell_id: str) -> None:
    """The user runs a cell in JupyterLab: the kernel runs it and the cell shows its new count."""
    cell = next(c for c in backend.notebooks[NOTEBOOK]["cells"] if c["id"] == cell_id)
    outputs, count, _ = backend._execute(cell["source"])
    cell.update(outputs=outputs, execution_count=count)


async def harness(tmp_path: Path, backend_cls: type[FakeBackend]):
    project = make_project(tmp_path)
    backend = backend_cls(project)
    client = Client(create_server(project, backend))
    await client.__aenter__()
    return Harness(project, backend, client, Turns(project, tmp_path / "data")), client


async def undo_leaves_total(h: Harness) -> None:
    h.turns.prompt("p1")
    await h.call("nh_add_cell", "p1", **TOTAL)
    h.turns.prompt("p2")
    body = text(await h.call("nh_undo", "p2"))
    assert body.startswith("Kernel ≠ notebook: `total`"), body


# ---------------------------------------------------------------------------- V1: any view sees a restart


@pytest.mark.parametrize("view", ["outline", "cell", "intents", "var", "vars", "overview"])
async def test_a_restart_found_by_any_view_clears_drift_and_is_told_once(
    nh: Harness, view: str
) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    nh.turns.prompt("p2")
    await nh.call("nh_add_cell", "p2", **DROP)
    nh.turns.prompt("p3")
    assert text(await nh.call("nh_undo", "p3")).startswith("Kernel ≠ notebook: `df_clean`")
    nh.backend.restart_kernel()
    args = {"view": view, "cell_id": code_cells(nh)[0]["id"] if view == "cell" else None}
    args["name"] = "df" if view == "var" else None
    body = text(await nh.call("nh_inspect", "p3", **args))
    assert body.startswith("NEW kernel: earlier variables are gone"), body
    assert "Kernel ≠ notebook" not in body
    assert state_json(nh, "kernel_drift.json") == {}
    assert "Kernel ≠ notebook" not in reminder(nh, "p4")
    again = text(await nh.call("nh_inspect", "p4", view="outline"))
    assert "NEW kernel" not in again and "Kernel ≠ notebook" not in again


# ---------------------------------------------------------------------------- V1: a user re-run


async def test_a_user_rerun_of_the_restored_cell_clears_drift(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **dict(LOAD, code="total = 1\ntotal"))
    uid = code_cells(nh)[0]["id"]
    nh.turns.prompt("p2")
    await nh.call("nh_edit_cell", "p2", cell_id=uid, code="total = 2\ntotal")
    nh.turns.prompt("p3")
    assert text(await nh.call("nh_undo", "p3")).startswith("Kernel ≠ notebook: `total`")
    other = nh.backend.user_insert(NOTEBOOK, len(nh.cells()), "unrelated = 3\nunrelated")
    user_run(nh.backend, other)  # a later run that doesn't bind `total` changes nothing
    overview = text(await nh.call("nh_inspect", "p3", view="overview"))
    assert overview.startswith("Kernel ≠ notebook: `total`"), overview
    user_run(nh.backend, uid)  # the user re-runs the restored cell in JupyterLab
    overview = text(await nh.call("nh_inspect", "p3", view="overview"))
    assert "Kernel ≠ notebook" not in overview
    assert state_json(nh, "kernel_drift.json") == {}
    assert "Kernel ≠ notebook" not in reminder(nh, "p4")


async def test_a_write_after_the_user_reran_the_cell_has_no_drift_lead(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **TOTAL)
    nh.turns.prompt("p2")
    await nh.call("nh_add_cell", "p2", **dict(DROP, title="Double it", code="twice = total * 2"))
    twice = code_cells(nh)[1]["id"]
    nh.turns.prompt("p3")
    assert text(await nh.call("nh_undo", "p3")).startswith("Kernel ≠ notebook: `twice`")
    retyped = nh.backend.user_insert(NOTEBOOK, len(nh.cells()), "twice = total + total")
    assert twice not in [c["id"] for c in code_cells(nh)]
    user_run(nh.backend, retyped)
    nh.turns.prompt("p4")
    body = text(await nh.call("nh_add_cell", "p4", **dict(DROP, code="half = twice / 4\nhalf")))
    assert "Kernel ≠ notebook" not in body and "ran ok" in body, body


# ---------------------------------------------------------------------------- V8/V14: refusals carry the news


async def test_the_new_kernel_lead_rides_on_a_refusal_and_is_told_once(nh: Harness) -> None:
    nh.turns.prompt("p1")
    await nh.call("nh_add_cell", "p1", **LOAD)
    nh.backend.restart_kernel()
    nh.turns.prompt("p2")
    refused = await nh.call("nh_add_cell", "p2", **dict(DROP, code="!pip install seaborn\nx = 1"))
    body = text(refused)
    assert refused.is_error and "nh: E120" in body
    assert body.startswith("NEW kernel: earlier variables are gone"), body
    assert body.splitlines()[1].startswith("Not written")
    later = text(await nh.call("nh_add_cell", "p2", **dict(LOAD, title="Reload sales data")))
    assert "ran ok" in later and "NEW kernel" not in later


async def test_an_e112_after_a_restart_carries_the_new_kernel_lead(nh: Harness) -> None:
    nh.turns.prompt("p1")
    uid = (await nh.call("nh_add_cell", "p1", **LOAD)).meta["nh/cell_id"]
    nh.backend.restart_kernel()
    refused = await nh.call("nh_edit_cell", "p1", cell_id=uid, code=LOAD["code"] + "\ndf")
    assert refused.is_error and "nh: E112" in text(refused)
    assert text(refused).startswith("NEW kernel")
    nh.turns.prompt("p2")
    assert "NEW kernel" not in text(await nh.call("nh_inspect", "p2", view="outline"))


# ---------------------------------------------------------------------------- V7/V13/V27: unknown incarnation


class BlindBackend(FakeBackend):
    """Like RtcBackend while the kernel is busy: nh can't see the kernel process."""

    blind = False

    async def kernel_status(self, ref, *, create=True):
        status = await super().kernel_status(ref, create=create)
        return dataclasses.replace(status, incarnation=None) if self.blind else status


async def test_an_unknown_incarnation_is_no_news_and_keeps_drift(tmp_path: Path) -> None:
    h, client = await harness(tmp_path, BlindBackend)
    try:
        await undo_leaves_total(h)
        known = state_json(h, "kernels.json")[NOTEBOOK]
        assert known.split(":", 1)[1]
        h.backend.blind = True
        h.turns.prompt("p3")
        body = text(await h.call("nh_add_cell", "p3", **dict(DROP, code="n = 1\nn")))
        assert "NEW kernel" not in body and body.startswith("Kernel ≠ notebook: `total`"), body
        assert state_json(h, "kernels.json")[NOTEBOOK] == known
        h.backend.blind = False
        h.turns.prompt("p4")
        body = text(await h.call("nh_add_cell", "p4", **dict(DROP, code="m = 2\nm")))
        assert "NEW kernel" not in body and body.startswith("Kernel ≠ notebook: `total`"), body
        assert state_json(h, "kernels.json")[NOTEBOOK] == known
    finally:
        await client.__aexit__(None, None, None)


async def test_a_first_blind_look_is_filled_in_later_without_news(tmp_path: Path) -> None:
    h, client = await harness(tmp_path, BlindBackend)
    try:
        h.backend.blind = True
        h.turns.prompt("p1")
        await h.call("nh_add_cell", "p1", **TOTAL)
        assert state_json(h, "kernels.json")[NOTEBOOK] == f"{h.backend.kernel_id}:"
        h.backend.blind = False
        body = text(await h.call("nh_inspect", "p1", view="outline"))
        assert "NEW kernel" not in body
        assert state_json(h, "kernels.json")[NOTEBOOK] == (
            f"{h.backend.kernel_id}:{h.backend.incarnation}"
        )
    finally:
        await client.__aexit__(None, None, None)


async def test_a_new_kernel_id_is_news_even_when_blind(tmp_path: Path) -> None:
    h, client = await harness(tmp_path, BlindBackend)
    try:
        await undo_leaves_total(h)
        h.backend.restart_kernel(new_id=True)
        h.backend.blind = True
        h.turns.prompt("p3")
        body = text(await h.call("nh_add_cell", "p3", **dict(DROP, code="n = 1\nn")))
        assert body.startswith("NEW kernel") and "Kernel ≠ notebook" not in body, body
    finally:
        await client.__aexit__(None, None, None)


# ---------------------------------------------------------------------------- V9/V32: the before-probe


async def test_the_before_probe_keeps_drift_names_past_the_max_vars_cut(nh: Harness) -> None:
    names = [f"a{i:02d}" for i in range(45)]
    nh.turns.prompt("p1")
    many = "\n".join(f"{n} = {i}" for i, n in enumerate(names)) + "\na00"
    await nh.call("nh_add_cell", "p1", **dict(LOAD, title="Set many numbers", code=many))
    nh.turns.prompt("p2")
    await nh.call("nh_add_cell", "p2", **dict(DROP, title="Set a leftover", code="zz_left = 5"))
    nh.turns.prompt("p3")
    assert text(await nh.call("nh_undo", "p3")).startswith("Kernel ≠ notebook: `zz_left`")
    nh.turns.prompt("p4")
    use = "sum_a = " + " + ".join(names) + "\nsum_a"
    body = text(await nh.call("nh_add_cell", "p4", **dict(DROP, title="Sum them", code=use)))
    assert body.startswith("Kernel ≠ notebook: `zz_left`"), body
    assert "zz_left" in state_json(nh, "kernel_drift.json")[NOTEBOOK]["names"]


class TruncatingBackend(FakeBackend):
    """Every vars probe runs out of its time budget before it reaches a name."""

    truncate = False

    async def probe(self, ref, name, args, timeout):
        if self.truncate and name == "vars":
            return {"vars": {}, "truncated": True, "packages": {}}
        return await super().probe(ref, name, args, timeout)


async def test_a_truncated_probe_prunes_no_drift_but_a_complete_one_does(tmp_path: Path) -> None:
    h, client = await harness(tmp_path, TruncatingBackend)
    try:
        await undo_leaves_total(h)
        h.backend.truncate = True
        h.turns.prompt("p3")
        body = text(await h.call("nh_add_cell", "p3", **dict(DROP, code="n = 1\nn")))
        assert body.startswith("Kernel ≠ notebook: `total`"), body
        assert text(await h.call("nh_inspect", "p3", view="vars")).startswith("Kernel ≠ notebook")
        h.backend.truncate = False
        h.backend._execute("del total")  # the user drops it: now the probe finds it gone
        h.turns.prompt("p4")
        body = text(await h.call("nh_add_cell", "p4", **dict(DROP, code="m = 2\nm")))
        assert "Kernel ≠ notebook" not in body, body
        assert state_json(h, "kernel_drift.json") == {}
    finally:
        await client.__aexit__(None, None, None)
