"""nhctl fresh-run with the real venv Python (it has nbclient and ipykernel) on tiny notebooks,
and its /nh:review mode (``--review``, design §6.10)."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest
from nhctl_testlib import NHCTL, PLUGIN, PYTHONS, SERVER, VENV_PYTHON

pytestmark = pytest.mark.skipif(not VENV_PYTHON.exists(), reason="server venv not synced")


def note(title, uid):
    return {"cell_type": "markdown", "id": uid + "-n",
            "metadata": {"nh": {"v": 1, "role": "note", "uid": uid + "-n", "pair_uid": uid}},
            "source": f"### {title}\n\n- one\n- two"}  # fmt: skip


def code(source, uid, count, intent=None):
    nh = {"v": 1, "role": "code", "uid": uid}
    if intent is not None:
        nh["intent"] = intent
    return {"cell_type": "code", "id": uid, "execution_count": count, "outputs": [],
            "metadata": {"nh": nh}, "source": source}  # fmt: skip


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


# ---------------------------------------------------------------------- --review (§6.10)

PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
REVIEW_KEYS = {
    "ok", "complete", "notebook", "kernel", "ms", "timeout_s", "code_cells", "ran", "failing",
    "not_run", "stopped_at", "hidden_state", "src_candidates", "max_cell_lines", "summary",
    "flagged", "report",
}  # fmt: skip


def md(source):
    return {"cell_type": "markdown", "metadata": {}, "source": source}


def ready_runtime(tmp_path, calls=None):
    """A ready runtime venv (libexec/nh-sync's layout) whose Python is the server venv's. With
    ``calls``, its ``bin/python`` is a wrapper that appends each call's argv and the launch's
    environment to that file, then runs the server venv's Python."""
    data = tmp_path / "plugin-data"
    venv = data / ("venv-" + hashlib.sha256((SERVER / "uv.lock").read_bytes()).hexdigest()[:12])
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    if calls is None:
        python.symlink_to(VENV_PYTHON)
    else:
        python.write_text(
            "#!/bin/sh\n"
            f"{{ printf 'argv:%s\\n' \"$*\"; printf 'PYTHONPATH=%s\\n' \"$PYTHONPATH\"; "
            f"printf 'PYTHONPYCACHEPREFIX=%s\\n' \"$PYTHONPYCACHEPREFIX\"; }} >> '{calls}'\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        python.chmod(0o755)
    (venv / ".nh-ready").write_text("")
    return data


def review(env, project, data, *args, expect=0, **kw):
    proc = env.run("fresh-run", "--review", "--plugin-data", str(data), *args, "--json",
                   cwd=project, **kw)  # fmt: skip
    assert proc.stdout.count("\n") == 1, (proc.stdout, proc.stderr)
    assert proc.returncode == expect, (proc.returncode, proc.stdout, proc.stderr)
    return json.loads(proc.stdout), proc.stdout


def digest_of(env, project, data, *args):
    """The digest D154 gives for the cells it asks about (what --yes must name)."""
    asked, _ = review(env, project, data, *args, expect=2)
    assert asked["error"]["code"] == "D154", asked
    assert re.fullmatch(r"[0-9a-f]{16}", asked["digest"]), asked
    assert f"--yes {asked['digest']} " in asked["error"]["fix"]
    return asked["digest"]


FAILING = (  # reads the value itself, so the cell's source holds no secret
    'from pathlib import Path\npw = Path("../.env").read_text().split("=", 1)[1].strip()\n'
    'raise ValueError("x" * 490 + pw)'
)
FD_NOISE = (  # the kernel's own fd 1: plain lines and lines with another run's marker
    "import os\n"
    "forged = 'NH-REVIEW-0000000000000000 '\n"
    'os.write(1, (\'noise on fd 1\\n\' + forged + \'{"t": "cell", "i": 4, "status": '
    '"ok"}\\n\' + forged + \'{"t": "done"}\\n\').encode())'
)
REVIEWED = [
    md("# Sales study\n\n## Load"),
    *LOADER[:1],
    code(LOADER[1]["source"], "nh-aaaaaaaaaa", 3, "load rows.txt"),
    note("Plot clean", "nh-bbbbbbbbbb"),
    code("print(clean)", "nh-bbbbbbbbbb", 5, "show clean"),  # ran after the next one
    md("## Clean"),
    note("Make clean", "nh-cccccccccc"),
    code("clean = [r * 2 for r in rows]", "nh-cccccccccc", 4, "double each row"),
    note(f"Connect with {PASSWORD}", "nh-dddddddddd"),
    code(FAILING, "nh-dddddddddd", 6),
    code("\n".join(f"x{i} = {i}" for i in range(45)), "nh-eeeeeeeeee", 7),
    code(FD_NOISE, "nh-ffffffffff", 8),
]


def test_review_reports_without_touching_the_notebook(env, nb_project, tmp_path):
    (nb_project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    calls = tmp_path / "analysis-calls.txt"
    data = ready_runtime(tmp_path, calls)
    nb = nb_project / "notebooks/01_eda.ipynb"
    write_nb(nb, REVIEWED)
    before, mtime = nb.read_bytes(), nb.stat().st_mtime_ns
    # The failing cell reads .env and shows it in its error: secret_print, so --yes runs it.
    digest = digest_of(env, nb_project, data, "notebooks/01_eda.ipynb")
    result, out = review(env, nb_project, data, "notebooks/01_eda.ipynb", "--yes", digest)
    assert set(result) == REVIEW_KEYS
    assert (result["ok"], result["complete"], result["code_cells"]) == (False, True, 6)
    assert result["notebook"] == "notebooks/01_eda.ipynb" and result["kernel"] == "python3"
    assert result["ran"] == {"ok": 4, "error": 2, "not_run": 0}
    assert result["failing"] == [
        {"index": 4, "label": '"Plot clean" [5]', "ename": "NameError",
         "evalue": "name 'clean' is not defined"},
        {"index": 9, "label": '"Connect with [redacted:DB_PASSWORD]" [6]', "ename": "ValueError",
         "evalue": "x" * 490 + "[redacted:"},
    ]  # fmt: skip
    hidden = result["hidden_state"]
    assert hidden["read_before_defined"] == [
        {"index": 4, "label": '"Plot clean" [5]',
         "names": [{"name": "clean", "defined_in": '"Make clean" [4]'}]},
    ]  # fmt: skip
    assert [(c["index"], c["ename"]) for c in hidden["fails_only_fresh"]] == [(4, "NameError")]
    assert [(c["index"], c["after"]["index"]) for c in hidden["out_of_order"]] == [(7, 4)]
    assert result["src_candidates"] == [
        {"index": 10, "label": "the cell `x0 = 0` [7]", "lines": 45}
    ]
    assert [g["heading"] for g in result["summary"]] == ["Load", "Clean"]
    assert result["summary"][0]["cells"][0]["intent"] == "load rows.txt"
    assert result["flagged"] == [
        {"index": 9, "label": '"Connect with [redacted:DB_PASSWORD]" [6]',
         "title": "Connect with [redacted:DB_PASSWORD]", "rules": ["secret_print"]},
    ]  # fmt: skip
    assert result["stopped_at"] is None and result["not_run"] == []

    # The report: a file under .nh/reviews; no copy path, no .nh/tmp path, no nh- id, no secret.
    report = nb_project / result["report"]
    assert report.parent == nb_project / ".nh/reviews" and report.name.endswith("-01_eda.md")
    text = report.read_text()
    assert text.startswith("# Review of notebooks/01_eda.ipynb\n")
    assert "## Hidden state" in text and "## Cells over 40 lines (candidates for src/)" in text
    for shown in (out, text):
        assert "copy" not in json.loads(out) and ".nh/tmp" not in shown and "nh-" not in shown
        assert PASSWORD[:6] not in shown
    assert nb.read_bytes() == before and nb.stat().st_mtime_ns == mtime
    assert list((nb_project / ".nh/tmp").glob("rt-*")) == []  # the private runtime dir is gone
    event = json.loads((nb_project / ".nh/log.jsonl").read_text().splitlines()[-1])
    assert {k: event[k] for k in ("event", "nb", "ok", "complete", "failing", "not_run")} == {
        "event": "review", "nb": "notebooks/01_eda.ipynb", "ok": False, "complete": True,
        "failing": 2, "not_run": 0,
    }  # fmt: skip
    assert (event["hidden_state"], event["n_cells"], event["via"]) == (3, 6, "nbclient")
    # The analysis, launched as nh-mcp launches the gateway (V8), plus -P: bytecode under the
    # data dir, never in the plugin's source tree; the project's folder off sys.path.
    assert (data / "pycache").is_dir()
    launches = calls.read_text().splitlines()
    assert [ln for ln in launches if ln.startswith("argv:")] == [
        "argv:-s -P -m nh_gateway.review flag",
        "argv:-s -P -m nh_gateway.review flag",
        "argv:-s -P -m nh_gateway.review report",
    ]
    assert f"PYTHONPATH={SERVER / 'src'}" in launches
    assert f"PYTHONPYCACHEPREFIX={data / 'pycache'}" in launches

    human = env.run(
        "fresh-run", "--review", "--yes", digest, "--plugin-data", str(data), cwd=nb_project
    )
    assert human.returncode == 0, human.stderr
    assert human.stdout.startswith("# Review of notebooks/01_eda.ipynb\n")
    last = human.stdout.rstrip().splitlines()[-1]
    assert last.startswith("Report: .nh/reviews/") and last.endswith("-01_eda.md")
    assert "nh-" not in human.stdout and PASSWORD[:6] not in human.stdout


def test_a_deadline_gives_a_partial_review(env, nb_project, tmp_path):
    """D153 (design §6.10): the cells before the one running at the deadline are reported, that
    one is `stopped`, the rest `timeout`, and the report says it is partial. The sleeper's
    progress output, with no newline, can't hide its `cell_start` line."""
    data = ready_runtime(tmp_path)
    sleeper = "import os, time\nos.write(1, b'50%')\ntime.sleep(600)"
    cells = LOADER + [code(sleeper, "nh-bbbbbbbbbb", 4),
                      code("after = 1", "nh-cccccccccc", 5)]  # fmt: skip
    write_nb(nb_project / "notebooks/01_eda.ipynb", cells)
    result, _ = review(env, nb_project, data, "--timeout", "10", expect=1)
    assert result["error"]["code"] == "D153"
    assert result["error"]["message"] == (
        "The review stopped after 10s while the cell `import os, time` [4] was running; the "
        "report covers the cells before it."
    )
    assert (result["ok"], result["complete"], result["timeout_s"]) == (False, False, 10)
    assert result["ran"] == {"ok": 1, "error": 0, "not_run": 2}
    assert result["stopped_at"] == {"index": 2, "label": "the cell `import os, time` [4]"}
    assert [(c["index"], c["reason"]) for c in result["not_run"]] == [
        (2, "stopped"),
        (3, "timeout"),
    ]
    assert "**Partial:**" in (nb_project / result["report"]).read_text()
    human = env.run("fresh-run", "--review", "--timeout", "10", "--plugin-data", str(data),
                    cwd=nb_project)  # fmt: skip
    assert human.returncode == 1
    lines = human.stdout.rstrip().splitlines()
    assert lines[-3].startswith("Report: .nh/reviews/")
    assert lines[-2].startswith("error: The review stopped after 10s while the cell `import os,")
    assert lines[-1].startswith("fix: Rerun with a larger --timeout")
    assert left_running(str(nb_project)) == []


def left_running(text, wait=5.0):
    """Live processes whose command line holds ``text`` (the runner's names the copy, the
    kernel's its connection file), after up to ``wait`` s for a dying one to go (``ps``: Linux
    and macOS; a zombie counts as gone)."""
    end = time.monotonic() + wait
    while True:
        out = subprocess.run(["ps", "-eo", "stat=,args="], capture_output=True, text=True).stdout
        rows = [r for r in out.splitlines() if text in r and not r.lstrip().startswith("Z")]
        if not rows or time.monotonic() > end:
            return rows
        time.sleep(0.2)


def running(pid):
    """Alive and not a zombie (``ps``: Linux and macOS)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


SLEEPER = (
    "import os, subprocess, time\nopen('kernel.pid', 'w').write(str(os.getpid()))\n"
    "child = subprocess.Popen(['sleep', '300'])\nopen('child.pid', 'w').write(str(child.pid))\n"
    "time.sleep(600)"
)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_nhctl_stopped_mid_review_stops_its_kernel_too(env, nb_project, tmp_path, sig):
    """nhctl stopped from outside (a Bash tool's timeout, Ctrl-C) stops the runner, the kernel and
    what its cells started as the deadline does, then reports D198 (design §6.10)."""
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", [code(SLEEPER, "nh-aaaaaaaaaa", 1)])
    argv = ["/bin/sh", str(NHCTL), "fresh-run", "--review", "--plugin-data", str(data), "--json"]
    proc = subprocess.Popen(
        argv, cwd=nb_project, env=env.vars, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    pids = nb_project / "notebooks/child.pid"
    end = time.monotonic() + 90
    while proc.poll() is None and time.monotonic() < end:
        if pids.exists() and pids.read_text().strip():
            break
        time.sleep(0.1)
    assert proc.poll() is None, proc.communicate()
    kernel = int((nb_project / "notebooks/kernel.pid").read_text())
    child = int(pids.read_text())
    proc.send_signal(sig)  # nhctl's Python: bin/nhctl execs it
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 1, (out, err)
    assert json.loads(out)["error"]["code"] == "D198"
    assert left_running(str(nb_project)) == []
    assert not running(kernel) and not running(child)


def test_a_deadline_before_any_cell_ran(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER + [code("x = 1", "nh-bbbbbbbbbb", 4)])
    result, _ = review(env, nb_project, data, "--timeout", "0.5", expect=1)
    assert result["error"] == {
        "code": "D153",
        "message": "The review stopped after 0.5s before any cell ran; the report covers the "
        "cells that finished.",
        "fix": "Rerun with a larger --timeout.",
    }
    assert result["stopped_at"] is None and result["complete"] is False
    assert result["ran"] == {"ok": 0, "error": 0, "not_run": 2}
    assert {c["reason"] for c in result["not_run"]} == {"timeout"}
    text = (nb_project / result["report"]).read_text()
    assert "**Partial:** the review stopped after 0.5 s before any cell ran" in text
    assert left_running(str(nb_project)) == []


def test_review_keeps_three_copies_and_leaves_fresh_runs_alone(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    tmp = nb_project / ".nh/tmp"
    tmp.mkdir(parents=True)
    for n in range(4):
        for kind in ("review", "fresh"):
            old = tmp / f"{kind}-2026010{n}T000000-1.ipynb"
            old.write_text("{}")
            os.utime(old, (1_700_000_000 + n, 1_700_000_000 + n))
    gone = subprocess.Popen(["true"])
    gone.wait()
    (tmp / f"rt-20260101T000000-{gone.pid}").mkdir()  # a SIGKILLed run's: its nhctl is gone
    (tmp / f"rt-20260101T000000-{os.getpid()}").mkdir()  # a running one's
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER)
    result, _ = review(env, nb_project, data)  # default: harness.toml's notebook
    assert result["ok"] is True and result["ran"] == {"ok": 1, "error": 0, "not_run": 0}
    reviews = sorted(p.name for p in tmp.glob("review-*.ipynb"))
    assert len(reviews) == 3
    assert reviews[:2] == ["review-20260102T000000-1.ipynb", "review-20260103T000000-1.ipynb"]
    assert len(list(tmp.glob("fresh-*.ipynb"))) == 4
    assert [p.name for p in tmp.glob("rt-*")] == [f"rt-20260101T000000-{os.getpid()}"]


TOKENS = (
    "import json, os\n"
    "names = ('JUPYTER_TOKEN', 'JUPYTER_TOKEN_FILE', 'NH_JUPYTER_TOKEN', 'NH_KEEP')\n"
    "seen = {n: n in os.environ for n in names}\n"
    "open('seen.json', 'w').write(json.dumps(seen))"
)


def test_no_fresh_run_passes_a_jupyter_token_to_the_kernel(env, nb_project, tmp_path):
    """The kernel runs the notebook's code: neither runner hands it a token (design §6.10)."""
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", [code(TOKENS, "nh-aaaaaaaaaa", 1)])
    extra = {"JUPYTER_TOKEN": "t0k3n-a", "JUPYTER_TOKEN_FILE": str(tmp_path / "tok"),
             "NH_JUPYTER_TOKEN": "t0k3n-b", "NH_KEEP": "1"}  # fmt: skip
    seen = nb_project / "notebooks/seen.json"
    expected = {"JUPYTER_TOKEN": False, "JUPYTER_TOKEN_FILE": False, "NH_JUPYTER_TOKEN": False,
                "NH_KEEP": True}  # fmt: skip
    assert env.json("fresh-run", cwd=nb_project, extra=extra)["ok"] is True
    assert json.loads(seen.read_text()) == expected
    seen.unlink()
    result, _ = review(env, nb_project, data, extra=extra)
    assert result["ok"] is True and result["flagged"] == []
    assert json.loads(seen.read_text()) == expected


NETWORK = "import urllib.request\nurllib.request.urlopen('https://data.example.org/x')"
FLAGGED = [
    code("import os\nopen('ran.txt', 'w').write('plain')", "nh-aaaaaaaaaa", 1),
    code("%pip install no-such-package-xyz", "nh-bbbbbbbbbb", 2),
    code(NETWORK, "nh-cccccccccc", 3),
    note("Show home", "nh-dddddddddd"),
    code("print(os.environ['HOME'])\nopen('home.txt', 'w').write('shown')", "nh-dddddddddd", 4),
    code("api_key = 'abc123'\napi_key", "nh-eeeeeeeeee", 5),
]


def test_cells_that_do_more_than_compute_stop_the_review_before_it_runs(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    nb = nb_project / "notebooks/01_eda.ipynb"
    write_nb(nb, FLAGGED)
    result, out = review(env, nb_project, data, expect=2)
    assert result["error"]["code"] == "D154"
    assert result["error"]["message"].endswith(". Nothing ran.")
    assert "--yes" in result["error"]["fix"] and "--skip-flagged" in result["error"]["fix"]
    assert result["notebook"] == "notebooks/01_eda.ipynb"
    assert [(f["index"], f["rules"]) for f in result["flagged"]] == [
        (1, ["package_install"]),
        (2, ["network"]),
        (4, ["secret_print"]),
        (5, ["secret_name"]),
    ]
    assert result["flagged"][2]["label"] == '"Show home" [4]'
    assert result["flagged"][2]["title"] == "Show home"
    assert "nh-" not in out
    assert not (nb_project / "notebooks/ran.txt").exists()  # nothing ran
    assert list((nb_project / ".nh/tmp").glob("review-*")) == []  # its copy is gone
    assert not (nb_project / ".nh/reviews").exists()
    human = env.run("fresh-run", "--review", "--plugin-data", str(data), cwd=nb_project)
    assert human.returncode == 2
    assert human.stdout.startswith("error: 4 cell(s) would do more than compute")
    assert '"Show home" [4] (secret_print)' in human.stdout


def test_skip_flagged_runs_the_rest(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", FLAGGED)
    result, _ = review(env, nb_project, data, "--skip-flagged")
    assert result["complete"] is True and result["ok"] is False
    assert result["ran"] == {"ok": 1, "error": 0, "not_run": 4}
    assert [(c["index"], c["reason"]) for c in result["not_run"]] == [
        (1, "flagged"),
        (2, "flagged"),
        (4, "flagged"),
        (5, "flagged"),
    ]
    assert (nb_project / "notebooks/ran.txt").read_text() == "plain"
    assert not (nb_project / "notebooks/home.txt").exists()
    text = (nb_project / result["report"]).read_text()
    assert "## Not run" in text and "flagged (secret_print), skipped" in text


def test_yes_runs_the_flagged_cells_too(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", [FLAGGED[0], *FLAGGED[3:]])
    digest = digest_of(env, nb_project, data)
    result, _ = review(env, nb_project, data, "--yes", digest)
    assert result["ok"] is True and result["ran"] == {"ok": 3, "error": 0, "not_run": 0}
    assert [f["rules"] for f in result["flagged"]] == [["secret_print"], ["secret_name"]]
    assert (nb_project / "notebooks/home.txt").read_text() == "shown"


def test_a_yes_runs_only_the_cells_it_was_given_for(env, nb_project, tmp_path):
    """--yes names the digest of the cells D154 listed: a cell flagged since (added, or its code
    changed) makes it D154 again, with the new list and digest, and nothing runs (design §6.10)."""
    data = ready_runtime(tmp_path)
    nb = nb_project / "notebooks/01_eda.ipynb"
    shown = code("import os\nprint(os.environ.get('HOME'))", "nh-aaaaaaaaaa", 1)
    write_nb(nb, [shown])
    first = digest_of(env, nb_project, data)
    sneaky = "import os\nopen('../never_shown.txt', 'w').write('x')\nprint(os.environ['PATH'])"
    write_nb(nb, [shown, code(sneaky, "nh-bbbbbbbbbb", 2)])
    again, _ = review(env, nb_project, data, "--yes", first, expect=2)
    assert again["error"]["code"] == "D154"
    assert again["error"]["message"].startswith(
        "The cells to ask about changed since that yes. 2 cell(s) would do more than compute"
    )
    assert [f["index"] for f in again["flagged"]] == [0, 1] and again["digest"] != first
    assert not (nb_project / "never_shown.txt").exists()
    assert not list((nb_project / ".nh/tmp").glob("review-*"))
    result, _ = review(env, nb_project, data, "--yes", again["digest"])
    assert result["complete"] is True and (nb_project / "never_shown.txt").exists()


def test_review_needs_nhs_own_runtime(env, nb_project, tmp_path):
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER)
    empty = tmp_path / "empty-data"
    empty.mkdir()
    result, _ = review(env, nb_project, empty, expect=1)
    assert result["error"]["code"] == "D120"
    assert "nhctl runtime sync" in result["error"]["fix"]
    data = ready_runtime(tmp_path)
    next(data.glob("venv-*/.nh-ready")).unlink()  # built, not ready
    assert review(env, nb_project, data, expect=1)[0]["error"]["code"] == "D120"
    # From the repo checkout nh can't tell where its data lives without --plugin-data.
    located = env.json("fresh-run", "--review", cwd=nb_project, expect=1)
    assert located["error"]["code"] == "D121"
    assert not (nb_project / ".nh/tmp").exists() or not list((nb_project / ".nh/tmp").iterdir())


def test_review_options_go_with_review(env, nb_project):
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER)
    for flag in (["--yes", "0123456789abcdef"], ["--skip-flagged"]):
        report = env.json("fresh-run", *flag, cwd=nb_project, expect=2)
        assert report["error"]["code"] == "D100"
        assert "--review" in report["error"]["message"]
    both = env.json(
        "fresh-run", "--review", "--yes", "0123456789abcdef", "--skip-flagged", cwd=nb_project,
        expect=2,
    )  # fmt: skip
    assert both["error"]["code"] == "D100" and "not allowed with" in both["error"]["message"]
    bare = env.json("fresh-run", "--review", "--yes", cwd=nb_project, expect=2)
    assert bare["error"]["code"] == "D100" and "--yes" in bare["error"]["message"]


@pytest.mark.parametrize(
    "option", ["--timeout nan", "--timeout inf", "--timeout -3", "--timeout 0",
               "--cell-timeout 0", "--cell-timeout -1", "--cell-timeout 2.5"],
)  # fmt: skip
@pytest.mark.parametrize("mode", [[], ["--review"]], ids=["plain", "review"])
def test_timeouts_must_be_finite_and_above_zero(env, nb_project, option, mode):
    """A bad --timeout would put NaN or Infinity in the JSON and stop the run at once (design
    §6.10): it is a usage error, in both modes, before anything runs."""
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER)
    proc = env.run("fresh-run", *mode, *option.split(), "--json", cwd=nb_project)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)

    def strict(constant):
        raise ValueError(f"non-standard JSON constant {constant}")

    report = json.loads(proc.stdout, parse_constant=strict)
    assert report["error"]["code"] == "D100"
    assert option.split()[0] in report["error"]["message"]
    assert not (nb_project / ".nh/tmp").exists()


# Calls freshrun's argument parser and helpers directly (design §6.10).
OPTIONS = """
import argparse, json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import freshrun
parser = argparse.ArgumentParser()
freshrun.add_parsers(parser.add_subparsers(), argparse.ArgumentParser(add_help=False))
skill = parser.parse_args(["fresh-run", "--review", "--timeout", "540"])  # the skill's
project = Path("/srv/work/sales")
dirs = [freshrun._notebook_dir(project, project / p) for p in
        ("notebooks/a.ipynb", "notebooks/eda/b.ipynb", "a.ipynb", "../other/c.ipynb",
         "..cache/d.ipynb")]
print(json.dumps({
    "skill": [skill.timeout, skill.cell_timeout],
    "defaults": [freshrun._review_cell_timeout(t) for t in (540, 10, 1800, 0.5, 100)],
    "dirs": dirs,
}))
"""


@pytest.mark.parametrize("python", PYTHONS)
def test_the_reviews_cell_timeout_and_notebook_dir(tmp_path, python):
    """The skill's --timeout 540 leaves --cell-timeout unset, so the review's default, 180 s,
    interrupts a hanging cell well before the deadline (V7's interrupt_on_timeout). L013 gets the
    notebook's folder relative to the project, "" for the root, and for a notebook outside the
    project its own folder, absolute: the review's kernel runs there (C10b's review)."""
    argv = [str(PLUGIN / "scripts" / "nhctl"), str(SERVER / "src")]
    proc = subprocess.run(
        [python, "-c", OPTIONS, *argv], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert shown["skill"] == [540.0, None]
    assert shown["defaults"] == [180, 30, 600, 30, 33]
    assert shown["dirs"] == ["notebooks", "notebooks/eda", "", "/srv/work/other", "..cache"]


def test_a_cell_over_its_timeout_is_interrupted_and_the_review_goes_on(env, nb_project, tmp_path):
    """interrupt_on_timeout=True (V7): the cell fails with KeyboardInterrupt, named as the
    per-cell limit's, and the cells below it still run."""
    data = ready_runtime(tmp_path)
    cells = [code("a = 1", "nh-aaaaaaaaaa", 1), code("import time\ntime.sleep(30)", "nh-bbbbbbbbbb", 2),
             code("b = a + 1", "nh-cccccccccc", 3)]  # fmt: skip
    write_nb(nb_project / "notebooks/01_eda.ipynb", cells)
    started = time.monotonic()
    result, _ = review(env, nb_project, data, "--cell-timeout", "2", "--timeout", "120")
    assert time.monotonic() - started < 60
    assert (result["complete"], result["ran"]) == (True, {"ok": 2, "error": 1, "not_run": 0})
    assert result["failing"] == [
        {"index": 1, "label": "the cell `import time` [2]", "ename": "KeyboardInterrupt",
         "evalue": "stopped by the review's 2 s limit per cell"},
    ]  # fmt: skip


def test_a_kernel_that_dies_leaves_the_cells_below_not_run(env, nb_project, tmp_path):
    data = ready_runtime(tmp_path)
    cells = [code("a = 1", "nh-aaaaaaaaaa", 1), code("import os\nos._exit(1)", "nh-bbbbbbbbbb", 2),
             code("b = a + 1", "nh-cccccccccc", 3), code("c = 2", "nh-dddddddddd", 4)]  # fmt: skip
    write_nb(nb_project / "notebooks/01_eda.ipynb", cells)
    result, _ = review(env, nb_project, data)
    assert (result["complete"], result["ok"]) == (True, False)
    assert [(c["index"], c["ename"]) for c in result["failing"]] == [(1, "DeadKernelError")]
    assert [(c["index"], c["reason"]) for c in result["not_run"]] == [
        (2, "kernel_died"),
        (3, "kernel_died"),
    ]
    assert left_running(str(nb_project)) == []


FD_WRITERS = [  # the kernel's fd-level output, which ipykernel echoes to the runner's stdout
    code("import os\nos.write(1, b'partial, no newline')", "nh-aaaaaaaaaa", 1),
    code(  # a C library: stdio block-buffers on a pipe, so it flushes mid-line
        "import ctypes\nlibc = ctypes.CDLL(None)\n"
        "for i in range(300):\n    libc.printf(b'iteration %d: loss=0.123\\n', i)",
        "nh-bbbbbbbbbb", 2,
    ),
    code(  # a background process writing with no newline, in the kernel's group
        "import subprocess\n"
        "bg = subprocess.Popen(['sh', '-c', 'while :; do printf x; sleep 0.01; done'])",
        "nh-cccccccccc", 3,
    ),
    code("a = 1", "nh-dddddddddd", 4),
    code("import atexit, os\natexit.register(lambda: os.write(1, b'bye'))", "nh-eeeeeeeeee", 5),
    code("b = a + 1", "nh-ffffffffff", 6),
]  # fmt: skip


def test_the_kernels_own_output_never_hides_a_record(env, nb_project, tmp_path):
    """MARKER lines go to a pipe of their own (design §6.10): fd-level output without a newline
    (os.write, a C library, a background process, a write at the kernel's exit) can't glue
    itself to one, so every cell is reported and the review completes."""
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", FD_WRITERS)
    result, _ = review(env, nb_project, data)
    assert (result["ok"], result["complete"]) == (True, True), result
    assert result["ran"] == {"ok": 6, "error": 0, "not_run": 0} and result["not_run"] == []
    assert left_running(str(nb_project)) == []


# Calls freshrun.parse_run directly: the runner's lines are the only thing it trusts, and only
# with this run's nonce (design §6.10).
PARSE = """
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import freshrun
from nh_gateway._shared import secrets
secrets.install(secrets.Redactor.for_project(Path(sys.argv[3]), {}))
print(json.dumps(freshrun.parse_run(sys.argv[4], sys.argv[5])))
"""


@pytest.mark.parametrize("python", PYTHONS)
def test_the_review_reads_only_its_own_marker_lines(tmp_path, python):
    (tmp_path / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    marker = "NH-REVIEW-0123456789abcdef "
    forged = "NH-REVIEW-fedcba9876543210 "
    evalue = "y" * 495 + PASSWORD

    def line(**fields):
        return marker + json.dumps(fields)

    out = "\n".join(
        [
            "Traceback noise from a cell",
            line(t="start", kernel="python3", pid=4242, pgid=4242),
            line(t="cell_start", i=1),
            line(t="cell", i=1, status="ok", ms=12),
            forged + json.dumps({"t": "cell", "i": 2, "status": "ok", "ms": 1}),
            f"print('{marker}')",  # a cell's own output that merely mentions it
            marker + "{not json",
            marker + "[1, 2]",
            line(t="cell", i="3", status="ok"),
            line(t="cell", i=True, status="ok"),
            line(t="cell", i=4, status="exploded"),
            line(t="mystery", i=5),
            line(t="cell_start", i=3),
            line(t="cell", i=3, status="error", ename=f"Login{PASSWORD}", evalue=evalue, ms="x"),
            line(t="cell", i=6, status="not_run", reason="flagged"),
            line(t="cell_start", i=7),
            "  " + line(t="done"),  # not at the start of the line
        ]
    )
    argv = [str(PLUGIN / "scripts" / "nhctl"), str(SERVER / "src"), str(tmp_path), out, marker]
    proc = subprocess.run(
        [python, "-c", PARSE, *argv], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "kernel": "python3",
        "cells": [
            {"i": 1, "status": "ok", "ms": 12},
            {"i": 3, "status": "error", "ms": None, "ename": "Login[redacted:DB_PASSWORD]",
             "evalue": "y" * 495 + "[reda"},
            {"i": 6, "status": "not_run", "ms": None, "reason": "flagged"},
        ],
        "running": 7,
        "done": False,
    }  # fmt: skip
    assert PASSWORD[:4] not in proc.stdout


# Calls freshrun._run_isolated directly with a writer standing in for the runner: 150 MB of a
# cell's output on stdout, its records on the record pipe (the last argument).
FLOOD = r'''
import json, resource, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import freshrun
marker = "NH-REVIEW-0123456789abcdef "
writer = r"""
import json, os, sys
records = os.fdopen(int(sys.argv[2]), "w")
line = b"y" * 1023 + b"\n"
for _ in range(150 * 1024):
    os.write(1, line)
os.write(1, b"last words, no newline")
records.write(sys.argv[1] + json.dumps({"t": "done"}) + "\n")
records.flush()
"""
scale = 1 if sys.platform != "darwin" else 1024  # ru_maxrss: KB on Linux, bytes on macOS
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // scale
run = freshrun._run_isolated([sys.executable, "-c", writer, marker], ".", {}, 120, marker)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // scale
parsed = freshrun.parse_run(run.out, marker)
print(json.dumps({
    "done": parsed["done"], "timed_out": run.timed_out, "grew_mb": (after - before) // 1024,
    "tail": run.tail.splitlines()[-2:], "tail_len": len(run.tail),
}))
'''


def test_nhctl_keeps_only_a_tail_of_the_runners_other_output(tmp_path):
    """The kernel's output reaches nhctl through the runner's stdout: nhctl keeps the records and
    the last 15 lines (at most 64 KB) of the rest, not every byte (design §6.10)."""
    argv = [str(PLUGIN / "scripts" / "nhctl"), str(SERVER / "src")]
    proc = subprocess.run(
        [sys.executable, "-c", FLOOD, *argv], capture_output=True, text=True, timeout=300,
        check=False, cwd=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert (shown["done"], shown["timed_out"]) == (True, False)
    assert shown["grew_mb"] < 32, shown
    assert shown["tail"][-1] == "last words, no newline"
    assert shown["tail_len"] <= 64 * 1024 + 100


NONCE = (
    "import os\n"
    "argv = open(f'/proc/{os.getppid()}/cmdline', 'rb').read().split(b'\\0')\n"
    "seen = [a.decode() for a in argv if a.startswith(b'NH-REVIEW-')]\n"
    "open('marker.txt', 'a').write(repr(seen) + '\\n')"
)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads the runner's argv in /proc")
def test_each_review_has_its_own_marker(env, nb_project, tmp_path):
    """`NH-REVIEW-<16 random hex> `, new for each run (design §6.10)."""
    data = ready_runtime(tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", [code(NONCE, "nh-aaaaaaaaaa", 1)])
    for _ in range(2):
        assert review(env, nb_project, data)[0]["ok"] is True
    lines = (nb_project / "notebooks/marker.txt").read_text().splitlines()
    markers = [ast.literal_eval(line) for line in lines]
    assert [len(m) for m in markers] == [1, 1]
    assert all(re.fullmatch(r"NH-REVIEW-[0-9a-f]{16} ", m[0]) for m in markers), markers
    assert markers[0] != markers[1]


def test_a_plain_fresh_run_past_its_deadline_leaves_nothing_running(env, nb_project):
    """D152 (plain fresh-run, design §6.10's deadline): the runner, the kernel and the child a
    cell started are stopped, and the private runtime dir is gone."""
    write_nb(nb_project / "notebooks/01_eda.ipynb", [code(SLEEPER, "nh-aaaaaaaaaa", 1)])
    report = env.json("fresh-run", "--timeout", "6", cwd=nb_project, expect=1)
    assert report["error"] == {
        "code": "D152",
        "message": "The fresh run took longer than 6s and was stopped.",
        "fix": "Rerun with a larger --timeout, or check for a cell that waits forever.",
    }
    kernel = int((nb_project / "notebooks/kernel.pid").read_text())
    child = int((nb_project / "notebooks/child.pid").read_text())
    assert left_running(str(nb_project)) == []
    assert not running(kernel) and not running(child)
    assert list((nb_project / ".nh/tmp").glob("rt-*")) == []


def own_session_child(pid_file, then=""):
    """A cell that starts a process in its own session (outside the kernel's group)."""
    return (
        "import subprocess, time\n"
        "esc = subprocess.Popen(['sleep', '299'], start_new_session=True)\n"
        f"open('{pid_file}', 'w').write(str(esc.pid))\n{then}"
    )


def gone(pid, wait=5.0):
    end = time.monotonic() + wait
    while running(pid) and time.monotonic() < end:
        time.sleep(0.2)
    alive = running(pid)
    if alive:  # the test's own leftover: stop it, then fail
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return not alive


def test_what_a_cell_started_in_its_own_session_is_stopped_too(env, nb_project, tmp_path):
    """Outside the kernel's group: the runner stops it after a complete review, nhctl at the
    deadline (it lists the runner's descendants before any signal; design §6.10)."""
    data = ready_runtime(tmp_path)
    nb = nb_project / "notebooks/01_eda.ipynb"
    write_nb(nb, [code(own_session_child("done.pid"), "nh-aaaaaaaaaa", 1)])
    assert review(env, nb_project, data)[0]["complete"] is True
    assert gone(int((nb_project / "notebooks/done.pid").read_text())), "after a complete review"
    write_nb(nb, [code(own_session_child("late.pid", "time.sleep(600)"), "nh-aaaaaaaaaa", 1)])
    result, _ = review(env, nb_project, data, "--timeout", "10", expect=1)
    assert result["error"]["code"] == "D153"
    assert gone(int((nb_project / "notebooks/late.pid").read_text())), "after the deadline"
    assert left_running(str(nb_project)) == []


# A stand-in for the project env's Python: it runs FAKE_RUNNER instead of the review runner,
# which writes a `cell` line for every code cell of the copy, then (`slow`) lingers as a kernel's
# slow shutdown would, or (`short`) leaves one cell out and still says `done`.
FAKE_RUNNER = """
import json, os, sys, time
copy, marker, skip, fd = sys.argv[1], sys.argv[5], sys.argv[6], int(sys.argv[7])
out = os.fdopen(fd, "w")
cells = json.load(open(copy))["cells"]
code = [i for i, c in enumerate(cells) if c["cell_type"] == "code"]
if os.environ.get("NH_FAKE") == "short":
    code = code[:-1]
out.write(marker + json.dumps({"t": "start", "kernel": "python3", "pid": None, "pgid": None}) + "\\n")
for i in code:
    out.write(marker + json.dumps({"t": "cell", "i": i, "status": "ok", "ms": 1}) + "\\n")
out.flush()
if os.environ.get("NH_FAKE") == "slow":
    time.sleep(120)
out.write(marker + json.dumps({"t": "done"}) + "\\n")
out.flush()
"""


def fake_env(nb_project, tmp_path):
    prefix = tmp_path / "fake-env"
    (prefix / "bin").mkdir(parents=True)
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER)
    python = prefix / "bin" / "python"
    python.write_text(f'#!/bin/sh\nshift 3\nexec "{VENV_PYTHON}" "{runner}" "$@"\n')  # -I -c code
    python.chmod(0o755)
    (nb_project / ".nh/state/env.json").write_text(
        json.dumps({"manager": "uv", "prefix": str(prefix)})
    )


def test_a_deadline_after_every_cells_line_is_a_complete_review(env, nb_project, tmp_path):
    """Only the kernel's shutdown (here a runner that lingers) was cut short: nothing the report
    needs is missing, so it is complete, exit 0, not D153 (design §6.10)."""
    data = ready_runtime(tmp_path)
    fake_env(nb_project, tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER + [code("x = 1", "nh-bbbbbbbbbb", 4)])
    result, _ = review(env, nb_project, data, "--timeout", "3", extra={"NH_FAKE": "slow"})
    assert (result["ok"], result["complete"], result["stopped_at"]) == (True, True, None)
    assert "error" not in result and result["ran"] == {"ok": 2, "error": 0, "not_run": 0}
    assert "Partial" not in (nb_project / result["report"]).read_text()
    event = json.loads((nb_project / ".nh/log.jsonl").read_text().splitlines()[-1])
    assert (event["event"], event["complete"]) == ("review", True)


def test_a_run_that_ends_without_every_cells_line_is_d151(env, nb_project, tmp_path):
    """Before the deadline, a missing `cell` line means the runner broke, whatever its `done`
    line says: D151, not a report that calls a cell "not reached"."""
    data = ready_runtime(tmp_path)
    fake_env(nb_project, tmp_path)
    write_nb(nb_project / "notebooks/01_eda.ipynb", LOADER + [code("x = 1", "nh-bbbbbbbbbb", 4)])
    result, _ = review(env, nb_project, data, expect=1, extra={"NH_FAKE": "short"})
    assert result["error"]["code"] == "D151"
    assert result["error"]["message"] == "The review run didn't finish (exit 0)."
    assert not (nb_project / ".nh/reviews").exists()
