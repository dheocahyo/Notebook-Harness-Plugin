"""/nh:review's analysis, ``nh_gateway.review`` (design §6.10): hidden state, out-of-order counts,
fresh-only failures, long cells, the intent summary, the flagged cells, the report and its
redaction, in-process and through ``python -m nh_gateway.review``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nh_gateway import review
from nh_gateway._shared import secrets

SRC = Path(__file__).resolve().parents[2] / "plugins" / "nh" / "server" / "src"
PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "work" / "sales_study"
    (root / ".nh" / "state").mkdir(parents=True)
    (root / "notebooks").mkdir()
    secrets.install(secrets.Redactor.for_project(root))
    yield root
    secrets.reset()


def note(title: str, uid: str) -> dict:
    meta = {"nh": {"v": 1, "role": "note", "uid": uid + "-n", "pair_uid": uid}}
    return {"cell_type": "markdown", "id": uid + "-n", "metadata": meta,
            "source": f"### {title}\n\n- one\n- two"}  # fmt: skip


def code(source: str, uid: str, count: int | None, intent: str | None = None, outputs=()) -> dict:
    nh: dict = {"v": 1, "role": "code", "uid": uid, "pair_uid": uid + "-n"}
    if intent is not None:
        nh["intent"] = intent
    return {"cell_type": "code", "id": uid, "execution_count": count, "outputs": list(outputs),
            "metadata": {"nh": nh}, "source": source}  # fmt: skip


def plain(source: str, count: int | None = None, outputs=()) -> dict:
    """A cell the user wrote: no nh metadata, no note."""
    return {"cell_type": "code", "execution_count": count, "outputs": list(outputs),
            "metadata": {}, "source": source}  # fmt: skip


def md(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source}


def cells_of(*items: dict) -> list[review.Cell]:
    return review.load_cells({"cells": list(items)})


ERROR_OUTPUT = {"output_type": "error", "ename": "KeyError", "evalue": "'price'", "traceback": []}
STREAM = {"output_type": "stream", "name": "stdout", "text": "3\n"}


def run_of(*cells: tuple, running=None, done=True) -> dict:
    """A parsed run: (index, status[, ename, evalue | reason])."""
    entries = []
    for index, status, *rest in cells:
        entry = {"i": index, "status": status, "ms": 5}
        if status == "error":
            entry.update(ename=rest[0], evalue=rest[1] if len(rest) > 1 else "")
        if status == "not_run":
            entry["reason"] = rest[0]
        entries.append(entry)
    return {"kernel": "python3", "cells": entries, "running": running, "done": done}


# ------------------------------------------------------------------- hidden state


def test_a_name_read_before_any_definition_names_the_later_cell_that_defines_it():
    cells = cells_of(
        note("Load rows", "nh-aaaaaaaaaa"), code("rows = [1, 2]", "nh-aaaaaaaaaa", 1),
        note("Plot clean", "nh-bbbbbbbbbb"), code("print(clean, rows, total)", "nh-bbbbbbbbbb", 3),
        note("Make clean", "nh-cccccccccc"), code("clean = rows * 2", "nh-cccccccccc", 2),
    )  # fmt: skip
    found = review.read_before_defined(cells, review.Labels(cells))
    assert found == [
        {"index": 3, "label": '"Plot clean" [3]', "names": [
            {"name": "clean", "defined_in": '"Make clean" [2]'},
            {"name": "total", "defined_in": None},
        ]},
    ]  # fmt: skip


def test_builtins_ipython_names_and_own_bindings_are_not_hidden_state():
    cells = cells_of(
        plain("import pandas as pd\ndf = pd.DataFrame()", 1),
        plain("display(df)\nprint(len(df), In, Out, _, _3, get_ipython())", 2),
        plain("def f():\n    return later\nlater = 1\nf()", 3),
        plain("for i in range(3):\n    total = i\ntotal", 4),
    )
    assert review.read_before_defined(cells, review.Labels(cells)) == []


def test_a_cell_that_reads_its_own_output_reads_it_before_defining_it():
    """``Flow.now``: a read before the cell's own binding counts (design §6.10), so the classic
    hidden-state cells are reported, while a binding in a branch above the read still counts."""
    for source, name in (
        ("df = df.dropna()", "df"),
        ("counter += 1", "counter"),
        ("x = x + 1", "x"),
        ("rows.append(1)\nrows = []", "rows"),
        ("print(total)\ntotal = 1", "total"),
        ("del gone", "gone"),
    ):
        cells = cells_of(plain("import pandas as pd", 1), plain(source, 2))
        found = review.read_before_defined(cells, review.Labels(cells))
        assert [(c["index"], [n["name"] for n in c["names"]]) for c in found] == [(1, [name])]
        assert found[0]["names"][0]["defined_in"] is None, source
    branch = cells_of(plain("if True:\n    y = 1\nprint(y)", 1))
    assert review.read_before_defined(branch, review.Labels(branch)) == []


def test_a_name_read_only_when_a_function_is_called():
    """``Flow.later``: a function, lambda or method body reads its names when it is called, so a
    helper written above the cell that defines its global isn't hidden state; a name no cell
    defines is (only the kernel had it)."""
    cells = cells_of(
        plain("def plot():\n    return df_clean.describe()", 1),
        plain("class A:\n    def m(self):\n        return later\n    size = 2", 2),
        plain("g = lambda: later2", 3),
        plain("df_clean = [1, 2]\nlater = later2 = 3", 4),
        plain("plot(), A().m(), g()", 5),
    )
    assert review.read_before_defined(cells, review.Labels(cells)) == []
    ghost = cells_of(plain("def f():\n    return nowhere\nk = lambda: also_nowhere", 1))
    found = review.read_before_defined(ghost, review.Labels(ghost))
    assert found[0]["names"] == [
        {"name": "also_nowhere", "defined_in": None},
        {"name": "nowhere", "defined_in": None},
    ]
    open_cell = cells_of(plain("def f():\n    return nowhere", 1), plain("exec('nowhere = 1')", 2))
    assert review.read_before_defined(open_cell, review.Labels(open_cell)) == []


def test_cells_nh_cant_read_are_skipped():
    """A cell that doesn't parse, a cell magic, or one below a cell that may bind anything (one
    that doesn't parse, a star import, %run) can't be judged: as L120, they report nothing."""
    for sources in (["print(x"], ["print(x", "print(y)"], ["%%bash\necho $x"]):
        cells = cells_of(*(plain(source) for source in sources))
        assert review.read_before_defined(cells, review.Labels(cells)) == [], sources
    star = cells_of(plain("from math import *"), plain("print(pi, nowhere)"))
    assert review.read_before_defined(star, review.Labels(star)) == []
    run = cells_of(plain("%run helpers.py"), plain("print(helper)"))
    assert review.read_before_defined(run, review.Labels(run)) == []


def test_out_of_order_counts():
    cells = cells_of(
        plain("a = 1", 1),
        plain("b = 2", 5),
        plain("c = 3", 3),
        plain("d = 4", None),  # never ran: skipped
        plain("e = 5", 4),
        plain("f = 6", 6),
    )
    found = review.out_of_order(cells, review.Labels(cells))
    assert [(c["index"], c["count"], c["after"]["index"], c["after"]["count"]) for c in found] == [
        (2, 3, 1, 5),
        (4, 4, 1, 5),
    ]
    assert found[0]["label"] == "the cell `c = 3` [3]"
    assert found[0]["after"]["label"] == "the cell `b = 2` [5]"
    assert found[0]["same_as"] is None
    in_order = cells_of(plain("a = 1", 1), plain("b = 2", 2), plain("c = 3", None))
    assert review.out_of_order(in_order, review.Labels(in_order)) == []


def test_equal_counts_come_from_different_sessions():
    """One kernel session never gives a count twice: a count equal to one above is reported as
    ``same_as`` that cell; a lower one stays ``after`` (design §6.10)."""
    cells = cells_of(plain("a = 1", 3), plain("b = a", 3), plain("c = b", 1), plain("d = 1", 4))
    found = review.out_of_order(cells, review.Labels(cells))
    assert [(c["index"], c["after"], c["same_as"]) for c in found] == [
        (1, None, {"index": 0, "label": "the cell `a = 1` [3]", "count": 3}),
        (2, {"index": 0, "label": "the cell `a = 1` [3]", "count": 3}, None),
    ]
    report = review.build_report(
        Path("/nonexistent"), cells, {"run": run_of((0, "ok"), (1, "ok"), (2, "ok"), (3, "ok"))}
    )
    markdown = review.render_markdown(report)
    assert (
        "- the cell `b = a` [3] has the same count as the cell `a = 1` [3] above it, so they ran "
        "in different kernel sessions"
    ) in markdown
    assert "- the cell `c = b` [1] ran before the cell `a = 1` [3] above it" in markdown


def test_name_and_key_errors_where_the_notebook_shows_a_clean_run():
    cells = cells_of(
        plain("x = 1", 1),
        plain("print(gone)", 2, [STREAM]),  # NameError now; ran clean in the notebook
        plain("d = {}\nd['price']", 3, [ERROR_OUTPUT]),  # already failed in the notebook
        plain("d['qty']", None),  # never ran in the notebook
        plain("raise ValueError('x')", 4),  # not a hidden-state error
        plain("def f():\n    v += 1\nf()", 5),  # UnboundLocalError counts as a NameError
        plain("cfg['k']", 6),
    )
    run = run_of(
        (0, "ok"),
        (1, "error", "NameError", "name 'gone' is not defined"),
        (2, "error", "KeyError", "'price'"),
        (3, "error", "KeyError", "'qty'"),
        (4, "error", "ValueError", "x"),
        (5, "error", "UnboundLocalError", "cannot access local variable 'v'"),
        (6, "error", "KeyError", "'k'"),
    )
    results = review.outcomes(cells, run, timed_out=False)
    found = review.fails_only_fresh(cells, results, review.Labels(cells))
    assert [(c["index"], c["ename"], c["after_not_run"], c["after_error"]) for c in found] == [
        (1, "NameError", False, False),
        (5, "UnboundLocalError", False, True),
        (6, "KeyError", False, True),
    ]


def test_a_fresh_only_failure_below_a_cell_that_didnt_run_says_so():
    cells = cells_of(plain("secret = 1", 1), plain("print(secret)", 2), plain("x = secret", 3))
    run = run_of(
        (0, "not_run", "flagged"),
        (1, "error", "NameError", "name 'secret' is not defined"),
        (2, "error", "NameError", "name 'secret' is not defined"),
    )
    results = review.outcomes(cells, run, timed_out=False)
    found = review.fails_only_fresh(cells, results, review.Labels(cells))
    assert [c["after_not_run"] for c in found] == [True, True]


def test_a_fresh_only_failure_below_a_failing_cell_says_so():
    """The cell above that failed in the review is likely the one that didn't define the name:
    ``after_error``, and the report says so."""
    cells = cells_of(
        plain("import json\ndata = json.load(open('gone.json'))", 1), plain("print(data)", 2)
    )
    run = run_of(
        (0, "error", "FileNotFoundError", "gone.json"),
        (1, "error", "NameError", "name 'data' is not defined"),
    )
    results = review.outcomes(cells, run, timed_out=False)
    [found] = review.fails_only_fresh(cells, results, review.Labels(cells))
    assert (found["index"], found["after_error"], found["after_not_run"]) == (1, True, False)
    report = review.build_report(Path("/nonexistent"), cells, {"run": run})
    assert (
        "- the cell `print(data)` [2]: NameError here, though the notebook shows it ran without "
        "an error (a cell above it failed in the review)"
    ) in review.render_markdown(report)


def test_a_run_that_isnt_partial_needs_every_cells_line(project: Path):
    """nhctl gives D151 before it gets here; the analysis refuses such a run too."""
    cells = cells_of(plain("a = 1", 1), plain("b = 2", 2))
    with pytest.raises(ValueError, match="no line for cell 1"):
        review.outcomes(cells, run_of((0, "ok")), timed_out=False)
    copy = project / "a.ipynb"
    copy.write_text(json.dumps({"cells": [plain("a = 1", 1), plain("b = 2", 2)]}))
    request = {"project": str(project), "copy": str(copy), "run": run_of((0, "ok"))}
    proc = run_module("report", request)
    assert proc.returncode == 1 and "no line for cell 1" in proc.stderr


# ----------------------------------------------------------------------- long cells


def test_cells_over_max_cell_lines_are_src_candidates(project: Path):
    at_limit = "\n".join(f"a{i} = {i}" for i in range(40))
    over = "\n".join(f"b{i} = {i}\n" for i in range(41))  # blank lines don't count
    cells = cells_of(plain(at_limit, 1), plain(over, 2), plain("# only\n\n", 3))
    labels = review.Labels(cells)
    assert review.src_candidates(cells, labels, 40) == [
        {"index": 1, "label": "the cell `b0 = 0` [2]", "lines": 41}
    ]
    (project / "harness.toml").write_text("[lint]\nmax_cell_lines = 10\n")
    report = review.build_report(project, cells, {"run": run_of((0, "ok"), (1, "ok"), (2, "ok"))})
    assert report["max_cell_lines"] == 10
    assert [c["lines"] for c in report["src_candidates"]] == [40, 41]


# ------------------------------------------------------------------- intent summary


def test_the_intent_summary_groups_cells_by_the_heading_above_them():
    cells = cells_of(
        plain("import pandas as pd", 1),  # before any heading
        md("# Sales study\n\nSome prose.\n\n## Load"),  # the last heading is the current one
        note("Load the data", "nh-aaaaaaaaaa"),  # a note's title is no heading
        code("df = pd.read_csv('x.csv')", "nh-aaaaaaaaaa", 2, "load  sales.csv\n and show it"),
        md("## Empty section"),  # no code cell under it: left out
        md("```\n# not a heading\n```\n### Clean ###"),
        note("Drop missing prices", "nh-bbbbbbbbbb"),
        code("df = df.dropna()", "nh-bbbbbbbbbb", 3, "drop rows with no price"),
        plain("df.head()", 4),  # the user's own cell: no intent
        plain("   ", None),  # empty: not in the summary
    )
    summary = review.intent_summary(cells, review.Labels(cells))
    assert summary == [
        {"heading": None, "cells": [
            {"index": 0, "label": "the cell `import pandas as pd` [1]", "intent": None}]},
        {"heading": "Load", "cells": [
            {"index": 3, "label": '"Load the data" [2]', "intent": "load sales.csv and show it"}]},
        {"heading": "Clean", "cells": [
            {"index": 7, "label": '"Drop missing prices" [3]', "intent": "drop rows with no price"},
            {"index": 8, "label": "the cell `df.head()` [4]", "intent": None}]},
    ]  # fmt: skip


def test_no_headings_one_group_and_no_code_no_group():
    cells = cells_of(plain("a = 1", 1), plain("b = 2", 2))
    assert [g["heading"] for g in review.intent_summary(cells, review.Labels(cells))] == [None]
    only_prose = cells_of(md("# Title"), md("text"))
    assert review.intent_summary(only_prose, review.Labels(only_prose)) == []


def test_headings_are_atx_lines_outside_fences():
    assert review.headings("# One\nnot #\n#no-space\n   ## Two ##\n~~~\n# no\n~~~\n#") == [
        "One",
        "Two",
    ]


def test_setext_headings_and_fences_closed_only_by_their_own_kind():
    """Jupyter renders setext headings; a ``~~~`` line doesn't close a backtick fence, nor a
    shorter run a longer one."""
    assert review.headings("Load the data\n=============") == ["Load the data"]
    assert review.headings("Clean\nthe rows\n-----\n\n---\n- item\n---") == ["Clean the rows"]
    assert review.headings("```python\n# comment\n~~~\n# still code\n```\n# Real") == ["Real"]
    assert review.headings("````\n```\n# still code\n````\n## Out") == ["Out"]
    assert review.headings("    indented code\n---\n> quote\n===") == []


# ----------------------------------------------------------------------------- flag


FLAG_CELLS = [
    plain("import os\nimport pandas as pd", 1),
    plain("%pip install six", 2),
    plain("raw = pd.read_csv('https://data.example.org/trips.csv')", 3),
    plain("raw.to_csv('/srv/exports/trips.csv')", 4),
    plain("print(os.environ['HOME'])", 5),
    plain("api_key = 'abc'\napi_key", 6),
    plain("len(raw)", 7),
]


def test_each_flag_rule_and_its_key(project: Path):
    cells = cells_of(*FLAG_CELLS)
    flagged = review.flag(project, cells, "notebooks")
    assert [(f["index"], f["rules"]) for f in flagged] == [
        (1, ["package_install"]),
        (2, ["network"]),
        (3, ["outside_write"]),
        (4, ["secret_print"]),
        (5, ["secret_name"]),
    ]
    assert flagged[0] == {
        "index": 1, "label": "the cell `%pip install six` [2]", "title": None,
        "rules": ["package_install"],
    }  # fmt: skip


def test_a_rule_set_off_flags_nothing_and_an_approved_host_isnt_network(project: Path):
    (project / "harness.toml").write_text(
        '[lint.rules]\npackage_install = "off"\nsecret_name = "off"\nsecret_print = "hint"\n'
    )
    (project / ".nh/state/approved_hosts.json").write_text('["data.example.org"]')
    flagged = review.flag(project, cells_of(*FLAG_CELLS), "notebooks")
    assert [(f["index"], f["rules"]) for f in flagged] == [
        (3, ["outside_write"]),
        (4, ["secret_print"]),  # a hint still flags: only off doesn't
    ]


def test_a_cell_nh_cant_parse_is_flagged_unreadable(project: Path):
    """Fail closed (design §6.10): the rules can't see into a cell nh's Python can't parse, so
    it is asked about, a syntax error on a magic line too (IPython dedents an indented first
    line and runs the cell). A cell magic whose body isn't Python is not."""
    cells = cells_of(
        plain("import requests\nr = requests.get(\n", 1),  # a syntax error: what does it do?
        plain("%%time\nprint(x", 2),  # its body is Python
        plain("%%bash\nif then fi (((", 3),
        plain("!ls (\nx = 1", 4),
        plain("x = 1", 5),
        plain('    !echo hi\nimport requests\nrequests.get("https://example.com/x")', 6),
    )
    flagged = review.flag(project, cells, "notebooks")
    assert [(f["index"], f["rules"]) for f in flagged] == [
        (0, ["unreadable"]),
        (1, ["unreadable"]),
        (5, ["unreadable"]),
    ]


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="nh's Python parses 3.12's syntax")
def test_newer_syntax_than_nhs_python_is_flagged(project: Path):
    newer = 'import requests\ncfg = {"host": "x"}\nrequests.get(f"https://{cfg["host"]}/x")'
    flagged = review.flag(project, cells_of(plain(newer, 1)), "notebooks")
    assert [f["rules"] for f in flagged] == [["unreadable"]]


