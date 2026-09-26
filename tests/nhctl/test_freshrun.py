"""nhctl fresh-run with the real venv Python (it has nbclient and ipykernel) on tiny notebooks."""

from __future__ import annotations

import json

import pytest
from nhctl_testlib import SERVER, VENV_PYTHON

pytestmark = pytest.mark.skipif(not VENV_PYTHON.exists(), reason="server venv not synced")


def note(title, uid):
    return {"cell_type": "markdown", "id": uid + "-n",
            "metadata": {"nh": {"v": 1, "role": "note", "uid": uid + "-n", "pair_uid": uid}},
            "source": f"### {title}\n\n- one\n- two"}  # fmt: skip


def code(source, uid, count):
    return {"cell_type": "code", "id": uid, "execution_count": count, "outputs": [],
            "metadata": {"nh": {"v": 1, "role": "code", "uid": uid}}, "source": source}  # fmt: skip


def write_nb(path, cells):
    nb = {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}}}  # fmt: skip
    path.write_text(json.dumps(nb, indent=1))


@pytest.fixture
def nb_project(project):
    (project / ".nh/state").mkdir(parents=True)
    (project / ".nh/state/env.json").write_text(
        json.dumps({"manager": "uv", "prefix": str(SERVER / ".venv")})
    )
    (project / "notebooks").mkdir()
    (project / "notebooks/rows.txt").write_text("a b c\n")  # read relative to the notebook
    return project


LOADER = [
    note("Load the rows", "nh-aaaaaaaaaa"),
    code(
        'from pathlib import Path\nrows = Path("rows.txt").read_text().split()\nlen(rows)',
        "nh-aaaaaaaaaa",
        3,
    ),
]


def test_passing_notebook(env, nb_project):
    nb = nb_project / "notebooks/01_eda.ipynb"
    write_nb(nb, LOADER + [code("print(len(rows) * 2)", "nh-bbbbbbbbbb", 4)])
    before = nb.read_bytes()
    result = env.json("fresh-run", "notebooks/01_eda.ipynb", cwd=nb_project)
    assert result["ok"] is True and result["failing"] is None
    assert result["code_cells"] == 2 and result["executed"] == 2
    assert nb.read_bytes() == before
    copy = nb_project / result["copy"]
    assert copy.parent == nb_project / ".nh/tmp" and copy.name.startswith("fresh-")
    outputs = json.loads(copy.read_text())["cells"][2]["outputs"]
    assert "".join(outputs[0]["text"]) == "6\n"
    event = json.loads((nb_project / ".nh/log.jsonl").read_text().splitlines()[-1])
    assert event["event"] == "fresh_run" and event["v"] == 1 and isinstance(event["ts"], float)
    assert {k: event[k] for k in ("nb", "ok", "failing_cell_uid", "n_cells", "via")} == {
        "nb": "notebooks/01_eda.ipynb", "ok": True, "failing_cell_uid": None, "n_cells": 2,
        "via": "nbclient",
    }  # fmt: skip


def test_failing_notebook_names_the_cell(env, nb_project):
    nb = nb_project / "notebooks/01_eda.ipynb"
    cells = LOADER + [note("Drop rows with missing price", "nh-cccccccccc"),
                      code('prices = {}\nprices["price"]', "nh-cccccccccc", 7),
                      code("print('never runs')", "nh-dddddddddd", 8)]  # fmt: skip
    write_nb(nb, cells)
    before = nb.read_bytes()
    proc = env.run("fresh-run", "--json", cwd=nb_project)  # default: harness.toml's notebook
    assert proc.returncode == 1, proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] is False
    assert result["failing"] == {
        "index": 3, "ename": "KeyError", "evalue": "'price'",
        "label": '"Drop rows with missing price" [7]', "uid": "nh-cccccccccc",
    }  # fmt: skip
    assert nb.read_bytes() == before
    human = env.run("fresh-run", "notebooks/01_eda.ipynb", cwd=nb_project)
    assert human.stdout.strip() == (
        'Fresh run FAILED in notebooks/01_eda.ipynb at "Drop rows with missing price" [7]: '
        "KeyError: 'price'"
    )
    assert "nh-" not in human.stdout
    events = [json.loads(line) for line in (nb_project / ".nh/log.jsonl").read_text().splitlines()]
    assert [e["failing_cell_uid"] for e in events] == ["nh-cccccccccc", "nh-cccccccccc"]


def test_needs_the_env(env, project):
    (project / ".nh").mkdir()
    (project / "notebooks").mkdir()
    write_nb(project / "notebooks/01_eda.ipynb", [])
    report = env.json("fresh-run", cwd=project, expect=1)
    assert report["error"]["code"] == "D140"
    missing = env.json("fresh-run", "nope.ipynb", cwd=project, expect=1)
    assert missing["error"]["code"] == "D150"


def test_uses_harness_kernel_name(env, nb_project):
    """[jupyter].kernel_name is the kernel to start, ahead of the notebook's kernelspec."""
    spec = env.tmp / "jupyter-data/kernels/myenv"
    spec.mkdir(parents=True)
    argv = [str(VENV_PYTHON), "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    (spec / "kernel.json").write_text(
        json.dumps({"argv": argv, "display_name": "myenv", "language": "python"})
    )
    (nb_project / "harness.toml").write_text('[jupyter]\nkernel_name = "myenv"\n')
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER)
    result = env.json("fresh-run", cwd=nb_project)
    assert result["ok"] is True and result["kernel"] == "myenv"
