"""nhctl fresh-run with the real venv Python (it has nbclient and ipykernel) on tiny notebooks."""

from __future__ import annotations

import json
import subprocess

import pytest
from nhctl_testlib import PLUGIN, PYTHONS, SERVER, VENV_PYTHON

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


def test_the_failing_cell_is_redacted_before_its_cuts(env, nb_project):
    """The label and the error message pass the project's redactor before their cuts
    (design §6.8); the copy in .nh/tmp keeps the real output, as notebooks do."""
    password = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
    (nb_project / ".env").write_text(f"DB_PASSWORD={password}\n")
    nb = nb_project / "notebooks/01_eda.ipynb"
    failing = (  # reads the value itself, so the cell's source holds no secret
        'from pathlib import Path\npw = Path("../.env").read_text().split("=", 1)[1].strip()\n'
        'raise ValueError("x" * 490 + pw)'
    )
    cells = LOADER + [note(f"Connect as app with {password}", "nh-cccccccccc"),
                      code(failing, "nh-cccccccccc", 7)]  # fmt: skip
    write_nb(nb, cells)
    proc = env.run("fresh-run", "--json", cwd=nb_project)
    assert proc.returncode == 1, proc.stderr
    result = json.loads(proc.stdout)
    assert result["failing"] == {
        "index": 3, "ename": "ValueError", "evalue": "x" * 490 + "[redacted:",
        "label": '"Connect as app with [redacted:DB_PASSWORD]" [7]', "uid": "nh-cccccccccc",
    }  # fmt: skip
    human = env.run("fresh-run", "notebooks/01_eda.ipynb", cwd=nb_project)
    assert '"Connect as app with [redacted:DB_PASSWORD]" [7]: ValueError: xxx' in human.stdout
    for out in (proc.stdout, human.stdout, (nb_project / ".nh/log.jsonl").read_text()):
        assert password[:3] not in out.replace("x" * 490, "")
    copy = json.loads((nb_project / result["copy"]).read_text())
    assert password in json.dumps(copy["cells"][3]["outputs"])


# Calls freshrun's label and report builders directly: printed output is scrubbed again, which
# would hide a site that cut before it redacted (review of C3).
DIRECT = """
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import freshrun
from nh_gateway._shared import secrets
secrets.install(secrets.Redactor.for_project(Path(sys.argv[3]), {}))
cells, result = json.loads(sys.argv[4]), json.loads(sys.argv[5])
report = freshrun.failing_report(result, cells)
print(json.dumps({"code": freshrun.cell_label(cells, 0), "failing": report}))
"""


@pytest.mark.parametrize("python", PYTHONS)
def test_labels_and_the_failing_report_are_redacted_before_their_cuts(tmp_path, python):
    password = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
    (tmp_path / ".env").write_text(f"DB_PASSWORD={password}\n")
    line = f'engine = create_engine("pg://app:{password}@db/x")'
    assert line.index(password) == 39 - 6  # cut first, 6 chars of it would show
    cells = [
        code(line, "nh-aaaaaaaaaa", 3),
        note(f"Connect as app with {password}", "nh-cccccccccc"),
        code("connect()", "nh-cccccccccc", 7),
    ]
    result = {"ok": False, "failing_index": 2, "ename": f"Login{password}Error",
              "evalue": "x" * 495 + password}  # fmt: skip
    argv = [str(PLUGIN / "scripts" / "nhctl"), str(SERVER / "src"), str(tmp_path)]
    argv += [json.dumps(cells), json.dumps(result)]
    proc = subprocess.run(
        [python, "-c", DIRECT, *argv], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert shown == {
        "code": '"engine = create_engine(\"pg://[redacted:…" [3]',
        "failing": {
            "index": 2, "ename": "Login[redacted:DB_PASSWORD]Error", "evalue": "x" * 495 + "[reda",
            "label": '"Connect as app with [redacted:DB_PASSWORD]" [7]', "uid": "nh-cccccccccc",
        },
    }  # fmt: skip
    assert password[:4] not in proc.stdout


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
