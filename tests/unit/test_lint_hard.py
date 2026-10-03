"""Hard lint rules (L001–L013): what rejects a cell, or holds it for the user's yes, before anything
is written."""

from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nh_gateway.config import ASK_RULES, Config, load
from nh_gateway.lint import lint as lint_module
from nh_gateway.lint import secret_scan
from nh_gateway.lint.lint import Issue, LintReport, lint_cell
from nh_gateway.lint.magics import lines_of, mask

GOOD = {
    "title": "Drop rows with missing price",
    "notes": ["Removes rows whose price is null.", "The plot needs a price for every row."],
    "intent": "remove the rows without a price",
}
OURS = sys.version_info[:2]
PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"


def config(**rules: str) -> Config:
    cfg = load(None)
    cfg.data["lint"]["rules"].update(rules)
    return cfg


def lint(code: str = "df_clean = df.dropna()\ndf_clean.shape", **overrides: Any) -> LintReport:
    args: dict[str, Any] = {
        **GOOD,
        "cfg": load(None),
        "require_note": True,
        "require_intent": True,
        "kernel_python": None,
        "names_above": None,
    }
    args.update(overrides)
    return lint_cell(code, **args)


def rules(report: LintReport, severity: str = "error") -> list[str]:
    found = {"error": report.errors, "hint": report.hints, "ask": report.asks}[severity]
    return [i.rule for i in found]


def test_clean_cell_passes() -> None:
    report = lint()
    assert report.ok
    assert report.errors == []
    assert report.asks == []
    assert report.title == GOOD["title"]
    assert report.bullets == GOOD["notes"]
    assert (report.defs, report.uses, report.parsed) == ({"df_clean"}, {"df"}, True)
    assert (report.code_lines, report.comment_lines) == (2, 0)


# L001 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["", "   \n\t\n", "# just a comment\n  # and another"])
def test_l001_empty_code(code: str) -> None:
    report = lint(code)
    assert rules(report) == ["L001"]
    assert report.hints == []


def test_l001_magic_only_cell_is_code() -> None:
    assert "L001" not in rules(lint("%matplotlib inline"))


# L002 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "separator",
    [
        "# %%",
        "#%%",
        "# %% [markdown]",
        "# In[ ]:",
        "# In[12]:",
        "#In[3]",
        "# <codecell>",
        "# <markdowncell>",
        "# COMMAND ----------",
        "    # %%",
    ],
)
def test_l002_separators(separator: str) -> None:
    report = lint(f"x_total = 1\n{separator}\ny_total = 2\ny_total")
    assert rules(report) == ["L002"]
    issue = report.errors[0]
    assert issue.key == "cell_separator"
    # Review finding 57: the fix must not contradict the E120 "fix and call again" Next line.
    assert issue.fix == (
        "Keep only the first step in this cell (no separators) and propose the rest in your reply."
    )


@pytest.mark.parametrize(
    "code",
    [
        'label = "# %%"\nlabel',
        's = """\n# In[1]:\n# %%\n"""\ns',
        'df["# %%"]',
        "x_total = 1  # %% not alone on its line\nx_total",
        "%%bash\n# %% a bash comment\nls",
    ],
)
def test_l002_not_in_strings_trailing_comments_or_other_languages(code: str) -> None:
    assert "L002" not in rules(lint(code))


# L003 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        "one two three four five six seven eight nine",
        "Load\ndata",
        "Use `df.dropna()` here",
        "![plot](x.png)",
        "<b>Load</b> data",
        "**Load** data",
        "Load ## data",
        "[Load](http://x) data",
        "###",
    ],
)
def test_l003_bad_titles(title: str) -> None:
    report = lint(title=title)
    assert rules(report) == ["L003"]
    assert report.errors[0].key == "title"


@pytest.mark.parametrize(
    "title",
    [
        "one two three four five six seven eight",
        "### Load raw data",
        "Rows with price < 100",
        "Fix issue #42 in prices",
        "price-by-region",
    ],
)
def test_l003_good_titles(title: str) -> None:
    assert "L003" not in rules(lint(title=title))


def test_l003_title_is_normalised() -> None:
    assert lint(title="### Load raw data").title == "Load raw data"


@pytest.mark.parametrize("title", [None, "", "   "])
def test_l003_title_required_only_with_a_note(title: str | None) -> None:
    assert rules(lint(title=title)) == ["L003"]
    assert rules(lint(title=title, notes=None, require_note=False)) == []


# L004 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "notes",
    [
        ["only one bullet"],
        ["a1", "b2", "c3", "d4", "e5", "f6"],
        [],
        None,
        "Just one sentence here.",
        ["word " * 41, "short"],
        ["## Findings", "short point"],
        ["```python", "short point"],
        ["see ![plot](p.png)", "short point"],
        ["a<br>b", "short point"],
        ["top level\n  - nested", "short point"],
        ["top level\n- sub item", "short point"],
        "- first\n  - nested\n- second",
        ["- - double marker", "short point"],
    ],
)
def test_l004_bad_notes(notes: Any) -> None:
    report = lint(notes=notes)
    assert set(rules(report)) == {"L004"}
    assert all(i.key == "notes" for i in report.errors)


@pytest.mark.parametrize(
    "notes",
    [
        ["two", "bullets"],
        ["1", "2", "3", "4", "5"],
        "- first point\n- second point",
        "First sentence here. Second sentence here.",
        ["word " * 40, "short"],
        ["Rows with <NA> in price are dropped.", "x < 5 is kept."],
        ["#1 cause of nulls is the north region", "second"],
    ],
)
def test_l004_good_notes(notes: Any) -> None:
    assert "L004" not in rules(lint(notes=notes))


def test_l004_bullets_are_normalised() -> None:
    report = lint(notes=["- first\n  continued", "2. second"])
    assert report.bullets == ["first continued", "second"]


def test_l004_a_wrapped_line_in_a_notes_string_continues_its_bullet() -> None:
    # Review finding 62: a wrapped line was a sentence fragment of its own (and a sixth bullet).
    notes = (
        "- Reads the raw CSV with pandas, keeping every column\n"
        "  as loaded so nothing is lost.\n"
        "- Shows the shape.\n- Parses dates.\n- Drops test orders.\n- Counts rows."
    )
    report = lint(notes=notes)
    assert "L004" not in rules(report)
    assert report.bullets[0] == (
        "Reads the raw CSV with pandas, keeping every column as loaded so nothing is lost."
    )
    assert len(report.bullets) == 5


def test_l004_notes_optional_without_a_note() -> None:
    assert rules(lint(notes=None, require_note=False)) == []
    assert rules(lint(notes="", require_note=False)) == []
    assert rules(lint(notes=["only one"], require_note=False)) == ["L004"]


def test_l004_uses_configured_limits() -> None:
    cfg = load(None)
    cfg.data["markdown"].update(notes_min=1, notes_max=2, bullet_max_words=3)
    assert rules(lint(notes=["one bullet"], cfg=cfg)) == []
    assert rules(lint(notes=["a b c d"], cfg=cfg)) == ["L004"]


# L005 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("intent", [None, "", "  \n"])
def test_l005_missing_intent(intent: str | None) -> None:
    assert rules(lint(intent=intent)) == ["L005"]
    assert rules(lint(intent=intent, require_intent=False)) == []


# L007 -------------------------------------------------------------------------------------------
def test_l007_error_when_kernel_is_no_newer() -> None:
    report = lint("x_total = (1,\nx_total", kernel_python=(3, 11))
    assert rules(report) == ["L007"]
    message = report.errors[0].message
    assert message.startswith("Python 3.11 can't parse this cell")
    assert "`x_total = (1,`" in message


def test_l007_same_version_kernel() -> None:
    assert rules(lint("x = )", kernel_python=OURS)) == ["L007"]


def test_l007_kernel_as_list_from_json() -> None:
    assert rules(lint("x = )", kernel_python=[3, 11])) == ["L007"]


@pytest.mark.parametrize("kernel", [None, (3, 99), (2, 7), (4, 0)])
def test_l007_hint_when_kernel_unknown_or_newer(kernel: Any) -> None:
    report = lint("x = )", kernel_python=kernel)
    assert report.ok
    assert "L007" in rules(report, "hint")


def test_l007_hint_is_not_promoted_by_strict_mode() -> None:
    cfg = load(None)
    cfg.data["lint"]["mode"] = "strict"
    report = lint("result_total = )", cfg=cfg)
    assert "L007" in rules(report, "hint")
    assert "L007" not in rules(report)


def test_l007_checks_the_kernels_grammar() -> None:
    match = "match command:\n    case 'go':\n        result_total = 1\nresult_total"
    assert rules(lint(match, kernel_python=(3, 9))) == ["L007"]
    assert rules(lint(match, kernel_python=(3, 10))) == []
    groups = "try:\n    run()\nexcept* ValueError:\n    pass\n"
    assert "L007" in rules(lint(groups, kernel_python=(3, 10)))
    assert "L007" not in rules(lint(groups, kernel_python=(3, 11)))


def test_l007_error_on_a_masked_line_is_only_a_hint() -> None:
    report = lint("for item in items:\n!echo {item}\n", kernel_python=(3, 11))
    assert report.ok
    assert "misreading IPython syntax" in report.hints[0].message


def test_l007_skipped_for_cell_magics() -> None:
    assert "L007" not in rules(lint("%%time\nx = (", kernel_python=(3, 11)))


def test_l007_parser_overflow_degrades_to_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def overflow(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError

    monkeypatch.setattr(lint_module.ast, "parse", overflow)
    report = lint(kernel_python=(3, 11))
    assert report.ok
    assert report.parsed is False
    assert "L007" in rules(report, "hint")


# L008 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code",
    [
        "import nbformat\nnbformat.write(nb, 'out.ipynb')",
        "%%writefile report.ipynb\n{}",
        "with open('copy.ipynb', 'w') as fh:\n    fh.write(text)",
        "json.dump(nb, open('x.ipynb', 'w'))",
        "!jupytext --to notebook analysis.py",
        "from pathlib import Path\nPath('a.ipynb').write_text(text)",
        "nb_path = 'reports/x.ipynb'\nwith open(nb_path, mode='w') as fh:\n    fh.write(text)",
        "fh = open(out_dir / 'x.ipynb', 'a')",
        "fh = open(f'{out_dir}/x.ipynb', 'w+')",
    ],
)
def test_l008_notebook_writes(code: str) -> None:
    assert "L008" in rules(lint(code))


@pytest.mark.parametrize(
    "code",
    [
        "nb = nbformat.read('x.ipynb', as_version=4)\nnb",
        "# never call nbformat.write(nb) here\ncells = 1\ncells",
        "text = open('x.ipynb').read()\ntext",
        "text = open('x.ipynb', 'r').read()\ntext",
        "fh = open('notes.txt', 'w')\nfh",
    ],
)
def test_l008_reads_and_comments_are_fine(code: str) -> None:
    assert "L008" not in rules(lint(code))


# L009 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code",
    [
        "!pip install seaborn",
        "%pip install seaborn",
        "!conda install -y seaborn",
        "%conda install seaborn",
        "!uv add seaborn",
        "!{sys.executable} -m pip install seaborn",
        "!python -m pip install seaborn",
        "import subprocess\nsubprocess.run(['pip', 'install', 'seaborn'])",
        "import os\nos.system('pip install seaborn')",
        "%%bash\npip install seaborn",
        "%%sh\nuv pip install seaborn",
    ],
)
def test_l009_package_installs(code: str) -> None:
    # An ask by default (design §6.4): no error, no hint; the cell waits for the user's yes.
    report = lint(code)
    assert rules(report, "ask") == ["L009"] and report.ok
    assert "L009" not in rules(report) + rules(report, "hint")
    issue = next(i for i in report.asks if i.rule == "L009")
    assert issue.severity == "ask"
    assert "seaborn" in issue.message
    assert "ask the user" in issue.fix
    assert "`seaborn`" in issue.question and "uv add" in issue.question
    # The same finding refuses the cell when the rule is set to error.
    strict = lint(code, cfg=config(package_install="error"))
    issue = next(i for i in strict.errors if i.rule == "L009")
    assert "seaborn" in issue.message and "ask the user" in issue.fix


@pytest.mark.parametrize(
    "code",
    [
        "# !pip install seaborn\nimport seaborn as sns",
        "!pip list",
        "!pip show pandas",
        "print('run pip install seaborn')",
        "%%bash\necho pip install seaborn",
    ],
)
def test_l009_not_installs(code: str) -> None:
    report = lint(code)  # in no list: L009 is an ask by default (design §6.4)
    assert "L009" not in rules(report) + rules(report, "hint") + rules(report, "ask")
    assert report.asks == []


def test_l009_is_an_ask_by_default() -> None:
    assert load(None).rule("package_install") == "ask"


@pytest.mark.parametrize(
    ("level", "mode", "where"),
    [
        ("ask", "advise", "ask"),
        ("ask", "strict", "ask"),  # strict turns hints into errors and leaves an ask an ask
        ("error", "advise", "error"),
        ("error", "strict", "error"),
        ("hint", "advise", "hint"),
        ("hint", "strict", "error"),
        ("off", "advise", None),
        ("off", "strict", None),
    ],
)
def test_l009_levels(level: str, mode: str, where: str | None) -> None:
    cfg = config(package_install=level)
    cfg.data["lint"]["mode"] = mode
    report = lint("!pip install seaborn", cfg=cfg)
    found = {s: rules(report, s) for s in ("error", "hint", "ask")}
    assert found == {s: (["L009"] if s == where else []) for s in found}
    assert report.ok == (where != "error")


L009_TEXTS = [
    (
        "!pip install seaborn",
        "The cell installs `seaborn` into the kernel only (`!pip install seaborn`).",
        "Remove the install and ask the user; after a yes, install it with `uv add seaborn` "
        "through Bash: the next env sync removes kernel-only installs.",
        "installs `seaborn` into the kernel only, and the next env sync removes it "
        "(`uv add seaborn` keeps it)",
    ),
    (
        "%pip install -q seaborn==0.13 numpy 'pandas>=2' > /dev/null",
        "The cell installs `seaborn==0.13`, `numpy` and `pandas>=2` into the kernel only "
        "(`%pip install -q seaborn==0.13 numpy 'pandas>=2' > /dev/null`).",
        "Remove the install and ask the user; after a yes, install them with "
        '`uv add seaborn==0.13 numpy "pandas>=2"` through Bash: the next env sync removes '
        "kernel-only installs.",
        "installs `seaborn==0.13`, `numpy` and `pandas>=2` into the kernel only, and the next "
        'env sync removes them (`uv add seaborn==0.13 numpy "pandas>=2"` keeps them)',
    ),
    (
        "!pip install -r requirements.txt -i https://pypi.example.com/simple",
        "The cell installs the packages in `requirements.txt` (from "
        "`https://pypi.example.com/simple`) into the kernel only "
        "(`!pip install -r requirements.txt -i https://pypi.example.co…`).",
        "Remove the install and ask the user; after a yes, install them with "
        "`uv add -r requirements.txt -i https://pypi.example.com/simple` through Bash: the next "
        "env sync removes kernel-only installs.",
        "installs the packages in `requirements.txt` (from `https://pypi.example.com/simple`) "
        "into the kernel only, and the next env sync removes them "
        "(`uv add -r requirements.txt -i https://pypi.example.com/simple` keeps them)",
    ),
    (
        "import subprocess, sys\nsubprocess.check_call([sys.executable, '-m', 'pip', 'install',"
        " 'plotly', '--quiet'])\n!pip install kaleido && echo done  # export",
        "The cell installs `plotly` and `kaleido` into the kernel only (`subprocess.check_call"
        "([sys.executable, '-m', 'pip', 'instal…` (+1 more)).",
        "Remove the install and ask the user; after a yes, install them with "
        "`uv add plotly kaleido` through Bash: the next env sync removes kernel-only installs.",
        "installs `plotly` and `kaleido` into the kernel only, and the next env sync removes "
        "them (`uv add plotly kaleido` keeps them)",
    ),
    (
        "!uv add seaborn",
        "The cell adds `seaborn` to the project's dependencies (`!uv add seaborn`).",
        "Remove it and ask the user; after a yes, run `uv add seaborn` through Bash.",
        "adds `seaborn` to the project's dependencies with `uv add`",
    ),
    (
        "%pip uninstall -y seaborn",
        "The cell removes `seaborn` from the kernel (`%pip uninstall -y seaborn`).",
        "Remove it and ask the user; after a yes, run `uv remove seaborn` through Bash.",
        "removes `seaborn` from the kernel",
    ),
    (
        "!pip install -e .",
        "The cell installs `.` (editable) into the kernel only (`!pip install -e .`).",
        "Remove the install and ask the user; after a yes, install it with "
        "`uv add --editable .` through Bash: the next env sync removes kernel-only installs.",
        "installs `.` (editable) into the kernel only, and the next env sync removes it "
        "(`uv add --editable .` keeps it)",
    ),
    (
        "!conda config --add channels conda-forge",  # detected, but no command L009 can name
        "The cell installs packages into the kernel only "
        "(`!conda config --add channels conda-forge`).",
        "Remove the install and ask the user; after a yes, install them with "
        "`uv add <package>` through Bash: the next env sync removes kernel-only installs.",
        "installs packages into the kernel only, and the next env sync removes them "
        "(`uv add <package>` keeps them)",
    ),
    # Every kind of command gets its own clause, in the order the kinds first appear (plan-R1).
    (
        "%pip install plotly\n%pip uninstall -y seaborn\nsorted([1])",
        "The cell installs `plotly` into the kernel only; it also removes `seaborn` from the "
        "kernel (`%pip install plotly` (+1 more)).",
        "Remove the package commands and ask the user; after a yes, install it with "
        "`uv add plotly`, then run `uv remove seaborn` through Bash: the next env sync removes "
        "kernel-only installs.",
        "installs `plotly` into the kernel only, and the next env sync removes it "
        "(`uv add plotly` keeps it); it also removes `seaborn` from the kernel",
    ),
    (
        "%pip uninstall -y seaborn\n%pip install plotly\nsorted([1])",
        "The cell removes `seaborn` from the kernel; it also installs `plotly` into the kernel "
        "only (`%pip uninstall -y seaborn` (+1 more)).",
        "Remove the package commands and ask the user; after a yes, run `uv remove seaborn`, "
        "then install it with `uv add plotly` through Bash: the next env sync removes "
        "kernel-only installs.",
        "removes `seaborn` from the kernel; it also installs `plotly` into the kernel only, and "
        "the next env sync removes it (`uv add plotly` keeps it)",
    ),
    (
        "!uv add polars\n!pip install kaleido",
        "The cell adds `polars` to the project's dependencies; it also installs `kaleido` into "
        "the kernel only (`!uv add polars` (+1 more)).",
        "Remove the package commands and ask the user; after a yes, run `uv add polars`, then "
        "install it with `uv add kaleido` through Bash: the next env sync removes kernel-only "
        "installs.",
        "adds `polars` to the project's dependencies with `uv add`; it also installs `kaleido` "
        "into the kernel only, and the next env sync removes it (`uv add kaleido` keeps it)",
    ),
    (
        "!pip install plotly && pip uninstall -y seaborn",
        "The cell installs `plotly` into the kernel only; it also removes `seaborn` from the "
        "kernel (`!pip install plotly && pip uninstall -y seaborn`).",
        "Remove the package commands and ask the user; after a yes, install it with "
        "`uv add plotly`, then run `uv remove seaborn` through Bash: the next env sync removes "
        "kernel-only installs.",
        "installs `plotly` into the kernel only, and the next env sync removes it "
        "(`uv add plotly` keeps it); it also removes `seaborn` from the kernel",
    ),
    (  # a removal detection alone misses is still named once the cell asks
        "%pip install plotly\n!pip uninstall -y seaborn",
        "The cell installs `plotly` into the kernel only; it also removes `seaborn` from the "
        "kernel (`%pip install plotly` (+1 more)).",
        "Remove the package commands and ask the user; after a yes, install it with "
        "`uv add plotly`, then run `uv remove seaborn` through Bash: the next env sync removes "
        "kernel-only installs.",
        "installs `plotly` into the kernel only, and the next env sync removes it "
        "(`uv add plotly` keeps it); it also removes `seaborn` from the kernel",
    ),
    (
        "!uv add x\n!uv remove y",
        "The cell adds `x` to the project's dependencies; it also removes `y` from the "
        "project's dependencies (`!uv add x` (+1 more)).",
        "Remove the package commands and ask the user; after a yes, run `uv add x`, then run "
        "`uv remove y` through Bash.",
        "adds `x` to the project's dependencies with `uv add`; it also removes `y` from the "
        "project's dependencies with `uv remove`",
    ),
    # Where a package comes from is named, and kept in the suggested command (gate-6).
    (
        "%pip install torch --index-url https://download.pytorch.org/whl/cu121",
        "The cell installs `torch` (from `https://download.pytorch.org/whl/cu121`) into the "
        "kernel only (`%pip install torch --index-url https://download.pytorch.org…`).",
        "Remove the install and ask the user; after a yes, install it with "
        "`uv add torch --index-url https://download.pytorch.org/whl/cu121` through Bash: the "
        "next env sync removes kernel-only installs.",
        "installs `torch` (from `https://download.pytorch.org/whl/cu121`) into the kernel only, "
        "and the next env sync removes it "
        "(`uv add torch --index-url https://download.pytorch.org/whl/cu121` keeps it)",
    ),
    (
        "%pip install --extra-index-url=https://pkgs.example.net/simple internal-utils",
        "The cell installs `internal-utils` (from `https://pkgs.example.net/simple`) into the "
        "kernel only (`%pip install --extra-index-url=https://pkgs.example.net/sim…`).",
        "Remove the install and ask the user; after a yes, install it with "
        "`uv add internal-utils --extra-index-url https://pkgs.example.net/simple` through "
        "Bash: the next env sync removes kernel-only installs.",
        "installs `internal-utils` (from `https://pkgs.example.net/simple`) into the kernel "
        "only, and the next env sync removes it "
        "(`uv add internal-utils --extra-index-url https://pkgs.example.net/simple` keeps it)",
    ),
    (
        "%pip install -e ../mylib",
        "The cell installs `../mylib` (editable) into the kernel only "
        "(`%pip install -e ../mylib`).",
        "Remove the install and ask the user; after a yes, install it with "
        "`uv add --editable ../mylib` through Bash: the next env sync removes kernel-only "
        "installs.",
        "installs `../mylib` (editable) into the kernel only, and the next env sync removes it "
        "(`uv add --editable ../mylib` keeps it)",
    ),
    (  # conda's channel is named; pip's -c (constraints) is not; never carried into uv add
        "%conda install -c conda-forge seaborn\n%pip install -c limits.txt plotly",
        "The cell installs `seaborn` (from channel `conda-forge`) and `plotly` into the kernel "
        "only (`%conda install -c conda-forge seaborn` (+1 more)).",
        "Remove the install and ask the user; after a yes, install them with "
        "`uv add seaborn plotly` through Bash: the next env sync removes kernel-only installs.",
        "installs `seaborn` (from channel `conda-forge`) and `plotly` into the kernel only, and "
        "the next env sync removes them (`uv add seaborn plotly` keeps them)",
    ),
    (  # a string argument ends at its closing quote
        "import subprocess\nsubprocess.run('pip install x', shell=True)",
        "The cell installs `x` into the kernel only "
        "(`subprocess.run('pip install x', shell=True)`).",
        "Remove the install and ask the user; after a yes, install it with `uv add x` through "
        "Bash: the next env sync removes kernel-only installs.",
        "installs `x` into the kernel only, and the next env sync removes it (`uv add x` keeps it)",
    ),
    (  # a word the shell reads otherwise is double-quoted, escaped (plan-R7)
        '%pip install "seaborn[stats]" $PKG',
        "The cell installs `seaborn[stats]` and `$PKG` into the kernel only "
        '(`%pip install "seaborn[stats]" $PKG`).',
        "Remove the install and ask the user; after a yes, install them with "
        '`uv add "seaborn[stats]" "\\$PKG"` through Bash: the next env sync removes '
        "kernel-only installs.",
        "installs `seaborn[stats]` and `$PKG` into the kernel only, and the next env sync "
        'removes them (`uv add "seaborn[stats]" "\\$PKG"` keeps them)',
    ),
]


@pytest.mark.parametrize(("code", "message", "fix", "question"), L009_TEXTS)
def test_l009_names_the_packages_and_what_keeps_them(
    code: str, message: str, fix: str, question: str
) -> None:
    [issue] = lint(code).asks
    assert (issue.rule, issue.message, issue.fix, issue.question) == (
        "L009",
        message,
        fix,
        question,
    )


def test_l009_in_a_conda_project_points_at_environment_yml() -> None:
    cfg = load(None)
    cfg.data["project"]["env_manager"] = "conda"
    [issue] = lint("%conda install -c conda-forge seaborn", cfg=cfg).asks
    assert issue.message == (
        "The cell installs `seaborn` (from channel `conda-forge`) into the kernel only "
        "(`%conda install -c conda-forge seaborn`)."
    )
    assert issue.fix == (
        "Remove the install and ask the user; after a yes, add `seaborn` (from channel "
        "`conda-forge`) to environment.yml and run `nhctl env sync` through Bash: the next env "
        "sync removes kernel-only installs."
    )
    assert issue.question == (
        "installs `seaborn` (from channel `conda-forge`) into the kernel only, and the next env "
        "sync removes it (adding it to environment.yml keeps it)"
    )
    [removal] = lint("%conda remove seaborn", cfg=cfg).asks
    assert removal.fix == (
        "Remove it and ask the user; after a yes, remove `seaborn` from environment.yml and run "
        "`nhctl env sync` through Bash."
    )
    [two] = lint("%conda remove seaborn plotly", cfg=cfg).asks
    assert two.fix == (
        "Remove it and ask the user; after a yes, remove `seaborn` and `plotly` from "
        "environment.yml and run `nhctl env sync` through Bash."
    )
    [mixed] = lint("%pip install plotly\n%conda remove -y seaborn", cfg=cfg).asks
    assert mixed.fix == (
        "Remove the package commands and ask the user; after a yes, add `plotly` to "
        "environment.yml and run `nhctl env sync`, then remove `seaborn` from environment.yml "
        "and run `nhctl env sync` through Bash: the next env sync removes kernel-only installs."
    )
    assert mixed.question == (
        "installs `plotly` into the kernel only, and the next env sync removes it (adding it to "
        "environment.yml keeps it); it also removes `seaborn` from the kernel"
    )
    [forced] = lint("%conda install -f seaborn", cfg=cfg).asks  # conda's -f takes no value
    assert "`seaborn`" in forced.question


def test_l009_text_scan_is_linear() -> None:
    """A long flagged line costs time in proportion to its length: a command's tool and verb
    have no other tool word between them (design §6.4). The quadratic scan took ~22 s here."""
    code = "!pip install x\n!" + "uv pip " * 8000 + "&& " * 10
    started = time.perf_counter()
    [issue] = lint(code).asks
    assert time.perf_counter() - started < 3.0
    assert issue.rule == "L009" and "`x`" in issue.question


# One cell per rule that can ask (config.ASK_RULES).
ASK_SAMPLES = {
    "package_install": ("L009", "!pip install seaborn"),
    "network": ("L012", 'trips = pd.read_csv("https://data.example.org/trips-2023.csv")'),
    "outside_write": ("L013", 'trips.to_csv("~/exports/trips.csv")'),
}


@pytest.mark.parametrize("level", ["ask", "hint", "error"])
def test_every_ask_rule_has_a_question(level: str) -> None:
    assert set(ASK_SAMPLES) == set(ASK_RULES)
    for key, (rule, code) in ASK_SAMPLES.items():
        report = lint(code, cfg=config(**{key: level}), project_root="/home/me/proj")
        [issue] = [i for i in report.errors + report.hints + report.asks if i.rule == rule]
        assert issue.question and not issue.question.endswith("."), key


@pytest.mark.parametrize(
    ("key", "rule", "code"),
    [
        ("notebook_write", "L008", "import nbformat\nnbformat.write(nb, 'x.ipynb')"),
        ("markdown_output", "L010", "%%markdown\n# Results"),
        ("long_line", "L101", "x = " + " + ".join(["1"] * 60)),
    ],
)
def test_a_rule_that_cant_ask_at_ask_is_an_error(key: str, rule: str, code: str) -> None:
    """config.load refuses "ask" for these; a hand-built Config that holds it fails closed."""
    report = lint(code, cfg=config(**{key: "ask"}))
    assert rule in rules(report) and rule not in rules(report, "ask") + rules(report, "hint")


def test_only_an_ask_rule_has_a_question() -> None:
    report = lint("# %%\n!pip install x", title="a b c d e f g h i")
    assert all(issue.question == "" for issue in report.errors + report.hints)
    assert [issue.question != "" for issue in report.asks] == [True]


# L012 -------------------------------------------------------------------------------------------
# Design §6.4: the scan's Source table, its "Not network" table, the quiet calls and the
# approved hosts. Every entry of network.py's hand-written tables has a row here that fails when
# the entry is taken out (L012_EACH_ENTRY, L012_PASSES_ON, L012_QUIET_EACH, L012_FILLS and
# L012_VALUE_OPTIONS hold the forms the generic rules would catch anyway); `_STR_METHODS`, built
# from `str`'s own methods, is sampled.
L012_FIX = (
    "Don't reach the network from this cell: ask the user to download what it needs into the "
    "project (for example `data/raw/`), then read it from there."
)
TRIPS_URL = "https://data.example.org/trips-2023.csv"


def network_issues(code: str, approved: Any = (), **overrides: Any) -> list[Issue]:
    report = lint(code, approved_hosts=approved, **overrides)
    return [i for i in report.errors + report.hints + report.asks if i.rule == "L012"]


def network_message(code: str, approved: Any = ()) -> str | None:
    found = network_issues(code, approved)
    return found[0].message if found else None


def reaches(hosts: str, where: str, more: int = 0) -> str:
    """The message for a site list: ``hosts`` as the clause names them ("" for none nh can
    read)."""
    clause = f"connects to {hosts} over the network" if hosts else "connects to the network"
    return f"The cell {clause} (`{where}`{f' (+{more} more)' if more else ''})."