def test_relative_writes_are_judged_from_the_notebooks_folder():
    """L013 resolves a relative path from the kernel's cwd, the notebook's folder: with a
    project outside /tmp (which L013 exempts), ``../`` stays inside, ``../../`` doesn't."""
    root = Path("/srv/nh-review-test/sales")
    secrets.install(secrets.Redactor.for_project(None))
    try:
        cells = cells_of(
            plain("open('../out.txt', 'w').write('x')", 1),
            plain("open('../../escape.txt', 'w').write('x')", 2),
        )
        inside = review.flag(root, cells, "notebooks")
        assert [(f["index"], f["rules"]) for f in inside] == [(1, ["outside_write"])]
        at_root = review.flag(root, cells, "")
        assert [f["index"] for f in at_root] == [0, 1]
    finally:
        secrets.reset()


def test_the_digest_names_the_flagged_cells_code(project: Path):
    cells = cells_of(*FLAG_CELLS)
    flagged = review.flag(project, cells, "notebooks")
    digest = review.flag_digest(cells, flagged)
    assert len(digest) == 16 and digest == review.flag_digest(cells, flagged)
    changed = cells_of(*FLAG_CELLS[:4], plain("print(os.environ['USER'])", 5), *FLAG_CELLS[5:])
    assert review.flag_digest(changed, review.flag(project, changed, "notebooks")) != digest
    calm = cells_of(plain("x = 2", 1), *FLAG_CELLS[1:])  # a cell that isn't flagged changed
    assert review.flag_digest(calm, review.flag(project, calm, "notebooks")) == digest


