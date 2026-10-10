"""RtcBackend against a real JupyterLab room: one-transaction writes, persistence on disk, conflicts,
deletes, reconnects and stuck-cell recovery."""

from __future__ import annotations

import asyncio

import nbformat
import pytest
from pycrdt import Map

from nh_gateway import meta
from nh_gateway.backend.base import CellConflict, CellMissing, CellPatch, NewCell
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.policy.errors import NhError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def pair(uid: str, code: str, title: str = "Load the data", turn: str = "turn-1") -> list[NewCell]:
    bullets = ["Reads the CSV with pandas", "Shows the shape to check the load"]
    code_md = {
        "tags": [meta.TAG],
        "nh": meta.code_metadata(
            uid=uid, intent="load the data", bullets=bullets, turn_id=turn, source=code
        ),
    }
    note = f"### {title}\n\n" + "\n".join(f"- {b}" for b in bullets)
    note_md = {"tags": [meta.TAG], "nh": meta.note_metadata(uid=uid, turn_id=turn, source=note)}
    return [
        NewCell(id=meta.note_id(uid), cell_type="markdown", source=note, metadata=note_md),
        NewCell(id=uid, cell_type="code", source=code, metadata=code_md),
    ]


async def saved(helpers, path, check, timeout=10.0):
    """Wait until the file on disk satisfies ``check`` (the server saves after its save delay)."""

    def ready():
        data = helpers.read_disk(path)
        return data if data is not None and check(data) else None

    return await helpers.eventually(ready, timeout)


async def test_insert_pair_in_one_transaction_persists_ids_and_metadata(
    backend: RtcBackend, project, helpers
):
    helpers.new_notebook(project / helpers.NB, [nbformat.v4.new_code_cell("human = 1")], minor=4)
    ref = await backend.resolve_notebook(helpers.NB)
    async with helpers.observer(ref.api_path) as user:
        events: list[int] = []
        ycells = user._doc.ycells
        subscription = ycells.observe(lambda event: events.append(len(event.target)))
        await backend.insert_cells(ref, 1, pair("nh-4f2a91c07b", "import pandas as pd\nx = 1"))
        cells = await helpers.eventually(
            lambda: len(helpers.cells_of(user)) == 3 and helpers.cells_of(user)
        )
        assert [c["id"] for c in cells[1:]] == ["nh-4f2a91c07b-n", "nh-4f2a91c07b"]
        assert cells[2]["metadata"]["nh"]["uid"] == "nh-4f2a91c07b"
        assert events == [3], "the note and its code cell must arrive in one update"
        ycells.unobserve(subscription)

    path = project / helpers.NB
    disk = await saved(helpers, path, lambda d: len(d["cells"]) == 3)
    notebook = nbformat.reads(path.read_text(), as_version=4)
    nbformat.validate(notebook)
    assert notebook.nbformat_minor == 5  # upgraded on the first nh write, so the ids survive saving
    assert [c.get("id") for c in notebook.cells[1:]] == ["nh-4f2a91c07b-n", "nh-4f2a91c07b"]
    assert notebook.cells[0].get("id")  # the human cell got an id too
    code = notebook.cells[2]
    assert code.metadata["tags"] == [meta.TAG]
    assert code.metadata["nh"]["pair_uid"] == "nh-4f2a91c07b-n"
    assert code.metadata["nh"]["source_sha"] == meta.source_sha("import pandas as pd\nx = 1")
    assert notebook.cells[1].metadata["nh"]["role"] == "note"
    assert all("execution_state" not in c for c in disk["cells"])
    assert any("nbformat 4.5" in note for note in backend.take_notices(ref))