DATA = "`data.example.org`"
API = "`api.example.org`"
BUCKET = "`s3://trips-bucket`"
HF = "`huggingface.co`"
GITHUB = "`github.com`"
L012_FIRES = [
    # a network URL literal passed to a call: positional, keyword, starred, in a display
    (f'trips = pd.read_csv("{TRIPS_URL}")', reaches(DATA, "pd.read_csv")),
    (
        'trips = pl.scan_parquet("s3://trips-bucket/2023/*.parquet")',
        reaches(BUCKET, "pl.scan_parquet"),
    ),
    (
        'trips.to_parquet("gs://trips-bucket/out.parquet")',
        reaches("`gs://trips-bucket`", "trips.to_parquet"),
    ),
    ('trips = pd.read_parquet("az://trips/t.parquet")', reaches("`az://trips`", "pd.read_parquet")),
    (
        'trips = pd.read_parquet("hf://datasets/org/trips/t.parquet")',
        reaches(HF, "pd.read_parquet"),
    ),
    # the userinfo may hold anything; an unknown port or userinfo leaves the host literal
    (
        'trips = pd.read_csv("https://user:p%40ss@data.example.org/x.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv("https://user:pa$$w0rd@data.example.org/x.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv("https://{}:{}@data.example.org/x.csv".format(user, pw))',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv(f"https://{user}:{pw}@data.example.org/x.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv(f"https://data.example.org:{port}/x.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv("https://%s@data.example.org/x.csv" % creds)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_parquet("abfss://trips-bucket@acct.dfs.core.windows.net/t")',
        reaches("`acct.dfs.core.windows.net`", "pd.read_parquet"),
    ),
    (
        'trips = load(url="ftp://ftp.example.org/pub/trips.csv")',
        reaches("`ftp.example.org`", "load"),
    ),
    (f'trips = load(*["{TRIPS_URL}"])', reaches(DATA, "load")),
    (f'trips = read_all({{"2023": "{TRIPS_URL}"}})', reaches(DATA, "read_all")),
    (
        f'trips = read_all(("{TRIPS_URL}", "https://api.example.org/t"))',
        reaches(f"{DATA} and {API}", "read_all"),
    ),
    (f'subprocess.run(["curl", "-O", "{TRIPS_URL}"])', reaches(DATA, "subprocess.run")),
    (f'loaders["csv"]("{TRIPS_URL}")', reaches(DATA, "loaders[…]")),
    (f'trips = pd.concat(map(pd.read_csv, ["{TRIPS_URL}"]))', reaches(DATA, "map")),
    # through a name, in statement order
    (f'TRIPS_URL = "{TRIPS_URL}"\ntrips = pd.read_csv(TRIPS_URL)', reaches(DATA, "pd.read_csv")),
    (
        f'SOURCES = {{"trips": "{TRIPS_URL}"}}\ntrips = pd.read_csv(SOURCES["trips"])',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'SOURCES = {{"trips": "{TRIPS_URL}"}}\nfor name, url in SOURCES.items():\n    frames[name] = pd.read_csv(url)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'SOURCES = {{"trips": "{TRIPS_URL}"}}\ntrips = pd.read_csv(SOURCES.get("trips"))',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URLS = ["{TRIPS_URL}"]\nframes = [pd.read_csv(u) for u in URLS]',
        reaches(DATA, "pd.read_csv"),
    ),
    (f'frames = {{u: pd.read_csv(u) for u in ["{TRIPS_URL}"]}}', reaches(DATA, "pd.read_csv")),
    (f'if (url := "{TRIPS_URL}"):\n    trips = pd.read_csv(url)', reaches(DATA, "pd.read_csv")),
    (f'def load(url="{TRIPS_URL}"):\n    return pd.read_csv(url)', reaches(DATA, "pd.read_csv")),
    (f'def load(*, url="{TRIPS_URL}"):\n    return pd.read_csv(url)', reaches(DATA, "pd.read_csv")),
    (
        f'URL = "{TRIPS_URL}"\nCLEAN = URL.strip()\ntrips = pd.read_csv(CLEAN)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URLS = []\nURLS.append("{TRIPS_URL}")\ntrips = pd.read_csv(URLS[0])',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'SRC, OUT = "{TRIPS_URL}", "out.csv"\ntrips = pd.read_csv(SRC)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'if fresh:\n    URL = "{TRIPS_URL}"\nelse:\n    URL = "trips.csv"\ntrips = pd.read_csv(URL)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URL = "{TRIPS_URL}"\ntrips = pd.read_csv(URL if fresh else "trips.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (f'URL = "{TRIPS_URL}"\ntrips = pd.read_csv(str(URL)[:200])', reaches(DATA, "pd.read_csv")),
    (
        f'cfg = {{}}\ncfg["url"] = "{TRIPS_URL}"\ntrips = pd.read_csv(cfg["url"])',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URL = os.environ.get("DATA_URL") or "{TRIPS_URL}"\ntrips = pd.read_csv(URL)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URLS = ["{TRIPS_URL}"]\nfor u in tqdm(URLS, desc="files"):\n    frames.append(pd.read_csv(u))',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'TRIPS_URL = "{TRIPS_URL}"\nstations = pd.read_csv(TRIPS_URL.rsplit("/", 1)[0] + "/stations.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        f'URL = "{TRIPS_URL}"\nclean = pd.read_csv(URL.split("?")[0])',
        reaches(DATA, "pd.read_csv"),
    ),
    # a literal host from a name, by itself or as one of several
    (
        'HOST = "data.example.org"\ntrips = pd.read_csv(f"https://{HOST}/trips.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'HOST = "data.example.org"\ntrips = pd.read_csv("https://" + HOST + "/trips.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'HOST = "data.example.org"\ntrips = pd.read_csv("https://%s/trips.csv" % HOST)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'HOST = "data.example.org"\ntrips = pd.read_csv("https://{}/trips.csv".format(HOST))',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'HOST = "data.example.org"\nPORT = 8443\ntrips = pd.read_csv(f"https://{HOST}:{PORT}/t.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'if prod:\n    HOST = "data.example.org"\nelse:\n    HOST = "api.example.org"\ntrips = pd.read_csv(f"https://{HOST}/t.csv")',
        reaches(f"{DATA} and {API}", "pd.read_csv"),
    ),
    # a string that starts with a URL, or with a name that holds one
    (
        'trips = pd.read_csv(f"https://data.example.org/trips/{year}.csv")',
        reaches(DATA, "pd.read_csv"),
    ),
    ('trips = pd.read_csv(f"https://data.example.org?year={year}")', reaches(DATA, "pd.read_csv")),
    ('trips = pd.read_csv("https://data.example.org/trips/" + name)', reaches(DATA, "pd.read_csv")),
    (
        'trips = pd.read_csv("https://data.example.org/trips/%s.csv" % year)',
        reaches(DATA, "pd.read_csv"),
    ),
    (
        'trips = pd.read_csv("https://data.example.org/trips/{}.csv".format(year))',
        reaches(DATA, "pd.read_csv"),
    ),
    ('BASE = "https://api.example.org"\nitems = fetch(f"{BASE}/items")', reaches(API, "fetch")),
    ('BASE = "https://api.example.org"\nitems = fetch(BASE + "/items")', reaches(API, "fetch")),
    (
        'BASE = "https://api.example.org"\nitems = fetch("{}/items".format(BASE))',
        reaches(API, "fetch"),
    ),
    (
        'BASE = "https://api.example.org/"\nitems = fetch(urljoin(BASE, "items"))',
        reaches(API, "fetch"),
    ),
    (
        'BASE = "https://api.example.org"\nitems = fetch(os.path.join(BASE, "items"))',
        reaches(API, "fetch"),
    ),
    (
        'BASE = "https://api.example.org"\nitems = fetch("/".join([BASE, "items"]))',
        reaches(API, "fetch"),
    ),
    # network library calls: always, whatever the URL's source
    (
        'r = requests.get("https://api.example.org/v1/trips", timeout=10)',
        reaches(API, "requests.get"),
    ),
    ("r = requests.post(ENDPOINT, json=payload)", reaches("", "requests.post")),
    ('r = requests.get(os.environ["API_URL"])', reaches("", "requests.get")),
    ("import requests as rq\nr = rq.put(url)", reaches("", "rq.put")),
    ("from requests import get\nr = get(url)", reaches("", "get")),
    (
        'with httpx.stream("GET", "https://api.example.org/t") as r:\n    pass',
        reaches(API, "httpx.stream"),
    ),
    (
        'import urllib.request\nurllib.request.urlretrieve("https://data.example.org/t.csv", "t.csv")',
        reaches(DATA, "urllib.request.urlretrieve"),
    ),
    (
        "from urllib.request import urlopen\nwith urlopen(url) as f:\n    body = f.read()",
        reaches("", "urlopen"),
    ),
    ('body = urlopen(Request("https://api.example.org/t")).read()', reaches(API, "urlopen")),
    (
        'import urllib3\nr = urllib3.request("GET", "https://api.example.org/t")',
        reaches(API, "urllib3.request"),
    ),
    ('import aiohttp\nr = aiohttp.request("GET", url)', reaches("", "aiohttp.request")),
    (
        'import socket\nsock = socket.create_connection(("api.example.org", 443))',
        reaches(API, "socket.create_connection"),
    ),
    (
        'import socket\nHOST = "api.example.org"\nsock = socket.create_connection((HOST, 443))',
        reaches(API, "socket.create_connection"),
    ),
    (
        'import socket\naddress = socket.gethostbyname("api.example.org")',
        reaches(API, "socket.gethostbyname"),
    ),
    (
        'import ftplib\nftp = ftplib.FTP("ftp.example.org")',
        reaches("`ftp.example.org`", "ftplib.FTP"),
    ),
    (
        'import smtplib\nserver = smtplib.SMTP("mail.example.org", 587)',
        reaches("`mail.example.org`", "smtplib.SMTP"),
    ),
    (
        'from ftplib import FTP\nftp = FTP(host="ftp.example.org")',
        reaches("`ftp.example.org`", "FTP"),
    ),
    (
        'sock = socket.create_connection(address=("api.example.org", 443))',
        reaches(API, "socket.create_connection"),
    ),
    ('r = requests.get(os.getenv("API_URL"))', reaches("", "requests.get")),
    # a network method of a session the cell made
    ('s = requests.Session()\nr = s.get("https://api.example.org/t")', reaches(API, "s.get")),
    ("s = requests.Session()\nr = s.get(url)", reaches("", "s.get")),
    (
        'r = requests.Session().get("https://api.example.org/t")',
        reaches(API, "requests.Session().get"),
    ),
    (
        'with httpx.Client(base_url="https://api.example.org") as client:\n    r = client.get("/trips")',
        reaches(API, "client.get"),
    ),
    (
        "async def main():\n    async with aiohttp.ClientSession() as session:\n        async with session.get(url) as r:\n            return await r.text()",
        reaches("", "session.get"),
    ),
    (
        'async with httpx.AsyncClient() as client:\n    r = await client.get("https://api.example.org/t")',
        reaches(API, "client.get"),
    ),
    (
        'import http.client\nconn = http.client.HTTPSConnection("api.example.org")\nconn.request("GET", "/v1")',
        reaches(API, "conn.request"),
    ),
    (
        'from http import client\nconn = client.HTTPConnection("api.example.org", 8080)\nconn.request("GET", "/")',
        reaches(API, "conn.request"),
    ),
    (
        'pool = urllib3.PoolManager()\nr = pool.request("GET", "https://api.example.org/t")',
        reaches(API, "pool.request"),
    ),
    (
        'opener = urllib.request.build_opener()\nbody = opener.open("https://api.example.org/t").read()',
        reaches(API, "opener.open"),
    ),
    ('sock = socket.socket()\nsock.connect(("api.example.org", 80))', reaches(API, "sock.connect")),
    # shell: `!`, `!!`, `x = !`, %sx, %system, shell cells, bash -c, Python shell calls
    (f"!curl -sSL {TRIPS_URL} -o ../data/raw/trips.csv", reaches(DATA, "!curl")),
    (f"!!wget {TRIPS_URL}", reaches(DATA, "!wget")),
    (f"listing = !curl -s {TRIPS_URL}", reaches(DATA, "!curl")),
    (f"%sx curl -s {TRIPS_URL}", reaches(DATA, "%sx curl")),
    (f"%system wget -q {TRIPS_URL}", reaches(DATA, "%system wget")),
    (f"%%bash\nset -e\ncurl -O {TRIPS_URL}", reaches(DATA, "curl")),
    (f"%%sh\nwget -q \\\n  {TRIPS_URL}", reaches(DATA, "wget")),
    (f"%%script bash\ncd /tmp && wget {TRIPS_URL}", reaches(DATA, "wget")),
    (f"%%script --bg bash\ncurl -O {TRIPS_URL}", reaches(DATA, "curl")),
    (f"%%script --out log --bg bash\nwget {TRIPS_URL}", reaches(DATA, "wget")),
    (f"%%system\ncurl {TRIPS_URL}", reaches(DATA, "curl")),
    (f"%%!\ncurl {TRIPS_URL}", reaches(DATA, "curl")),
    (f"for year in years:\n    !curl -O {TRIPS_URL}", reaches(DATA, "!curl")),
    (f"!bash -c 'curl -O {TRIPS_URL}'", reaches(DATA, "!curl")),
    (f"!sudo curl -O {TRIPS_URL}", reaches(DATA, "!curl")),
    (f"!env HTTPS_PROXY=http://proxy:3128 curl -O {TRIPS_URL}", reaches(DATA, "!curl")),
    (f"!mkdir -p raw && curl -s {TRIPS_URL} | head -5 > raw/head.csv", reaches(DATA, "!curl")),
    ("!echo $(curl -s https://api.example.org/ip)", reaches(API, "!curl")),
    ("!wget $DATA_URL", reaches("", "!wget")),
    (f'URL = "{TRIPS_URL}"\n!curl -O {{URL}}', reaches(DATA, "!curl")),
    (f'URL = "{TRIPS_URL}"\n!wget $URL', reaches(DATA, "!wget")),
    ("!git clone https://github.com/org/repo", reaches(GITHUB, "!git clone")),
    ("!git clone --depth 1 git@github.com:org/repo.git", reaches(GITHUB, "!git clone")),
    ("!git clone $REPO_URL", reaches("", "!git clone")),
    (
        'REPO = "https://github.com/org/repo"\n!git clone {REPO} vendor/repo',
        reaches(GITHUB, "!git clone"),
    ),
    ("!git -C repo pull", reaches("", "!git pull")),
    ("!git pull", reaches("", "!git pull")),
    ("!git push origin main", reaches("", "!git push")),
    ("!git fetch https://github.com/org/repo main", reaches(GITHUB, "!git fetch")),
    ("!git ls-remote git@github.com:org/repo.git", reaches(GITHUB, "!git ls-remote")),
    ("!git submodule update --init", reaches("", "!git submodule")),
    (
        "!git submodule add https://github.com/org/lib vendor/lib",
        reaches(GITHUB, "!git submodule"),
    ),
    (
        "!scp trips.csv analyst@backup.example.org:/srv/data/",
        reaches("`backup.example.org`", "!scp"),
    ),
    ("!scp trips.csv backup:/srv/data/", reaches("", "!scp")),
    (
        "!rsync -av data/ rsync://mirror.example.org/data/",
        reaches("`mirror.example.org`", "!rsync"),
    ),
    ("!rsync -av data/ mirror.example.org::data", reaches("`mirror.example.org`", "!rsync")),
    ("!ssh -p 2222 deploy@box.example.org 'ls /srv'", reaches("`box.example.org`", "!ssh")),
    ("!sftp box", reaches("", "!sftp")),
    ("!aws s3 cp s3://trips-bucket/t.csv ../data/raw/", reaches(BUCKET, "!aws")),
    # commands that download by name, reach a host by itself, or install system packages
    ("!kaggle datasets download -d org/trips -p ../data/raw", reaches("", "!kaggle")),
    ("!gdown 1AbCdEf -O ../data/raw/trips.csv", reaches("", "!gdown")),
    ("!huggingface-cli download org/trips --repo-type dataset", reaches("", "!huggingface-cli")),
    ("!hf download org/trips", reaches("", "!hf")),
    ("!gh release download v1 -R org/repo", reaches("", "!gh")),
    ("!ping -c 1 api.example.org", reaches(API, "!ping")),
    ("!nc -z api.example.org 443", reaches(API, "!nc")),
    ("!dig @8.8.8.8 api.example.org", reaches(f"`8.8.8.8` and {API}", "!dig")),
    ("!pip download pandas -d ../wheels", reaches("", "!pip download")),
    ("!python -m pip download pandas", reaches("", "!pip download")),
    (
        "!pip3 download https://example.org/pkg-1.0-py3-none-any.whl",
        reaches("`example.org`", "!pip download"),
    ),
    ("!apt-get install -y graphviz", reaches("", "!apt-get")),
    ("!sudo apt update", reaches("", "!apt")),
    ("!brew install wget", reaches("", "!brew")),
    ("!npm ci", reaches("", "!npm")),
    (f'os.system("curl -O {TRIPS_URL}")', reaches(DATA, "os.system")),
    (f'subprocess.run(["wget", "{TRIPS_URL}"], check=True)', reaches(DATA, "subprocess.run")),
    (
        f'URL = "{TRIPS_URL}"\nsubprocess.check_output(f"curl -s {{URL}}", shell=True)',
        reaches(DATA, "subprocess.check_output"),
    ),
    (f'URL = "{TRIPS_URL}"\nsubprocess.run(["curl", URL])', reaches(DATA, "subprocess.run")),
    ('subprocess.run(["git", "clone", repo])', reaches("", "subprocess.run")),
    (f'get_ipython().system("curl -O {TRIPS_URL}")', reaches(DATA, "get_ipython().system")),
    # Python shell calls whose command nh renders
    (
        f'URL = "{TRIPS_URL}"\nos.system("wget -q -P ../data/raw " + URL)',
        reaches(DATA, "os.system"),
    ),
    (f'URL = "{TRIPS_URL}"\nos.system("wget %s" % URL)', reaches(DATA, "os.system")),
    (f'URL = "{TRIPS_URL}"\nos.system("curl -O {{}}".format(URL))', reaches(DATA, "os.system")),
    (
        f'URL = "{TRIPS_URL}"\nos.system(" ".join(["curl", "-O", URL]))',
        reaches(DATA, "os.system"),
    ),
    (
        f'cmd = "curl -L -o t.csv {TRIPS_URL}"\nsubprocess.run(cmd, shell=True)',
        reaches(DATA, "subprocess.run"),
    ),
    (
        f'URL = "{TRIPS_URL}"\nsubprocess.run(shlex.split(f"curl -O {{URL}}"))',
        reaches(DATA, "subprocess.run"),
    ),
    (
        f'URL = "{TRIPS_URL}"\ncmd = shlex.split(f"curl -O {{URL}}")\nsubprocess.run(cmd)',
        reaches(DATA, "subprocess.run"),
    ),
    (
        f'URL = "{TRIPS_URL}"\nsubprocess.run(["bash", "-c", f"curl -O {{URL}}"])',
        reaches(DATA, "subprocess.run"),
    ),
    (
        'BASE = "https://data.example.org"\nsubprocess.run(["curl", "-O", f"{BASE}/t.csv"])',
        reaches(DATA, "subprocess.run"),
    ),
    ('subprocess.run(f"curl -O {url}", shell=True)', reaches("", "subprocess.run")),
    (f'ip = get_ipython()\nip.system(f"curl -O {TRIPS_URL}")', reaches(DATA, "ip.system")),
    # IPython calls, magics with a statement, python -c: the code they run
    (
        f'get_ipython().run_line_magic("sx", "curl {TRIPS_URL}")',
        reaches(DATA, "get_ipython().run_line_magic"),
    ),
    (
        f'get_ipython().run_cell_magic("bash", "", "curl -O {TRIPS_URL}\\n")',
        reaches(DATA, "get_ipython().run_cell_magic"),
    ),
    (
        f'get_ipython().run_cell_magic("time", "", "pd.read_csv(\'{TRIPS_URL}\')")',
        reaches(DATA, "get_ipython().run_cell_magic"),
    ),
    (
        'get_ipython().run_line_magic("load", "https://example.org/plot.py")',
        reaches("`example.org`", "get_ipython().run_line_magic"),
    ),
    (
        'get_ipython().run_line_magic("timeit", "requests.get(\'https://api.example.org/t\')")',
        reaches(API, "get_ipython().run_line_magic"),
    ),
    (
        '%timeit -n 1 -r 1 requests.get("https://api.example.org/t")',
        reaches(API, "requests.get"),
    ),
    (f'%prun -s cumulative pd.read_csv("{TRIPS_URL}")', reaches(DATA, "pd.read_csv")),
    (f'%time trips = pd.read_csv("{TRIPS_URL}")', reaches(DATA, "pd.read_csv")),
    (
        f"!python -c \"import urllib.request; urllib.request.urlretrieve('{TRIPS_URL}', 't.csv')\"",
        reaches(DATA, "!python -c"),
    ),
    (
        f"%%bash\npython3 -c \"import pandas as pd; pd.read_csv('{TRIPS_URL}')\"",
        reaches(DATA, "python3 -c"),
    ),
    ("%load https://example.org/snippets/plot.py", reaches("`example.org`", "%load")),
    (f"%%time\ntrips = pd.read_csv('{TRIPS_URL}')", reaches(DATA, "pd.read_csv")),
    (
        f"%%script python3\nimport pandas as pd\npd.read_csv('{TRIPS_URL}')",
        reaches(DATA, "pd.read_csv"),
    ),
    # several sites: the hosts in source order, once each
    (
        f'a = pd.read_csv("{TRIPS_URL}")\nb = pd.read_csv("https://api.example.org/b.csv")\nc = pd.read_csv("{TRIPS_URL}")',
        reaches(f"{DATA} and {API}", "pd.read_csv", 2),
    ),
    (
        "\n".join(f'f{n} = pd.read_csv("https://h{n}.example.org/t.csv")' for n in range(5)),
        reaches(
            "`h0.example.org`, `h1.example.org`, `h2.example.org` and 2 more", "pd.read_csv", 4
        ),
    ),
    (
        f'trips = pd.read_csv("{TRIPS_URL}")\nr = requests.get(url)',
        reaches(f"{DATA} and other hosts", "pd.read_csv", 1),
    ),
]


@pytest.mark.parametrize(("code", "message"), L012_FIRES)
def test_l012_fires_on_every_source(code: str, message: str) -> None:
    [issue] = network_issues(code)
    assert (issue.message, issue.severity, issue.key) == (message, "ask", "network")
    assert issue.fix == L012_FIX
    assert issue.message == f"The cell {issue.question} ({issue.message.split(' (', 1)[1]}"


# Each call, session, method and command network.py lists, in a form only its entry makes fire:
# a URL nh can't read (no literal URL for the generic rule to catch), a bare host, a command
# string. Taking an entry out of its table fails its row. The verbs are spelled out here, not
# read from the scanner.
_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")
FTP = "`ftp.example.org`"
SMTP = "`smtp.example.org`"
L012_EACH_ENTRY = [
    *[(f"r = requests.{verb}(url)", reaches("", f"requests.{verb}")) for verb in _VERBS],
    *[(f"r = requests.api.{verb}(url)", reaches("", f"requests.api.{verb}")) for verb in _VERBS],
    *[(f"r = httpx.{verb}(url)", reaches("", f"httpx.{verb}")) for verb in (*_VERBS, "stream")],
    ("r = urlopen(url)", reaches("", "urlopen")),
    ('urlretrieve(url, "t.csv")', reaches("", "urlretrieve")),
    ('urllib.request.urlretrieve(url, "t.csv")', reaches("", "urllib.request.urlretrieve")),
    ('r = urllib3.request("GET", url)', reaches("", "urllib3.request")),
    ('socket.getaddrinfo("api.example.org", 443)', reaches(API, "socket.getaddrinfo")),
    ('socket.gethostbyname_ex("api.example.org")', reaches(API, "socket.gethostbyname_ex")),
    ('ftp = ftplib.FTP_TLS("ftp.example.org")', reaches(FTP, "ftplib.FTP_TLS")),
    ('server = smtplib.SMTP_SSL("smtp.example.org")', reaches(SMTP, "smtplib.SMTP_SSL")),
    # a session's own host, for a later method given none
    ('ftp = ftplib.FTP("ftp.example.org")\nftp.connect()', reaches(FTP, "ftplib.FTP", 1)),
    ('ftp = ftplib.FTP_TLS("ftp.example.org")\nftp.connect()', reaches(FTP, "ftplib.FTP_TLS", 1)),
    (
        'server = smtplib.SMTP("smtp.example.org")\nserver.send(b"NOOP\\r\\n")',
        reaches(SMTP, "smtplib.SMTP", 1),
    ),
    (
        'server = smtplib.SMTP_SSL("smtp.example.org")\nserver.send(b"NOOP\\r\\n")',
        reaches(SMTP, "smtplib.SMTP_SSL", 1),
    ),
    (
        'pool = urllib3.HTTPConnectionPool("api.example.org")\nr = pool.request("GET", "/t")',
        reaches(API, "pool.request"),
    ),
    (
        'pool = urllib3.HTTPSConnectionPool("api.example.org")\nr = pool.request("GET", "/t")',
        reaches(API, "pool.request"),
    ),
    # sessions, then a method given a URL nh can't read
    *[
        (f"s = {session}()\nr = s.get(url)", reaches("", "s.get"))
        for session in (
            "requests.session",
            "requests.sessions.Session",
            "httpx.AsyncClient",
            "urllib3.PoolManager",
        )
    ],
    ('s = urllib3.ProxyManager(proxy)\nr = s.request("GET", url)', reaches("", "s.request")),
    ("opener = urllib.request.build_opener()\nr = opener.open(url)", reaches("", "opener.open")),
    *[
        (f"s = requests.Session()\nr = s.{method}(url)", reaches("", f"s.{method}"))
        for method in ("post", "put", "patch", "delete", "head", "options", "stream")
        + ("send", "urlopen")
    ],
    (
        "async def main():\n    async with aiohttp.ClientSession() as s:\n        ws = await s.ws_connect(url)",
        reaches("", "s.ws_connect"),
    ),
    # FTP and SMTP made with no host connect through `connect`; a socket's address
    ('ftp = ftplib.FTP()\nftp.connect("ftp.example.org")', reaches(FTP, "ftp.connect")),
    ('ftp = ftplib.FTP_TLS()\nftp.connect("ftp.example.org")', reaches(FTP, "ftp.connect")),
    (
        'server = smtplib.SMTP()\nserver.connect("smtp.example.org", 25)',
        reaches(SMTP, "server.connect"),
    ),
    (
        'server = smtplib.SMTP_SSL()\nserver.connect("smtp.example.org", 465)',
        reaches(SMTP, "server.connect"),
    ),
    (
        'sock = socket.socket()\nsock.connect_ex(("api.example.org", 443))',
        reaches(API, "sock.connect_ex"),
    ),
    (
        'sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\nsock.sendto(b"ping", ("api.example.org", 53))',
        reaches(API, "sock.sendto"),
    ),
    # Python shell calls given a command string
    (f'out = os.popen("curl -s {TRIPS_URL}").read()', reaches(DATA, "os.popen")),
    (f'subprocess.call("curl -O {TRIPS_URL}", shell=True)', reaches(DATA, "subprocess.call")),
    (
        f'subprocess.check_call("curl -O {TRIPS_URL}", shell=True)',
        reaches(DATA, "subprocess.check_call"),
    ),
    (f'p = subprocess.Popen("curl -O {TRIPS_URL}", shell=True)', reaches(DATA, "subprocess.Popen")),
    (f'out = subprocess.getoutput("curl -s {TRIPS_URL}")', reaches(DATA, "subprocess.getoutput")),
    (
        f'code, out = subprocess.getstatusoutput("curl -s {TRIPS_URL}")',
        reaches(DATA, "subprocess.getstatusoutput"),
    ),
    (
        f'out = get_ipython().getoutput("curl -s {TRIPS_URL}")',
        reaches(DATA, "get_ipython().getoutput"),
    ),
    # the host tools, the package managers' verbs, each shell for %%script, its value options
    *[
        (f"!{tool} api.example.org", reaches(API, f"!{tool}"))
        for tool in ("ftp", "telnet", "ncat", "netcat", "ping6", "nslookup", "host")
        + ("traceroute", "whois")
    ],
    ("!yarn add lodash", reaches("", "!yarn")),
    ("!pnpm add lodash", reaches("", "!pnpm")),
    ("!npm i lodash", reaches("", "!npm")),
    ("!apt-get upgrade -y", reaches("", "!apt-get")),
    *[
        (f"%%script {shell}\ncurl -O {TRIPS_URL}", reaches(DATA, "curl"))
        for shell in ("sh", "zsh", "dash", "ksh")
    ],
    (f"%%script --err e bash\ncurl -O {TRIPS_URL}", reaches(DATA, "curl")),
    (f"%%script --proc p bash\ncurl -O {TRIPS_URL}", reaches(DATA, "curl")),
    (f"%%sx\ncurl -O {TRIPS_URL}", reaches(DATA, "curl")),
    # rendered commands: shlex.join, shlex.quote, str(), a name holding a list
    (
        'os.system(shlex.join(["kaggle", "datasets", "download", "-d", "org/trips"]))',
        reaches("", "os.system"),
    ),
    (
        f'URL = "{TRIPS_URL}"\nos.system("curl -sO " + shlex.quote(URL))',
        reaches(DATA, "os.system"),
    ),
    (f'URL = "{TRIPS_URL}"\nq = shlex.quote(URL)\n!curl -sO {{q}}', reaches(DATA, "!curl")),
    (f'URLS = ["{TRIPS_URL}"]\nq = shlex.quote(URLS[0])\n!curl -sO {{q}}', reaches(DATA, "!curl")),
    (
        f'URL = "{TRIPS_URL}"\nsubprocess.run(["curl", "-sO", str(URL)])',
        reaches(DATA, "subprocess.run"),
    ),
    (
        f'URL = "{TRIPS_URL}"\nARGS = ["curl", "-sO", URL]\nsubprocess.run(ARGS)',
        reaches(DATA, "subprocess.run"),
    ),
]


@pytest.mark.parametrize(("code", "message"), L012_EACH_ENTRY)
def test_l012_each_listed_call_and_command_fires_by_itself(code: str, message: str) -> None:
    assert network_message(code) == message


# What hands a URL back (a name, a loop or a call that passes it on), each by itself: the URL
# reaches the call that reads it, not the one that passed it on.
_URLS = f'URLS = ["{TRIPS_URL}"]\n'
_URL = f'URL = "{TRIPS_URL}"\n'
_LOOP = "\n    frames.append(pd.read_csv(u))"
L012_PASSES_ON = [
    *[
        _URLS + f"for u in {wrap}(URLS):{_LOOP}"
        for wrap in ("sorted", "reversed", "set", "frozenset", "list", "tuple", "iter")
        + ("tqdm", "tqdm.tqdm", "copy.copy", "copy.deepcopy")
    ],
    _URLS + f"for u in filter(None, URLS):{_LOOP}",
    _URLS + f"for i, u in enumerate(URLS):{_LOOP}",
    _URLS + f'for name, u in zip(["trips"], URLS):{_LOOP}',
    _URLS + f"from tqdm.auto import tqdm\nfor u in tqdm(URLS):{_LOOP}",
    _URLS + f"from tqdm.notebook import tqdm\nfor u in tqdm(URLS):{_LOOP}",
    _URLS + f"import tqdm.auto\nfor u in tqdm.auto.tqdm(URLS):{_LOOP}",
    _URLS + f"import tqdm.notebook\nfor u in tqdm.notebook.tqdm(URLS):{_LOOP}",
    _URLS + f"for u in URLS.copy():{_LOOP}",
    f'SOURCES = {{"trips": "{TRIPS_URL}"}}\nfor u in SOURCES.values():{_LOOP}',
    _URLS + "trips = pd.read_csv(max(URLS))",
    _URLS + "trips = pd.read_csv(min(URLS))",
    _URLS + "trips = pd.read_csv(next(iter(URLS)))",
    *[
        _URL + f"trips = pd.read_csv(URL.{method})"
        for method in ("strip()", "rstrip()", "lstrip()", "lower()", "casefold()")
        + ('replace("2023", "2024")', 'removeprefix(" ")', 'removesuffix("/")')
        + ("encode().decode()",)
    ],
    _URL + 'trips = pd.read_csv(URL.partition("?")[0])',
    _URL + 'BASE = URL.rpartition("/")[0]\ntrips = pd.read_csv(BASE + "/trips-2024.csv")',
    _URL + "trips = pd.read_csv(urlparse(URL).geturl())",
    _URL + "trips = pd.read_csv(urlunsplit(urlsplit(URL)))",
    _URL + "trips = pd.read_csv(urldefrag(URL)[0])",
]


@pytest.mark.parametrize("code", L012_PASSES_ON)
def test_l012_a_url_passed_on_reaches_the_call_that_reads_it(code: str) -> None:
    assert network_message(code) == reaches(DATA, "pd.read_csv")


L012_QUIET = [
    # not network (plan default 12): databases, env-sourced URLs, run-time URLs, loopback, files
    'engine = sqlalchemy.create_engine("postgresql://analyst:pw@db.example.org:5432/sales")',
    'trips = pd.read_sql("SELECT * FROM trips LIMIT 5", "mysql+pymysql://u:p@db.example.org/x")',
    'con = psycopg.connect(host="db.example.org", dbname="sales")',
    'con = sqlite3.connect("../data/raw/trips.db")',
    'con = duckdb.connect("md:trips")',
    'trips = pd.read_csv(os.environ["DATA_URL"])',
    f'trips = pd.read_csv(os.getenv("DATA_URL", "{TRIPS_URL}"))',
    f'trips = pd.read_csv(os.environ.get("DATA_URL", "{TRIPS_URL}"))',
    f'URL = getenv("DATA_URL", "{TRIPS_URL}")',
    f'SRC, OUT = "{TRIPS_URL}", "out.csv"\ntrips.to_csv(OUT)',  # each name its own value
    f'URL = environ.get("DATA_URL", "{TRIPS_URL}")',
    f'src = np.select([is_remote, is_local], ["{TRIPS_URL}", "../data/raw/t.csv"])',
    f'req = requests.Request("GET", "{TRIPS_URL}")',
    f'req = httpx.Request("GET", "{TRIPS_URL}")',
    'DATA_URL = os.environ.get("DATA_URL") or env_values["DATA_URL"].strip("\'\\"")\ntrips = pd.read_csv(DATA_URL)',
    'trips = pd.read_csv(f"https://{HOST}/trips.csv")',
    'trips = pd.read_csv("https://" + host + "/trips.csv")',
    'trips = pd.read_csv("https://{}/trips.csv".format(host))',
    'trips = pd.read_csv("https://%s/trips.csv" % host)',
    "trips = pd.read_csv(make_url(year))",
    "trips = pd.read_csv(cfg.url)",
    'trips = pd.read_csv("http://localhost:8000/trips.csv")',
    'r = requests.get("http://127.0.0.1:5000/api")',
    'r = requests.get("http://[::1]:8888/api/status")',
    'r = requests.get("http://0.0.0.0:8000/")',
    'r = requests.get("http://lab.localhost/api")',
    'r = requests.get(f"http://localhost:{port}/api/kernels")',
    'conn = http.client.HTTPConnection("localhost", 8888)\nconn.request("GET", "/api")',
    "!curl -s http://localhost:8888/api/status",
    'trips = pd.read_csv("file:///home/me/trips.csv")',
    'trips = pd.read_csv("../data/raw/trips.csv")',
    'trips = pd.read_parquet("data/raw/trips/")',
    # prose holding a URL, and the quiet calls
    f'print("Source: {TRIPS_URL}")',
    f'plt.title("Trips (from {TRIPS_URL})")',
    f'URL = "{TRIPS_URL}"\nprint(URL)',
    f'URL = "{TRIPS_URL}"\nprint(f"reading {{URL}}")',
    f'URL = "{TRIPS_URL}"\ndisplay(Markdown(f"[trips]({{URL}})"))',
    f'URL = "{TRIPS_URL}"\nlogger.info("reading %s", URL)',
    f'URL = "{TRIPS_URL}"\nlogging.getLogger(__name__).debug(URL)',
    f'URL = "{TRIPS_URL}"\nwarnings.warn(f"{{URL}} is slow")',
    f'URL = "{TRIPS_URL}"\nraise ValueError(f"bad URL: {{URL}}")',
    f'URL = "{TRIPS_URL}"\nraise ValueError(URL)',
    f'URL = "{TRIPS_URL}"\nok = name.endswith(URL)',
    f'URL = "{TRIPS_URL}"\nfrom rich import print as rprint\nrprint(URL)',
    # containers, frames and labels that hold a URL as a value
    'pd.DataFrame({"url": ["https://a.example.com/x", "https://b.example.com/y"]})',
    f'URL = "{TRIPS_URL}"\nsources = pd.Series([URL, URL])',
    'df[df["referrer"].isin(["https://www.google.com/"])]',
    f'URL = "{TRIPS_URL}"\nmine = df["src"].eq(URL)',
    f'URL = "{TRIPS_URL}"\ndf = df.fillna({{"src": URL}}).assign(origin=URL)',
    f'URL = "{TRIPS_URL}"\nflag = np.where(df["src"] == URL, 1, 0)',
    f'URL = "{TRIPS_URL}"\ncounts = Counter([URL])',
    f'URL = "{TRIPS_URL}"\nlocal = Path("../data/raw") / Path(URL).name',
    f'URL = "{TRIPS_URL}"\nname = os.path.basename(URL)',
    f'URL = "{TRIPS_URL}"\nkey = hashlib.sha256(URL.encode()).hexdigest()',
    f'URL = "{TRIPS_URL}"\nquoted = shlex.quote(URL)',
    f'URL = "{TRIPS_URL}"\nax.set_title(URL)',
    f'ax.set_title("{TRIPS_URL}")',
    f'URL = "{TRIPS_URL}"\ntrips.plot(title=URL)',
    f'URL = "{TRIPS_URL}"\nax.plot(x, y, label=URL)',
    f'URL = "{TRIPS_URL}"\nfor row in tqdm(rows, desc=URL):\n    pass',
    f'URL = "{TRIPS_URL}"\nfig.update_layout(title=URL)',
    f'URL = "{TRIPS_URL}"\nparser.add_argument("--url", default=URL)',
    f'URL = "{TRIPS_URL}"\nfrom pydantic import HttpUrl\nu = HttpUrl(URL)',
    'trips = pd.read_csv(f"https://{sub}.example.org/t.csv")',
    'note(f"https://data.example.org/t.csv was {state}")',
    f'URL = "{TRIPS_URL}"\nhost = urlparse(URL).netloc',
    f'URL = "{TRIPS_URL}"\nparts = urllib.parse.urlsplit(URL)',
    f'URL = "{TRIPS_URL}"\nok = URL.startswith("https://")',
    f'URL = "{TRIPS_URL}"\nname = URL.split("/")[-1]',
    f'URL = "{TRIPS_URL}"\nok = re.match(r"https://", URL)',
    f'URL = "{TRIPS_URL}"\nn = len(URL)',
    f'URL = "{TRIPS_URL}"\nreq = Request(URL)',
    f'URL = "{TRIPS_URL}"\nconfig = dict(url=URL)',
    f'URL = "{TRIPS_URL}"\ntext = json.dumps({{"url": URL}})',
    f'URL = "{TRIPS_URL}"\nwith open("sources.txt", "w") as f:\n    f.write(URL)',
    f'URL = "{TRIPS_URL}"\nkey = {{"a": 1}}.get("k", URL)',
    'IFrame("https://data.example.org/dashboard", width=600, height=400)',
    f'URL = "{TRIPS_URL}"',
    f'SOURCES = {{"trips": "{TRIPS_URL}"}}',
    # shell that stays here, or is L009's
    f"!echo {TRIPS_URL}",
    f"!printf '%s\\n' {TRIPS_URL}",
    "!pip install git+https://github.com/org/pkg",
    "%pip install https://example.org/pkg-1.0-py3-none-any.whl",
    "!python -m pip install https://example.org/pkg-1.0-py3-none-any.whl",
    *[f"!{tool} install -c https://conda.example.org/main trips" for tool in ("conda", "mamba")],
    "!micromamba install -c https://conda.example.org/main trips",
    "!uv pip install https://example.org/pkg-1.0-py3-none-any.whl",
    f"!bash -c 'echo {TRIPS_URL}'",
    "!git status",
    "!git clone",
    "!git clone ../other-repo copy",
    "!git clone /srv/repos/trips.git",
    "!git clone file:///srv/repos/trips.git",
    "!git submodule status",
    "!git submodule foreach git status",
    "!git fetch ../other-repo",
    "!git pull file:///srv/repos/trips.git main",
    "!git log --oneline -5",
    "!kaggle --version",
    "!apt list --installed",
    "!npm run build",
    "!pip list",
    f"%%script --bg bash\necho {TRIPS_URL}",
    'subprocess.run(["ls", "-la", "../data/raw"])',
    f'URL = "{TRIPS_URL}"\nos.system("echo " + URL)',
    f'URL = "{TRIPS_URL}"\ncmd = f"curl -O {{URL}}"\nprint(cmd)',
    "%timeit trips.dropna()",
    "!curl --version",
    "!scp trips.csv backup/trips.csv",
    "!rsync -av data/ /mnt/backup/",
    "!ls -la ../data/raw",
    # cells nh doesn't read as code
    f'%%html\n<img src="{TRIPS_URL}">',
    "%%sql\nSELECT * FROM trips",
    f"%%writefile fetch.py\nimport requests\nrequests.get('{TRIPS_URL}')",
    "",
]


@pytest.mark.parametrize("code", L012_QUIET)
def test_l012_is_quiet_on_code_that_stays_here(code: str) -> None:
    assert network_issues(code) == []


# Each quiet call, label keyword, write, logging call, shell word and option network.py lists,
# given the URL by itself: taking the entry out makes the row ask. Spelled out here, not read
# from the scanner.
L012_QUIET_EACH = [
    *[
        _URL + f"x = {name}(URL)"
        for name in ("print", "pprint", "pp", "display", "repr", "ascii", "format", "len")
        + ("bool", "type", "hash", "id", "callable", "help", "Markdown", "HTML", "Latex")
        + ("Code", "IFrame", "display_markdown", "display_html", "warn", "DataFrame", "Series")
        + ("Index", "Categorical", "array", "asarray", "Counter", "OrderedDict", "defaultdict")
        + ("deque", "HttpUrl", "AnyUrl", "AnyHttpUrl", "ValueError", "UserWarning")
        + ("os.getenv", "os.getenvb", "os.environ.get", "os.environ.setdefault", "json.dumps")
        + ("json.dump", "warnings.warn", "np.select", "yarl.URL", "getenv")
        # urllib.parse's functions, imported or not; the quiet modules
        + ("urljoin", "urlparse", "urlsplit", "urlunparse", "urlunsplit", "urlencode", "quote")
        + ("quote_plus", "unquote", "unquote_plus", "parse_qs", "parse_qsl", "urldefrag")
        + ("urllib.parse.quote", "re.escape", "logging.info", "IPython.display.Image")
        + ("textwrap.dedent", "os.path.basename", "posixpath.basename", "ntpath.basename")
        + ("pathlib.PurePosixPath", "hashlib.md5", "shlex.shlex")
    ],
    _URL + "x = isinstance(URL, str)",
    _URL + "x = issubclass(URL, str)",
    _URL + "log = logging.getLogger(URL)",
    _URL + 'from numpy import where\nx = where(is_remote, URL, "")',
    _URL + 'URL = environ.get("DATA_URL", URL)\ntrips = pd.read_csv(URL)',
    # methods that compare, relabel or label
    *[
        _URL + f"x = obj.{method}(URL)"
        for method in ("isin", "eq", "ne", "lt", "le", "gt", "ge", "fillna", "where", "mask")
        + ("assign", "rename", "groupby", "contains", "drop", "query", "set_title", "set_xlabel")
        + ("set_ylabel", "set_zlabel", "suptitle", "supxlabel", "supylabel", "xlabel", "ylabel")
        + ("text", "annotate", "figtext", "legend", "set_label", "set_text", "set_caption")
        + ("set_xticklabels", "set_yticklabels", "add_annotation", "update_layout")
        + ("update_xaxes", "update_yaxes", "properties", "add_argument")
        # file writes
        + ("write", "writelines", "writerow", "writerows", "write_text", "write_bytes")
    ],
    *[
        _URL + f"chart(rows, {keyword}=URL)"
        for keyword in (
            "title",
            "label",
            "xlabel",
            "ylabel",
            "zlabel",
            "desc",
            "description",
            "caption",
            "help",
            "name",
            "text",
            "legend",
        )
    ],
    *[
        _URL + f"log.{method}(URL)"
        for method in ("debug", "info", "warning", "warn", "error", "critical", "exception")
        + ("fatal", "log")
    ],
    # a lookup's default is handed back, not requested
    *[_URL + f'x = cache.{method}("trips", URL)' for method in ("get", "pop", "setdefault")],
    # shell words that only say their arguments
    *[f"!{word} {TRIPS_URL}" for word in ("echo", "printf", "print", "true", ":")],
]


@pytest.mark.parametrize("code", L012_QUIET_EACH)
def test_l012_each_listed_quiet_call_is_quiet(code: str) -> None:
    assert network_issues(code) == []


# A name a mutating method fills (`URLS.extend([...])`) holds the URL; the method itself is no
# request.
L012_FILLS = [
    "URLS = []\nURLS.append(URL)",
    "URLS = []\nURLS.extend([URL])",
    "URLS = []\nURLS.insert(0, URL)",
    "URLS = set()\nURLS.add(URL)",
    "URLS = set()\nURLS.update({URL})",
    "URLS = deque()\nURLS.appendleft(URL)",
    "URLS = deque()\nURLS.extendleft([URL])",
    'SOURCES = {}\nSOURCES.setdefault("trips", URL)\nURLS = SOURCES.values()',
]


@pytest.mark.parametrize("fill", L012_FILLS)
def test_l012_a_filled_name_holds_the_url(fill: str) -> None:
    code = f"{_URL}{fill}\nfor u in URLS:{_LOOP}"
    assert network_message(code) == reaches(DATA, "pd.read_csv")


# Options that take a value, so the word after them is not the host, command or statement.
L012_VALUE_OPTIONS = [
    *[
        (f"!ssh {option} x analyst@api.example.org", reaches(API, "!ssh"))
        for option in ("-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m")
        + ("-O", "-o", "-P", "-p", "-Q", "-R", "-S", "-W", "-w", "-B")
    ],
    *[
        (f"!git {option} x fetch https://github.com/org/repo", reaches(GITHUB, "!git fetch"))
        for option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
    ],
    *[
        (f"!env {option} x curl -sO {TRIPS_URL}", reaches(DATA, "!curl"))
        for option in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string")
    ],
    *[
        (f"%timeit {option} 3 pd.read_csv(URL)", reaches(DATA, "pd.read_csv"))
        for option in ("-n", "-r", "-p", "-l", "-s", "-T", "-D")
    ],
]


@pytest.mark.parametrize(("code", "message"), L012_VALUE_OPTIONS)
def test_l012_an_option_s_value_is_not_its_word(code: str, message: str) -> None:
    assert network_message(_URL + code if code.startswith("%") else code) == message


def test_l012_canonical_text() -> None:
    [issue] = lint(f'TRIPS_URL = "{TRIPS_URL}"\ntrips = pd.read_csv(TRIPS_URL)').asks
    assert issue.rule == "L012" and issue.key == "network"
    assert (
        issue.message == "The cell connects to `data.example.org` over the network (`pd.read_csv`)."
    )
    assert issue.question == "connects to `data.example.org` over the network"
    assert issue.fix == L012_FIX
    unknown = lint("r = requests.get(url)").asks[0]
    assert unknown.question == "connects to the network"


APPROVED_FORMS = [
    (TRIPS_URL, ["data.example.org"]),
    ("https://analyst:pw@DATA.Example.ORG.:8443/trips.csv?token=x", ["data.example.org"]),
    (TRIPS_URL, ["DATA.EXAMPLE.ORG."]),
    (TRIPS_URL, ["data.example.org:443"]),
    (TRIPS_URL, ["analyst@data.example.org"]),
    (TRIPS_URL, ["[::1]", "data.example.org", 7, None, "not a host!"]),
    ("https://bücher.example/trips.csv", ["xn--bcher-kva.example"]),
    ("https://BÜCHER.example/trips.csv", ["Bücher.Example."]),
    ("https://xn--bcher-kva.example/trips.csv", ["bücher.example"]),
    ("http://[2001:DB8::1]:8080/trips.csv", ["2001:db8::1"]),
    ("s3://trips-bucket/2023/trips.parquet", ["s3://trips-bucket"]),
    ("s3a://trips-bucket/2023/trips.parquet", ["S3://Trips-Bucket/"]),
    ("gcs://trips-bucket/t.parquet", ["gs://trips-bucket"]),
    ("hf://datasets/org/trips/t.parquet", ["huggingface.co"]),
    ("https://user:p%40ss@data.example.org/x.csv", ["data.example.org"]),
    ("https://user:pa$$w0rd@data.example.org/x.csv", ["data.example.org"]),
]


@pytest.mark.parametrize(("url", "approved"), APPROVED_FORMS)
def test_l012_skips_an_approved_host(url: str, approved: list[Any]) -> None:
    code = f'DATA_URL = "{url}"\ntrips = pd.read_csv(DATA_URL)'
    assert network_issues(code) != []
    hosts = [h for h in approved if isinstance(h, str)]
    assert network_issues(code, approved=frozenset(hosts)) == []


@pytest.mark.parametrize(
    "code",
    [
        f"!curl -O {TRIPS_URL}",
        f"%%bash\nwget {TRIPS_URL}",
        f'r = requests.get("{TRIPS_URL}")',
        'with httpx.Client(base_url="https://data.example.org") as c:\n    r = c.get("/t")',
        'sock = socket.create_connection(("data.example.org", 443))',
        "!git clone git@data.example.org:org/repo.git",
        f'a = pd.read_csv("{TRIPS_URL}")\nb = pd.read_csv("http://localhost:8000/b.csv")',
    ],
)
def test_l012_approved_hosts_cover_every_kind_of_site(code: str) -> None:
    assert network_issues(code) != []
    assert network_issues(code, approved=["data.example.org"]) == []


def test_l012_approval_is_exact() -> None:
    """A subdomain isn't covered, nor is a parent domain; a site nh can't read is never
    skipped, and a cell with an approved and another host names only the other."""
    sub = 'trips = pd.read_csv("https://api.data.example.org/t.csv")'
    assert network_message(sub, ["data.example.org"]) == reaches(
        "`api.data.example.org`", "pd.read_csv"
    )
    assert network_message(TRIPS_URL.join(['t = pd.read_csv("', '")']), ["example.org"]) is not None
    assert network_message("r = requests.get(url)", ["data.example.org"]) == reaches(
        "", "requests.get"
    )
    both = f'a = pd.read_csv("{TRIPS_URL}")\nb = pd.read_csv("https://api.example.org/b.csv")'
    assert network_message(both, ["data.example.org"]) == reaches(API, "pd.read_csv")
    one_site = f'frames = read_all(["{TRIPS_URL}", "https://api.example.org/b.csv"])'
    assert network_message(one_site, ["data.example.org"]) == reaches(API, "read_all")
    unreadable = f'frames = read_all(["{TRIPS_URL}", "https://faß.de/b.csv"])'
    assert network_message(unreadable, ["data.example.org"]) == reaches("", "read_all")
    alias = "!scp a.csv data.example.org:/x backup:/srv"
    assert network_message(alias, ["data.example.org"]) == reaches("", "!scp")


def test_l012_buckets_and_hosts_are_separate_keys() -> None:
    s3 = 'trips = pd.read_parquet("s3://trips-bucket/t.parquet")'
    assert network_issues(s3, approved=["s3://trips-bucket"]) == []
    for other in (["trips-bucket"], ["gs://trips-bucket"], ["s3://trips-bucket/2023"]):
        assert network_message(s3, other) == reaches(BUCKET, "pd.read_parquet"), other
    named = 'trips = pd.read_csv("https://trips-bucket/t.csv")'
    assert network_message(named, ["s3://trips-bucket"]) == reaches("`trips-bucket`", "pd.read_csv")


def test_l012_idna_deviation_characters_are_never_approved() -> None:
    """IDNA 2003 maps `faß.de` to `fass.de`; requests sends `xn--fa-hia.de`: a host nh can't
    read, so no entry approves it."""
    code = 'r = requests.get("https://faß.de/data.json")'
    for approved in (["fass.de"], ["faß.de"], ["xn--fa-hia.de"]):
        assert network_message(code, approved) == reaches("", "requests.get"), approved
    wire = 'r = requests.get("https://xn--fa-hia.de/data.json")'
    assert network_issues(wire, approved=["xn--fa-hia.de"]) == []


ABOVE = [
    f'TRIPS_URL = "{TRIPS_URL}"',
    'client = httpx.Client(base_url="https://api.example.org")',
    "import requests as rq\nfrom urllib.request import urlopen as fetch_url",
    'HOST = "data.example.org"',
]


@pytest.mark.parametrize(
    ("code", "above", "alone"),
    [
        (
            'stations = pd.read_csv(TRIPS_URL.rsplit("/", 1)[0] + "/stations.csv")',
            reaches(DATA, "pd.read_csv"),
            None,
        ),
        ("trips = pd.read_csv(TRIPS_URL)", reaches(DATA, "pd.read_csv"), None),
        ('r = client.get("/trips")', reaches(API, "client.get"), None),
        ("r = rq.get(url)", reaches("", "rq.get"), None),
        ("body = fetch_url(url).read()", reaches("", "fetch_url"), None),
        ('trips = pd.read_csv(f"https://{HOST}/t.csv")', reaches(DATA, "pd.read_csv"), None),
        ("!curl -O {TRIPS_URL}", reaches(DATA, "!curl"), reaches("", "!curl")),
    ],
)
def test_l012_reads_names_from_the_cells_above(code: str, above: str, alone: str | None) -> None:
    """A name, session or import bound in an earlier cell (one the user approved, say) counts
    here; without the cells above nh knows nothing of it. The approved list still applies."""
    assert network_message(code) == alone
    assert [i.message for i in network_issues(code, code_above=ABOVE)] == [above]
    both = ["data.example.org", "api.example.org"]
    left = [i.message for i in network_issues(code, approved=both, code_above=ABOVE)]
    assert left == ([] if "`" in above.split(" (")[0] else [above])


def test_l012_cells_above_bind_but_never_ask() -> None:
    from nh_gateway.lint import network

    reads = "trips = pd.read_csv(URL)"
    assert network_issues("trips.head()", code_above=[f'pd.read_csv("{TRIPS_URL}")']) == []
    assert network_issues(reads, code_above=[f'URL = "{TRIPS_URL}"']) != []
    rebound = [f'URL = "{TRIPS_URL}"', 'URL = "../data/raw/trips.csv"']
    assert network_issues(reads, code_above=rebound) == []  # the last binding counts
    here = f'URL = "../data/raw/trips.csv"\n{reads}'
    assert network_issues(here, code_above=[f'URL = "{TRIPS_URL}"']) == []
    broken = [f'URL = "{TRIPS_URL}"', "def broken(:", "%%bash\nURL=x", "%%sql\nSELECT 1"]
    assert network_issues(reads, code_above=broken) != []  # an unparsable cell binds nothing
    assert network_issues(reads, code_above=[f'%%time\nURL = "{TRIPS_URL}"']) != []
    assert network.bindings([f'URL = "{TRIPS_URL}"', "def broken(:"]) == network.bindings(
        [f'URL = "{TRIPS_URL}"']
    )


def test_l012_a_walk_of_the_cells_above_that_raises_leaves_the_seed_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nh_gateway.lint import network

    def boom(source: str, seed: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(network, "_cell_seed", boom)
    above = [f'URL = "{TRIPS_URL}"']
    assert network.bindings(above) == network.EMPTY
    assert network_issues("trips = pd.read_csv(URL)", code_above=above) == []
    assert network_issues(f'trips = pd.read_csv("{TRIPS_URL}")', code_above=above) != []


def test_l012_walks_each_cell_above_once() -> None:
    from nh_gateway.lint import network

    above = [f'URL_{n} = "https://h{n}.example.org/t.csv"' for n in range(200)]
    network._cell_seed.cache_clear()
    started = time.perf_counter()
    for _ in range(5):
        [issue] = network_issues("trips = pd.read_csv(URL_199)", code_above=above)
    assert time.perf_counter() - started < 3.0
    assert issue.message == reaches("`h199.example.org`", "pd.read_csv")
    info = network._cell_seed.cache_info()
    assert info.misses == 200 and info.hits == 4 * 200


def test_l012_reads_the_approved_hosts_file(tmp_path: Path) -> None:
    """A missing or corrupt file approves nothing (the gateway reads it per call)."""
    from nh_gateway._shared import hosts

    code = f'trips = pd.read_csv("{TRIPS_URL}")'
    path = tmp_path / "approved_hosts.json"
    for text in [None, "", "{", '{"hosts": ["data.example.org"]}', '"data.example.org"', "\xff"]:
        if text is not None:
            path.write_bytes(text.encode("utf-8", "surrogateescape") if text != "\xff" else b"\xff")
        assert network_issues(code, approved=hosts.read_approved(path)) != [], text
    path.write_text('["data.example.org"]', encoding="utf-8")
    assert network_issues(code, approved=hosts.read_approved(path)) == []


SECRET_URL = (
    "https://analyst:s3cr3t-pw@data.example.org:8443/private/trips.csv?token=tok_abc123#frag"
)


@pytest.mark.parametrize(
    "code",
    [
        f'trips = pd.read_csv("{SECRET_URL}")',
        f'URL = "{SECRET_URL}"\nr = requests.get(URL, auth=("analyst", "s3cr3t-pw"))',
        f'r = requests.Session().get("{SECRET_URL}", headers={{"Authorization": "Bearer tok_abc123"}})',
        f'!curl -H "Authorization: Bearer tok_abc123" "{SECRET_URL}"',
        f'os.system("curl -u analyst:s3cr3t-pw {SECRET_URL}")',
        "!scp trips.csv analyst@data.example.org:/private/tok_abc123/",
    ],
)
def test_l012_text_holds_no_userinfo_path_query_or_token(code: str) -> None:
    [issue] = network_issues(code)
    text = f"{issue.message} {issue.question} {issue.fix}"
    assert "`data.example.org`" in text
    for part in ("analyst", "s3cr3t", "8443", "private", "trips.csv", "tok_abc", "frag", "Bearer"):
        assert part not in text, (part, text)


@pytest.mark.parametrize(
    ("level", "mode", "where"),
    [
        ("ask", "advise", "ask"),
        ("ask", "strict", "ask"),
        ("error", "advise", "error"),
        ("hint", "advise", "hint"),
        ("hint", "strict", "error"),
        ("off", "advise", None),
        ("off", "strict", None),
    ],
)
def test_l012_levels(level: str, mode: str, where: str | None) -> None:
    cfg = config(network=level)
    cfg.data["lint"]["mode"] = mode
    report = lint(f'trips = pd.read_csv("{TRIPS_URL}")\ntrips.head()', cfg=cfg)
    found = {s: rules(report, s) for s in ("error", "hint", "ask")}
    assert found == {s: (["L012"] if s == where else []) for s in found}
    assert report.ok == (where != "error")


def test_l012_is_off_without_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """At `off` the scan never runs; a scan that fails finds nothing, as L011's (§6.7)."""
    from nh_gateway.lint import network

    calls: list[str] = []
    monkeypatch.setattr(network, "scan", lambda *args: calls.append("scan") or [])
    lint(f'trips = pd.read_csv("{TRIPS_URL}")', cfg=config(network="off"))
    assert calls == []

    def boom(*args: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(network, "scan", boom)
    report = lint(f'!pip install x\ntrips = pd.read_csv("{TRIPS_URL}")')
    assert rules(report, "ask") == ["L009"]


def test_l012_asks_with_l009_in_one_question() -> None:
    """Both asks, the install first: the gate joins their clauses into one question
    (approvals.question; its text is pinned in tests/gateway/test_approvals.py)."""
    report = lint(f'!pip install seaborn\ntrips = pd.read_csv("{TRIPS_URL}")')
    assert rules(report, "ask") == ["L009", "L012"] and report.ok
    assert [issue.question for issue in report.asks] == [
        "installs `seaborn` into the kernel only, and the next env sync removes it "
        "(`uv add seaborn` keeps it)",
        "connects to `data.example.org` over the network",
    ]
    order = [rule for rule, _, _ in lint_module._CHECKS]
    assert order.index("L009") + 1 == order.index("L012")


def test_l012_scan_knows_the_same_python_cell_magics() -> None:
    from nh_gateway.lint import network

    assert network._PYTHON_CELL_MAGICS == lint_module.PYTHON_CELL_MAGICS


def test_l012_scan_is_fast_on_a_long_cell() -> None:
    lines = [
        f'f{n} = pd.read_csv(f"https://h{n % 7}.example.org/{{y}}.csv").dropna()'
        for n in range(400)
    ]
    lines += [f"!curl -s https://h{n % 7}.example.org/{n}.csv | head" for n in range(100)]
    started = time.perf_counter()
    [issue] = network_issues("\n".join(lines))
    assert time.perf_counter() - started < 3.0
    assert "and 4 more" in issue.question


def _first_cell_with_url(url: str) -> str:
    block = _first_cell_blocks()[0]
    assert 'DATA_PATH = "../data/raw/sales.csv"' in block and "pd.read_csv(DATA_PATH)" in block
    return block.replace('DATA_PATH = "../data/raw/sales.csv"', f'DATA_URL = "{url}"').replace(
        "pd.read_csv(DATA_PATH)", "pd.read_csv(DATA_URL)"
    )


def test_l012_first_cell_with_a_url_asks_unless_its_host_is_approved() -> None:
    """/nh:init's first cell reads a plain data URL as a literal (first-cell.md): scaffold
    approves its host, so nh writes it with no question; any other host asks."""
    cell = _first_cell_with_url(TRIPS_URL)
    approved = lint(cell, kernel_python=(3, 11), approved_hosts=frozenset({"data.example.org"}))
    assert approved.asks == [] and approved.errors == []
    [issue] = lint(cell, kernel_python=(3, 11)).asks
    assert issue.question == "connects to `data.example.org` over the network"
    credentials = _first_cell_blocks()[1]  # the URL comes from .env: not network (default 12)
    assert network_issues(credentials) == []


# L013 -------------------------------------------------------------------------------------------
# Design §6.4 "L013 `outside_write`": its Sinks, "A path nh reads", Shell commands, Outside and
# Exempt tables. Every entry of writes.py's hand-written tables has a row here that fails when
# the entry is taken out (L013_EACH_ENTRY: a form only that entry decides); L013_QUIET holds what
# never asks. The project is at ROOT and the notebook in its `notebooks/` folder unless a test says
# otherwise.
ROOT = "/home/me/proj"
L013_FIX = (
    "Keep the cell's files inside the project (for example under `data/processed/` or "
    "`reports/`); ask the user before writing anywhere else."
)


def outside_issues(code: str, notebook_dir: str = "notebooks", **overrides: Any) -> list[Issue]:
    overrides.setdefault("project_root", ROOT)
    report = lint(code, notebook_dir=notebook_dir, **overrides)
    return [i for i in report.errors + report.hints + report.asks if i.rule == "L013"]


def outside_message(code: str, notebook_dir: str = "notebooks", **overrides: Any) -> str | None:
    found = outside_issues(code, notebook_dir, **overrides)
    return found[0].message if found else None


def wrote(paths: str, where: str, more: int = 0) -> str:
    """The message for writes only: ``paths`` as the clause names them."""
    return f"The cell writes to {paths}, outside the project (`{where}`{_plus(more)})."


def removed(paths: str, where: str, more: int = 0) -> str:
    return f"The cell removes {paths}, outside the project (`{where}`{_plus(more)})."


def moved(to: str, away: str, where: str, more: int = 1) -> str:
    """A write and a removal: a move's destination and its source."""
    return (
        f"The cell writes to {to} and removes {away}, outside the project (`{where}`{_plus(more)})."
    )


def _plus(more: int) -> str:
    return f" (+{more} more)" if more else ""


OUT = "`/data/out.csv`"
L013_FIRES = [
    # the three ways out: `~`, an absolute path, `..` climbing above the root from notebooks/
    ('trips.to_csv("~/exports/trips.csv")', wrote("`~/exports/trips.csv`", "trips.to_csv")),
    ('trips.to_csv("~analyst/trips.csv")', wrote("`~analyst/trips.csv`", "trips.to_csv")),
    ('trips.to_csv("~")', wrote("`~`", "trips.to_csv")),
    ('trips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("../../trips.csv")', wrote("`/home/me/trips.csv`", "trips.to_csv")),
    ('trips.to_csv("../../../../../x.csv")', wrote("`/x.csv`", "trips.to_csv")),
    ('trips.to_csv("./../.././trips.csv")', wrote("`/home/me/trips.csv`", "trips.to_csv")),
    ('trips.to_csv("../data/../../trips.csv")', wrote("`/home/me/trips.csv`", "trips.to_csv")),
    # an absolute path is resolved before it is compared; a sibling sharing the root's prefix is
    # outside, and so are folders whose names start like an exempt one
    (
        'trips.to_csv("/home/me/proj/../other/x.csv")',
        wrote("`/home/me/other/x.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv("/home/me/proj2/x.csv")', wrote("`/home/me/proj2/x.csv`", "trips.to_csv")),
    ('trips.to_csv("/home/me/projx.csv")', wrote("`/home/me/projx.csv`", "trips.to_csv")),
    ('trips.to_csv("/home/me")', wrote("`/home/me`", "trips.to_csv")),
    ('trips.to_csv("/tmpdata/x.csv")', wrote("`/tmpdata/x.csv`", "trips.to_csv")),
    ('trips.to_csv("/devices/x.csv")', wrote("`/devices/x.csv`", "trips.to_csv")),
    ('trips.to_csv("/var/x.csv")', wrote("`/var/x.csv`", "trips.to_csv")),
    ('trips.to_csv("/private/x.csv")', wrote("`/private/x.csv`", "trips.to_csv")),
    # a leading `//`, which posixpath.normpath keeps, reads `/`
    ('trips.to_csv("//data//out.csv")', wrote(OUT, "trips.to_csv")),
    # file:// is its path
    ('trips.to_csv("file:///data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("FILE:///data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("file://localhost/data/out.csv")', wrote(OUT, "trips.to_csv")),
    # through a name, in the cell: assigned, annotated, a default, a walrus, rebound last
    ('OUT = "/data/out.csv"\ntrips.to_csv(OUT)', wrote(OUT, "trips.to_csv")),
    ('OUT: str = "/data/out.csv"\ntrips.to_csv(OUT)', wrote(OUT, "trips.to_csv")),
    ('def save(path="/data/out.csv"):\n    trips.to_csv(path)', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv(out := "/data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('OUT = "x.csv"\nOUT = "/data/out.csv"\ntrips.to_csv(OUT)', wrote(OUT, "trips.to_csv")),
    (
        'EXPORTS = Path("/data/exports")\nOUT = EXPORTS / "trips.csv"\ntrips.to_csv(OUT)',
        wrote("`/data/exports/trips.csv`", "trips.to_csv"),
    ),
    # Path joins and the string forms L012 renders
    ('trips.to_csv(Path("/data") / "out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("/data" / Path("out.csv"))', wrote(OUT, "trips.to_csv")),
    (
        'trips.to_csv(Path("/data", "exports", "out.csv"))',
        wrote("`/data/exports/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(Path("data") / "/abs/out.csv")', wrote("`/abs/out.csv`", "trips.to_csv")),
    ('trips.to_csv(Path("..") / ".." / "out.csv")', wrote("`/home/me/out.csv`", "trips.to_csv")),
    (
        'trips.to_csv(Path("/data/").joinpath("a", "out.csv"))',
        wrote("`/data/a/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(os.path.join("/data", "out.csv"))', wrote(OUT, "trips.to_csv")),
    (
        'trips.to_csv(os.path.join("..", "..", "out.csv"))',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(os.path.join("data", "/data/out.csv"))', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("/data" + "/out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("%s/out.csv" % "/data")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("{}/out.csv".format("/data"))', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv("/".join(["", "data", "out.csv"]))', wrote(OUT, "trips.to_csv")),
    ("trips.to_csv(f\"{'/data'}/out.csv\")", wrote(OUT, "trips.to_csv")),
    ('D = "/data"\ntrips.to_csv(f"{D}/out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv(str(Path("/data") / "out.csv"))', wrote(OUT, "trips.to_csv")),
    # a path with unknown parts: the folder before the first one, shown with "/…"
    ('trips.to_csv(f"/data/exports/{name}.csv")', wrote("`/data/exports/…`", "trips.to_csv")),
    ('trips.to_csv(f"/data/{year}/{name}.csv")', wrote("`/data/…`", "trips.to_csv")),
    ('trips.to_csv(f"~/{name}.csv")', wrote("`~/…`", "trips.to_csv")),
    ('trips.to_csv(f"~{user}/x.csv")', wrote("`~…`", "trips.to_csv")),
    ('trips.to_csv(f"../../exports/{name}.csv")', wrote("`/home/me/exports/…`", "trips.to_csv")),
    ('trips.to_csv(Path("/data") / name)', wrote("`/data/…`", "trips.to_csv")),
    ('trips.to_csv(os.path.join("/data", name, "x.csv"))', wrote("`/data/…`", "trips.to_csv")),
    ('trips.to_csv("/data/" + name + ".csv")', wrote("`/data/…`", "trips.to_csv")),
    # an unknown part stands for text within one folder name: these can't be in the project
    ('trips.to_csv(f"/home/me/{name}.csv")', wrote("`/home/me/…`", "trips.to_csv")),
    ('trips.to_csv(f"/home/{user}/x.csv")', wrote("`/home/…`", "trips.to_csv")),
    ('trips.to_csv(f"/{name}.csv")', wrote("`/…`", "trips.to_csv")),
    ('trips.to_csv(f"../../{name}.csv")', wrote("`/home/me/…`", "trips.to_csv")),
    (
        'for trip_id, group in trips.groupby("trip_id"):\n'
        '    group.to_csv(f"../../trip_{trip_id}.csv")',
        wrote("`/home/me/…`", "group.to_csv"),
    ),
    ('fig.savefig(f"../../{title}.png")', wrote("`/home/me/…`", "fig.savefig")),
    (
        'trips.to_csv(os.path.join("..", "..", f"{name}.csv"))',
        wrote("`/home/me/…`", "trips.to_csv"),
    ),
    # the folder the cell is in
    (
        'trips.to_csv(Path.cwd().parent.parent / "out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    (
        'trips.to_csv(Path(os.getcwd()).parent.parent / "out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(f"{os.getcwd()}/../../out.csv")', wrote("`/home/me/out.csv`", "trips.to_csv")),
    (
        'trips.to_csv(os.path.join("data", os.getcwd(), "../../out.csv"))',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(os.path.abspath("../../out.csv"))', wrote("`/home/me/out.csv`", "trips.to_csv")),
    (
        'trips.to_csv(Path("x.csv").resolve().parent.parent.parent / "out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(Path.home() / "out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    ('trips.to_csv(Path.home().parent / "out.csv")', wrote("`~/../out.csv`", "trips.to_csv")),
    ('%cd /data\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    ('%cd /data/exports\n%cd ..\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    ('%cd ..\n%cd ..\ntrips.to_csv("out.csv")', wrote("`/home/me/out.csv`", "trips.to_csv")),
    ('%cd ~\ntrips.to_csv("out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    ('%cd\ntrips.to_csv("out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    ('%cd -q /data\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    ('%pushd /data\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    # a `~` path doesn't start from the folder the cell moved to, and `~` is a folder to move to
    ('%cd /data\ntrips.to_csv("~/out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    (
        '%cd /data\n%cd ~/exports\ntrips.to_csv("out.csv")',
        wrote("`~/exports/out.csv`", "trips.to_csv"),
    ),
    ('%cd\ntrips.to_csv(Path.cwd() / "out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    ("!cd /data && touch ~/out.csv", wrote("`~/out.csv`", "!touch")),
    ('os.chdir("/data")\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    ('os.chdir(path="/data")\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    (
        'os.chdir(Path.cwd().parent.parent)\ntrips.to_csv("out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('%cd /data\ntrips.to_csv(Path.cwd() / "out.csv")', wrote(OUT, "trips.to_csv")),
    ('%cd /data/exports\ntrips.to_csv(Path.cwd().parent / "out.csv")', wrote(OUT, "trips.to_csv")),
    (
        '%cd sub\ntrips.to_csv(Path.cwd().parent.parent.parent / "out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    (
        'trips.to_csv(Path.cwd().parent.parent.parent / "out.csv")',
        wrote("`/home/out.csv`", "trips.to_csv"),
    ),
    # `/` is its own parent; a trailing `/` is no folder of its own
    ('trips.to_csv(Path("/").parent / "data" / "out.csv")', wrote(OUT, "trips.to_csv")),
    (
        'trips.to_csv(Path("data/").parent / "../../x.csv")',
        wrote("`/home/me/x.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(os.path.abspath("~/x.csv"))', wrote("`~/x.csv`", "trips.to_csv")),
    ('DEST = "/data"\n%cd $DEST\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    ('DEST = "/data"\n%cd {DEST}\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    # defining a function runs nothing: a chdir in its body moves only the rest of that body
    (
        'def enter_sub():\n    os.chdir("sub/deeper")\n\ntrips.to_csv("../../x.csv")',
        wrote("`/home/me/x.csv`", "trips.to_csv"),
    ),
    (
        'go = lambda: os.chdir("sub/deeper")\ntrips.to_csv("../../x.csv")',
        wrote("`/home/me/x.csv`", "trips.to_csv"),
    ),
    ('def go():\n    os.chdir("/data")\n    trips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
    # `with contextlib.chdir(…):` moves the folder for its body only
    (
        'from contextlib import chdir\nwith chdir("/data"):\n    trips.to_csv("out.csv")',
        wrote(OUT, "trips.to_csv"),
    ),
    (
        'with contextlib.chdir("../.."):\n    trips.to_csv("x.csv")',
        wrote("`/home/me/x.csv`", "trips.to_csv"),
    ),
    (
        'with contextlib.chdir(path="/data") as here, open("out.csv", "w") as f:\n    f.write("1")',
        wrote(OUT, "open"),
    ),
    (
        'with contextlib.chdir("sub/deeper"):\n    pass\ntrips.to_csv("../../x.csv")',
        wrote("`/home/me/x.csv`", "trips.to_csv"),
    ),
    (
        'HERE = Path.cwd()\n%cd /x\ntrips.to_csv(HERE / "../../out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    # Python run elsewhere in the cell
    ('%timeit open("/data/out.csv", "w")', wrote(OUT, "open")),
    ('%%time\ntrips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('with open("/data/out.csv", "w") as f:\n    f.write("x")', wrote(OUT, "open")),
    ('if ok:\n    trips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('def save():\n    trips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    (
        'class Saver:\n    def save(self):\n        trips.to_csv("/data/out.csv")',
        wrote(OUT, "trips.to_csv"),
    ),
    ('save = lambda: trips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    ('for t in tables:\n    t.to_csv("/data/out.csv")', wrote(OUT, "t.to_csv")),
    ('trips.dropna().to_csv("/data/out.csv")', wrote(OUT, "trips.dropna().to_csv")),
    ('pickle.dump(obj, open("/data/out.csv", "wb"))', wrote(OUT, "open")),
    (
        'get_ipython().run_cell_magic("time", "", "x = 1\\ntrips.to_csv(\'/data/out.csv\')")',
        wrote(OUT, "get_ipython().run_cell_magic"),
    ),
    # a function the cell defines: a call writes what its body writes, with its arguments
    (
        'def export(frame, path):\n    frame.to_csv(path, index=False)\nexport(trips, "~/trips.csv")',
        wrote("`~/trips.csv`", "export"),
    ),
    (
        'def export(frame, path):\n    frame.to_csv(path)\nexport(trips, path="/data/out.csv")',
        wrote(OUT, "export"),
    ),
    ('save = lambda frame, p: frame.to_csv(p)\nsave(trips, "/data/out.csv")', wrote(OUT, "save")),
    (
        'def export(frame, name):\n    frame.to_csv(os.path.join("/data", name))\n'
        'export(trips, "out.csv")',
        wrote(OUT, "export"),
    ),
    (  # a join restarts at an absolute argument
        'def export(frame, name):\n    frame.to_csv(os.path.join("../data", name))\n'
        'export(trips, "/data/out.csv")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, folder):\n    frame.to_csv(Path(folder) / "out.csv")\n'
        'export(trips, "/data")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, folder):\n    frame.to_csv(f"{folder}/out.csv")\nexport(trips, "/data")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, folder):\n    folder = Path(folder)\n    path = folder / "out.csv"\n'
        '    frame.to_csv(path)\nexport(trips, "/data")',
        wrote(OUT, "export"),
    ),
    (
        "def export(frame, path):\n    frame.to_csv(path)\ndef publish(frame, path):\n"
        '    export(frame, path)\npublish(trips, "/data/out.csv")',
        wrote(OUT, "publish"),
    ),
    (
        "def publish(frame, path):\n    def export():\n        frame.to_csv(path)\n    export()\n"
        'publish(trips, "/data/out.csv")',
        wrote(OUT, "publish"),
    ),
    (  # the default, where it is defined and at a call that leaves it out
        'def export(frame, path="/data/out.csv"):\n    frame.to_csv(path)\nexport(trips)',
        wrote(OUT, "frame.to_csv", 1),
    ),
    (
        'def export(frame, path="../data/x.csv"):\n    frame.to_csv(path)\n'
        'export(trips, "/data/out.csv")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, *, path):\n    frame.to_csv(path)\nexport(trips, path="/data/out.csv")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, path, /):\n    frame.to_csv(path)\nexport(trips, "/data/out.csv")',
        wrote(OUT, "export"),
    ),
    ('def save(path, mode="w"):\n    open(path, mode)\nsave("/data/out.csv")', wrote(OUT, "save")),
    ('def copy(path):\n    !cp a.csv {path}\ncopy("/data/out.csv")', wrote(OUT, "copy")),
    ('def drop(path):\n    shutil.rmtree(path)\ndrop("/data/old")', removed("`/data/old`", "drop")),
    (  # the body's own move, then the call's folder
        'def export(frame, name):\n    os.chdir("/data")\n    frame.to_csv(name)\n'
        'export(trips, "out.csv")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, name):\n    frame.to_csv(name)\n%cd /data\nexport(trips, "out.csv")',
        wrote(OUT, "export"),
    ),
    (
        'def export(frame, folder, name="x.csv"):\n    frame.to_csv(f"{folder}/{name}")\n'
        'export(trips, "/data", *names)',
        wrote("`/data/…`", "export"),
    ),
    (  # one path a body writes twice is one site; the first sink names it
        'def export(frame, path="/data/out.csv"):\n    frame.to_csv(path)\n'
        "    frame.to_parquet(path)",
        wrote(OUT, "frame.to_csv"),
    ),
    ('def bench(path):\n    %timeit open(path, "w")\nbench("/data/out.csv")', wrote(OUT, "bench")),
    (  # a parameter is the body's own: the cell's name of the same name holds after it
        'OUT = "/data/out.csv"\ndef export(OUT):\n    pass\ntrips.to_csv(OUT)',
        wrote(OUT, "trips.to_csv"),
    ),
    (
        'def export(frame, folder, name="x.csv"):\n    frame.to_csv(f"{folder}/{name}")\n'
        'export(trips, "/data", **options)',
        wrote("`/data/…`", "export"),
    ),
    # a scan that would raise on one line still reads the rest
    ('f = open("/data/a.csv", mode)\ntrips.to_csv("/data/out.csv")', wrote(OUT, "trips.to_csv")),
    (
        '!curl https://h.org/x.csv --output\ntrips.to_csv("/data/out.csv")',
        wrote(OUT, "trips.to_csv"),
    ),
    # shell lines in every form L012 reads
    ("!touch /data/out.csv", wrote(OUT, "!touch")),
    ("!!touch /data/out.csv", wrote(OUT, "!touch")),
    ("files = !touch /data/out.csv && ls", wrote(OUT, "!touch")),
    ("%sx touch /data/out.csv", wrote(OUT, "%sx touch")),
    ("%system touch /data/out.csv", wrote(OUT, "%system touch")),
    ("%%bash\ntouch /data/out.csv", wrote(OUT, "touch")),
    ("%%sh\necho x > /data/out.csv", wrote(OUT, "echo >")),
    ("%%script bash\ntouch /data/out.csv", wrote(OUT, "touch")),
    ('os.system("touch /data/out.csv")', wrote(OUT, "os.system")),
    ('os.popen("cp a.csv /data/out.csv")', wrote(OUT, "os.popen")),
    ('subprocess.run(["touch", "/data/out.csv"])', wrote(OUT, "subprocess.run")),
    ('subprocess.run("touch /data/out.csv", shell=True)', wrote(OUT, "subprocess.run")),
    ('get_ipython().system("touch /data/out.csv")', wrote(OUT, "get_ipython().system")),
    ("!bash -c 'touch /data/out.csv'", wrote(OUT, "!touch")),
    ("!echo $(touch /data/out.csv)", wrote(OUT, "!touch")),
    ("!sudo rm -rf /data/old", removed("`/data/old`", "!rm")),
    ("!env -i HOME=/x touch /data/out.csv", wrote(OUT, "!touch")),
    ("!A=1 touch /data/out.csv", wrote(OUT, "!touch")),
    ("!/usr/bin/touch /data/out.csv", wrote(OUT, "!touch")),
    ("!TOUCH /data/out.csv", wrote(OUT, "!touch")),
    ('!touch "/data/my file.csv"', wrote("`/data/my file.csv`", "!touch")),
    ("!touch '~/x.csv'", wrote("`~/x.csv`", "!touch")),
    # the folder a shell line is in: a `cd` holds for the rest of its line, or of a shell cell
    ("!cd /data && touch out.csv", wrote(OUT, "!touch")),
    ("!cd /data; touch out.csv", wrote(OUT, "!touch")),
    ("!cd ~ && touch out.csv", wrote("`~/out.csv`", "!touch")),
    ("!cd && touch out.csv", wrote("`~/out.csv`", "!touch")),
    ("!cd .. && cd .. && touch out.csv", wrote("`/home/me/out.csv`", "!touch")),
    ("!pushd /data && touch out.csv", wrote(OUT, "!touch")),
    ("%%bash\ncd /data\ntouch out.csv", wrote(OUT, "touch")),
    ("%%bash\ncd $HOME\ntouch out.csv", wrote("`~/out.csv`", "touch")),
    ("%%sh\necho x > ../../out.csv", wrote("`/home/me/out.csv`", "echo >")),
    ("%cd /data\n!touch out.csv", wrote(OUT, "!touch")),
    ('os.chdir("/data")\nos.system("touch out.csv")', wrote(OUT, "os.system")),
    ("!cd /data && python -c \"open('out.csv', 'w')\"", wrote(OUT, "!python -c")),
    # a subshell's glued `(` and `)` come off; its `cd` holds until its `)`
    ("!(cd /data && touch out.csv)", wrote(OUT, "!touch")),
    ("!(cd ~/Desktop && touch out.csv)", wrote("`~/Desktop/out.csv`", "!touch")),
    ("!(cp a.csv /data/out.csv)", wrote(OUT, "!cp")),
    ("!(mkdir -p /data/out.csv)", wrote(OUT, "!mkdir")),
    ("%%bash\n(cd /data && echo a > out.csv)", wrote(OUT, "echo >")),
    ("!(cd /data && make) && cd /data && touch out.csv", wrote(OUT, "!touch")),
    ('!(cd /data && touch "a(b") && touch x.csv', wrote("`/data/a(b`", "!touch")),
    (
        'os.chdir("/data")\nget_ipython().run_cell_magic("bash", "", "touch out.csv")',
        wrote(OUT, "get_ipython().run_cell_magic"),
    ),
    # IPython fills a `!` line's {name} and $name from Python names holding a path
    ('DEST = "/data"\n!cp a.csv {DEST}/out.csv', wrote(OUT, "!cp")),
    ('DEST = "/data"\n!cp a.csv $DEST/out.csv', wrote(OUT, "!cp")),
    ("!touch $HOME/out.csv", wrote("`~/out.csv`", "!touch")),
    ("!touch ${HOME}/out.csv", wrote("`~/out.csv`", "!touch")),
    ("!touch $HOME/$NAME.csv", wrote("`~/…`", "!touch")),
    ("%%bash\ntouch $HOME/out.csv", wrote("`~/out.csv`", "touch")),
    ("HERE = os.getcwd()\n!touch {HERE}/../../out.csv", wrote("`/home/me/out.csv`", "!touch")),
    # %%writefile and its call form
    ("%%writefile /data/out.py\nx = 1", wrote("`/data/out.py`", "%%writefile")),
    ("%%writefile -a ~/notes.py\nx = 1", wrote("`~/notes.py`", "%%writefile")),
    ("%%file /data/out.py\nx = 1", wrote("`/data/out.py`", "%%file")),
    ("%%writefile ../../out.py\nx = 1", wrote("`/home/me/out.py`", "%%writefile")),
    ('%%writefile "/data/my file.py"\nx = 1', wrote("`/data/my file.py`", "%%writefile")),
    ("%%writefile $HOME/notes.py\nx = 1", wrote("`~/notes.py`", "%%writefile")),
    (
        'get_ipython().run_cell_magic("writefile", "/data/out.py", "x = 1")',
        wrote("`/data/out.py`", "get_ipython().run_cell_magic"),
    ),
    (
        'get_ipython().run_cell_magic("file", "-a /data/out.py", "x = 1")',
        wrote("`/data/out.py`", "get_ipython().run_cell_magic"),
    ),
    (
        'get_ipython().run_cell_magic("bash", "", "touch /data/out.csv")',
        wrote(OUT, "get_ipython().run_cell_magic"),
    ),
]


@pytest.mark.parametrize(("code", "message"), L013_FIRES)
def test_l013_fires(code: str, message: str) -> None:
    assert outside_message(code) == message


# Each sink, value form and shell command writes.py lists, in a form only its entry makes fire.
L013_EACH_ENTRY = [
    # frame and series writers, each, then each path keyword
    *[
        (f'trips.{method}("/data/out.csv")', wrote(OUT, f"trips.{method}"))
        for method in [
            "to_csv",
            "to_parquet",
            "to_excel",
            "to_pickle",
            "to_json",
            "to_feather",
            "to_hdf",
            "to_stata",
            "to_html",
            "to_latex",
            "to_markdown",
            "to_xml",
            "to_orc",
            "to_netcdf",
            "to_zarr",
            "to_file",
        ]
    ],
    ('writer = pd.ExcelWriter("/data/out.csv")', wrote(OUT, "pd.ExcelWriter")),
    (
        'from pandas import ExcelWriter\nwriter = ExcelWriter("/data/out.csv")',
        wrote(OUT, "ExcelWriter"),
    ),
    *[
        (f'trips.to_csv({keyword}="/data/out.csv")', wrote(OUT, "trips.to_csv"))
        for keyword in [
            "path",
            "path_or_buf",
            "path_or_buffer",
            "buf",
            "excel_writer",
            "fname",
            "filename",
            "store",
            "file",
        ]
    ],
    # polars' writers and sinks, plotly's write_image; each keyword
    ('pl_trips.write_parquet("/data/out.csv")', wrote(OUT, "pl_trips.write_parquet")),
    ('fig.write_image("/data/out.csv")', wrote(OUT, "fig.write_image")),
    ('lazy.sink_csv("/data/out.csv")', wrote(OUT, "lazy.sink_csv")),
    ('from helpers import write_text\nwrite_text("/data/out.csv", "x")', wrote(OUT, "write_text")),
    *[
        (f'pl_trips.write_csv({keyword}="/data/out.csv")', wrote(OUT, "pl_trips.write_csv"))
        for keyword in ["file", "path", "target", "workbook", "fname", "filename"]
    ],
    # saves, each, then each keyword; a function imported by name
    *[
        (f'thing.{method}("/data/out.csv")', wrote(OUT, f"thing.{method}"))
        for method in [
            "savefig",
            "save",
            "save_model",
            "save_weights",
            "save_pretrained",
            "save_to_disk",
            "to_disk",
            "tofile",
        ]
    ],
    *[
        (f'fig.savefig({keyword}="/data/out.csv")', wrote(OUT, "fig.savefig"))
        for keyword in [
            "fname",
            "fp",
            "filepath",
            "filename",
            "file",
            "path",
            "save_directory",
            "dataset_path",
        ]
    ],
    ('from matplotlib.pyplot import savefig\nsavefig("/data/out.csv")', wrote(OUT, "savefig")),
    # a Spark frame's writer: each method, each setting before it, the keyword
    *[
        (f'sdf.write.{method}("/data/out.csv")', wrote(OUT, f"sdf.write.{method}"))
        for method in ["csv", "parquet", "json", "orc", "text"]
    ],
    *[
        (f'sdf.write.{step}(x).csv("/data/out.csv")', wrote(OUT, f"sdf.write.{step}().csv"))
        for step in ["mode", "option", "options", "format", "partitionBy", "bucketBy", "sortBy"]
    ],
    ('sdf.write.csv(path="/data/out.csv")', wrote(OUT, "sdf.write.csv")),
    # functions by name: each, and each keyword
    ('np.savez("/data/out.csv", a=a)', wrote(OUT, "np.savez")),
    ('np.savez(file="/data/out.csv", a=a)', wrote(OUT, "np.savez")),
    ('np.savez_compressed("/data/out.csv", a=a)', wrote(OUT, "np.savez_compressed")),
    ('np.savez_compressed(file="/data/out.csv", a=a)', wrote(OUT, "np.savez_compressed")),
    ('np.savetxt("/data/out.csv", a)', wrote(OUT, "np.savetxt")),
    ('np.savetxt(fname="/data/out.csv", X=a)', wrote(OUT, "np.savetxt")),
    ('np.save("/data/out.csv", a)', wrote(OUT, "np.save")),
    # images, sound, matrices and log files: the first argument, or its keyword
    ('import matplotlib.pyplot as mpl\nmpl.imsave("/data/out.csv", a)', wrote(OUT, "mpl.imsave")),
    ('plt.imsave(fname="/data/out.csv", arr=a)', wrote(OUT, "plt.imsave")),
    ('from matplotlib import image\nimage.imsave("/data/out.csv", a)', wrote(OUT, "image.imsave")),
    (
        'from matplotlib import image\nimage.imsave(fname="/data/out.csv", arr=a)',
        wrote(OUT, "image.imsave"),
    ),
    ('cv2.imwrite("/data/out.csv", img)', wrote(OUT, "cv2.imwrite")),
    ('cv2.imwrite(filename="/data/out.csv", img=img)', wrote(OUT, "cv2.imwrite")),
    ('imageio.imwrite("/data/out.csv", img)', wrote(OUT, "imageio.imwrite")),
    ('import imageio.v2 as iio\niio.imwrite("/data/out.csv", img)', wrote(OUT, "iio.imwrite")),
    (
        'import imageio.v3 as iio\niio.imwrite(uri="/data/out.csv", image=img)',
        wrote(OUT, "iio.imwrite"),
    ),
    ('from skimage import io\nio.imsave("/data/out.csv", img)', wrote(OUT, "io.imsave")),
    ('from skimage import io\nio.imsave(fname="/data/out.csv", arr=img)', wrote(OUT, "io.imsave")),
    ('import scipy.io\nscipy.io.savemat("/data/out.csv", d)', wrote(OUT, "scipy.io.savemat")),
    (
        'import scipy.io\nscipy.io.savemat(file_name="/data/out.csv", mdict=d)',
        wrote(OUT, "scipy.io.savemat"),
    ),
    (
        'from scipy.io import wavfile\nwavfile.write("/data/out.csv", 44100, data)',
        wrote(OUT, "wavfile.write"),
    ),
    (
        'from scipy.io import wavfile\nwavfile.write(filename="/data/out.csv", rate=1, data=d)',
        wrote(OUT, "wavfile.write"),
    ),
    ('import soundfile as sf\nsf.write("/data/out.csv", data, 44100)', wrote(OUT, "sf.write")),
    (
        'import soundfile as sf\nsf.write(file="/data/out.csv", data=d, samplerate=1)',
        wrote(OUT, "sf.write"),
    ),
    *[
        (
            f'import logging.handlers\nh = logging.{handler}("/data/out.csv")',
            wrote(OUT, f"logging.{handler}"),
        )
        for handler in [
            "FileHandler",
            "handlers.RotatingFileHandler",
            "handlers.TimedRotatingFileHandler",
            "handlers.WatchedFileHandler",
        ]
    ],
    ('h = logging.FileHandler(filename="/data/out.csv")', wrote(OUT, "logging.FileHandler")),
    ('logging.basicConfig(filename="/data/out.csv")', wrote(OUT, "logging.basicConfig")),
    ('joblib.dump(model, "/data/out.csv")', wrote(OUT, "joblib.dump")),
    ('joblib.dump(model, filename="/data/out.csv")', wrote(OUT, "joblib.dump")),
    ('pickle.dump(obj, "/data/out.csv")', wrote(OUT, "pickle.dump")),
    ('pickle.dump(obj, file="/data/out.csv")', wrote(OUT, "pickle.dump")),
    ('torch.save(state, "/data/out.csv")', wrote(OUT, "torch.save")),
    ('torch.save(state, f="/data/out.csv")', wrote(OUT, "torch.save")),
    ('pd.to_pickle(trips, "/data/out.csv")', wrote(OUT, "pd.to_pickle")),
    (
        'import pandas\npandas.to_pickle(trips, filepath_or_buffer="/data/out.csv")',
        wrote(OUT, "pandas.to_pickle"),
    ),
    ('tf.saved_model.save(model, "/data/out.csv")', wrote(OUT, "tf.saved_model.save")),
    (
        'import tensorflow\ntensorflow.saved_model.save(model, export_dir="/data/out.csv")',
        wrote(OUT, "tensorflow.saved_model.save"),
    ),
    ('torch.onnx.export(model, x, "/data/out.csv")', wrote(OUT, "torch.onnx.export")),
    ('torch.onnx.export(model, x, f="/data/out.csv")', wrote(OUT, "torch.onnx.export")),
    (
        'import pyarrow.parquet as pq\npq.write_table(table, "/data/out.csv")',
        wrote(OUT, "pq.write_table"),
    ),
    (
        'import pyarrow.parquet as pq\npq.write_table(table, where="/data/out.csv")',
        wrote(OUT, "pq.write_table"),
    ),
    (
        'from pyarrow import feather\nfeather.write_feather(table, "/data/out.csv")',
        wrote(OUT, "feather.write_feather"),
    ),
    (
        'from pyarrow import feather\nfeather.write_feather(table, dest="/data/out.csv")',
        wrote(OUT, "feather.write_feather"),
    ),
    ('from pyarrow import csv\ncsv.write_csv(table, "/data/out.csv")', wrote(OUT, "csv.write_csv")),
    (
        'from pyarrow import csv\ncsv.write_csv(table, output_file="/data/out.csv")',
        wrote(OUT, "csv.write_csv"),
    ),
    ('urllib.request.urlretrieve(URL, "/data/out.csv")', wrote(OUT, "urllib.request.urlretrieve")),
    (
        'from urllib.request import urlretrieve\nurlretrieve(URL, filename="/data/out.csv")',
        wrote(OUT, "urlretrieve"),
    ),
    *[
        (f'{function}("a.csv", "/data/out.csv")', wrote(OUT, function))
        for function in [
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "os.symlink",
            "os.link",
        ]
    ],
    *[
        (f'{function}("a.csv", dst="/data/out.csv")', wrote(OUT, function))
        for function in [
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "os.symlink",
            "os.link",
        ]
    ],
    *[
        (f'{function}("/data/a.csv", "/data/out.csv")', moved(OUT, "`/data/a.csv`", function))
        for function in ["shutil.move", "os.rename", "os.replace", "os.renames"]
    ],
    *[
        (
            f'{function}(src="/data/a.csv", dst="/data/out.csv")',
            moved(OUT, "`/data/a.csv`", function),
        )
        for function in ["shutil.move", "os.rename", "os.replace"]
    ],
    (
        'os.renames(old="/data/a.csv", new="/data/out.csv")',
        moved(OUT, "`/data/a.csv`", "os.renames"),
    ),
    ('os.rename("/data/a.csv", "b.csv")', removed("`/data/a.csv`", "os.rename")),
    ('os.mkdir("/data/out.csv")', wrote(OUT, "os.mkdir")),
    ('os.mkdir(path="/data/out.csv")', wrote(OUT, "os.mkdir")),
    ('os.makedirs("/data/out.csv", exist_ok=True)', wrote(OUT, "os.makedirs")),
    ('os.makedirs(name="/data/out.csv")', wrote(OUT, "os.makedirs")),
    ('shutil.make_archive("/data/out.csv", "zip", ".")', wrote(OUT, "shutil.make_archive")),
    (
        'shutil.make_archive(base_name="/data/out.csv", format="zip")',
        wrote(OUT, "shutil.make_archive"),
    ),
    *[
        (f'{function}("/data/old")', removed("`/data/old`", function))
        for function in ["os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree"]
    ],
    *[
        (f'{function}(path="/data/old")', removed("`/data/old`", function))
        for function in ["os.remove", "os.unlink", "os.rmdir", "shutil.rmtree"]
    ],
    ('os.removedirs(name="/data/old")', removed("`/data/old`", "os.removedirs")),
    ('import shutil as sh\nsh.rmtree("/data/old")', removed("`/data/old`", "sh.rmtree")),
    # opens with a mode: each opener, each keyword, each mode letter
    *[
        (f'f = {opener}("/data/out.csv", "w")', wrote(OUT, opener))
        for opener in [
            "open",
            "io.open",
            "builtins.open",
            "codecs.open",
            "gzip.open",
            "bz2.open",
            "lzma.open",
            "tarfile.open",
            "zipfile.ZipFile",
        ]
    ],
    ('f = open(file="/data/out.csv", mode="w")', wrote(OUT, "open")),
    ('f = gzip.open(filename="/data/out.csv", mode="wt")', wrote(OUT, "gzip.open")),
    ('f = tarfile.open(name="/data/out.csv", mode="w:gz")', wrote(OUT, "tarfile.open")),
    ('f = tarfile.open("/data/out.csv", "x:xz")', wrote(OUT, "tarfile.open")),
    ('f = tarfile.open("/data/out.csv", "a|")', wrote(OUT, "tarfile.open")),
    *[
        (f'f = open("/data/out.csv", "{mode}")', wrote(OUT, "open"))
        for mode in ["w", "a", "x", "r+", "wb", "ab", "xt", "rb+"]
    ],
    ('f = open("/data/out.csv", mode="a")', wrote(OUT, "open")),
    ('MODE = "w"\nf = open("/data/out.csv", MODE)', wrote(OUT, "open")),
    ('f = open("/data/out.csv", f"{kind}w")', wrote(OUT, "open")),
    # a path's own methods
    *[
        (f'Path("/data/out.csv").{method}(x)', wrote(OUT, f"Path().{method}"))
        for method in ["write_text", "write_bytes", "symlink_to", "hardlink_to"]
    ],
    *[
        (f'Path("/data/out.csv").{method}()', wrote(OUT, f"Path().{method}"))
        for method in ["touch", "mkdir"]
    ],
    *[
        (f'Path("/data/old").{method}()', removed("`/data/old`", f"Path().{method}"))
        for method in ["unlink", "rmdir"]
    ],
    *[
        (
            f'Path("/data/a.csv").{method}("/data/out.csv")',
            moved(OUT, "`/data/a.csv`", f"Path().{method}"),
        )
        for method in ["rename", "replace"]
    ],
    ('Path("a.csv").rename(target="/data/out.csv")', wrote(OUT, "Path().rename")),
    ('Path("/data/out.csv").open("w")', wrote(OUT, "Path().open")),
    ('Path("/data/out.csv").open(mode="a")', wrote(OUT, "Path().open")),
    ('p = Path("/data/out.csv")\nwith p.open("x") as f:\n    f.write("1")', wrote(OUT, "p.open")),
    # each value form
    *[
        (f'trips.to_csv(pathlib.{kind}("/data", "out.csv"))', wrote(OUT, "trips.to_csv"))
        for kind in ["Path", "PurePath", "PosixPath", "PurePosixPath"]
    ],
    ('%cd /data\ntrips.to_csv(Path() / "out.csv")', wrote(OUT, "trips.to_csv")),
    *[
        (f'trips.to_csv({join}("/data", "out.csv"))', wrote(OUT, "trips.to_csv"))
        for join in ["os.path.join", "posixpath.join"]
    ],
    *[
        (f'trips.to_csv({same}("/data/out.csv"))', wrote(OUT, "trips.to_csv"))
        for same in [
            "os.path.expanduser",
            "os.path.normpath",
            "posixpath.expanduser",
            "posixpath.normpath",
            "os.fspath",
        ]
    ],
    ('trips.to_csv(str(Path("/data/out.csv")))', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv(Path("/data/out.csv").expanduser())', wrote(OUT, "trips.to_csv")),
    *[
        (f'trips.to_csv({absolute}("../../out.csv"))', wrote("`/home/me/out.csv`", "trips.to_csv"))
        for absolute in [
            "os.path.abspath",
            "os.path.realpath",
            "posixpath.abspath",
            "posixpath.realpath",
        ]
    ],
    *[
        (
            f'trips.to_csv(Path("x.csv").{method}().parent.parent.parent / "out.csv")',
            wrote("`/home/me/out.csv`", "trips.to_csv"),
        )
        for method in ["resolve", "absolute"]
    ],
    *[
        (
            f'trips.to_csv({dirname}("/data/sub/x.csv") + "/out.csv")',
            wrote("`/data/sub/out.csv`", "trips.to_csv"),
        )
        for dirname in ["os.path.dirname", "posixpath.dirname"]
    ],
    (
        'trips.to_csv(os.path.join(os.path.dirname(os.getcwd()), "..", "out.csv"))',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    ('trips.to_csv(Path("/data/exports").parent / "out.csv")', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv(Path("/data/x.csv").with_name("out.csv"))', wrote(OUT, "trips.to_csv")),
    ('trips.to_csv(Path("/data/out.tsv").with_suffix(".csv"))', wrote(OUT, "trips.to_csv")),
    *[
        (f'trips.to_csv({home}() / "out.csv")', wrote("`~/out.csv`", "trips.to_csv"))
        for home in ["pathlib.Path.home", "pathlib.PosixPath.home"]
    ],
    *[
        (
            f'trips.to_csv({cwd}().parent.parent / "out.csv")',
            wrote("`/home/me/out.csv`", "trips.to_csv"),
        )
        for cwd in ["pathlib.Path.cwd", "pathlib.PosixPath.cwd"]
    ],
    (
        'trips.to_csv(Path(os.getcwd()).parent.parent / "out.csv")',
        wrote("`/home/me/out.csv`", "trips.to_csv"),
    ),
    *[
        (f'trips.to_csv({read}("HOME") + "/out.csv")', wrote("`~/out.csv`", "trips.to_csv"))
        for read in ["os.getenv", "os.environ.get"]
    ],
    ('trips.to_csv(os.environ["HOME"] + "/out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    ('trips.to_csv(os.path.expanduser("~") + "/out.csv")', wrote("`~/out.csv`", "trips.to_csv")),
    # shell commands: each, and each option nh reads
    ("!echo x | tee /data/out.csv", wrote(OUT, "!tee")),
    (
        "!echo x | tee -a /data/a.csv /data/out.csv",
        wrote("`/data/a.csv` and `/data/out.csv`", "!tee", 1),
    ),
    ("!mkdir -p /data/out.csv", wrote(OUT, "!mkdir")),
    ("!touch /data/out.csv", wrote(OUT, "!touch")),
    ("!rm -rf /data/old", removed("`/data/old`", "!rm")),
    ("!rmdir /data/old", removed("`/data/old`", "!rmdir")),
    ("!unlink /data/old", removed("`/data/old`", "!unlink")),
    ("!cp a.csv /data/out.csv", wrote(OUT, "!cp")),
    ("!cp -r a b /data/out.csv", wrote(OUT, "!cp")),
    ("!cp -t /data/out.csv a.csv b.csv", wrote(OUT, "!cp")),
    ("!cp --target-directory=/data/out.csv a.csv", wrote(OUT, "!cp")),
    ("!cp --target-directory /data/out.csv a.csv", wrote(OUT, "!cp")),
    ("!cp -- a.csv /data/out.csv", wrote(OUT, "!cp")),
    ("!cp -- -S /data/out.csv", wrote(OUT, "!cp")),  # after `--`, `-S` is a file, not an option
    ("!mv /data/a.csv /data/out.csv", moved(OUT, "`/data/a.csv`", "!mv")),
    ("!mv -S .bak a.csv /data/out.csv", wrote(OUT, "!mv")),
    ("!mv -bS /x/.bak a.csv /data/out.csv", wrote(OUT, "!mv")),
    ("!mv --suffix /x/.bak a.csv /data/out.csv", wrote(OUT, "!mv")),
    ("!mv --suffix=/x/.bak a.csv /data/out.csv", wrote(OUT, "!mv")),
    ("!mv /data/a.csv b.csv", removed("`/data/a.csv`", "!mv")),
    ("!ln -s a.csv /data/out.csv", wrote(OUT, "!ln")),
    ("!rsync -av a.csv /data/out.csv", wrote(OUT, "!rsync")),
    ("!curl -o /data/out.csv https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!curl -sLo /data/out.csv https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!curl -o/data/out.csv https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!curl --output /data/out.csv https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!curl --output=/data/out.csv https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!curl --output-dir /data/out.csv -O https://h.org/x.csv", wrote(OUT, "!curl")),
    ("!wget -O /data/out.csv https://h.org/x.csv", wrote(OUT, "!wget")),
    ("!wget -qO/data/out.csv https://h.org/x.csv", wrote(OUT, "!wget")),
    ("!wget --output-document=/data/out.csv https://h.org/x.csv", wrote(OUT, "!wget")),
    ("!wget -P /data/out.csv https://h.org/x.csv", wrote(OUT, "!wget")),
    ("!wget --directory-prefix /data/out.csv https://h.org/x.csv", wrote(OUT, "!wget")),
    ("!dd if=/dev/zero of=/data/out.csv bs=1k count=1", wrote(OUT, "!dd")),
    ("!zip -r /data/out.csv data", wrote(OUT, "!zip")),
    ("!zip ../data/a.zip more.csv -O /data/out.csv", wrote(OUT, "!zip")),
    ("!zip --output-file=/data/out.csv data.zip more.csv", wrote(OUT, "!zip")),
    ("!jupyter nbconvert --to html a.ipynb --output-dir /data/out.csv", wrote(OUT, "!jupyter")),
    ("!jupyter-nbconvert --output-dir=/data/out.csv a.ipynb", wrote(OUT, "!jupyter-nbconvert")),
    *[
        (f"!ls {op} /data/out.csv", wrote(OUT, "!ls >"))
        for op in [">", ">>", ">|", "&>", "&>>", "2>", "2>>", "1>", ">&"]
    ],
    ("!ls >/data/out.csv", wrote(OUT, "!ls >")),
    ("!> /data/out.csv", wrote(OUT, "!>")),
    ("!ls 2>&1 > /data/out.csv", wrote(OUT, "!ls >")),
    ("!python -c \"open('/data/out.csv', 'w')\"", wrote(OUT, "!python -c")),
    (
        "!python3 -uc \"import shutil; shutil.rmtree('/data/old')\"",
        removed("`/data/old`", "!python3 -c"),
    ),
    ("!cd /data && touch out.csv", wrote(OUT, "!touch")),
    ("!pushd /data && touch out.csv", wrote(OUT, "!touch")),
]


@pytest.mark.parametrize(("code", "message"), L013_EACH_ENTRY)
def test_l013_each_entry(code: str, message: str | None) -> None:
    assert outside_message(code) == message


L013_QUIET = [
    # inside the project: from notebooks/, from the root, absolute, climbing back in
    'trips.to_csv("trips.csv")',
    'trips.to_csv("../data/processed/trips.csv")',
    'trips.to_csv("../reports/../data/x.csv")',
    'trips.to_csv("/home/me/proj/data/x.csv")',
    'trips.to_csv("/home/me/proj/notebooks/../data/x.csv")',
    'trips.to_csv("/home/me/proj")',
    'trips.to_csv("/home/me/proj/")',
    'trips.to_csv("../../proj/data/x.csv")',
    'trips.to_csv(Path("..") / "data" / "x.csv")',
    'trips.to_csv(Path.cwd().parent / "data" / "x.csv")',
    'trips.to_csv(os.path.abspath("../data/x.csv"))',
    'trips.to_csv(Path("..").parent / "x.csv")',
    "!cp a.csv ../data/raw/",
    "!cd .. && touch data/x.csv",
    "!cd /data\n!touch x.csv",
    "!(cd /data && make) && touch x.csv",
    "!((cd /data) && cd /x) && touch x.csv",
    "!(cd /data; touch ../../../home/me/proj/x.csv)",
    '!echo "$(date)" > ../data/x.txt',
    'get_ipython().run_cell_magic("bash", "", "cd /data")\n'
    'get_ipython().run_cell_magic("bash", "", "touch x.csv")',
    "%%writefile ../src/helpers.py\nx = 1",
    # exempt: device files and the system temp folders
    'trips.to_csv("/tmp/trips.csv")',
    'trips.to_csv("/tmp")',
    'trips.to_csv("/var/tmp/trips.csv")',
    'trips.to_csv("/private/tmp/trips.csv")',
    'trips.to_csv("/private/var/tmp/trips.csv")',
    'trips.to_csv("/dev/null")',
    'trips.to_csv("/dev/stdout")',
    'open("/dev/stderr", "w").write("x")',
    "!run > /dev/null 2>&1",
    "!run &> /dev/null",
    "!run 2>/dev/null | tee /dev/stderr",
    'trips.to_csv(f"/{name}/x.csv")',  # may be /tmp/x.csv
    "!curl -o /dev/null https://h.org/x",
    'trips.to_csv(f"/tmp/{name}.csv")',
    # reads, and writes nh doesn't list
    'trips = pd.read_csv("/data/trips.csv")',
    'f = open("/data/trips.csv")',
    'f = open("/data/trips.csv", "r")',
    'f = open("/data/trips.csv", "rb")',
    'f = open("/data/trips.csv", mode="rt")',
    'f = open("/data/trips.csv", mode)',
    'with tarfile.open("/data/a.tar.xz", "r:xz") as t:\n    names = t.getnames()',
    't = tarfile.open("/data/a.tar.xz", mode="r|xz")',
    'f = open("/data/trips.csv", encoding="utf-8")',
    'text = Path("/data/trips.csv").read_text()',
    'f = Path("/data/trips.csv").open()',
    'f = Path("/data/trips.csv").open("r")',
    'a = np.load("/data/a.npy")',
    'shutil.copy("/data/trips.csv", "trips.csv")',
    "!cp /data/trips.csv .",
    "!cat /data/trips.csv",
    "!ls /data > files.txt",
    "!wc -l < /data/trips.csv",
    "!curl -o - https://h.org/x.csv",
    "!wget -O - https://h.org/x.csv",
    "!run >&2",
    "!run 2>&1",
    "!run >&-",
    "!ln -s /data/big.csv big.csv",
    "%cd /data\n!rsync -av data/ backup.example.org:/backup/",
    "%cd /data\n!rsync -av data/ rsync://backup.example.org/backup/",
    "%cd /data\n!rsync -av data/ analyst@backup.example.org:/backup/",
    # not a local file
    'trips.to_sql("trips", "sqlite:////data/trips.db")',
    'pl_trips.write_database("trips", "sqlite:////data/trips.db")',
    'pl_trips.write_database("/data/trips", "sqlite:///trips.db")',  # a table's name
    'pl_trips.write_clipboard("/data/out.csv")',
    '%cd /data\ntrips.to_csv("s3://bucket/trips.csv")',
    'trips.to_csv("sqlite:////data/trips.db")',
    '%cd /data\ntrips.to_csv("https://h.org/upload.csv")',
    'sdf.write.saveAsTable("/data/trips")',  # a table's name
    'sdf.read.csv("/data/trips.csv")',
    'frame.csv("/data/out.csv")',
    'logging.basicConfig("/data/out.csv")',  # takes keywords only
    'pd = load_module()\npd.to_pickle(trips, "/data/out.csv")',
    'import mylib as pd\npd.to_pickle(trips, "/data/out.csv")',
    "!jupyter nbconvert --to html a.ipynb --output ../reports/a",
    "!jupyter kernelspec install --output-dir /data/out.csv k",
    *[f"!zip -{letter} /data/tmp data.zip data" for letter in "bnPtZ"],
    "!zip --temp-path /data/tmp data.zip data",
    # not a path
    'trips = trips.rename(columns={"a": "/b"})',
    'name = name.replace("/a", "/b")',
    's = s.rename("/abs/name")',
    "text = trips.to_csv()",
    "trips.to_csv(buffer)",
    "fig.savefig(io.BytesIO())",
    "trips.to_csv(12)",
    'df = df.write_text("/data/x.csv")',
    # a path nh can't read: unknown start, a folder above the project before an unknown part,
    # an attribute, a container, a loop, one of two, an unknown folder
    "trips.to_csv(out_path)",
    'trips.to_csv(f"{out_dir}/x.csv")',
    'trips.to_csv(os.environ["OUT"])',
    'trips.to_csv(Path(os.environ["OUT"]).parent / "x.csv")',
    'trips.to_csv(f"/home/me/{name}/x.csv")',
    'trips.to_csv(f"/home/{user}/proj/x.csv")',
    'trips.to_csv(f"/home/me/proj{suffix}/x.csv")',
    'trips.to_csv(f"../../{name}/x.csv")',
    'trips.to_csv(Path(name).parent / "../../x.csv")',
    'trips.to_csv(os.path.dirname(name) + "/../../x.csv")',
    'trips.to_csv(f"x{name}.csv")',
    "trips.to_csv(cfg.out_dir)",
    'trips.to_csv(paths["out"])',
    'for p in ["/a.csv", "/b.csv"]:\n    trips.to_csv(p)',
    'OUT = "/data/a" if fast else "/data/b"\ntrips.to_csv(OUT)',
    "!cp a.csv $DEST/",
    "%%writefile {OUT}\nx = 1",
    "trips.to_csv(Path(*parts))",
    '%cd /data\n%popd\ntrips.to_csv("x.csv")',
    '%cd -2\ntrips.to_csv("x.csv")',
    '%cd -b data\ntrips.to_csv("../../../x.csv")',
    '%cd -\ntrips.to_csv("../../../x.csv")',
    'os.chdir(somewhere)\ntrips.to_csv("x.csv")',
    '%cd /data\n%cd $DIR\ntrips.to_csv("x.csv")',
    "!cd /data && cd $DIR && touch x.csv",
    "!cd /data && cd - && touch x.csv",
    "!cd /data && popd && touch x.csv",
    # a function's parameter: its writes wait for a call, and a call gives its own path
    'def export(frame, name):\n    frame.to_csv(os.path.join("/data", name))',
    'def export(frame, path):\n    frame.to_csv(path)\nexport(trips, "../data/x.csv")',
    "def export(frame, path):\n    frame.to_csv(path)\nexport(trips, out_path)",
    'def export(frame, path):\n    frame.to_csv(path)\nsaver.export(trips, "/data/out.csv")',
    'def export(frame, path):\n    frame.to_csv(path)\nexport = print\nexport(trips, "/data/out.csv")',
    'class Saver:\n    def export(self, path):\n        trips.to_csv(path)\nexport(1, "/data/out.csv")',
    'def save(path, mode="r"):\n    open(path, mode)\nsave("/data/out.csv")',
    # a function's names are its own
    'def setup():\n    OUT = "/data/out.csv"\ntrips.to_csv(OUT)',
    'paths = "/data/out.csv"\ndef export(*paths):\n    trips.to_csv(paths)',
    'options = "/data/out.csv"\ndef export(**options):\n    trips.to_csv(options)',
    "def outer():\n    def export(frame, path):\n        frame.to_csv(path)\n"
    'export(trips, "/data/out.csv")',
    "def export(frame, path):\n    frame.to_csv(path)\nprint(path)\ntrips.to_csv(path)",
    "!touch {Path.cwd()}/../../x.csv",
    # IPython fills `{HOME}` only from a Python name (else the shell gets `{HOME}` as written),
    # and `${DEST}` keeps its `$`: `$/data/x.csv` is a relative path
    "!touch {HOME}/x.csv",
    'DEST = "/data"\n!cp a.csv ${DEST}/x.csv',
    # comments, strings and markdown don't write
    '# trips.to_csv("/data/out.csv")',
    "help_text = \"trips.to_csv('/data/out.csv')\"",
    "%%markdown\nRun `!touch /data/out.csv`.",
]


@pytest.mark.parametrize("code", L013_QUIET)
def test_l013_quiet(code: str) -> None:
    assert outside_issues(code) == []


@pytest.mark.parametrize(
    ("notebook_dir", "path", "shown"),
    [
        ("", "x.csv", None),
        ("", "data/x.csv", None),
        ("", "../x.csv", "/home/me/x.csv"),
        ("", "../proj/x.csv", None),
        ("notebooks", "../x.csv", None),
        ("notebooks", "../../x.csv", "/home/me/x.csv"),
        ("notebooks/eda", "../../x.csv", None),
        ("notebooks/eda", "../../../x.csv", "/home/me/x.csv"),
        ("notebooks/eda", "/home/me/proj/x.csv", None),
        ("notebooks/eda", "~/x.csv", "~/x.csv"),
    ],
)
def test_l013_relative_paths_start_from_the_notebooks_folder(
    notebook_dir: str, path: str, shown: str | None
) -> None:
    """The kernel runs in the notebook's folder (first-cell.md "Paths"), so `../` is the project
    root from notebooks/ and outside it from a notebook at the root."""
    expected = None if shown is None else wrote(f"`{shown}`", "trips.to_csv")
    assert outside_message(f'trips.to_csv("{path}")', notebook_dir) == expected


@pytest.mark.parametrize(
    ("path", "root", "notebook_dir", "shown"),
    [
        ("/data/x.csv", "/home/me/proj", "", "/data/x.csv"),
        ("/data/x.csv", "/home/me/proj/", "notebooks", "/data/x.csv"),
        ("/home/me/proj/x.csv", "/home/me/proj/", "", None),
        ("../../x.csv", "/home/me/proj", "notebooks", "/home/me/x.csv"),
        ("~/x.csv", "/home/me/proj", "", "~/x.csv"),
        ("~", "/home/me/proj", "", "~"),
        # a project inside /tmp (tests, evals): every /tmp path is exempt
        ("/tmp/pytest-1/other/x.csv", "/tmp/pytest-1/proj", "", None),
        ("../../x.csv", "/tmp/pytest-1/proj", "notebooks", None),
        ("/data/x.csv", "/tmp/pytest-1/proj", "notebooks", "/data/x.csv"),
        # unknown parts, each text within one folder name: outside when no path they can stand
        # for is in the project or exempt, named by the folder before the first one
        ("/data/\0.csv", "/home/me/proj", "", "/data/…"),
        ("/data/a/\0/\0.csv", "/home/me/proj", "", "/data/a/…"),
        ("\0/x.csv", "/home/me/proj", "", None),
        ("x\0.csv", "/home/me/proj", "", None),
        ("/\0.csv", "/home/me/proj", "", "/…"),
        ("/\0/x.csv", "/home/me/proj", "", None),
        ("/home/\0/x.csv", "/home/me/proj", "", "/home/…"),
        ("/home/\0/proj/x.csv", "/home/me/proj", "", None),
        ("/home/me/\0.csv", "/home/me/proj", "", "/home/me/…"),
        ("/home/me/pro\0/x.csv", "/home/me/proj", "", None),
        ("/home/me/\0x/y.csv", "/home/me/proj", "", "/home/me/…"),
        ("/home/me/\0", "/home/me/proj", "", None),
        ("/home/\0", "/home/me/proj", "", "/home/…"),
        ("../../\0/x.csv", "/home/me/proj", "notebooks", None),
        ("../../\0.csv", "/home/me/proj", "notebooks", "/home/me/…"),
        ("../../../\0/x.csv", "/home/me/proj", "notebooks", "/home/…"),
        ("/data/\0/../x.csv", "/home/me/proj", "", "/data/x.csv"),
        ("/var/\0/x.csv", "/home/me/proj", "", None),
        ("/private/\0/\0/x.csv", "/home/me/proj", "", None),
        # a leading `//` reads `/`
        ("//data/x.csv", "/home/me/proj", "", "/data/x.csv"),
        ("//home/me/proj/x.csv", "/home/me/proj", "", None),
        ("//tmp/x.csv", "/home/me/proj", "", None),
        ("../../other/\0.csv", "/home/me/proj", "notebooks", "/home/me/other/…"),
        ("~/\0.csv", "/home/me/proj", "", "~/…"),
        ("~\0/x.csv", "/home/me/proj", "", "~…"),
        ("/tmp/\0.csv", "/home/me/proj", "", None),
        ("/home/me/proj/\0.csv", "/home/me/proj", "", None),
        ("", "/home/me/proj", "", None),
    ],
)
def test_l013_outside_resolves_text_only(
    path: str, root: str, notebook_dir: str, shown: str | None
) -> None:
    from nh_gateway.lint import writes

    assert writes.outside(path, root, notebook_dir) == shown


def test_l013_exempt_places_are_the_decided_ones() -> None:
    """Design §6.4 "Exempt": device files and the system temp folders, nothing else."""
    from nh_gateway.lint import writes

    assert writes.EXEMPT == ("/dev", "/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")
    for place in writes.EXEMPT:
        assert writes.outside(f"{place}/x/y.csv", ROOT) is None, place
        assert writes.outside(f"{place}x/y.csv", ROOT) == f"{place}x/y.csv", place


def test_l013_canonical_text() -> None:
    [issue] = lint(
        'trips.to_csv("~/exports/trips.csv")', project_root=ROOT, notebook_dir="notebooks"
    ).asks
    assert (issue.rule, issue.key, issue.severity) == ("L013", "outside_write", "ask")
    assert issue.message == (
        "The cell writes to `~/exports/trips.csv`, outside the project (`trips.to_csv`)."
    )
    assert issue.question == "writes to `~/exports/trips.csv`, outside the project"
    assert issue.fix == L013_FIX


@pytest.mark.parametrize(
    ("code", "message", "question"),
    [
        (
            'a.to_csv("/data/a.csv")\nb.to_csv("/data/b.csv")',
            wrote("`/data/a.csv` and `/data/b.csv`", "a.to_csv", 1),
            "writes to `/data/a.csv` and `/data/b.csv`, outside the project",
        ),
        (
            'a.to_csv("/data/a.csv")\nb.to_csv("/data/b.csv")\n!touch ~/c.csv',
            wrote("`/data/a.csv`, `/data/b.csv` and `~/c.csv`", "a.to_csv", 2),
            "writes to `/data/a.csv`, `/data/b.csv` and `~/c.csv`, outside the project",
        ),
        (
            "\n".join(f'f{n}.to_csv("/data/{n}.csv")' for n in range(5)),
            wrote("`/data/0.csv`, `/data/1.csv`, `/data/2.csv` and 2 more", "f0.to_csv", 4),
            "writes to `/data/0.csv`, `/data/1.csv`, `/data/2.csv` and 2 more, outside the project",
        ),
        (  # one path written twice is named once; each site counts
            'a.to_csv("/data/a.csv")\na.to_csv("/data/a.csv")',
            wrote("`/data/a.csv`", "a.to_csv", 1),
            "writes to `/data/a.csv`, outside the project",
        ),
        (  # inside writes are left out, and the first outside site is the one named
            'a.to_csv("../data/a.csv")\n!rm -r /data/old\nb.to_csv("/data/b.csv")',
            "The cell writes to `/data/b.csv` and removes `/data/old`, outside the project "
            "(`!rm` (+1 more)).",
            "writes to `/data/b.csv` and removes `/data/old`, outside the project",
        ),
        (
            'shutil.rmtree("/data/a")\nos.remove("/data/b.csv")',
            removed("`/data/a` and `/data/b.csv`", "shutil.rmtree", 1),
            "removes `/data/a` and `/data/b.csv`, outside the project",
        ),
        (
            'trips.to_csv(f"/data/exports/{name}.csv")',
            wrote("`/data/exports/…`", "trips.to_csv"),
            "writes to `/data/exports/…`, outside the project",
        ),
        (  # a command's own paths before its redirections'
            "!cp a.csv /data/b.csv > /data/log",
            wrote("`/data/b.csv` and `/data/log`", "!cp", 1),
            "writes to `/data/b.csv` and `/data/log`, outside the project",
        ),
        (
            "!mv /data/a.csv b.csv 2> /data/err",
            "The cell writes to `/data/err` and removes `/data/a.csv`, outside the project "
            "(`!mv` (+1 more)).",
            "writes to `/data/err` and removes `/data/a.csv`, outside the project",
        ),
        (  # a path stays on one line, readable inside E122's single quotes
            'trips.to_csv("~/Bob\'s data/x.csv")',
            wrote('`"~/Bob\'s data/x.csv"`', "trips.to_csv"),
            'writes to `"~/Bob\'s data/x.csv"`, outside the project',
        ),
        (
            "trips.to_csv('/data/it\\'s \"x\".csv')",
            wrote('`"/data/it\'s \\"x\\".csv"`', "trips.to_csv"),
            'writes to `"/data/it\'s \\"x\\".csv"`, outside the project',
        ),
        (
            'trips.to_csv("/data/a`b.csv")',
            wrote("`` /data/a`b.csv ``", "trips.to_csv"),
            "writes to `` /data/a`b.csv ``, outside the project",
        ),
        (
            'trips.to_csv("/data/a\\nb\\t.csv")',
            wrote("`/data/a\\nb\\t.csv`", "trips.to_csv"),
            "writes to `/data/a\\nb\\t.csv`, outside the project",
        ),
    ],
)
def test_l013_text_lists_paths_once_each_by_kind(code: str, message: str, question: str) -> None:
    [issue] = outside_issues(code)
    assert (issue.message, issue.question) == (message, question)


def test_l013_needs_a_project_root() -> None:
    """With no root L013 finds nothing: the gateway always passes one."""
    code = 'trips.to_csv("/data/out.csv")'
    assert outside_issues(code, project_root=None) == []
    assert outside_issues(code, project_root="") == []
    assert "L013" not in rules(lint(code), "ask")
    assert outside_issues(code) != []


L013_ABOVE = [
    'EXPORTS = Path("/data/exports")',
    'def save(frame, path="/data/out.csv"):\n    frame.to_csv(path)',
    "import shutil as sh",
    'DEST = "/data"',
    "HERE = Path.cwd()",
    'import pandas as pd\nFINAL = "/data/final.csv"\n',
    "def export(frame, path):\n    frame.to_csv(path)\n\n\ndef publish(frame, path):\n"
    "    export(frame, path)",
]


@pytest.mark.parametrize(
    ("code", "above"),
    [
        ('trips.to_csv(EXPORTS / "trips.csv")', wrote("`/data/exports/trips.csv`", "trips.to_csv")),
        ('sh.rmtree("/data/old")', removed("`/data/old`", "sh.rmtree")),
        ("!cp a.csv {DEST}/out.csv", wrote(OUT, "!cp")),
        ('trips.to_csv(f"{DEST}/out.csv")', wrote(OUT, "trips.to_csv")),
        (
            'trips.to_csv(HERE.parent.parent / "out.csv")',
            wrote("`/home/me/out.csv`", "trips.to_csv"),
        ),
        ("trips.to_csv(FINAL)", wrote("`/data/final.csv`", "trips.to_csv")),
        ('%cd $DEST\ntrips.to_csv("out.csv")', wrote(OUT, "trips.to_csv")),
        ("%%writefile {DEST}/out.csv\nx = 1", wrote(OUT, "%%writefile")),
        ('export(trips, "~/trips.csv")', wrote("`~/trips.csv`", "export")),
        ('publish(trips, "~/trips.csv")', wrote("`~/trips.csv`", "publish")),
        ("save(trips)", wrote(OUT, "save")),
        ('save(trips, "~/trips.csv")', wrote("`~/trips.csv`", "save")),
    ],
)
def test_l013_reads_names_from_the_cells_above(code: str, above: str) -> None:
    """A path, import or folder bound in an earlier cell counts here; alone nh knows nothing."""
    assert outside_issues(code) == []
    assert [i.message for i in outside_issues(code, code_above=L013_ABOVE)] == [above]


def test_l013_a_shell_cell_reads_no_python_name() -> None:
    """IPython fills {name} and $name in `!` lines only: a shell cell's $DEST is the shell's."""
    assert outside_issues("%%bash\ncp a.csv $DEST/out.csv", code_above=L013_ABOVE) == []
    assert outside_issues("!cp a.csv $DEST/out.csv", code_above=L013_ABOVE) != []


def test_l013_cells_above_bind_but_never_ask() -> None:
    from nh_gateway.lint import writes

    above = ['trips.to_csv("/data/out.csv")', "%cd /data", 'os.chdir("/data")']
    assert outside_issues("trips.head()", code_above=above) == []
    # an earlier cell's %cd isn't followed: relative paths start from the notebook's folder
    assert outside_issues('trips.to_csv("x.csv")', code_above=above) == []
    reads = "trips.to_csv(OUT)"
    assert outside_issues(reads, code_above=['OUT = "/data/out.csv"']) != []
    rebound = ['OUT = "/data/out.csv"', 'OUT = "../data/out.csv"']
    assert outside_issues(reads, code_above=rebound) == []
    here = f'OUT = "../data/out.csv"\n{reads}'
    assert outside_issues(here, code_above=['OUT = "/data/out.csv"']) == []
    broken = ['OUT = "/data/out.csv"', "def broken(:", "%%bash\nOUT=x", "%%sql\nSELECT 1"]
    assert outside_issues(reads, code_above=broken) != []
    assert writes.bindings(['OUT = "/data/out.csv"', "def broken(:"]) == writes.bindings(
        ['OUT = "/data/out.csv"']
    )


def test_l013_a_walk_of_the_cells_above_that_raises_leaves_the_seed_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nh_gateway.lint import network, writes

    def boom(source: str, seed: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(writes, "_cell_seed", boom)
    above = ['OUT = "/data/out.csv"']
    assert writes.bindings(above) == network.EMPTY
    assert outside_issues("trips.to_csv(OUT)", code_above=above) == []
    assert outside_issues('trips.to_csv("/data/out.csv")', code_above=above) != []


def test_l013_walks_each_cell_above_once_in_a_cache_of_its_own() -> None:
    from nh_gateway.lint import network, writes

    above = [f'OUT_{n} = "/data/{n}.csv"' for n in range(200)]
    writes._cell_seed.cache_clear()
    network._cell_seed.cache_clear()
    started = time.perf_counter()
    for _ in range(5):
        [issue] = outside_issues("trips.to_csv(OUT_199)", code_above=above)
    assert time.perf_counter() - started < 3.0
    assert issue.message == wrote("`/data/199.csv`", "trips.to_csv")
    info = writes._cell_seed.cache_info()
    assert info.misses == 200 and info.hits == 4 * 200
    # L012 walks the same cells into a cache of its own
    assert network._cell_seed.cache_info().misses == 200
    writes._cell_seed.cache_clear()
    network._cell_seed.cache_clear()
    assert writes.bindings(above) != network.bindings(above)
    assert writes._cell_seed.cache_info().misses == network._cell_seed.cache_info().misses == 200
    writes.bindings(above)
    assert network._cell_seed.cache_info().hits == 0


@pytest.mark.parametrize(
    ("level", "mode", "where"),
    [
        ("ask", "advise", "ask"),
        ("ask", "strict", "ask"),
        ("error", "advise", "error"),
        ("hint", "advise", "hint"),
        ("hint", "strict", "error"),
        ("off", "advise", None),
        ("off", "strict", None),
    ],
)
def test_l013_levels(level: str, mode: str, where: str | None) -> None:
    cfg = config(outside_write=level)
    cfg.data["lint"]["mode"] = mode
    report = lint(
        'trips.to_csv("~/exports/trips.csv")\ntrips.head()',
        cfg=cfg,
        project_root=ROOT,
        notebook_dir="notebooks",
    )
    found = {s: rules(report, s) for s in ("error", "hint", "ask")}
    assert found == {s: (["L013"] if s == where else []) for s in found}
    assert report.ok == (where != "error")


def test_l013_is_off_without_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """At `off` the scan never runs; a scan that fails finds nothing, and only L013 is lost."""
    from nh_gateway.lint import writes

    calls: list[str] = []
    monkeypatch.setattr(writes, "scan", lambda *args: calls.append("scan") or [])
    lint('trips.to_csv("/data/out.csv")', cfg=config(outside_write="off"), project_root=ROOT)
    assert calls == []
    lint('trips.to_csv("/data/out.csv")', project_root=ROOT)
    assert calls == ["scan"]

    def boom(*args: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(writes, "scan", boom)
    report = lint(
        f'!pip install x\ntrips = pd.read_csv("{TRIPS_URL}")\ntrips.to_csv("/data/out.csv")',
        project_root=ROOT,
    )
    assert rules(report, "ask") == ["L009", "L012"]


def test_l013_asks_with_l009_and_l012_in_one_question() -> None:
    """All three asks, in _CHECKS order: the gate joins their clauses into one question."""
    report = lint(
        f'!pip install seaborn\ntrips = pd.read_csv("{TRIPS_URL}")\n'
        'trips.to_csv("~/exports/trips.csv")',
        project_root=ROOT,
        notebook_dir="notebooks",
    )
    assert rules(report, "ask") == ["L009", "L012", "L013"] and report.ok
    assert [issue.question for issue in report.asks][1:] == [
        "connects to `data.example.org` over the network",
        "writes to `~/exports/trips.csv`, outside the project",
    ]
    order = [rule for rule, _, _ in lint_module._CHECKS]
    assert order.index("L012") + 1 == order.index("L013")


def test_l013_and_l012_read_one_shell_line_each_their_way() -> None:
    """A download into a folder outside the project is both: a site and a write."""
    report = lint(f"!curl -o ~/trips.csv {TRIPS_URL}", project_root=ROOT, notebook_dir="notebooks")
    assert rules(report, "ask") == ["L012", "L013"]
    assert report.asks[1].message == wrote("`~/trips.csv`", "!curl")


def test_l013_scan_is_fast_on_a_long_cell() -> None:
    lines = [f'f{n} = trips.to_csv(f"../data/{{n}}/{n}.csv")' for n in range(400)]
    lines += [f"!cp a.csv ../data/{n}.csv && touch ../data/{n}.done" for n in range(100)]
    lines.append('trips.to_csv("/data/out.csv")')
    started = time.perf_counter()
    [issue] = outside_issues("\n".join(lines))
    assert time.perf_counter() - started < 3.0
    assert issue.message == wrote(OUT, "trips.to_csv")


def test_l013_first_cell_writes_nothing_outside() -> None:
    """/nh:init's first cell (first-cell.md) reads from data/raw/: no question."""
    for block in _first_cell_blocks():
        assert outside_issues(block, kernel_python=(3, 11)) == []


@pytest.mark.parametrize(
    ("line", "stages"),
    [
        ("echo x > a.txt", [(["echo", "x"], ["a.txt"])]),
        ("cmd >> a; cmd2 2> b", [(["cmd"], ["a"]), (["cmd2"], ["b"])]),
        ("cmd &> a", [(["cmd"], ["a"])]),
        ("cmd &>> a", [(["cmd"], ["a"])]),
        ("cmd >| a", [(["cmd"], ["a"])]),
        ("cmd >&a", [(["cmd"], ["a"])]),
        ("cmd 1>a 3>b", [(["cmd"], ["a", "b"])]),
        ("cmd 2>&1", [(["cmd"], [])]),
        ("cmd >&2", [(["cmd"], [])]),
        ("cmd >&-", [(["cmd"], [])]),
        ("cmd < in.txt", [(["cmd"], [])]),
        ("> a", [([], ["a"])]),
        ('cmd > "my file.txt"', [(["cmd"], ["my file.txt"])]),
        ("cmd 2>>err.log | tee out.log", [(["cmd"], ["err.log"]), (["tee", "out.log"], [])]),
        ("bash -c 'echo x > a'", [(["bash", "-c", "echo x > a"], []), (["echo", "x"], ["a"])]),
        ("echo $(cat a > b)", [(["echo", "$(cat a > b)"], []), (["cat", "a"], ["b"])]),
        ("sudo cmd > a", [(["cmd"], ["a"])]),
        # quotes that don't close: read as raw text
        ('echo "x > a.txt', [(["echo", "x", "a.txt"], ["a.txt"])]),
        ('echo "x 2>>a.txt 2>&1', [(["echo", "x"], ["a.txt"])]),
    ],
)
def test_shell_stages_name_each_commands_redirection_targets(
    line: str, stages: list[tuple[list[str], list[str]]]
) -> None:
    found = secret_scan.shell_stages(line)
    assert [(words, outputs) for _, words, outputs in found] == stages
    # shell_commands is the same list without the outputs, and without a bare redirection
    assert secret_scan.shell_commands(line) == [(raws, words) for raws, words, _ in found if words]


# L010 -------------------------------------------------------------------------------------------
LONG = "the price column has many nulls in the north region this quarter"


@pytest.mark.parametrize(
    "code",
    [
        "%%markdown\n# Findings",
        "%%html\n<b>x</b>",
        f"%%latex\n{LONG}",
        f"from IPython.display import Markdown\nMarkdown('## {LONG}')",
        f"display(HTML('<h2>{LONG}</h2>'))",
        f"import IPython\nIPython.display.Markdown(data='{LONG}')",
        f"n = 3\nMarkdown(f'**{{n}}** {LONG}')",
        f"Markdown('{{}} {LONG}'.format(n))",
        f'md = """## Findings\n{LONG}"""\ndisplay(Markdown(md))',
        f"Markdown('{LONG}')\nbroken = (",  # regex fallback when the cell doesn't parse
    ],
)
def test_l010_prose_from_code(code: str) -> None:
    report = lint(code)
    assert "L010" in rules(report)
    assert next(i for i in report.errors if i.rule == "L010").key == "markdown_output"


@pytest.mark.parametrize(
    "code",
    [
        "Markdown('**Total rows:** 10,432')",
        "HTML(df_summary.to_html())",
        "%%latex\n$x^2 + y^2$",
        f"print('{LONG}')",
    ],
)
def test_l010_short_or_data_output_is_fine(code: str) -> None:
    assert "L010" not in rules(lint(code))


# L011 -------------------------------------------------------------------------------------------
# Design §6.7. Each list pins the rows of one table there: taking a row out of secret_scan.py
# fails at least one case below.
def secret_hits(code: str, **overrides: Any) -> list[Issue]:
    report = lint(code, **overrides)
    return [i for i in report.errors + report.hints if i.rule == "L011"]


def secret_error(code: str, **overrides: Any) -> Issue:
    report = lint(code, **overrides)
    [issue] = [i for i in report.errors if i.rule == "L011"]
    assert issue.key == "secret_print" and issue.severity == "error"
    assert "L014" not in rules(report, "hint")  # one finding per mistake
    return issue


# Design §6.7's Sources table and import names, each shown by `print` or as the last line.
L011_SOURCES = [
    'import os\nprint(os.environ["OPENAI_API_KEY"])',
    'import os\nprint(os.environ.get("OPENAI_API_KEY"))',
    'import os\nprint(os.environ.get("OPENAI_API_KEY", ""))',
    'import os\nprint(os.environ.pop("OPENAI_API_KEY"))',
    'import os\nprint(os.environ.setdefault("OPENAI_API_KEY", ""))',
    'import os\nprint(os.environ.__getitem__("OPENAI_API_KEY"))',
    'import os\nprint(os.getenv("OPENAI_API_KEY"))',
    'import os\nprint(os.getenv(key="OPENAI_API_KEY"))',
    'import os\nprint(os.getenvb(b"OPENAI_API_KEY"))',
    "import os\nprint(os.environ)",
    "import os\nprint(os.environb)",
    "import os\nprint(dict(os.environ))",
    "import os\nprint(os.environ.copy())",
    "import os\nprint({**os.environ})",
    'import os\nprint(os.environ | {"A": "1"})',
    "import os\nprint(os.environ.values())",
    "import os\nprint(os.environ.items())",
    # the live object: its views' and bound methods' reprs hold environ({…})
    "import os\nprint(os.environ.keys())",
    "import os\nos.environ.keys()",
    "import os\ndisplay(os.environ.keys())",
    'import os\nprint(f"vars: {os.environ.keys()}")',
    "import os\nnames = os.environ.keys()\nnames",
    "import os\nenv = os.environ\nprint(env.keys())",
    "import os\nfrom dotenv import dotenv_values\nos.environ.update(dotenv_values())\n"
    "print(os.environ.keys())",
    "from os import environ\nenviron.keys()",
    "import os\nprint(os.environ.get)",
    "import os\nos.environ.keys",
    "from dotenv import dotenv_values\nprint(dotenv_values())",
    'import dotenv\nprint(dotenv.dotenv_values(".env"))',
    'from dotenv.main import dotenv_values\nprint(dotenv_values(".env"))',
    'import dotenv.main\nprint(dotenv.main.dotenv_values(".env"))',
    'from dotenv import dotenv_values\nprint(dotenv_values(".env")["OPENAI_API_KEY"])',
    'from dotenv import dotenv_values\nprint(dotenv_values(dotenv_path=".env.local"))',
    'from dotenv import get_key\nprint(get_key(".env", "OPENAI_API_KEY"))',
    'import dotenv\nprint(dotenv.get_key(".env", "OPENAI_API_KEY"))',
    'from dotenv.main import get_key\nprint(get_key(".env", "OPENAI_API_KEY"))',
    'import os\nprint(os.path.expandvars("key=$OPENAI_API_KEY"))',
    'import os\nprint(os.path.expandvars("key=${OPENAI_API_KEY}"))',
    'import posixpath\nprint(posixpath.expandvars("$OPENAI_API_KEY"))',
    'from os import environ\nprint(environ["OPENAI_API_KEY"])',
    'from os import getenv\nprint(getenv("OPENAI_API_KEY"))',
    'from os import environ as env, getenv as get\nprint(env["A"], get("B"))',
    'import os as o\nprint(o.environ["OPENAI_API_KEY"])',
    'import os\nget = os.getenv\nprint(get("OPENAI_API_KEY"))',
    # imported by an earlier cell: the default names
    'print(os.environ["OPENAI_API_KEY"])',
    'print(environ["OPENAI_API_KEY"])',
    'print(getenv("OPENAI_API_KEY"))',
    "print(dotenv_values())",
    'print(dotenv.dotenv_values(".env"))',
    'print(subprocess.getoutput("env"))',
    'print(open(".env").read())',
    'print(Path(".env").read_text())',
    'print(pathlib.Path(".env").read_text())',
    # shell output read in Python
    'import subprocess\nprint(subprocess.check_output(["printenv", "OPENAI_API_KEY"]))',
    'import subprocess\nprint(subprocess.getoutput("env"))',
    'import subprocess\nprint(subprocess.getstatusoutput("printenv OPENAI_API_KEY"))',
    'import os\nprint(os.popen("printenv OPENAI_API_KEY").read())',
    'print(get_ipython().getoutput("printenv OPENAI_API_KEY"))',
    'import subprocess\nsubprocess.run(["printenv", "K"], capture_output=True, text=True).stdout',
    'import subprocess\nr = subprocess.run(["env"], capture_output=True, text=True)\nprint(r.stdout)',
    'import subprocess\nr = subprocess.run("env 1>&2", shell=True, capture_output=True)\nr.stderr',
    'import subprocess\nsubprocess.run(["printenv", "K"], capture_output=True)',
    'import subprocess\nout = subprocess.run(["env"], stdout=subprocess.PIPE)\nprint(out)',
    'import subprocess\np = subprocess.Popen(["env"], stdout=subprocess.PIPE)\n'
    "print(p.communicate())",
    # a .env file read in Python
    'print(open(".env").read())',
    'import io\nprint(io.open(".env").read())',
    'import codecs\nprint(codecs.open(".env").read())',
    'from pathlib import Path\nPath(".env").read_text()',
    'from pathlib import Path\nprint(Path("../.env").read_bytes())',
    'from pathlib import PurePath\nprint(open(PurePath(".env")).read())',
    'import pathlib\nprint(pathlib.PosixPath(".env").read_text())',
    'import pathlib\nprint(pathlib.WindowsPath(".env").read_text())',
    'import os\nprint(open(os.path.join("..", ".env")).read())',
    'from pathlib import Path\nROOT = Path("..")\nprint((ROOT / ".env").read_text())',
    'from pathlib import Path\nENV_FILE = Path("../.env")\nprint(ENV_FILE.open().read())',
    'with open(".env") as f:\n    print(f.read())',
    'with open(".env") as f:\n    print(f.readlines())',
    'with open(".env") as f:\n    print(f.readline())',
    'with open(".env") as f:\n    for line in f:\n        print(line)',
    'print(list(open(".env")))',
    'print(open(".envrc").read())',
    'ENV_FILE = "../.env"\nprint(open(ENV_FILE).read())',
    'from pathlib import Path\nENV_FILE = "../.env"\nPath(ENV_FILE).read_text()',
    # the text of a taint: __repr__, __str__, __format__
    "import os\nprint(os.environ.__repr__())",
    "import os\nprint(os.environ.__str__())",
    'import os\nprint(os.environ.__format__(""))',
    'import os\nkey = os.getenv("K")\nprint(key.__repr__())',
    "import os\nprint(os.environ.items().__str__())",
    # an attribute of a value: a frame's or a series' data, a captured command's output
    "import os\nimport pandas as pd\nenv = pd.Series(dict(os.environ))\n"
    'env.loc[env.index.str.contains("KEY")]',
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items(), columns=['name', 'value'])\n"
    "env.iloc[:5]",
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items(), columns=['name', 'value'])\n"
    "env.T",
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items(), columns=['name', 'value'])\n"
    "env.value.str[:4]",
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items(), columns=['name', 'value'])\n"
    "print(env.values)",
    'import os\nimport pandas as pd\ns = pd.Series({"k": os.environ["K"]})\ns.str[:4]',
    "out = !printenv OPENAI_API_KEY\nout.s[:4]",
    "lines = !cat ../.env\nprint(lines.n)",
    "lines = !cat ../.env\nprint(lines.l)",
    # a .env path through a path method or function
    'from pathlib import Path\nprint(Path("~/.env").expanduser().read_text())',
    'from pathlib import Path\nprint(Path(".env").resolve().read_text())',
    'from pathlib import Path\nprint(Path(".env").absolute().read_text())',
    'from pathlib import Path\nprint(open(Path(".env").as_posix()).read())',
    'import os\nprint(open(os.path.expanduser("~/.env")).read())',
    'import os\nprint(open(os.path.abspath(".env")).read())',
    'import os\nprint(open(os.path.realpath(".env")).read())',
    'import os\nprint(open(os.path.normpath("./.env")).read())',
    'from pathlib import Path\nprint(Path.home().joinpath(".env").read_text())',
    # what a function the cell defines returns; class attributes; match captures
    'import os\ndef get_key():\n    return os.environ["K"]\nprint(get_key())',
    'import os\ndef get_key():\n    return os.environ["K"]\nget_key()',
    'import os\ndef same(v):\n    return v\nprint(same(os.environ["K"]))',
    'import os\ndef same(v):\n    return v\nprint(same(v=os.environ["K"]))',
    'import os\ndef prefix():\n    return key[:4]\nkey = os.getenv("K")\nprint(prefix())',
    'import os\nclass Settings:\n    api = os.getenv("K")\nprint(Settings.api)',
    'import os\nkey = os.getenv("K")\nmatch key:\n    case str() as k:\n        print(k)',
    'import os\nmatch os.environ:\n    case {"K": v}:\n        print(v)',
    "import os\nmatch os.environ:\n    case {**rest}:\n        print(rest)",
    'import os\nmatch [os.getenv("K")]:\n    case [*rest]:\n        print(rest)',
    # get_ipython() bound to a name; a dict the cell builds
    'ip = get_ipython()\nprint(ip.getoutput("printenv K"))',
    'import os\nsettings = {"key": os.getenv("K")}\nprint(settings["key"])',
    'import os\nsettings = {}\nsettings.setdefault("key", os.getenv("K"))\nprint(settings)',
    "import os\nfrom collections import defaultdict\nd = defaultdict(str)\n"
    'd["k"] = os.getenv("K")\nprint(d)',
]


# The Sinks table.
L011_SINKS = [
    'import os\nos.environ["OPENAI_API_KEY"]',  # the last line
    'import os\nos.getenv("OPENAI_API_KEY")',
    "import os\nos.environ",
    "import os\ndict(os.environ)",
    'import os\nkey = os.getenv("K")\nrepr(key)',
    'import os\nkey = os.getenv("K")\nstr(key)',
    'import os\nkey = os.getenv("K")\nkey',
    'import os\nprint(os.environ["K"], file=sys.stderr)',
    'import os\nimport sys\nprint(os.environ["K"], file=sys.stdout)',
    'import os\nkey = os.getenv("K")\nprint("a", end=key)',
    'import os\nkey = os.getenv("K")\nprint("a", "b", sep=key)',
    'import os\ndisplay(os.environ["K"])',
    'import os\nfrom IPython.display import display\ndisplay(os.getenv("K"))',
    "import os\nfrom pprint import pprint\npprint(dict(os.environ))",
    "import os\nimport pprint\npprint.pprint(os.environ)",
    "import os\nfrom pprint import pp\npp(os.environ)",
    'import os\nfrom rich import print as rprint\nrprint(os.environ["K"])',
    'import os\nconsole.print(os.environ["K"])',
    "import os\nprint(f\"key={os.environ['K']}\")",
    'import os\nprint("key=%s" % os.getenv("K"))',
    'import os\nprint("key={}".format(os.getenv("K")))',
    'import os\nprint("key=" + os.getenv("K"))',
    'import os, sys\nsys.stdout.write(os.environ["K"])',
    'import os, sys\nsys.stderr.write(os.environ["K"])',
    'import os\nfrom tqdm import tqdm\ntqdm.write(os.environ["K"])',
    'import os\ntqdm.write(os.environ["K"])',
    'import os\nimport tqdm\ntqdm.tqdm.write(os.environ["K"])',
    'import logging, os\nlogging.warning("key %s", os.environ["K"])',
    'import logging, os\nlogger = logging.getLogger(__name__)\nlogger.info(os.environ["K"])',
    'import logging, os\nlogging.getLogger().error(os.getenv("K"))',
    'import logging, os\nlogging.log(logging.INFO, os.getenv("K"))',
    'import os\nlogging.debug(os.getenv("K"))',  # logging imported by an earlier cell
    'import logging, os\nlogging.warn(os.getenv("K"))',
    'import logging, os\nlogging.critical(os.getenv("K"))',
    'import logging, os\nlogging.exception(os.getenv("K"))',
    'import logging, os\nlogging.fatal(os.getenv("K"))',
    'import os\nlog.info(os.getenv("K"))',
    'import os\napp_log.info(os.getenv("K"))',
    'import os\n_logger.error(os.getenv("K"))',
    'import os\nself_logger.warning(os.getenv("K"))',
    'import os, warnings\nwarnings.warn(os.getenv("K"))',
    'import os\nwarnings.warn(os.getenv("K"))',  # warnings imported by an earlier cell
    'import os\nraise ValueError(os.getenv("K"))',
    "import os\nraise ValueError(f\"bad key {os.getenv('K')}\")",
    'import os\nkey = os.getenv("K")\nraise key',
    'import os\nkey = os.getenv("K")\nassert key.startswith("sk-"), key',
    'import os\nos.system("printenv OPENAI_API_KEY")',
    'import subprocess\nsubprocess.run(["printenv", "OPENAI_API_KEY"])',
    'import subprocess\nsubprocess.run("env", shell=True)',
    'import subprocess\nsubprocess.call("echo $OPENAI_API_KEY", shell=True)',
    'import subprocess\nsubprocess.check_call(["printenv"])',
    'import subprocess\nsubprocess.Popen(["env"])',
    'subprocess.run(["env"])',  # subprocess imported by an earlier cell
    'get_ipython().system("env")',
    'import os\ndef show():\n    print(os.environ["K"])',
    'import os\nclass Settings:\n    print(os.environ["K"])',
    'import os\nshow = lambda: print(os.environ["K"])',
    '%time print(os.environ["K"])',
    '%%time\nimport os\nprint(os.environ["K"])',
    '%%capture\nimport os\nprint(os.environ["K"])',
    '%%timeit\nimport os\nprint(os.environ["K"])',
    '%%prun\nimport os\nprint(os.environ["K"])',
    '%%python\nimport os\nprint(os.environ["K"])',
    '%%python3\nimport os\nprint(os.environ["K"])',
    '%%debug\nimport os\nprint(os.environ["K"])',
    # %timeit / %prun lines: the statement after the options runs, and prints
    'import os\n%timeit print(os.environ["OPENAI_API_KEY"])',
    'import os\n%timeit -n1 -r1 print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%timeit -n 1 print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%timeit -r 1 print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%timeit -p 3 print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%prun print(os.environ["OPENAI_API_KEY"])',
    'import os\n%prun -l 5 print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%prun -s cumulative print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%prun -T out.txt print(os.getenv("OPENAI_API_KEY"))',
    'import os\n%prun -D out.prof print(os.getenv("OPENAI_API_KEY"))',
    'import os\nfor _ in range(2):\n    %timeit -q print(os.getenv("OPENAI_API_KEY"))',
    '%timeit print(os.environ["K"])',  # on the first line: its statement is no magic line
    '%timeit x = print(os.environ["K"])',
    '%timeit pass\nprint(os.environ["K"])',
    # a %%timeit or %%prun line: the statement after its options runs too (timeit's setup)
    '%%timeit -n 1 print(os.environ["K"])\nx = 1',
    '%%prun -s cumulative print(os.environ["K"])\nx = 1',
    # loggers by any name, logging's own functions, keyword arguments
    'import logging, os\nrootLogger = logging.getLogger()\nrootLogger.warning(os.environ["K"])',
    'import logging, os\nlg = logging.getLogger(__name__)\nlg.info(os.environ["K"])',
    'import os\nlg = logging.getLogger()\nlg.info(os.environ["K"])',  # logging imported earlier
    'import logging as lg, os\nlg.warning(os.environ["K"])',
    'import os\nself.logger.info(os.environ["K"])',
    'import os\nLOGGER.info(os.environ["K"])',
    'import os\nfrom logging import warning\nwarning("key %s", os.environ["K"])',
    'import os\nfrom logging import warning as w\nw("key %s", os.environ["K"])',
    'import os\nfrom logging import log\nlog(20, os.environ["K"])',
    'import os\nfrom loguru import logger as L\nL.info(os.environ["K"])',
    'import os, structlog\nevents = structlog.get_logger()\nevents.info(os.environ["K"])',
    'import os, structlog\nevents = structlog.getLogger()\nevents.info(os.environ["K"])',
    "import os, logging\nlogging.warning(msg=f\"key {os.environ['K']}\")",
    "import os, warnings\nwarnings.warn(message=f\"key {os.environ['K']}\")",
    "import os\nfrom pprint import pprint\npprint(object=dict(os.environ))",
    'import os\nfrom rich.console import Console\nconsole = Console()\nconsole.log(os.environ["K"])',
    'import os, structlog\nstructlog.get_logger().info(os.environ["K"])',
    'import os\nprint(os.environ["K"]).x = 1',  # an assignment's target runs too
    # a sink anywhere in an expression runs: a raise's cause, a subscript's key, a format spec,
    # a lambda's default
    'import os\nraise RuntimeError("bad") from print(os.environ["K"])',
    'import os\nd = {}\nd[print(os.environ["K"])]',
    """import os\nprint(f"{0:{print(os.environ['K']) or 'd'}}")""",
    'import os\nf = lambda x=print(os.environ["K"]): x',
    'import os\nd = {}\nd[print(os.environ["K"])] = 1',
    'import os\nprint(os.environ["K"])[0] = 1',
    'import os\nd = {}\ndel d[print(os.environ["K"])]',
    'import os\ndef f(x: print(os.environ["K"])):\n    pass',
    'import os\ndef f(x=print(os.environ["K"])):\n    pass',
    'import os\ndef f() -> print(os.environ["K"]):\n    pass',
    'import os\nok = 0 < print(os.environ["K"])',
    'import os\nnames = [x for x in "ab" if print(os.environ["K"])]',
    'import os\nassert print(os.environ["K"]) is None',
    'import os\nprint({os.environ[k]: 1 for k in ["A"]})',
    'import os\nprint(dict(key=os.environ["K"]))',
    'import os\nos.system("# show it\\nprintenv OPENAI_API_KEY")',
    'import os\nself.console.log(os.environ["K"])',
    # shell calls by other names
    'ip = get_ipython()\nip.system("printenv OPENAI_API_KEY")',
    'from os import system\nsystem("printenv K")',
    'from os import popen\nprint(popen("printenv K").read())',
    'from subprocess import run\nrun(["printenv", "K"])',
    # a function the cell defines: a call shows what reaches the body's sinks
    'import os\ndef show(v):\n    print(v)\nshow(os.environ["K"])',
    'import os\ndef describe(name, value):\n    print(f"{name}: {value[:4]}...")\n'
    'describe("K", os.environ["K"])',
    'import os\ndef show(*, v):\n    print(v)\nshow(v=os.environ["K"])',
    'import os\ndef report():\n    print(f"key prefix: {key[:4]}")\nkey = os.getenv("K")\nreport()',
    'import os\ndef outer(v):\n    def inner():\n        print(v)\n    inner()\nouter(os.environ["K"])',
    'import os\ndef show(v):\n    print(v)\ndef relay(x):\n    show(x)\nrelay(os.environ["K"])',
    'import os\nasync def show(v):\n    print(v)\nawait show(os.environ["K"])',
    # a sink inside a test or a comparison, or after a tainted element
    'import os\nok = print(os.environ["K"]) is None',
    'import os\nok = 1 if print(os.environ["K"]) else 2',
    'import os\nkey = os.getenv("K")\npair = (key, print(os.environ["K"]))',
    'import os\nkey = os.getenv("K")\nx = f"{key}{print(os.environ[\'K\'])}"',
]


# A sink in every kind of branch and body the walk goes into.
L011_BRANCHES = [
    'import os\nkey = os.getenv("K")\nif key is None:\n    print("missing")\nelse:\n    print(key)',
    'import os\nkey = os.getenv("K")\nif key is None:\n    print("missing")\nelif key.startswith("sk-"):\n'
    "    print(key[:8])",
    'import os\ntry:\n    x = 1\nexcept KeyError:\n    print(os.environ["K"])',
    'import os\ntry:\n    x = 1\nexcept KeyError:\n    pass\nelse:\n    print(os.environ["K"])',
    'import os\ntry:\n    x = 1\nfinally:\n    print(os.environ["K"])',
    'import os\ntry:\n    x = 1\nexcept* KeyError:\n    print(os.environ["K"])',
    'import os\nfor i in range(2):\n    pass\nelse:\n    print(os.environ["K"])',
    'import os\nwhile x:\n    pass\nelse:\n    print(os.environ["K"])',
    'import os\nwhile print(os.environ["K"]):\n    pass',
    'import os\nmatch x:\n    case 1 if print(os.environ["K"]):\n        pass',
    'import os\nmatch print(os.environ["K"]):\n    case _:\n        pass',
    'import os\nmatch x:\n    case 1:\n        pass\n    case _:\n        print(os.environ["K"])',
    'import os\nwith open("x") as f:\n    print(os.environ["K"])',
    'import os\nasync def main():\n    async for x in y:\n        print(os.environ["K"])',
    'import os\nasync def main():\n    async with y as z:\n        print(os.environ["K"])',
    'import os\ndef f(x=print(os.environ["K"])):\n    pass',  # a default runs at the def
    'import os\n@register(print(os.environ["K"]))\ndef f():\n    pass',
    # `except … as key` binds the exception only when the handler runs; else key keeps its value
    'import os\nkey = os.getenv("K")\ntry:\n    x = 1\nexcept Exception as key:\n    pass\nprint(key)',
    'import os\nkey = os.getenv("K")\ntry:\n    x = 1\nexcept Exception as key:\n    pass\nkey',
]


# The Magics table and the shell rules.
L011_MAGICS = [
    "%env",
    "%env OPENAI_API_KEY",
    "!env",
    "!env -0",
    "!env -u OPENAI_API_KEY",
    "!env --unset OPENAI_API_KEY",
    "!env -C /tmp",
    "!env --chdir /tmp",
    "!env FOO=bar",
    "!printenv",
    "!printenv OPENAI_API_KEY",
    "!set",
    "!export",
    "!export -p",
    "!declare -p OPENAI_API_KEY",
    "!declare -x",
    "!typeset -p",
    "!echo $OPENAI_API_KEY",
    "!echo ${OPENAI_API_KEY}",
    '!echo "key: $OPENAI_API_KEY"',
    "!printf '%s' $OPENAI_API_KEY",
    "!print $OPENAI_API_KEY",
    "!!printenv OPENAI_API_KEY",
    "!sudo printenv",
    "!command printenv",
    "!builtin set",
    "!nohup printenv",
    "!time printenv",
    "!exec printenv",
    "!for i in 1; do echo $OPENAI_API_KEY; done",
    "!if true; then printenv; fi",
    "!true || if true; then echo ok; else printenv; fi",
    "!while true; do env; break; done",
    "!until false; do printenv; done",
    "!if printenv OPENAI_API_KEY; then true; fi",
    "!while printenv OPENAI_API_KEY; do break; done",
    "!until printenv OPENAI_API_KEY; do break; done",
    "!true && ! printenv OPENAI_API_KEY",
    "!! printenv",
    "!{ printenv; }",
    "!(printenv)",
    "!( printenv )",
    "!cat .env",
    "!cat ../.env",
    "!cat .env.local",
    "!cat .envrc",
    "!head -3 prod.env",
    "!tail .env",
    "!less .env",
    "!more .env",
    "!bat .env",
    "!batcat .env",
    "!tac .env",
    "!nl .env",
    "!strings .env",
    "!sort .env",
    "!uniq .env",
    "!xxd .env",
    "!od -c .env",
    "!grep KEY .env",
    "!egrep KEY .env",
    "!fgrep KEY .env",
    "!rg KEY .env",
    "!env | grep KEY",
    "!env | sort",
    "!ls && printenv OPENAI_API_KEY",
    "!ls; printenv OPENAI_API_KEY",
    "!false || printenv OPENAI_API_KEY",
    "!env 2> errors.txt",
    "!echo \\\n  $OPENAI_API_KEY",
    "%sx printenv OPENAI_API_KEY",
    "%system env",
    "%pycat .env",
    "%less .env",
    "%more .env",
    "%page .env",
    "%cat .env",
    "import os\nkey = os.getenv('K')\n!echo $key",
    "import os\nkey = os.getenv('K')\n!echo {key}",
    "import os\nkey = os.getenv('K')\n!echo {key.strip()}",
    "for name in names:\n    !printenv OPENAI_API_KEY",
    "!X=$OPENAI_API_KEY; echo $X",
    "!export X=$OPENAI_API_KEY; echo $X",
    "!export X=1; printenv OPENAI_API_KEY",
    "%%bash\nprintenv OPENAI_API_KEY",
    "%%bash\necho $OPENAI_API_KEY",
    "%%sh\nenv",
    "%%system\nprintenv",
    "%%sx\nprintenv",
    "%%!\nprintenv",
    "%%script bash\nexport -p",
    "%%script sh\nprintenv",
    "%%script zsh\nprintenv",
    "%%script dash\nprintenv",
    "%%script ksh\nprintenv",
    "%%bash\ncd /tmp && \\\n  printenv",
    "%%bash\nKEY=$OPENAI_API_KEY\necho $KEY",
    "%%bash\nlocal KEY=$OPENAI_API_KEY\necho $KEY",
    "%%bash\ndeclare KEY=$OPENAI_API_KEY\necho $KEY",
    "%%bash\ntypeset KEY=$OPENAI_API_KEY\necho $KEY",
    "%%bash\nreadonly KEY=$OPENAI_API_KEY\necho $KEY",
    "%%bash\nfor v in $OPENAI_API_KEY; do\n  echo $v\ndone",
    "%%time\n!printenv OPENAI_API_KEY\nx = 1",
    "%%time\n!printenv OPENAI_API_KEY\nx = (",  # unparsable: its magic lines still count
    "x = !printenv OPENAI_API_KEY\nprint(x)",
    "x = %env OPENAI_API_KEY\nx",
    "values = %env\nprint(values)",
    "lines = !cat .env\nlines",
    # $NAME: IPython fills it from Python first, then the environment
    "OPENAI_API_KEY = os.environ['OPENAI_API_KEY']\n!echo $OPENAI_API_KEY",
    # %prun's -r and -q are flags; so are %timeit's -o, -q and -c
    'import os\n%prun -r print(os.environ["K"])',
    'import os\n%prun -q print(os.environ["K"])',
    'import os\n%timeit -o print(os.environ["K"])',
    'import os\n%timeit -c print(os.environ["K"])',
    # IPython's help and %whos
    'import os\nkey = os.getenv("K")\nkey?',
    'import os\nkey = os.getenv("K")\nkey??',
    'import os\nkey = os.getenv("K")\n?key',
    "import os\nos.environ?",
    'import os\nkey = os.getenv("K")\n%pinfo key',
    'import os\nkey = os.getenv("K")\n%pinfo2 key',
    "import os\n%pinfo os.environ",
    'import os\nkey = os.getenv("K")\n%whos',
    # an operator inside quotes is text
    '!grep -E "API_KEY|TOKEN" .env',
    "!grep -E 'OPENAI|HF' ../.env",
    '%%bash\ngrep -E "KEY|TOKEN|SECRET" .env',
    '!echo "OPENAI_API_KEY -> $OPENAI_API_KEY"',
    '!echo "key => ${OPENAI_API_KEY:0:6}"',
    '!echo "<b>$OPENAI_API_KEY</b>"',
    '!echo "OPENAI_API_KEY | $OPENAI_API_KEY"',
    '!echo "status: ok; key=$OPENAI_API_KEY"',
    '!echo "a && $OPENAI_API_KEY"',
    "!echo 'no > here' $OPENAI_API_KEY",
    '%%bash\nprintf "%-20s -> %s\\n" OPENAI_API_KEY "${OPENAI_API_KEY:0:4}"',
    "import os\nos.system('echo \"key -> $OPENAI_API_KEY\"')",
    '%%bash\necho "a\nb $OPENAI_API_KEY"',  # a quote open across lines
    '!echo "$OPENAI_API_KEY',  # a quote that never closes: read as raw text
    '!true; printenv "OPENAI_API_KEY',
    '!true && printenv "OPENAI_API_KEY',
    '!false || printenv "OPENAI_API_KEY',
    '%%bash\necho "a\nprintenv OPENAI_API_KEY',  # still open at the end: raw text, line by line
    # expansions
    '%%bash\nfor v in OPENAI_API_KEY HF_TOKEN; do echo "$v=${!v}"; done',
    "!echo $(cat .env)",
    '!echo "$(printenv OPENAI_API_KEY)"',
    "!echo `printenv OPENAI_API_KEY`",
    "!X=$(printenv OPENAI_API_KEY); echo $X",
    "!echo ${OPENAI_API_KEY:-unset}",
    "!echo $((1 + 2)) $OPENAI_API_KEY",
    "import os\nkey = os.getenv('K')\n!echo '$key'",  # IPython fills $name even in single quotes
    # a shell inside, other readers, stdin and /proc
    'import subprocess\nsubprocess.run(["bash", "-c", "echo $OPENAI_API_KEY"])',
    "!sh -lc 'printenv OPENAI_API_KEY'",
    "!zsh -c 'env'",
    "!cat /proc/self/environ",
    "!strings /proc/1/environ",
    "!cat < .env",
    "%%bash\ncat < .env",
    "!head -n 3 < ../.env",
    "!tr '\\n' ' ' < .env",
    "!tee < .env",
    "!awk -F= '{print $2}' .env",
    "!sed 's/^/x /' .env",
    "!cut -d= -f2 .env",
    "!paste .env",
    "!column .env",
    "!rev .env",
    "!base64 .env",
    "!gawk '{print}' .env",
    "!mawk '{print}' .env",
    "!env | cut -d= -f2",
    "!env | cut -f1",
    "!env | awk '{print $0}'",
    "!printenv OPENAI_API_KEY > /dev/stdout",
    "!printenv OPENAI_API_KEY > /dev/stderr",
    "!printenv OPENAI_API_KEY > /dev/tty",
    "!printenv OPENAI_API_KEY > /dev/fd/1",
    "!printenv OPENAI_API_KEY 1> /dev/fd/2",
    "!cat -o '^[^=]*' .env",  # -o keeps only the names for grep alone
    '!echo "\\" | cat" $OPENAI_API_KEY',  # an escaped quote inside double quotes
    "!cat .en\\v",  # a backslash before an ordinary character is dropped
    "!echo x#y $OPENAI_API_KEY",  # a # inside a word starts no comment
    "!echo \"$(echo ')' ; printenv OPENAI_API_KEY)\"",  # a ) quoted inside $(…)
    "!echo ${OPENAI_API_KEY:-a;b}",  # a ; inside ${…} is part of the word
    '!cat ".env"',
    "!echo \"'$OPENAI_API_KEY'\"",  # single quotes inside double quotes are characters
    "!env | grep --context=1 KEY",  # a long option is no quiet flag
    "\n%%script bash\nprintenv",  # the cell magic is the first non-blank line
    '!bash -c "echo \\$OPENAI_API_KEY"',  # the outer shell drops the backslash
    "!bash -c -e 'printenv OPENAI_API_KEY'",
    "!env | cut -f1 --output-delimiter =",  # tab-split: the whole line
    "!env | cut -d : -f1",
    "!env | cut -d = -f 2",
    "!env | awk -F : '{print $1}'",
    # a heredoc's apostrophe: the raw reading still finds `echo`, redirection taken out
    "%%bash\ncat <<EOF\nit's done\nEOF\n>&2 echo $OPENAI_API_KEY",
    '!echo "$(echo " # ")" && printenv OPENAI_API_KEY',  # quotes nest inside "$(…)"
    '!echo "`echo " # "`" && printenv OPENAI_API_KEY',
    "!cat <> .env",
    "!cat '.env'",
    "!cat 0< .env",
    "!cat \\.env",
    "!cat<.env",
    "!export X=$OPENAI_API_KEY; printenv X",
    "!export OPENAI_API_KEY; printenv OPENAI_API_KEY",  # export alone keeps the value
    "%%bash\nexport OPENAI_API_KEY\necho $OPENAI_API_KEY",
    "!declare OPENAI_API_KEY; echo $OPENAI_API_KEY",
    "%%bash\nlocal OPENAI_API_KEY\necho $OPENAI_API_KEY",  # no function: local fails, the value stays
    "!env | grep -e '^[^=]*' -e 'foo'",  # no -o: whole lines
    "%%bash\necho key: \\\n  $OPENAI_API_KEY",  # a continuation line
    "%%bash\necho 'key: '$OPENAI_API_KEY",  # a quote closes: the rest of the word expands
    "!printenv OPENAI_API_KEY 1>/dev/stdout",  # `1>`: the descriptor isn't the file's name
    "!env | awk -F= '{print $1, $2}'",
    "!env | awk -F= '{print $1; print $0}'",
    "!env | awk -F= '{print $1; print}'",
    '!echo "${X:-" # "}" && printenv OPENAI_API_KEY',
    "!bash -c \"echo \\'\\$OPENAI_API_KEY\\'\"",  # in double quotes \' keeps its backslash
    '!bash -c "echo \\`printenv OPENAI_API_KEY\\`"',
    '!bash -c "echo \\"\\$OPENAI_API_KEY\\""',
    '!echo $(printf "x) $OPENAI_API_KEY',  # a quote left open inside $(…)
    "!env | grep local",  # a word, not a quiet flag
    "!env | awk '{print $1}'",  # no -F=: $1 is the whole NAME=value
    "!env | awk -F= '{print $2}'",
    "!env | awk -F= '{print \"x\"}'",  # only the listed names-only forms are quiet
    "!env | grep -o 'KEY=.*'",
    "!env | grep '^[^=]*'",
    "!printenv OPENAI_API_KEY 2>&1",
    "!printenv OPENAI_API_KEY >&2",
    "!printenv OPENAI_API_KEY 2> err.txt",
    "!printenv OPENAI_API_KEY 2>> err.txt",
    "!printenv OPENAI_API_KEY < /dev/null",
    "!printenv OPENAI_API_KEY <<< x",
    "!LANG=C printenv OPENAI_API_KEY",
    "!true; printenv OPENAI_API_KEY &",
    "!printenv OPENAI_API_KEY |& cat",
    "%%script python3\nimport os\nprint(os.environ['K'])",
    "%%script python\nimport os\nprint(os.environ)",
    "%%script /usr/bin/python3\nimport os\nprint(os.environ)",
    "%%script /bin/bash\nprintenv",
    "a, b = %env K\nprint(a)",
]


# Magics and shell lines that show no value: a count, the names, a file, a comment, a literal.
L011_QUIET = [
    "%env OPENAI_API_KEY=placeholder",
    "%env OPENAI_API_KEY placeholder",
    "%set_env OPENAI_API_KEY=placeholder",
    "!export OPENAI_API_KEY=placeholder",
    "!declare -x MODE=dev",
    "!env | wc -l",
    "!env | grep -q OPENAI_API_KEY",
    "!env | grep -c KEY",
    "!env | grep -l KEY",
    "!env | grep -L KEY",
    "!env | cut -d= -f1",
    "!env | cut -d= -f 1",
    "!env > env.txt",
    "!printenv OPENAI_API_KEY >> saved.txt",
    "!env &> env.txt",
    "!env 1> env.txt",
    "!env -i",
    "!env -i FOO=1",
    "!env - FOO=1",
    "!env --ignore-environment",
    "!env FOO=1 python -c 'print(1)'",
    "!env -u OPENAI_API_KEY python x.py",
    "!env -C /tmp ls",
    "!set -e",
    "!set -euo pipefail",
    "!export -n OPENAI_API_KEY",
    "!declare -x MODE=dev",
    "!echo ${#OPENAI_API_KEY}",
    "!echo ${OPENAI_API_KEY:+set}",
    "!echo ${OPENAI_API_KEY+set}",
    "!echo hello",
    "!echo $lower_case_name",
    "!cat .env.example",
    "!cat .env.sample",
    "!cat .env.template",
    "!cat .env.dist",
    "!cat .env.defaults",
    "!cat .env.tpl",
    "!cat config.txt",
    "!grep -c KEY .env",
    "!grep -q KEY .env",
    "!grep -l KEY .env",
    "!ls -la",
    "%pycat notes.txt",
    "%cat notes.txt",
    "x = !printenv OPENAI_API_KEY",
    "x = %env OPENAI_API_KEY",
    "%%bash\nls -la\necho done",
    "%%sql\nSELECT * FROM env",
    "%%writefile show.py\nimport os\nprint(os.environ)",
    "%%html\n<p>$OPENAI_API_KEY</p>",
    "key = 'x'\n!echo {len(key)}",
    # $NAME that means a Python name or a shell variable, not the environment
    "DATA_PATH = 'data.csv'\n!echo $DATA_PATH",
    "import os\nOPENAI_API_KEY = 'x'\n!echo $OPENAI_API_KEY",
    "%%bash\nDATA_DIR=data\necho $DATA_DIR",
    "%%bash\nexport DATA_DIR=data\necho $DATA_DIR",
    "%%bash\nlocal DATA_DIR=data\necho $DATA_DIR",
    "%%bash\ndeclare DATA_DIR=data\necho $DATA_DIR",
    "%%bash\ntypeset DATA_DIR=data\necho $DATA_DIR",
    "%%bash\nreadonly DATA_DIR=data\necho $DATA_DIR",
    "%%bash\nread -r NAME\necho $NAME",
    "%%bash\nfor FILE in *.csv; do\n  echo $FILE\ndone",
    "%%bash\nDATA_DIR=data > /dev/null\necho $DATA_DIR",
    "!DATA_DIR=data; echo $DATA_DIR",
    "!for F in a b; do echo $F; done",
    "!export X=1; printenv X",
    "import os\nos.system('echo {key}')",
    "!env | grep -i key | sed 's/=.*//'",
    "!env | sed -e 's|=.*||'",
    "!printenv | awk -F= '/KEY/ {print $1}'",
    "!printenv | awk -F = '{print $1}'",
    "!env | grep -o '^[^=]*'",
    "!env | cut -d '=' -f 1",
    "!sed 's/=.*//' .env",
    "!awk -F= '{print $1}' .env",
    "!cut -d= -f1 .env",
    "!grep -o '^[^=]*' .env",
    "!wc -l < .env",
    "!grep -c KEY < .env",
    "!ls -la .env",
    "!declare -f",
    "!echo hi # $OPENAI_API_KEY",
    "!echo '$OPENAI_API_KEY'",
    '!echo "\\$OPENAI_API_KEY"',
    "!echo ${!OPENAI*}",
    "!printenv OPENAI_API_KEY > out.txt 2>&1",
    "!printenv OPENAI_API_KEY &> out.txt",
    "!printenv OPENAI_API_KEY >| out.txt",
    "!printenv OPENAI_API_KEY >&out.txt",
    "!printenv | cat > out.txt",
    '!printenv "OPENAI_API_KEY > out.txt',  # a quote that never closes: read as raw text
    '!printenv "OPENAI_API_KEY 1> out.txt',
    '!printenv "OPENAI_API_KEY &> out.txt',
    '!printenv "OPENAI_API_KEY >> out.txt',
    '!env | wc -l "x',
    "!echo 'x\\' \"; printenv K\"",  # in single quotes a backslash is just a character
    "!echo \\; printenv OPENAI_API_KEY",  # an escaped ; is a character
    '!echo "a\\" > out.txt"',
    "!bash check.sh printenv",  # a script file runs, not a -c string
    "!bash -x printenv",
    "%%writefile python_helper.py\nimport os\nprint(os.environ)",
    "%%script perl\nprint(os.environ)",
    "%ls .env",
    "!echo '$OPENAI_API_KEY",  # a single quote that never closes: bash runs nothing
    "!export x=$OPENAI_API_KEY | cat; echo $x",  # a pipeline stage sets no local
    "!cat <<< .env",  # a here-string is the word itself
    "%%timeit -n 1\nx = 1",
    "%%bash\nread OPENAI_API_KEY\necho $OPENAI_API_KEY",  # read sets it from stdin
    '!echo ${x:-(} "; printenv OPENAI_API_KEY"',  # a ${…} closes at its own brace
    "!x=$(echo ${y:-)}; printenv OPENAI_API_KEY)",  # a ${…} inside $(…) is one unit
    "!printenv OPENAI_API_KEY | > out.txt",
    "%%writefile sh\nprintenv OPENAI_API_KEY",  # a file named sh, not a shell
    "!env | cut -d = -f 1",
    "!echo ${!OPENAI@}",  # the names
    # a `"` inside a quote only an escape or a closed `$(…)` skips: read as the shell does
    '%%bash\necho "$(echo a\\\\)" "x; printenv OPENAI_API_KEY"',
    '%%bash\necho $(echo "a") "x; printenv OPENAI_API_KEY"',
    "!echo done \\\n!printenv",  # a continuation line is part of the line above
    "!cat data.csv 3< .env",  # only fd 0 is what cat reads
    '!echo "a\\" ; printenv OPENAI_API_KEY"',  # an escaped quote doesn't close it
    "!printenv OPENAI_API_KEY 1>&out.txt",
    "!printenv OPENAI_API_KEY 2>&err.txt",  # an ambiguous redirect: bash runs nothing
    '!bash -c "echo \\\\\\`printenv OPENAI_API_KEY\\`"',  # an escaped backtick never closes
    "!x=$(if (true); then printenv OPENAI_API_KEY; fi)",  # captured, brackets nested
    "%set_env",
    "%set_env K",
    "!env | gawk -F= '{print $1}'",
    "!env | mawk -F= '{print $1}'",
    'client().system("printenv K")',
    'import subprocess\nsubprocess.run(["printenv", name])',
    'import subprocess\nsubprocess.run(["bash", "script.sh"])',
    "!bash script.sh",
    "!bash -x",
    "%%script python\nprint(1)",
]


# Taint through names, formatting, containers, set-in-the-cell and built dicts.
L011_TAINT = [
    'import os\nkey = os.environ["OPENAI_API_KEY"]\nprint(key)',
    'import os\nkey = os.getenv("K")\nmessage = "key: " + key\nprint(message)',
    'import os\nkey = os.getenv("K")\nprint(f"key={key}")',
    'import os\nkey = os.getenv("K")\nprint(f"{key!r}")',
    'import os\nkey = os.getenv("K")\nprint(f"{key[:4]}...")',
    'import os\nkey = os.getenv("K")\nprint("%s" % key)',
    'import os\nkey = os.getenv("K")\nprint("{}".format(key))',
    'import os\nkey = os.getenv("K")\nprint(format(key, ">10"))',
    'import os\nkey = os.getenv("K")\nprint(", ".join([key]))',
    'import os\nkey = os.getenv("K")\nprint(key * 2)',
    'import os\nkey = os.getenv("K")\nprint(-key)',
    'import os\nkey = os.getenv("K")\nprint(key.strip().upper())',
    'import os\nkey = os.getenv("K")\nprint(key.encode())',
    'import os\nkey = os.getenv("K") or "none"\nprint(key)',
    'import os\nkey = os.getenv("K")\nprint(key if key else "none")',
    'import os\nprint(os.getenv("A") and os.getenv("B"))',
    'import os\nkey = ""\nkey += os.getenv("K")\nprint(key)',
    'import os\na, b = os.getenv("A"), os.getenv("B")\nprint(b)',
    'import os\na, *rest = os.getenv("A"), os.getenv("B")\nprint(rest)',
    'import os\nfirst, second = os.getenv("A").split(":")\nprint(second)',
    'import os\nif (key := os.getenv("K")):\n    print(key)',
    'import os\nprint(key := os.getenv("K"))',
    'import os\nsettings = {"key": os.getenv("K")}\nprint(settings)',
    'import os\nsettings = {os.getenv("K"): 1}\nprint(settings)',
    'import os\nsettings = {}\nsettings["key"] = os.getenv("K")\nprint(settings)',
    'import os\nfound = []\nfound.append(os.getenv("K"))\nprint(found)',
    'import os\nfound = []\nfound.extend([os.getenv("K")])\nprint(found)',
    'import os\nfound = []\nfound.insert(0, os.getenv("K"))\nprint(found)',
    'import os\nfound = set()\nfound.add(os.getenv("K"))\nprint(found)',
    'import os\nfrom collections import deque\nq = deque()\nq.appendleft(os.getenv("K"))\nprint(q)',
    'import os\nfrom collections import deque\nq = deque()\nq.extendleft([os.getenv("K")])\n'
    "print(q)",
    "import os\nmerged = {}\nmerged.update(os.environ)\nprint(merged)",
    "import os\nmerged = {}\nmerged.update(os.environ)\nprint(merged['OPENAI_API_KEY'])",
    'import os\nclass Cfg: ...\nCfg.key = os.getenv("K")\nprint(Cfg.key)',
    "import os\nfor name, value in os.environ.items():\n    print(value)",
    "import os\nfor value in os.environ.values():\n    print(value)",
    "import os\nfor i, value in enumerate(os.environ.values()):\n    print(value)",
    "import os\nfor pair in os.environ.items():\n    print(pair)",
    "import os\nprint(*os.environ.values())",
    "import os\n{k: v for k, v in os.environ.items() if k.startswith('AWS')}",
    "import os\n[v for v in os.environ.values()]",
    "import os\n{v for v in os.environ.values()}",
    "import os\n(v for v in os.environ.values())",
    "import os\nsorted(os.environ.items())",
    "import os\nnext(iter(os.environ.values()))",
    "import os\nprint(list(os.environ.values()))",
    "import os\nprint(tuple(os.environ.values()))",
    "import os\nprint(set(os.environ.values()))",
    "import os\nprint(frozenset(os.environ.values()))",
    "import os\nprint(sorted(os.environ.values()))",
    "import os\nprint(reversed(os.environ.values()))",
    "import os\nprint(min(os.environ.values()))",
    "import os\nprint(max(os.environ.values()))",
    "import os\nprint(list(filter(None, os.environ.values())))",
    "import os\nprint(list(map(str.strip, os.environ.values())))",
    "import os\nprint(list(map(os.environ.get, ['A', 'B'])))",
    "import os\nprint(list(zip(os.environ.values(), [1])))",
    "import os\nfor name, value in zip(names, os.environ.values()):\n    print(value)",
    'import os\nprint(int(os.getenv("PORT")))',
    'import os\nprint(float(os.getenv("RATE")))',
    'import os\nprint(complex(os.getenv("Z")))',
    'import os\nprint(abs(int(os.getenv("N"))))',
    'import os\nprint(round(float(os.getenv("N"))))',
    'import os\nprint(ascii(os.getenv("K")))',
    'import os\nprint(bytes(os.getenv("K"), "utf-8"))',
    'import os\nprint(bytearray(os.getenv("K"), "utf-8"))',
    'import os\nprint("{OPENAI_API_KEY}".format_map(os.environ))',
    'import os\nfrom string import Template\nprint(Template("$K").substitute(os.environ))',
    'import os\nfrom string import Template\nTemplate("$K").safe_substitute(os.environ)',
    'import os\nfrom string import Template\nTemplate("$K").substitute(K=os.getenv("K"))',
    "import json, os\nprint(json.dumps(dict(os.environ), indent=2))",
    "import os\nfrom pprint import pformat\nprint(pformat(dict(os.environ)))",
    "import os\nimport pandas as pd\npd.DataFrame(os.environ.items(), columns=['name', 'value'])",
    'import os\nimport pandas as pd\npd.Series({"K": os.getenv("K")})',
    "import os\nimport pandas as pd\npd.DataFrame.from_dict(dict(os.environ), orient='index')",
    "import os\nimport pandas as pd\npd.DataFrame.from_records(list(os.environ.items()))",
    'import os\nfrom IPython.display import Markdown\nMarkdown(os.environ["K"])',
    'import os\nfrom IPython.display import HTML\nHTML(os.environ["K"])',
    'import os\nfrom IPython.display import Latex\nLatex(os.environ["K"])',
    'import os\nfrom IPython.display import Pretty\nPretty(os.environ["K"])',
    "import os\nfrom IPython.display import JSON\nJSON(dict(os.environ))",
    'import os\nfrom IPython.display import Code\nCode(os.environ["K"])',
    'import os\nprev = None\nfor name in ["A", "B"]:\n    print(prev)\n    prev = os.environ[name]',
    'import os\nkey = os.getenv("K")\ndef show():\n    print(key)',
    'import os\nkey = os.getenv("K")\nif key:\n    print(key)',
    'import os\ntry:\n    key = os.environ["K"]\nexcept KeyError:\n    key = None\nprint(key)',
    'import os\nkey = os.getenv("K")\nwhile key:\n    print(key)\n    break',
    'import os\nkey = os.getenv("K")\nmatch key:\n    case _:\n        print(key)',
    'import os\nwith open("x") as f:\n    key = os.getenv("K")\nprint(key)',
    "from dotenv import dotenv_values\nconfig = dotenv_values()\nprint(config['OPENAI_API_KEY'])",
    "from dotenv import dotenv_values\nconfig = dotenv_values()\nfor k, v in config.items():\n"
    "    print(k, v)",
    'import os\nkey = os.getenv("K")\nlabel = "sk" if key else ""\nprint(label, key)',
    # the env the cell set: a tainted value, a del or a pop undoes it
    'import os\nos.environ["MODE"] = os.getenv("SECRET")\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\ndel os.environ["MODE"]\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.environ.pop("MODE")\nprint(os.getenv("MODE"))',
    'import os\nif x:\n    os.environ["MODE"] = "dev"\nprint(os.environ["MODE"])',
    'import os\nif x:\n    %env MODE=dev\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.environ["MODE"] = os.getenv("SECRET")\n'
    'print(os.environ["MODE"])',
    'import os\n%time key = os.getenv("K")\nprint(key)',  # %time stmt is Python
    # an env var the cell set to something that isn't a literal reads as the env var
    'import os, getpass\nos.environ["K"] = getpass.getpass()\nprint(os.environ["K"][:5])',
    'import os\nfrom google.colab import userdata\nos.environ["K"] = userdata.get("K")\n'
    'print(os.environ["K"][:8])',
    'import os\nos.environ["K"] = secret_value\nos.environ["K"]',
    'import os\nos.environ["DB_URL"] = f"postgresql://{user}:{pw}@host/db"\nprint(os.environ["DB_URL"])',
    'import os\nfrom dotenv import load_dotenv\nos.environ["K"] = ""\nload_dotenv(override=True)\n'
    'print(os.environ["K"][:4])',
    'import os\nos.environ["K"] = "x"\nload_dotenv()\nprint(os.environ["K"])',  # imported earlier
    "%env K=$key\n%env K",
    "%env K={key}\n%env K",
    'import os\nos.environ.update(K=key)\nprint(os.environ["K"])',
    'import os\nos.environ["K"] = "x"\nos.environ.update(dotenv_values())\nprint(os.environ["K"])',
    'import os\nos.environ["K"] = "x"\nos.environ.update({name: "y"})\nprint(os.environ["K"])',
    'import os\nos.environ["K"] = "x"\nos.environ.update(**extra)\nprint(os.environ["K"])',
    'import os\nos.environ.setdefault("MODE", "dev")\nprint(os.environ["MODE"])',  # keeps a set value
    'import os\nif x:\n    os.environ.update(MODE="dev")\nprint(os.environ["MODE"])',
    'import os\ndef setup():\n    os.environ["MODE"] = "dev"\nprint(os.environ["MODE"])',
    # a dict the cell builds holds its values
    'import os\nvals = {n: os.getenv(n) for n in ["A", "B"]}\nfor name, val in vals.items():\n'
    "    print(val)",
    'import os\nvals = {n: os.getenv(n) for n in ["A", "B"]}\nprint(vals)',
    'import os\nvals = {n: os.getenv(n) for n in ["A", "B"]}\nprint(vals["A"])',
    'import os\nvals = {n: os.getenv(n) for n in ["A", "B"]}\nprint(list(vals.values()))',
    'import os\nvals = {}\nfor n in ["A"]:\n    vals[n] = os.getenv(n)\nprint(vals)',
    'import os\nvals = dict()\nvals["a"] = os.getenv("A")\nprint(vals["a"])',
    'import os\nd = {"k": os.getenv("K")}\nprint(d.get("k"))',
    'import os\nd = {"k": os.getenv("K")}\nprint(dict(d))',
    'import os\nd = {"k": os.getenv("K")}\nprint(d.copy())',
    'import os\nd = {os.getenv("K"): 1}\nprint(list(d))',  # a tainted key shows in the names
    "import os\nfor i, (k, v) in enumerate(os.environ.items()):\n    print(v)",
    "import os\nfor i, pair in enumerate(os.environ.items()):\n    print(pair)",
    'import os\nfound = [None]\nfound[0] = os.getenv("K")\nprint(list(found))',  # a list holds values
    'import os\nd = {}\nd.update(MODE="dev")\nprint(os.environ["MODE"])',  # a dict's update, not the env's
    'import os\nfound = get_list()\nfound[0] = os.getenv("K")\nprint(list(found))',  # no dict type
    "import os\nfor value, name in zip(os.environ.values(), names):\n    print(value)",
    "import os\nfor a, b in zip(os.environ.values(), os.environ.values()):\n    print(a)",
    "import os\nprint(list(filter(bool, os.environ.values())))",
    "import os\nprint(list(filter(len, os.environ.values())))",
    'import os, yaml\ncfg = {"api_key": os.environ["K"]}\nprint(yaml.safe_dump(cfg))',
    'import os, yaml\ncfg = {"api_key": os.environ["K"]}\nprint(yaml.dump(cfg))',
    'import os\nfrom tabulate import tabulate\nrows = [(k, os.environ[k]) for k in ["A"]]\nprint(tabulate(rows))',
    "from dotenv import dotenv_values\nconfig = dotenv_values()\nfor k in config:\n"
    '    print(config[k].split("=")[0])',
    'import os\nfor n in names:\n    print(os.environ[n].split("=")[0])',
    'for line in open(".env"):\n    print(line.split()[0])',
    'for line in open(".env"):\n    print(line.split(":")[0])',
    'from dotenv import dotenv_values\nprint(dotenv_values("settings.example"))',
    'import os\nd = dict(os.environ)\nd["MODE"] = "dev"\nprint(os.environ["MODE"])',  # a copy, not the env
    'from dotenv import dotenv_values\nimport os\nos.environ["MODE"] = "dev"\n'
    'print(dotenv_values()["MODE"])',  # set in the env, not in .env
    "%env K $key\n%env K",
    "%env K=dev\n%env K=$key\n%env K",
    'import os\nkey = os.getenv("K")\nkey: str\nprint(key)',  # an annotation alone binds nothing
    'import os\nd = {}\nd = []\nd["k"] = os.getenv("K")\nprint(list(d))',  # rebound: no dict now
    # a function's own names are its locals, unless declared global or nonlocal, or bound only
    # in a nested scope (a function, a lambda, a comprehension's target)
    'import os\nkey = os.getenv("K")\ndef f():\n    global key\n    if x:\n        key = "a"\n    print(key)\nf()',
    'import os\ndef outer():\n    key = os.getenv("K")\n    def inner():\n        nonlocal key\n        if x:\n'
    '            key = "a"\n        print(key)\n    inner()',
    'import os\nkey = os.getenv("K")\ndef f():\n    def g():\n        key = "a"\n    print(key)\nf()',
    'import os\nkey = os.getenv("K")\ndef f():\n    g = lambda: (key := "a")\n    print(key)\nf()',
    'import os\nkey = os.getenv("K")\ndef f():\n    names = [key for key in ["a"]]\n    print(key)\nf()',
    'import os\nkey = os.getenv("K")\nif x:\n    del key\nprint(key)',  # the branch may not run
    # a set, del or pop of a name nh can't read may change any var
    'import os\nos.environ["MODE"] = "dev"\nos.environ[name] = value\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\ndel os.environ[name]\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.environ.pop(name)\nprint(os.environ["MODE"])',
    'import os\ndef show(environ):\n    print(environ)\nshow(os.getenv("K"))',
    'import logging, os\nlog = logging.getLogger()\nlog = os.getenv("LOG_LEVEL")\nprint(log)',
    'print(open(".env").buffer.read())',
    'import os\nx = %time os.environ["K"]\nprint(x)',  # %time returns the value
    "import os\nfor k, (a, b) in os.environ.items():\n    print(a)",  # a piece of a value
    'from dotenv import dotenv_values\nprint(dotenv_values()["K"].split("=")[0])',  # a value's piece
    "import os\ndef get():\n    return f\"{os.environ['K']}\"\nprint(get())",
    'import os\nprint("{}".format(os.environ))',
    "import os\nimport pandas as pd\npd.DataFrame(data=dict(os.environ))",
    'import os\nprint("{K}".format(**os.environ))',
    'import os\nfrom dotenv.main import load_dotenv\nos.environ["K"] = "x"\nload_dotenv()\n'
    'print(os.environ["K"])',
    'for line in open(".env"):\n    print(line.split("=", 1)[1])',
    'for line in open(".env"):\n    print(line.strip("=")[0])',
    'import os\nkey = os.getenv("K")\nprint(key.split("=")[0])',  # a piece of a named value
    'import os\nimport pandas as pd\ns = pd.Series({"K": os.getenv("K")})\nfor label, value in s.items():\n'
    "    print(value)",
]


# What shows no value: checks, names only, facts, a value passed on, literals the cell set.
L011_EXEMPT = [
    'import os\nprint(len(os.environ["OPENAI_API_KEY"]))',
    'import os\nprint(bool(os.getenv("OPENAI_API_KEY")))',
    'import os\nprint(hash(os.getenv("OPENAI_API_KEY")))',
    'import os\nprint(id(os.getenv("OPENAI_API_KEY")))',
    'import os\nprint(type(os.getenv("OPENAI_API_KEY")))',
    'import os\nprint(isinstance(os.getenv("K"), str))',
    'import os\nprint(callable(os.getenv("K")))',
    'import os\nprint(hasattr(os.getenv("K"), "strip"))',
    'import os\nprint(os.getenv("OPENAI_API_KEY") is None)',
    'import os\nprint(os.getenv("OPENAI_API_KEY") is not None)',
    'import os\nprint("OPENAI_API_KEY" in os.environ)',
    'import os\n"OPENAI_API_KEY" in os.environ',
    'import os\nprint("OPENAI_API_KEY" in os.environ.keys())',
    'import os\nprint(not os.getenv("OPENAI_API_KEY"))',
    'import os\nprint(os.getenv("OPENAI_API_KEY") == "")',
    'import os\nprint(os.getenv("OPENAI_API_KEY", "").startswith("sk-"))',
    'import os\nprint(os.getenv("OPENAI_API_KEY", "").endswith("x"))',
    *(
        f'import os\nprint(os.getenv("K", "").{method}())'
        for method in ["isalnum", "isalpha", "isascii", "isdecimal", "isdigit", "isidentifier"]
    ),
    *(
        f'import os\nprint(os.getenv("K", "").{method}())'
        for method in ["islower", "isnumeric", "isprintable", "isspace", "istitle", "isupper"]
    ),
    'import os\nprint(os.getenv("K", "").count("-"))',
    'import os\nprint(os.getenv("K", "").find("-"))',
    'import os\nprint(os.getenv("K", "").rfind("-"))',
    'import os\nprint(os.getenv("K", "-").index("-"))',
    'import os\nprint(os.getenv("K", "-").rindex("-"))',
    'import os\nprint(os.getenv("K", "").__len__())',
    'import os\nprint(os.getenv("K", "").__contains__("x"))',
    "import os\nprint(list(os.environ))",
    "import os\nprint(sorted(os.environ))",
    "import os\nprint(sorted(os.environ.keys()))",
    "import os\nprint(list(os.environ.keys()))",
    "import os\nprint(len(os.environ.keys()))",
    "import os\nfor name in os.environ.keys():\n    print(name)",
    "import os\nprint(dict(os.environ).keys())",
    "import os\nprint(os.environ.copy().keys())",
    "from dotenv import dotenv_values\nprint(dotenv_values().keys())",
    "import os\nprint(len(os.environ))",
    "import os\nprint(*os.environ)",
    "import os\nprint(sorted(os.environ, key=os.environ.get))",
    *(
        f"import os\nprint(list(map({function}, os.environ.values())))"
        for function in ["len", "bool", "hash", "id", "type", "callable"]
    ),
    "import os\nfor name in os.environ | {}:\n    print(name)",
    "import os\nfor name in {**os.environ}:\n    print(name)",
    "import os\nprint((os.environ | {}).keys())",
    "import os\nmerged = {}\nmerged.update(os.environ)\nprint(sorted(merged))",
    "import os\n[name for name in os.environ if name.endswith('_KEY')]",
    "import os\nfor name in os.environ:\n    print(name)",
    "import os\nfor name, value in os.environ.items():\n    print(name)",
    "import os\nfor i, value in enumerate(os.environ.values()):\n    print(i)",
    "import os\nfor name, value in zip(names, os.environ.values()):\n    print(name)",
    "import os\nprint(os.environ.items().__len__())",
    "import os\nprint(os.environ.items().isdisjoint([]))",
    "import os\n{name: len(value) for name, value in os.environ.items()}",
    'import os\nprint(f"{len(os.environ)} variables")',
    'import os\nkey = os.getenv("K")\nprint("set" if key else "missing")',
    'import os\nkey = os.getenv("K")\nprint(f"key set: {key is not None}")',
    'import os\nkey = os.getenv("K")\nprint(key and "set")',
    'import os\nkey = os.getenv("K")\nassert key, "OPENAI_API_KEY is not set"',
    'import os\nkey = os.getenv("K")\nkey = "replaced"\nprint(key)',
    'import os\nkey = os.getenv("K")\ndel key\nkey = 1\nkey',
    'import os\nkey = os.getenv("K")\nstr(key);',
    'import os\nkey = os.getenv("K")\nx = str(key)',
    'import os\nos.environ["MODE"] = "dev"',
    'import os\nos.environ.setdefault("MODE", "dev");',
    "import os\nos.environ.update(MODE='dev')",
    # an env var the cell set to a clean value reads clean
    'import os\nos.environ["MODE"] = "dev"\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.getenv("MODE")',
    "%env MODE=dev\nimport os\nprint(os.environ['MODE'])",
    "%env MODE dev\nimport os\nprint(os.environ['MODE'])",
    "%set_env MODE dev\nimport os\nprint(os.environ['MODE'])",
    "%env MODE=dev\n%env MODE",
    'from dotenv import dotenv_values\nprint(dotenv_values(".env.example"))',
    'from dotenv import dotenv_values\nprint(dotenv_values("config/.env.sample"))',
    "import os\nfrom sqlalchemy import create_engine\n\n"
    'engine = create_engine(os.environ["DB_URL"])\nengine.dialect.name',
    'import os\nfrom huggingface_hub import login\n\ntoken = os.getenv("HF_TOKEN")\nlogin(token=token)',
    'import os\nimport pandas as pd\n\ndf = pd.read_csv(os.environ["DATA_URL"])\ndf.shape',
    'import os\nkey = os.getenv("K")\nprint(key, file=log_file)',
    'import os\nimport subprocess\nresult = subprocess.run(["printenv", "K"], capture_output=True)',
    'import subprocess\nresult = subprocess.run(["env"], stdout=subprocess.PIPE)\n'
    "print(result.returncode)",
    'import subprocess\nsubprocess.run(["env"], stdout=subprocess.DEVNULL);',
    "import os\nprint(os.getcwd())",
    "print(environment_name)",
    'import os\nkey = os.getenv("K")\ndef check(key):\n    print(key)',
    'import os\nkey = os.getenv("K")\ncheck = lambda key: print(key)',
    'import os\nkey = os.getenv("K")\ntry:\n    pass\nexcept KeyError as key:\n    print(key)',
    'import os\nprint(os.path.expandvars("data/raw.csv"))',
    'from pathlib import Path\nENV_FILE = Path("../.env")\nprint(ENV_FILE)',
    'from pathlib import Path\nprint(Path("../.env").exists())',
    'print(open("notes.txt").read())',
    'from pathlib import Path\nprint(Path("data.csv").read_text())',
    'print(open(".env.example").read())',
    'print(".env")',
    'ENV_FILE = "../.env"\nprint(ENV_FILE)',
    "from dotenv import load_dotenv\nload_dotenv()",
    # os.system and shell cells get no IPython expansion: $key and {key} aren't Python's
    'import os\nkey = os.getenv("K")\nos.system("echo $key")',
    'import os\nkey = os.getenv("K")\nos.system("echo {key}")',
    # a dict the cell builds: its keys are names, and facts about its values are clean
    'import os\nvals = {n: os.getenv(n) for n in ["OPENAI_API_KEY", "HF_TOKEN"]}\n'
    'for name, val in vals.items():\n    print(name, "set" if val else "missing")',
    'import os\nenv = {n: os.getenv(n) for n in ["A"]}\nprint({n: v is not None for n, v in env.items()})',
    'import os\nimport pandas as pd\nenv = {n: os.getenv(n) for n in ["A"]}\n'
    'pd.Series({n: bool(v) for n, v in env.items()}, name="set")',
    'import os\nenv = {n: os.getenv(n) for n in ["A"]}\nmissing = [n for n, v in env.items() if not v]\n'
    'print("Missing:", missing or "none")',
    'import os\nvals = {}\nfor n in ["A", "B"]:\n    vals[n] = os.getenv(n)\n'
    "print([k for k, v in vals.items() if v is None])",
    'import os\nstatus = {}\nfor n in ["A", "B"]:\n    status[n] = os.getenv(n)\nprint(list(status))',
    'import os\nstatus = {}\nfor n in ["A", "B"]:\n    status[n] = os.getenv(n)\n'
    "print(sorted(status.keys()))",
    'import os\nfrom collections import OrderedDict\nstatus = OrderedDict()\nstatus["A"] = os.getenv("A")\n'
    "print(list(status))",
    'import os\nstatus = dict()\nstatus["A"] = os.getenv("A")\nprint(list(status))',
    'import os\nmerged = os.environ.copy()\nmerged["X"] = os.getenv("K")\nprint(list(merged))',
    'import os\nd = {}\nd.setdefault("k", os.getenv("K"))\nprint(list(d))',
    'import logging, os\nlogger = logging.getLogger()\nlogger.setLevel(os.environ["LEVEL"])',
    'import os\nprint(list(filter(os.environ.get, ["A", "B"])))',  # the names whose value is set
    'import os\ndef outer():\n    def inner():\n        return os.environ["K"]\n    return 1\n'
    "print(outer())",  # a nested function's return isn't the outer one's
    'import os\nenv = os.environ\nenv["MODE"] = "dev"\nprint(env["MODE"])',
    'import os\nmerged = {}\nmerged.update(dict(a=os.getenv("K")))\nprint(list(merged))',
    'r = run_job(["printenv", "K"], stdout=log)\nprint(r)',  # not a subprocess call
    'print(load_config(".env").read_text())',  # an unknown function of the path
    'import os\nd = {}\nif x:\n    d = []\nd["k"] = os.getenv("K")\nprint(list(d))',  # maybe still a dict
    # a name the function assigns is its local: never the cell's `key` (read unbound, it raises)
    'import os\nkey = os.getenv("K")\ndef f(flag):\n    if flag:\n        key = "a"\n    else:\n'
    '        key = "b"\n    print(key)\nf(True)',
    'import os\nkey = os.getenv("K")\ndef f():\n    if x:\n        del key\n    print(key)',
    'import os\nkey = os.getenv("K")\nclass C:\n    def f(self):\n        if x:\n            key = "a"\n'
    "        print(key)",
    'import os\nfrom rich.console import Console\nconsole = Console()\nconsole.save_text(os.environ["LOG_FILE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.environ["MODE"] += "-1"\nprint(os.environ["MODE"])',
    "from .dotenv import dotenv_values\nprint(dotenv_values())",  # the package's own module
    'import os\nkey = os.getenv("K")\ndel key\nprint(key)',
    'import os\nkey = os.getenv("K")\nclass key:\n    pass\nprint(key)',  # a def or class rebinds
    'import os\nkey = os.getenv("K")\ndef key():\n    return 1\nprint(key)',
    'from os import getenv\nclass getenv:\n    pass\nprint(getenv("K"))',
    "def show(environ):\n    print(environ)",  # a parameter hides os.environ
    "f = lambda environ: print(environ)",
    'import os\nkey = os.getenv("K")\nfrom settings import key\nprint(key)',
    'from .utils import getenv\nprint(getenv("K"))',
    'import os\ncfg.d = {}\ncfg.d["k"] = os.getenv("K")\nprint(list(cfg.d))',  # a dict: its keys
    'import logging, os\nlog = os.getenv("LOG")\nlog = logging.getLogger()\nprint(log)',
    'from os import getenv\ngetenv = lambda name: "demo"\nprint(getenv("K"))',
    'import os\nkey = os.getenv("K")\nfor key in ["a", "b"]:\n    print(key)',
    'import os\nkey = os.getenv("K")\nfor key, value in os.environ.items():\n    print(key)',
    'import os\nprint(os.path.expandvars("${#OPENAI_API_KEY}"))',  # Python leaves ${#…} as it is
    'import os\ncfg.env = os.environ\ncfg.env["MODE"] = "dev"\nprint(cfg.env["MODE"])',  # the env
    'import os\nkey = os.getenv("K")\nclass C:\n    pass\nprint(C.key)',  # C sets no key
    "import os\nd = dict(os.environ)\nprint(d.get)",  # a bound method's repr
    "import os, tqdm\nfor v in tqdm.tqdm(os.environ.values()):\n    pass",  # a bar shows counts
    'import os\nfrom report import info\ninfo(os.environ["K"])',  # not logging's info
    'with open(".env") as f:\n    print(f.fileno())',  # a number, not the file's text
    'import os\nos.environ["MODE"] = "dev"\nd = {"MODE": 1}\ndel d["MODE"]\nprint(os.environ["MODE"])',
    'import os\nos.environ["MODE"] = "dev"\nos.environ.pop("OTHER")\nprint(os.environ["MODE"])',
    'import os\nkey = os.getenv("K")\nos.system("echo \'$key\'")',  # the shell's quotes: no Python fill
    'import os\nd = dict(os.environ)\nos.environ["MODE"] = "dev"\nd.pop("MODE")\n'
    'print(os.environ["MODE"])',  # a copy's pop leaves the env
    'import os\nos.environ["MODE"] = "dev"\nos.environ.get("MODE")\nprint(os.environ["MODE"])',
    'import logging, os\nlogger = logging.getLogger()\nlogger.info("done", extra={"key": os.environ["K"]})',
    'from logging import getLogger\nimport os\nevents = getLogger(os.environ["LOGGER"])',
    'import os\nprint(round(2.5, int(os.getenv("DIGITS"))))',  # a number's second argument
    'import os\nfrom collections import defaultdict\nstatus = defaultdict(str)\nstatus["A"] = os.getenv("A")\n'
    "print(list(status))",
    'import os\nenv = {k: v for k, v in os.environ.items() if k.startswith("MY_")}\nprint(sorted(env))',
    'import os\nenv = {k: v for k, v in os.environ.items() if k.startswith("MY_")}\n'
    "print(list(env.keys()))",
    'import os\nvals = dict((n, os.getenv(n)) for n in ["A"])\n'
    "print({n: v is not None for n, v in vals.items()})",
    "import os\nfor i, (k, v) in enumerate(os.environ.items()):\n    print(i, k)",
    'import os\nimport pandas as pd\ns = pd.Series({"K": os.getenv("K")})\nfor label, value in s.items():\n'
    "    print(label)",
    # set operations on os.environ.keys() give names
    'import os\nrequired = {"A", "B"}\nmissing = required - os.environ.keys()\n'
    'print(f"Missing: {sorted(missing)}")',
    'import os\nprint(os.environ.keys() & {"OPENAI_API_KEY"})',
    'import os\npresent = {"OPENAI_API_KEY"} & os.environ.keys()\nprint(present)',
    'import os\nprint(sorted(os.environ.keys() - {"PATH"}))',
    'import os\nprint(os.environ.keys() ^ {"PATH"})',
    'import os\nprint(os.environ.keys() | {"X"})',
    # the name part of a .env line
    'for line in open(".env"):\n    print(line.split("=")[0])',
    'for line in open(".env"):\n    print(line.partition("=")[0])',
    'names = [line.split("=", 1)[0] for line in open(".env") if "=" in line]\nprint(names)',
    'from pathlib import Path\nprint(Path(".env").read_text().split("=")[0])',
    # facts about a frame or a result, and its names
    "import os\nimport pandas as pd\nenv = pd.Series(dict(os.environ))\nprint(env.index.tolist())",
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items(), columns=['name', 'value'])\n"
    "print(env.shape, env.columns, env.size, env.ndim, env.empty)",
    "import os\nimport pandas as pd\nenv = pd.Series(dict(os.environ))\nprint(env.dtype, env.name, env.nbytes)",
    "import os\nimport pandas as pd\nenv = pd.DataFrame(os.environ.items())\nprint(env.dtypes, env.names)",
    "import subprocess\nr = subprocess.run(['env'], capture_output=True)\nprint(r.returncode, r.args)",
    "import subprocess\np = subprocess.Popen(['env'], stdout=subprocess.PIPE)\nprint(p.pid)",
    # env vars the cell set to literals
    'import os\nos.environ["PORT"] = 8080\nprint(os.environ["PORT"])',
    'import os\nos.environ["MODE"] = f"dev"\nprint(os.environ["MODE"])',
    'import os\nos.environ.update(MODE="dev")\nprint(os.environ["MODE"])',
    'import os\nos.environ.update({"MODE": "dev"})\nprint(os.environ["MODE"])',
    # functions that show nothing, or only a fact
    'import os\ndef check(v):\n    return v is not None\nprint(check(os.environ["K"]))',
    'import os\ndef show(v):\n    print(len(v))\nshow(os.environ["K"])',
    'import os\ndef show(v, label):\n    print(label)\nshow(os.environ["K"], "key")',
    'import os\ndef show(v):\n    print(v)\nshow(*[os.environ["K"]])',  # *args: not followed (Known gaps)
    # loggers and consoles that aren't
    'import os\nkey = os.getenv("K")\ndf.info(key)',
    'import os\nkey = os.getenv("K")\ncatalog.info(key)',
    'import logging, os\nlogger = logging.getLogger()\nlogger.log(os.environ["K"], "x")',
    'from logging import log as emit\nimport os\nemit(os.environ["K"], "x")',
    'from math import log\nimport os\nlog(float(os.environ["K"]))',
    # other names that are no shell call, no env read and no sink
    'client().system("printenv K")',
    "import sys\nfor line in sys.stdin:\n    print(line)",
    'import os\nprint(", ".join(os.environ))',
    'import os\nkey = os.getenv("K")\nclient.set_api_key(key)\nprint(client)',
    'import os\nkey = os.getenv("K")\nwith open("out.txt", "w") as f:\n    f.write(key)',
    "import os\nfor k in dict(os.environ.items()):\n    print(k)",
    'from pathlib import Path\nprint(Path("..").joinpath("data.csv").read_text())',
]


@pytest.mark.parametrize("code", L011_SOURCES)
def test_l011_every_source(code: str) -> None:
    secret_error(code)


@pytest.mark.parametrize("code", L011_SINKS)
def test_l011_every_sink(code: str) -> None:
    secret_error(code)


@pytest.mark.parametrize("code", L011_BRANCHES)
def test_l011_every_branch(code: str) -> None:
    secret_error(code)


@pytest.mark.parametrize("code", L011_MAGICS)
def test_l011_every_magic(code: str) -> None:
    secret_error(code)


@pytest.mark.parametrize("code", L011_QUIET)
def test_l011_magics_that_show_no_value(code: str) -> None:
    assert secret_hits(code) == []


@pytest.mark.parametrize(
    ("code", "above", "flagged"),
    [
        ("!echo $DATA_PATH", None, True),  # unknown: an upper-case name is an env var
        ("!echo $DATA_PATH", {"DATA_PATH"}, False),  # a cell above defines it in Python
        ("!echo $DATA_PATH", {"*"}, True),  # a star import: nothing known for sure
        ("!echo $DATA_PATH", {"*", "DATA_PATH"}, False),
        ("!printf '%s files' $N_FILES", {"N_FILES"}, False),
        ("!echo $OPENAI_API_KEY", {"OPENAI_API_KEY"}, True),  # a secret's name: still L011
        ("%%bash\necho $DATA_PATH", {"DATA_PATH"}, True),  # a %%bash body isn't expanded
        ("import os\nos.system('echo $DATA_PATH')", {"DATA_PATH"}, True),  # nor os.system
        ("get_ipython().system('echo $DATA_PATH')", {"DATA_PATH"}, False),
    ],
)
def test_l011_names_above_and_shell_locals(code: str, above: set[str] | None, flagged: bool):
    assert bool(secret_hits(code, names_above=above)) is flagged


@pytest.mark.parametrize("code", L011_TAINT)
def test_l011_taint_through_names_and_formatting(code: str) -> None:
    secret_error(code)


@pytest.mark.parametrize("code", L011_EXEMPT)
def test_l011_exemptions(code: str) -> None:
    assert secret_hits(code) == []
    assert "L014" not in rules(lint(code), "hint")


def test_l011_multi_line_cell_quotes_the_first_sink_and_counts_the_rest() -> None:
    code = "\n".join(
        [
            "import os",
            "",
            "import pandas as pd",
            "",
            'api_key = os.environ.get("OPENAI_API_KEY")',
            "model = 'gpt-5'",
            "df = pd.read_csv('sales.csv')",
            "print(df.shape)",
            'print(f"key: {api_key}")',
            "%env OPENAI_API_KEY",
            "df.head()",
        ]
    )
    issue = secret_error(code)
    assert issue.message == (
        '`print(f"key: {api_key}")` would show `api_key`, which holds the value of env var '
        "`OPENAI_API_KEY` (+1 more)."
    )


@pytest.mark.parametrize(
    ("code", "message", "fix"),
    [
        (
            'import os\nprint(os.environ["OPENAI_API_KEY"])',
            '`print(os.environ["OPENAI_API_KEY"])` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nkey = os.getenv("OPENAI_API_KEY")\nkey',
            "The last line would show `key`, which holds the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nos.getenv("OPENAI_API_KEY")',
            'The last line `os.getenv("OPENAI_API_KEY")` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nprint(os.getenv("A") and os.getenv("B"))',
            '`print(os.getenv("A") and os.getenv("B"))` would show the value of env var `B`.',
            'Check it without showing the value, e.g. `print("B" in os.environ)` or '
            '`print(bool(os.getenv("B")))`.',
        ),
        (
            'import os\nprint(os.getenv(key="OPENAI_API_KEY"))',
            '`print(os.getenv(key="OPENAI_API_KEY"))` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\n%timeit -n 1 print(os.getenv("OPENAI_API_KEY"))',
            '`print(os.getenv("OPENAI_API_KEY"))` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            "%env",
            "`%env` would show every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
        (
            "import os\nprint(os.environ.keys())",
            "`print(os.environ.keys())` would show every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
        (
            "import os\nenv = dict(os.environ)\nenv",
            "The last line would show `env`, which holds every env var's value.",
            "Show only the names, e.g. `sorted(env)`, or check one without its value, "
            'e.g. `print("NAME" in env)`.',
        ),
        (
            "!cat .env",
            "`!cat .env` would show every value in `.env`.",
            "Show only the names, e.g. `sorted(dotenv_values())`, or check one without its "
            'value, e.g. `print("NAME" in dotenv_values())`.',
        ),
        (
            "from dotenv import dotenv_values\nconfig = dotenv_values()\nprint(config.values())",
            "`print(config.values())` would show every value in `.env`.",
            "Show only the names, e.g. `sorted(config)`, or check one without its value, "
            'e.g. `print("NAME" in config)`.',
        ),
        (
            "from dotenv import dotenv_values\nconfig = dotenv_values()\nfor k in config:\n"
            "    value = config[k]\n    print(value)",
            "`print(value)` would show `value`, which holds a value from `.env`.",
            "Check it without showing the value, e.g. `print(value is not None)` or "
            "`print(bool(value))`.",
        ),
        (
            'from dotenv import dotenv_values\nenv_values = dotenv_values("../.env")\n'
            'print(env_values["DATA_URL"])',
            '`print(env_values["DATA_URL"])` would show the value of `DATA_URL` from `.env`.',
            'Check it without showing the value, e.g. `print("DATA_URL" in env_values)` or '
            '`print(bool(env_values.get("DATA_URL")))`.',
        ),
        (
            'from dotenv import dotenv_values\nprint(dotenv_values()["DATA_URL"])',
            '`print(dotenv_values()["DATA_URL"])` would show the value of `DATA_URL` from `.env`.',
            'Check it without showing the value, e.g. `print("DATA_URL" in dotenv_values())` or '
            '`print(bool(dotenv_values().get("DATA_URL")))`.',
        ),
        (
            "import os\nfor name in names:\n    value = os.environ[name]\n    print(value)",
            "`print(value)` would show `value`, which holds an env var's value.",
            "Check it without showing the value, e.g. `print(value is not None)` or "
            "`print(bool(value))`.",
        ),
        (
            "import os\nprint(os.environ[name])",
            "`print(os.environ[name])` would show an env var's value.",
            'Check it without showing the value, e.g. `print("NAME" in os.environ)` or '
            '`print(bool(os.getenv("NAME")))`.',
        ),
        # a variable of values gets the names of what they came from, never sorted(values)
        (
            'from pathlib import Path\nENV_FILE = Path("../.env")\n'
            "env_lines = ENV_FILE.read_text().splitlines()\nprint(env_lines)",
            "`print(env_lines)` would show `env_lines`, which holds every value in `.env`.",
            "Show only the names, e.g. `sorted(dotenv_values())`, or check one without its "
            'value, e.g. `print("NAME" in dotenv_values())`.',
        ),
        (
            "import os\nvalues = list(os.environ.values())\nprint(values)",
            "`print(values)` would show `values`, which holds every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
        (
            'import os\nmerged = {}\nmerged.update(os.environ)\nprint(merged["OPENAI_API_KEY"])',
            '`print(merged["OPENAI_API_KEY"])` would show the value of env var `OPENAI_API_KEY`.',
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nprint({**os.environ}["OPENAI_API_KEY"])',
            '`print({**os.environ}["OPENAI_API_KEY"])` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            "!printenv OPENAI_API_KEY",
            "`!printenv OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nkey = os.getenv("K")\ndef show():\n    print(key)\nshow()',
            "`print(key)` would show `key`, which holds the value of env var `K`.",
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'import os\ndef f():\n    if x:\n        return os.environ["A"]\n    return os.environ["B"]\n'
            "print(f())",
            "`print(f())` would show the value of env var `A`.",
            'Check it without showing the value, e.g. `print("A" in os.environ)` or '
            '`print(bool(os.getenv("A")))`.',
        ),
        (
            'import os\nprint(os.path.expandvars("${OPENAI_API_KEY}"))',
            '`print(os.path.expandvars("${OPENAI_API_KEY}"))` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nprint(os.path.expandvars("$OPENAI_API_KEY"))',
            '`print(os.path.expandvars("$OPENAI_API_KEY"))` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nrows = [os.getenv("A")]\nrows[0] = os.getenv("B")\nprint(rows)',
            "`print(rows)` would show `rows`, which holds the value of env var `B`.",
            'Check it without showing the value, e.g. `print("B" in os.environ)` or '
            '`print(bool(os.getenv("B")))`.',
        ),
        (  # a list holding the environ is no mapping: sorted(lst) would name nothing
            "import os\nlst = []\nlst.append(os.environ)\nprint(lst)",
            "`print(lst)` would show `lst`, which holds every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
        (  # a copy's view still names the dict it came from
            "import os\nd = dict(os.environ)\nprint(d.copy().items())",
            "`print(d.copy().items())` would show every env var's value.",
            'Show only the names, e.g. `sorted(d)`, or check one without its value, e.g. `print("NAME" in d)`.',
        ),
        (
            "!printenv OPENAI_API_KEY\n!echo done",  # the next magic line is no continuation
            "`!printenv OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            "%%bash\necho start\nprintenv OPENAI_API_KEY",  # a shell cell quotes its line
            "`printenv OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            "!printenv -0 OPENAI_API_KEY",  # an option is no name
            "`!printenv -0 OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            "!declare -p OPENAI_API_KEY",
            "`!declare -p OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (  # the first spread names the mapping
            "import os\nfrom dotenv import dotenv_values\nprint({**os.environ, **dotenv_values()})",
            "`print({**os.environ, **dotenv_values()})` would show every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
        # a loop body walked twice reports each sink once
        (
            'import os\nprev = None\nfor k in ["A"]:\n    !printenv OPENAI_API_KEY\n'
            "    prev = os.environ[k]",
            "`!printenv OPENAI_API_KEY` would show the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nprev = None\nfor k in ["A"]:\n    %timeit print(os.environ["K"])\n'
            "    prev = os.environ[k]",
            '`print(os.environ["K"])` would show the value of env var `K`.',
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'import os\nprev = None\nfor k in ["A"]:\n    !printenv K\n    prev = os.environ[k]',
            "`!printenv K` would show the value of env var `K`.",
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        # a dict the cell built: its names, not the env's
        (
            'import os\nsettings = {"key": os.getenv("K")}\nprint(settings)',
            "`print(settings)` would show `settings`, which holds the value of env var `K`.",
            "Show only the names, e.g. `sorted(settings)`, or check one without its value, "
            'e.g. `print("NAME" in settings)`.',
        ),
        (
            'import os\nsettings = {"key": os.getenv("K")}\nprint(settings["key"])',
            '`print(settings["key"])` would show the value of env var `K`.',
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        # a sink over several lines is quoted on one
        (
            'import os\nprint(\n    os.environ["OPENAI_API_KEY"]\n)',
            '`print(os.environ["OPENAI_API_KEY"])` would show the value of env var '
            "`OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        (
            'import os\nkey = os.getenv("OPENAI_API_KEY")\nprint(key[:4])',
            "`print(key[:4])` would show `key`, which holds the value of env var `OPENAI_API_KEY`.",
            'Check it without showing the value, e.g. `print("OPENAI_API_KEY" in os.environ)` or '
            '`print(bool(os.getenv("OPENAI_API_KEY")))`.',
        ),
        # a key that is no identifier names no env var
        (
            'import os\nprint(os.environ["MY-KEY"])',
            '`print(os.environ["MY-KEY"])` would show an env var\'s value.',
            'Check it without showing the value, e.g. `print("NAME" in os.environ)` or '
            '`print(bool(os.getenv("NAME")))`.',
        ),
        (
            'import os\nprint(os.getenvb(b"K"))',
            '`print(os.getenvb(b"K"))` would show an env var\'s value.',
            'Check it without showing the value, e.g. `print("NAME" in os.environ)` or '
            '`print(bool(os.getenv("NAME")))`.',
        ),
        (
            'import os\na, b = os.getenv("A"), os.getenv("B")\nprint(b)',
            "`print(b)` would show `b`, which holds the value of env var `B`.",
            'Check it without showing the value, e.g. `print("B" in os.environ)` or '
            '`print(bool(os.getenv("B")))`.',
        ),
        # one hit per call: no "(+1 more)"
        (
            'import os\nos.system("printenv K")',
            '`os.system("printenv K")` would show the value of env var `K`.',
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'print(get_ipython().getoutput("printenv K"))',
            '`print(get_ipython().getoutput("printenv K"))` would show the value of env var `K`.',
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        # a call to a function the cell defined, IPython's help, %whos
        (
            'import os\ndef show(v):\n    print(v)\nshow(os.environ["K"])',
            '`show(os.environ["K"])` would show the value of env var `K`.',
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'import os\ndef report():\n    print(key)\nkey = os.getenv("K")\nreport()',
            "`report()` would show `key`, which holds the value of env var `K`.",
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'import os\nkey = os.getenv("K")\nkey?',
            "`key?` would show `key`, which holds the value of env var `K`.",
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            'import os\nkey = os.getenv("K")\n%whos',
            "`%whos` would show `key`, which holds the value of env var `K`.",
            'Check it without showing the value, e.g. `print("K" in os.environ)` or '
            '`print(bool(os.getenv("K")))`.',
        ),
        (
            "!cat /proc/self/environ",
            "`!cat /proc/self/environ` would show every env var's value.",
            "Show only the names, e.g. `sorted(os.environ)`, or check one without its value, "
            'e.g. `print("NAME" in os.environ)`.',
        ),
    ],
)
def test_l011_messages(code: str, message: str, fix: str) -> None:
    issue = secret_error(code)
    assert (issue.message, issue.fix) == (message, fix)


def test_l011_names_the_env_var_never_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "sk-" + "live-VALUE-0123456789abcdef"  # fake
    monkeypatch.setenv("OPENAI_API_KEY", value)
    for code in (
        'import os\nprint(os.environ["OPENAI_API_KEY"])',
        "%env OPENAI_API_KEY",
        "!echo $OPENAI_API_KEY",
        'import os\nkey = os.getenv("OPENAI_API_KEY")\nkey',
        "import os\nos.environ",
    ):
        issue = secret_error(code)
        text = f"{issue.message} {issue.fix}"
        assert value not in text and value[:8] not in text and value[-6:] not in text
        assert "OPENAI_API_KEY" in text or "every env var" in text


def test_l011_is_an_error_by_default_and_blocks_the_write() -> None:
    report = lint('import os\nprint(os.getenv("OPENAI_API_KEY"))')
    assert not report.ok and rules(report) == ["L011"]
    assert load(None).rule("secret_print") == "error"


def test_l011_comes_after_the_other_hard_rules_in_order() -> None:
    code = '!pip install openai\nimport os\nprint(os.getenv("OPENAI_API_KEY"))'
    report = lint(code, cfg=config(package_install="error"))
    assert rules(report) == ["L009", "L011"]
    default = lint(code)  # L009 is an ask by default (design §6.4): the error refuses first
    assert rules(default) == ["L011"] and rules(default, "ask") == ["L009"]
    order = [rule for rule, _, _ in lint_module._CHECKS]
    assert order.index("L010") + 1 == order.index("L011") < order.index("L014")


def test_l011_scan_knows_the_same_python_cell_magics() -> None:
    assert secret_scan._PYTHON_CELL_MAGICS == lint_module.PYTHON_CELL_MAGICS


def test_l011_a_scan_failure_drops_only_the_secret_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any) -> Any:
        raise RecursionError("deep")

    monkeypatch.setattr(secret_scan, "scan", boom)
    code = '!pip install x\nimport os\nprint(os.getenv("K"))'
    report = lint(code, cfg=config(package_install="error"))
    assert rules(report) == ["L009"]
    assert rules(lint(code)) == [] and rules(lint(code), "ask") == ["L009"]


def test_l011_loop_walk_is_two_passes() -> None:
    """Taint carried round a loop by one assignment is seen on the second pass; through two
    assignments it would need a third, which the walk doesn't make (a known gap, §6.7)."""
    one_hop = 'import os\nprev = None\nfor k in ["A"]:\n    print(prev)\n    prev = os.environ[k]'
    assert secret_hits(one_hop)
    two_hops = (
        'import os\nb = a = None\nfor k in ["A"]:\n    print(b)\n    b = a\n    a = os.environ[k]'
    )
    assert secret_hits(two_hops) == []


def _walked(code: str) -> int:
    """How many statements the scan walks for ``code``."""
    count = 0
    original = secret_scan._Walker.stmt

    def counting(self: Any, node: ast.stmt, definite: bool) -> None:
        nonlocal count
        count += 1
        original(self, node, definite)

    secret_scan._Walker.stmt = counting  # type: ignore[method-assign]
    try:
        secret_scan.scan(*_scan_args(code))
    finally:
        secret_scan._Walker.stmt = original  # type: ignore[method-assign]
    return count


def _nested_loops(depth: int) -> str:
    """Loops nested ``depth`` deep, each body tainting a new name: every level changes the taint
    on its first pass, the worst case for a walk that repeats a loop body."""
    return "import os\nkey = os.getenv('K')\n" + "".join(
        "    " * i + f"for i{i} in range(2):\n" + "    " * (i + 1) + f"v{i} = key\n"
        for i in range(depth)
    )


def _evaluated(code: str) -> int:
    """How many expressions the scan evaluates for ``code`` (a cached one counts too)."""
    count = 0
    original = secret_scan._Walker.eval

    def counting(self: Any, node: Any) -> Any:
        nonlocal count
        count += 1
        return original(self, node)

    secret_scan._Walker.eval = counting  # type: ignore[method-assign]
    try:
        secret_scan.scan(*_scan_args(code))
    finally:
        secret_scan._Walker.eval = original  # type: ignore[method-assign]
    return count


def _nested(depth: int, shape: str) -> str:
    """``key`` wrapped ``depth`` times in ``shape`` (its ``{}``): a sink that evaluated its
    arguments again would cost 2^depth."""
    expression = "key"
    for _ in range(depth):
        expression = shape.format(expression)
    return f"import os\nkey = os.getenv('K')\nx = {expression}"


def test_l011_scan_cost_is_linear() -> None:
    """Counted, not timed: a wide cell walks each statement once, nested loops cost a small
    polynomial in the depth, never 2^depth, and so do nested expressions."""
    wide = "import os\n" + "\n".join(
        f"x{i} = os.getenv('K{i}')\nprint(len(x{i}))" for i in range(400)
    )
    assert _walked(wide) == 801
    shallow, deep = _walked(_nested_loops(10)), _walked(_nested_loops(20))
    assert deep < 5 * shallow, (shallow, deep)  # quadratic at most; 2^depth would be 1024x
    shapes = [
        'print("x", end={})',  # a sink's keyword, evaluated once
        'print("x", sep={})',
        'dict(x=print("x", end={}))',
        "str(repr({}))",
        "({} + 1)",
        "{}[0]",
        '"%s" % ({},)',
        "[{}, 1]",
        "show({})",
        "{} or None",
    ]
    for shape in shapes:
        shallow, deep = _evaluated(_nested(10, shape)), _evaluated(_nested(20, shape))
        assert deep < 3 * shallow, (shape, shallow, deep)


def _scan_args(code: str) -> tuple[Any, list[str], Any]:
    """What lint_cell hands the scan for a Python cell."""
    source = "\n".join(lines_of(code))
    masked = mask(source)
    return masked, lines_of(source), ast.parse(masked.text)


FIRST_CELL = PLUGIN / "skills" / "init" / "reference" / "first-cell.md"


def _first_cell_blocks() -> list[str]:
    blocks = re.findall(r"```python\n(.*?)```", FIRST_CELL.read_text("utf-8"), flags=re.S)
    assert len(blocks) == 2 and "DATA_URL" in blocks[1]
    return blocks


def test_l011_first_cell_reference_lints_clean() -> None:
    for block in _first_cell_blocks():
        report = lint(block, kernel_python=(3, 11))
        assert report.errors == [], [(e.rule, e.message) for e in report.errors]
        assert {"L011", "L014"}.isdisjoint(rules(report, "hint"))
        assert secret_scan.scan(*_scan_args(block)).hits == []


def _check_fix(name: str, values: str) -> str:
    return (
        f'Check it without showing the value, e.g. `print("{name}" in {values})` or '
        f'`print(bool({values}.get("{name}")))`.'
    )


NAMES_FIX = (
    "Show only the names, e.g. `sorted(env_values)`, or check one without its value, "
    'e.g. `print("NAME" in env_values)`.'
)
DOTENV_FIX = (
    "Show only the names, e.g. `sorted(dotenv_values())`, or check one without its value, "
    'e.g. `print("NAME" in dotenv_values())`.'
)


@pytest.mark.parametrize(
    ("shown", "message", "fix"),
    [
        (
            "env_values",
            "The last line would show `env_values`, which holds every value in `.env`.",
            NAMES_FIX,
        ),
        (
            "print(env_lines)",
            "`print(env_lines)` would show `env_lines`, which holds every value in `.env`.",
            DOTENV_FIX,
        ),
        (
            "ENV_FILE.read_text()",
            "The last line `ENV_FILE.read_text()` would show every value in `.env`.",
            DOTENV_FIX,
        ),
        (
            'print(env_values["DATA_URL"])',
            '`print(env_values["DATA_URL"])` would show the value of `DATA_URL` from `.env`.',
            _check_fix("DATA_URL", "env_values"),
        ),
        (
            "for line in env_lines:\n    print(line)",
            "`print(line)` would show `line`, which holds a value from `.env`.",
            "Check it without showing the value, e.g. `print(line is not None)` or "
            "`print(bool(line))`.",
        ),
        (
            "print(DATA_URL)",
            "`print(DATA_URL)` would show `DATA_URL`, which holds the value of env var `DATA_URL`.",
            'Check it without showing the value, e.g. `print("DATA_URL" in os.environ)` or '
            '`print(bool(os.getenv("DATA_URL")))`.',
        ),
    ],
)
def test_l011_first_cell_env_read_is_followed(shown: str, message: str, fix: str) -> None:
    """The credentials cell reads `.env` in Python; showing what it read is refused, and the fix
    never suggests sorting a variable of values."""
    credentials = _first_cell_blocks()[1].rstrip("\n")
    issue = secret_error(f"{credentials}\n{shown}", kernel_python=(3, 11))
    assert (issue.message, issue.fix) == (message, fix)


# Everyday code with no env value in it, put before a leak: none of it may stop the scan.
EVERYDAY = "\n".join(
    [
        "import subprocess",
        "from math import log",
        "from string import Template",
        'names = ["a", "b"]',
        'print(", ".join(names))',
        'cmd = ["ls", "-l"]',
        "subprocess.run(cmd)",
        "result = subprocess.run(cmd, capture_output=True)",
        'Template("$x").substitute(x=1)',
        "for a, b, c in [(1, 2, 3)]:",
        "    total = a + b + c",
        "log(2)",
        "%timeit",
        "x = !echo {names}",
        "d = {}",
        'd["a"] = 1',
        'names.append("c")',
        "for i, n in enumerate(names):",
        "    pairs = list(zip(names, names))",
    ]
)


def scan_returns(code: str) -> secret_scan.Scan:
    """The scan as lint_cell runs it, with no catch-all: an exception here would silently drop
    L011 and L014 from lint_cell (a scan failure only costs the secret rules)."""
    source = "\n".join(lines_of(code))
    masked = mask(source)
    try:
        tree = ast.parse(masked.text) if masked.cell_magic is None else None
    except SyntaxError:
        tree = None  # its magic lines are still scanned
    return secret_scan.scan(masked, lines_of(source), tree)


def test_l011_a_bare_raise_shows_nothing() -> None:
    scan = scan_returns("try:\n    x\nexcept ValueError:\n    raise")
    assert (scan.hits, scan.shown) == ([], [])


def test_l011_scan_never_raises() -> None:
    cells = [
        *L011_SOURCES,
        *L011_SINKS,
        *L011_BRANCHES,
        *L011_MAGICS,
        *L011_QUIET,
        *L011_TAINT,
        *L011_EXEMPT,
        *L011_KNOWN_GAPS,
        *CORPUS,
        "",  # an empty cell has no last statement
        "# only a comment",
        "% 5",  # a % line that names no magic
        "%set_env",
        "%timeit -n",
        "list(map())",
        "list(filter())",
        'for line in open(".env"):\n    line.split()[0]',
        'import os\nclass C:\n    def f(self):\n        return os.environ["K"]',
        "!for",
        "!bash -c",
        "%timeit x = (",
        'print("".join())',
        "import os\ndef f():\n    print(os)",
        '!echo "x',
        "!echo `x",
        "!echo $(x",
        "!echo ${x",
        '!echo "$(x"',
        '!echo $(echo "x)',
        "%",
        "%%",
        "update({})\nappend(1)",  # functions named like a dict's or a list's methods
        "import subprocess\nsubprocess.run([])",
        'd = {**{"a": 1}}',
        "def f(a):\n    print(a)\nf(1, 2)",  # more arguments than parameters
        "import os\nos.path.expandvars(p)",
        "import subprocess\nout = subprocess.check_output(cmd)",
        "!echo a \\",  # a continuation with no line after it
        "key???",
        "a?b?",
    ]
    for code in cells:
        assert isinstance(scan_returns(code), secret_scan.Scan)
        assert isinstance(scan_returns(f"{EVERYDAY}\n{code}"), secret_scan.Scan)


@pytest.mark.parametrize(
    "leak",
    [
        'import os\nprint(os.environ["K"])',
        "!printenv OPENAI_API_KEY",
        'import os\nkey = os.getenv("K")\nkey',
        'import os\ndef show(v):\n    print(v)\nshow(os.environ["K"])',
    ],
)
def test_l011_everyday_code_before_a_leak_changes_nothing(leak: str) -> None:
    assert [hit.taint.var for hit in scan_returns(f"{EVERYDAY}\n{leak}").hits] == [
        hit.taint.var for hit in scan_returns(leak).hits
    ]
    secret_error(f"{EVERYDAY}\n{leak}")


# Known gaps (§6.7), pinned so a change that closes one updates the design too.
L011_KNOWN_GAPS = [
    "%%bash\ncat <<EOF\n$OPENAI_API_KEY\nEOF",
    "!python -c \"import os; print(os.environ['K'])\"",
    'import os\nf = os.environ.get\nprint(f("K"))',
    'import os\nshow = lambda v: print(v)\nshow(os.environ["K"])',
    'import os\nprint(getattr(os, "environ"))',
    'import os\nclass Box:\n    def show(self, v):\n        print(v)\nBox().show(os.environ["K"])',
    'import os\ndef show(v):\n    print(v)\nfor _ in map(show, [os.environ["K"]]):\n    pass',
    'import os\nkey = os.getenv("K")\nbox = {}\nbox.inner = key\nprint(box)',
    'import os\nrows = [{}]\nrows[0]["key"] = os.environ["K"]\nprint(rows)',
    'import os\nprint(open(os.getcwd() + "/.env").read())',
    "!set -x; true $OPENAI_API_KEY",
]


@pytest.mark.parametrize("code", L011_KNOWN_GAPS)
def test_l011_known_gaps(code: str) -> None:
    assert secret_hits(code) == []


# configurable severity ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "rule", "key"),
    [
        ("nbformat.write(nb, 'x.ipynb')", "L008", "notebook_write"),
        ("!pip install seaborn", "L009", "package_install"),
        ("%%markdown\n# Findings", "L010", "markdown_output"),
        ('import os\nprint(os.environ["OPENAI_API_KEY"])', "L011", "secret_print"),
        ("%env", "L011", "secret_print"),
        ("!printenv OPENAI_API_KEY", "L011", "secret_print"),
        ('trips = pd.read_csv("https://data.example.org/t.csv")', "L012", "network"),
        ("!curl -O https://data.example.org/t.csv", "L012", "network"),
    ],
)
def test_configurable_hard_rules(code: str, rule: str, key: str) -> None:
    assert rule in rules(lint(code, cfg=config(**{key: "hint"})), "hint")
    assert rule not in rules(lint(code, cfg=config(**{key: "hint"})))
    off = lint(code, cfg=config(**{key: "off"}))
    assert rule not in rules(off) + rules(off, "hint")
    strict = config(**{key: "hint"})
    strict.data["lint"]["mode"] = "strict"
    assert rule in rules(lint(code, cfg=strict))


# report shape -------------------------------------------------------------------------------------
def test_all_problems_reported_at_once_in_rule_order() -> None:
    args: dict[str, Any] = dict(title="a b c d e f g h i", notes=["one"], intent="")
    report = lint("# %%\n!pip install x", cfg=config(package_install="error"), **args)
    assert rules(report) == ["L002", "L003", "L004", "L005", "L009"]
    default = lint("# %%\n!pip install x", **args)  # the ask rides along with the errors
    assert rules(default) == ["L002", "L003", "L004", "L005"]
    assert rules(default, "ask") == ["L009"]


CORPUS = [
    "x = (",
    "# %%\ny = 1",
    "!pip install x",
    "nbformat.write(nb, 'x.ipynb')",
    "%%markdown\nhi",
    "for item in items:\n!ls\n",
    "Markdown('" + LONG + "')",
    "\n\n\nz = )",
    'import os\n\nkey = os.environ["OPENAI_API_KEY"]\nprint(key)',
    "%env",
    "api_key = load_key()\nprint(api_key)",
    'trips = pd.read_csv("https://analyst:pw@data.example.org/t.csv")',
    "r = requests.get(url)",
]


@pytest.mark.parametrize("code", CORPUS)
@pytest.mark.parametrize("kernel", [None, (3, 11)])
def test_messages_quote_code_not_line_numbers(code: str, kernel: Any) -> None:
    report = lint(code, kernel_python=kernel)
    found = [("error", report.errors), ("hint", report.hints), ("ask", report.asks)]
    for severity, issues in found:
        for issue in issues:  # every finding sits in the list of its own severity
            text = f"{issue.message} {issue.fix} {issue.question}"
            assert not re.search(r"\b(?:line|lines|row)\s+\d", text, re.I), text
            assert "nh-" not in text
            assert issue.severity == severity
            assert issue.fix.endswith(".")
    assert sum(len(issues) for _, issues in found) > 0