def test_a_cell_with_several_rules_lists_them_in_order_with_its_title(project: Path):
    cells = cells_of(
        note(f"Fetch with {PASSWORD}", "nh-aaaaaaaaaa"),
        code(
            "import os, urllib.request\nprint(os.environ['TOKEN'])\n"
            "urllib.request.urlretrieve('https://api.example.com/a', '/srv/a.json')",
            "nh-aaaaaaaaaa",
            None,
        ),
    )
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(project))
    [entry] = review.flag(project, cells, "")
    assert entry["rules"] == ["network", "outside_write", "secret_print"]
    assert entry["title"] == "Fetch with [redacted:DB_PASSWORD]"
    assert entry["label"] == '"Fetch with [redacted:DB_PASSWORD]"'


# --------------------------------------------------------------------------- report


def notebook_for_report() -> list[review.Cell]:
    return cells_of(
        md("# Load"),
        note("Load rows", "nh-aaaaaaaaaa"),
        code("rows = [1, 2, 3]\nlen(rows)", "nh-aaaaaaaaaa", 1, "load the rows", [STREAM]),
        note("Plot clean", "nh-bbbbbbbbbb"),
        code("print(clean)", "nh-bbbbbbbbbb", 5, "show clean", [STREAM]),
        md(f"## Clean for {PASSWORD}"),
        note(f"Connect with {PASSWORD}", "nh-cccccccccc"),
        code("clean = [r * 2 for r in rows]", "nh-cccccccccc", 4, f"use {PASSWORD} here"),
        code("\n".join(f"x{i} = {i}" for i in range(45)), "nh-dddddddddd", 6),
        plain("time.sleep(600)", 7),
        plain("after = 1", 8),
    )