async def test_snapshot_views_and_notebook_metadata(backend: RtcBackend, project, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000001", "print('x')"))
    cells = await backend.snapshot(ref, outputs="summary")
    assert [(c.id, c.index, c.cell_type) for c in cells] == [
        ("title", 0, "markdown"),
        ("nh-0000000001-n", 1, "markdown"),
        ("nh-0000000001", 2, "code"),
    ]
    assert cells[2].metadata["nh"]["role"] == "code" and cells[2].summary is not None
    assert cells[2].execution_count is None and not cells[2].running
    await backend.set_notebook_meta(ref, "nh", {"v": 1, "goal": "explore sales"})
    assert (await backend.notebook_meta(ref))["nh"] == {"v": 1, "goal": "explore sales"}
    await saved(
        helpers,
        project / helpers.NB,
        lambda d: d["metadata"].get("nh", {}).get("goal") == "explore sales",
    )


async def test_update_detects_user_edits_and_patches_in_place(
    backend: RtcBackend, project, helpers
):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000002", "df = load()\ndf.shape"))
    async with helpers.observer(ref.api_path) as user:
        await helpers.eventually(lambda: len(helpers.cells_of(user)) == 3)
        with user._lock:
            code = next(y for y in user._doc.ycells if y["id"] == "nh-0000000002")
            code["source"].insert(0, "# mine\n")  # the user types in JupyterLab
        current = await helpers.eventually(
            lambda: next(
                (
                    c.source
                    for c in backend._docs[str(ref.abs_path)].cells("none")
                    if c.id == "nh-0000000002" and c.source.startswith("# mine")
                ),
                None,
            )
        )
        with pytest.raises(CellConflict) as conflict:
            await backend.update_cells(
                ref,
                [
                    CellPatch(id="nh-0000000002-n", source="### Changed\n\n- a\n- b"),
                    CellPatch(
                        id="nh-0000000002",
                        source="df = load(2)",
                        base_source="df = load()\ndf.shape",
                    ),
                ],
            )
        assert conflict.value.current == current
        assert (await backend.snapshot(ref, outputs="none"))[1].source.startswith(
            "### Load the data"
        )

        new_nh = {**(await backend.snapshot(ref, outputs="none"))[2].metadata["nh"], "edits": 1}
        await backend.update_cells(
            ref,
            [
                CellPatch(
                    id="nh-0000000002",
                    base_source=current,
                    source="# mine\ndf = load(path)\ndf.shape\n",
                    metadata=new_nh,
                    tags_add=("reviewed",),
                    clear_outputs=True,
                    execution_count=None,
                )
            ],
        )
        seen = await helpers.eventually(
            lambda: next(
                (
                    c
                    for c in helpers.cells_of(user)
                    if c["id"] == "nh-0000000002" and "load(path)" in c["source"]
                ),
                None,
            )
        )
        assert seen["source"] == "# mine\ndf = load(path)\ndf.shape\n"
        assert seen["metadata"]["nh"]["edits"] == 1 and seen["metadata"]["tags"] == [
            meta.TAG,
            "reviewed",
        ]
    with pytest.raises(CellMissing):
        await backend.update_cells(ref, [CellPatch(id="nh-nope", source="x")])
    disk = await saved(
        helpers,
        project / helpers.NB,
        lambda d: len(d["cells"]) == 3 and "load(path)" in "".join(d["cells"][2]["source"]),
    )
    assert disk["cells"][2]["metadata"]["nh"]["edits"] == 1


async def test_update_keeps_concurrent_typing_in_other_cells(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000003", "a = 1\nb = 2\nc = 3"))
    async with helpers.observer(ref.api_path) as user:
        await helpers.eventually(lambda: len(helpers.cells_of(user)) == 3)
        with user._lock:
            text = next(y for y in user._doc.ycells if y["id"] == "nh-0000000003-n")["source"]
            text += "\n- the user's own bullet"  # typing at the end of the note
        await backend.update_cells(
            ref,
            [
                CellPatch(
                    id="nh-0000000003",
                    source="a = 1\nb = 20\nc = 3",
                    base_source="a = 1\nb = 2\nc = 3",
                )
            ],
        )
        cells = await helpers.eventually(
            lambda: (
                lambda cs: (
                    cs if "b = 20" in cs[2]["source"] and "own bullet" in cs[1]["source"] else None
                )
            )(helpers.cells_of(user))
        )
        assert cells[2]["source"] == "a = 1\nb = 20\nc = 3"


async def test_delete_returns_cells_and_skips_missing(backend: RtcBackend, project, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000004", "x = 1"))
    await backend.insert_cells(ref, -1, pair("nh-0000000005", "y = 2"))
    async with helpers.observer(ref.api_path) as user:
        await helpers.eventually(lambda: len(helpers.cells_of(user)) == 5)
        deleted = await backend.delete_cells(ref, ["nh-0000000004", "nh-0000000004-n", "nh-gone"])
        assert [c.id for c in deleted] == ["nh-0000000004-n", "nh-0000000004"]
        assert deleted[1].metadata["nh"]["uid"] == "nh-0000000004"
        remaining = await helpers.eventually(
            lambda: len(helpers.cells_of(user)) == 3 and helpers.cells_of(user)
        )
        assert [c["id"] for c in remaining] == ["title", "nh-0000000005-n", "nh-0000000005"]
    await saved(
        helpers,
        project / helpers.NB,
        lambda d: [c["id"] for c in d["cells"]] == ["title", "nh-0000000005-n", "nh-0000000005"],
    )


async def test_reconnect_after_the_room_drops(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000006", "x = 1"))
    doc = backend._docs[str(ref.abs_path)]
    generation = doc.generation
    doc._task.cancel()  # the websocket drops
    await asyncio.wait({doc._task})
    assert not doc.synced
    await backend.update_cells(
        ref, [CellPatch(id="nh-0000000006", source="x = 2", base_source="x = 1")]
    )
    assert doc.generation == generation + 1
    assert [c.source for c in await backend.snapshot(ref)][2] == "x = 2"


async def test_nh_cells_left_running_are_reset_on_open(project, helpers):
    first = RtcBackend(backend_layout(project), backend_cfg(project))
    ref = await first.resolve_notebook(helpers.NB)
    await first.insert_cells(ref, -1, pair("nh-0000000007", "x = 1"))
    async with helpers.observer(ref.api_path) as user:
        with user._lock, user._doc._ydoc.transaction():
            code = next(y for y in user._doc.ycells if y["id"] == "nh-0000000007")
            code["execution_state"] = "running"  # what a gateway that died mid-run leaves behind
        await asyncio.sleep(0.5)
        await first.aclose()
        second = RtcBackend(backend_layout(project), backend_cfg(project))
        try:
            cells = await second.snapshot(ref, outputs="full")
            assert not cells[2].running
            assert "nh restarted" in cells[2].outputs[-1]["text"]
            assert any("still marked running" in note for note in second.take_notices(ref))
        finally:
            await second.aclose()


async def test_missing_notebook_is_e132(backend: RtcBackend):
    with pytest.raises(NhError) as info:
        await backend.resolve_notebook("notebooks/nope.ipynb")
    assert info.value.code == "E132" and "notebooks/01_eda.ipynb" in str(info.value)


async def test_presence_is_visible_to_peers(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.open(ref)
    async with helpers.observer(ref.api_path) as user:

        def nh_peer():
            return any(
                isinstance(state, (dict, Map))
                and (state.get("user") or {}).get("name") == "nh (agent)"
                for state in user._doc.awareness.states.values()
            )

        await helpers.eventually(nh_peer, timeout=15)
        assert await helpers.eventually(
            lambda: backend._docs[str(ref.abs_path)].peers() >= 1, timeout=15
        )


def backend_layout(project):
    from nh_gateway._shared.paths import Layout

    return Layout(project)


def backend_cfg(project):
    from nh_gateway.config import ConfigCache

    return ConfigCache(project)


async def test_writes_survive_disconnecting_right_away(backend: RtcBackend, project, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    await backend.insert_cells(ref, -1, pair("nh-0000000008", "x = 1"))
    await backend.close(ref)  # no browser tab: the room is cleaned up after document_cleanup_delay
    await asyncio.sleep(4)
    disk = await saved(helpers, project / helpers.NB, lambda d: len(d["cells"]) == 3)
    assert disk["cells"][2]["id"] == "nh-0000000008"


async def test_twenty_reconnects_leave_no_duplicate_ids(backend: RtcBackend, project, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    for round_ in range(20):
        uid = f"nh-00000001{round_:02d}"
        await backend.insert_cells(ref, -1, pair(uid, f"x = {round_}"))
        if round_ % 2:
            await backend.delete_cells(ref, [uid, meta.note_id(uid)])
        doc = backend._docs[str(ref.abs_path)]
        doc._task.cancel()
        await asyncio.wait({doc._task})
    ids = [c.id for c in await backend.snapshot(ref, outputs="none")]
    assert len(ids) == len(set(ids)) == 21
    disk = await saved(helpers, project / helpers.NB, lambda d: len(d["cells"]) == 21)
    assert len({c["id"] for c in disk["cells"]}) == 21


async def test_source_patches_handle_non_ascii(backend: RtcBackend, helpers):
    ref = await backend.resolve_notebook(helpers.NB)
    old = "label = 'café 😀'\nprint(label)"
    await backend.insert_cells(ref, -1, pair("nh-0000000009", old))
    new = "label = 'crème brûlée 😀🎉'\nprint(label, 'ok')"
    await backend.update_cells(ref, [CellPatch(id="nh-0000000009", source=new, base_source=old)])
    async with helpers.observer(ref.api_path) as user:
        # The room reads nh's patch only after the save nh asked for after the insert (design
        # §6.13): wait until the patch arrived, then check what it made.
        cell = await helpers.eventually(
            lambda: next(
                (
                    c
                    for c in helpers.cells_of(user)
                    if c["id"] == "nh-0000000009" and c["source"] != old
                ),
                None,
            )
        )
        assert cell["source"] == new