def test_a_full_report_and_its_markdown(project: Path):
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(project))
    cells = notebook_for_report()
    run = run_of(
        (2, "ok"),
        (4, "error", "NameError", "name 'clean' is not defined"),
        (7, "ok"),
        (8, "error", "ValueError", f"bad password {PASSWORD}\nsecond line"),
        (9, "ok"),
        (10, "ok"),
    )
    request = {"notebook": "notebooks/eda.ipynb", "run": run, "ms": 2500, "timeout_s": 540}
    report = review.build_report(project, cells, request)
    assert set(report) == {
        "ok", "complete", "notebook", "kernel", "ms", "timeout_s", "code_cells", "ran", "failing",
        "not_run", "stopped_at", "hidden_state", "src_candidates", "max_cell_lines", "summary",
        "flagged",
    }  # fmt: skip
    assert (report["ok"], report["complete"], report["code_cells"]) == (False, True, 6)
    assert report["ran"] == {"ok": 4, "error": 2, "not_run": 0}
    assert report["failing"][1] == {
        "index": 8, "label": "the cell `x0 = 0` [6]", "ename": "ValueError",
        "evalue": "bad password [redacted:DB_PASSWORD]\nsecond line",
    }  # fmt: skip
    assert report["stopped_at"] is None and report["not_run"] == []
    hidden = report["hidden_state"]
    assert hidden["read_before_defined"][0]["names"] == [
        {"name": "clean", "defined_in": '"Connect with [redacted:DB_PASSWORD]" [4]'}
    ]
    assert hidden["fails_only_fresh"][0]["index"] == 4
    assert [c["index"] for c in hidden["out_of_order"]] == [7]
    assert report["src_candidates"] == [{"index": 8, "label": "the cell `x0 = 0` [6]", "lines": 45}]
    assert report["summary"][1]["heading"] == "Clean for [redacted:DB_PASSWORD]"
    assert report["summary"][1]["cells"][0]["intent"] == "use [redacted:DB_PASSWORD] here"
    text = json.dumps(report)
    assert PASSWORD[:6] not in text and "nh-" not in text

    markdown = review.render_markdown(report, "2026-10-10 10:15")
    assert markdown.startswith("# Review of notebooks/eda.ipynb\n\n2026-10-10 10:15 · kernel ")
    assert "6 code cells: 4 ok, 2 failed · 2.5 s" in markdown
    for heading in (
        "## Failing cells",
        "## Hidden state",
        "## Cells over 40 lines (candidates for src/)",
        "## Intent summary",
        "### Load",
        "### Clean for [redacted:DB_PASSWORD]",
    ):
        assert f"\n{heading}\n" in markdown, heading
    assert "## Not run" not in markdown and "Partial" not in markdown
    failing = "- the cell `x0 = 0` [6]: ValueError: bad password [redacted:DB_PASSWORD] second line"
    assert failing in markdown
    assert '- "Plot clean" [5] reads `clean`, defined later in "Connect with' in markdown
    assert PASSWORD[:6] not in markdown and "nh-" not in markdown


def test_a_timed_out_run_is_partial(project: Path):
    cells = notebook_for_report()
    run = run_of((2, "ok"), (4, "ok"), (7, "ok"), (8, "ok"), running=9, done=False)
    report = review.build_report(project, cells, {"run": run, "timed_out": True, "timeout_s": 30})
    assert (report["ok"], report["complete"]) == (False, False)
    assert report["ran"] == {"ok": 4, "error": 0, "not_run": 2}
    assert report["stopped_at"] == {"index": 9, "label": "the cell `time.sleep(600)` [7]"}
    assert [(c["index"], c["reason"]) for c in report["not_run"]] == [
        (9, "stopped"),
        (10, "timeout"),
    ]
    markdown = review.render_markdown(report)
    assert "**Partial:** the review stopped after 30 s while the cell `time.sleep(600)` [7]" in (
        markdown
    )
    assert "- the cell `time.sleep(600)` [7]: running when the review stopped" in markdown
    assert "- the cell `after = 1` [8]: not reached before the review stopped" in markdown


def test_a_partial_run_with_no_cell_running(project: Path):
    cells = notebook_for_report()
    between = run_of((2, "ok"), (4, "ok"), done=False)  # the deadline fell between two cells
    report = review.build_report(project, cells, {"run": between, "timed_out": True,
                                                  "timeout_s": 0.5})  # fmt: skip
    assert report["stopped_at"] is None
    assert {c["reason"] for c in report["not_run"]} == {"timeout"}
    partial = "**Partial:** the review stopped after 0.5 s between two cells, so the cells after"
    assert partial in review.render_markdown(report)
    starting = run_of(done=False)  # the kernel was still starting
    report = review.build_report(project, cells, {"run": starting, "timed_out": True})
    assert report["ran"] == {"ok": 0, "error": 0, "not_run": 6}
    assert "**Partial:** the review stopped before any cell ran (its kernel was starting)." in (
        review.render_markdown(report)
    )


def test_skipped_and_kernel_died_cells_are_not_run(project: Path):
    cells = cells_of(plain("a = 1", 1), plain("%pip install six", 2), plain("b = 2", 3))
    flagged = [{"index": 1, "label": "x", "title": None, "rules": ["package_install"]}]
    run = run_of((0, "ok"), (1, "not_run", "flagged"), (2, "not_run", "kernel_died"))
    report = review.build_report(project, cells, {"run": run, "flagged": flagged})
    assert report["complete"] is True and report["ok"] is False
    assert [c["reason"] for c in report["not_run"]] == ["flagged", "kernel_died"]
    markdown = review.render_markdown(report)
    assert "- the cell `%pip install six` [2]: flagged (package_install), skipped" in markdown
    assert "not run: the kernel died at an earlier cell" in markdown


def test_a_clean_notebook_says_so_in_every_section(project: Path):
    cells = cells_of(plain("a = 1", 1), plain("a", 2))
    report = review.build_report(project, cells, {"run": run_of((0, "ok"), (1, "ok"))})
    assert report["ok"] is True
    markdown = review.render_markdown(report)
    assert "None: every cell that ran finished without an error." in markdown
    assert "## Hidden state\n\nNone found." in markdown
    assert "## Cells over 40 lines (candidates for src/)\n\nNone." in markdown


def test_an_evalue_is_redacted_before_its_cut(project: Path):
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(project))
    cells = cells_of(plain("boom()", 1))
    evalue = "x" * 495 + PASSWORD
    report = review.build_report(project, cells, {"run": run_of((0, "error", "E", evalue))})
    assert report["failing"][0]["evalue"] == "x" * 495 + "[reda"


# ----------------------------------------------------------------------------- main


def run_module(
    mode: str, request: dict, env: dict | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    """As nhctl launches it (design §6.10): ``-s -P -m``, nh's source on PYTHONPATH."""
    return subprocess.run(
        [sys.executable, "-s", "-P", "-m", "nh_gateway.review", mode],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(SRC), **(env or {})),
        cwd=cwd,
        timeout=120,
        check=False,
    )


def test_python_dash_m_flag_and_report(project: Path):
    (project / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    copy = project / ".nh" / "tmp" / "review-x.ipynb"
    copy.parent.mkdir(parents=True)
    nb = {"cells": [md("# Part"), plain("print(os.environ['HOME'])", 1),
                    plain(f"pw = '{PASSWORD}'\nboom()", 2)]}  # fmt: skip
    copy.write_text(json.dumps(nb))
    flagged = run_module("flag", {"project": str(project), "copy": str(copy)})
    assert flagged.returncode == 0, flagged.stderr
    answer = json.loads(flagged.stdout)
    assert answer["flagged"][0]["rules"] == ["secret_print"]
    assert len(answer["digest"]) == 16
    run = run_of((1, "ok"), (2, "error", "NameError", "name 'boom' is not defined"))
    request = {"project": str(project), "copy": str(copy), "notebook": "nb.ipynb", "run": run,
               "when": "2026-10-10 10:15", "ms": 10}  # fmt: skip
    proc = run_module("report", request)
    assert proc.returncode == 0, proc.stderr
    answer = json.loads(proc.stdout)
    assert answer["report"]["failing"][0]["label"] == "the cell `pw = '[redacted:DB_PASSWORD]'` [2]"
    assert answer["markdown"].startswith("# Review of nb.ipynb\n\n2026-10-10 10:15 · ")
    assert PASSWORD[:6] not in proc.stdout


def test_the_analysis_never_imports_the_projects_modules(project: Path):
    """``-P``: run with the project as cwd, a project's own ``random.py`` (the analysis imports
    ``random``) stays out of nh's process, which holds the Jupyter tokens."""
    (project / "random.py").write_text(
        "import os\nopen(os.path.join(os.path.dirname(__file__), 'RAN'), 'w').write("
        "os.environ.get('JUPYTER_TOKEN', ''))\n"
    )
    copy = project / "a.ipynb"
    copy.write_text(json.dumps({"cells": [plain("x = 1", 1)]}))
    request = {"project": str(project), "copy": str(copy), "notebook_dir": ""}
    proc = run_module("flag", request, env={"JUPYTER_TOKEN": "tok-123"}, cwd=project)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert not (project / "RAN").exists(), "the project's random.py ran in nh's analysis"


def test_python_dash_m_refuses_a_bad_mode_or_request(project: Path):
    assert run_module("explain", {}).returncode == 2
    assert run_module("flag", []).returncode == 2  # type: ignore[arg-type]


def test_the_analysis_imports_nothing_heavy():
    """Its cold start stays V8's stub's: no nbformat, fastmcp, requests or the backends."""
    code = (
        "import sys, nh_gateway.review\n"
        "heavy = [m for m in ('nbformat', 'fastmcp', 'requests', 'pycrdt', 'nh_gateway.app',"
        " 'nh_gateway.backend', 'nh_gateway.tools') if m in sys.modules]\n"
        "print(heavy)"
    )
    proc = subprocess.run(
        [sys.executable, "-s", "-c", code],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
