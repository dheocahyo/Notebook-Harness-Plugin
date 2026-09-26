"""Hard lint rules (L001–L010): what rejects a cell before anything is written."""

from __future__ import annotations

import re
import sys
from typing import Any

import pytest

from nh_gateway.config import Config, load
from nh_gateway.lint import lint as lint_module
from nh_gateway.lint.lint import LintReport, lint_cell

GOOD = {
    "title": "Drop rows with missing price",
    "notes": ["Removes rows whose price is null.", "The plot needs a price for every row."],
    "intent": "remove the rows without a price",
}
OURS = sys.version_info[:2]


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
    return [i.rule for i in (report.errors if severity == "error" else report.hints)]


def test_clean_cell_passes() -> None:
    report = lint()
    assert report.ok
    assert report.errors == []
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
    report = lint(code)
    assert "L009" in rules(report)
    issue = next(i for i in report.errors if i.rule == "L009")
    assert "seaborn" in issue.message
    assert "ask the user" in issue.fix


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
    assert "L009" not in rules(lint(code))


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


# configurable severity ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "rule", "key"),
    [
        ("nbformat.write(nb, 'x.ipynb')", "L008", "notebook_write"),
        ("!pip install seaborn", "L009", "package_install"),
        ("%%markdown\n# Findings", "L010", "markdown_output"),
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
    report = lint("# %%\n!pip install x", title="a b c d e f g h i", notes=["one"], intent="")
    assert rules(report) == ["L002", "L003", "L004", "L005", "L009"]


CORPUS = [
    "x = (",
    "# %%\ny = 1",
    "!pip install x",
    "nbformat.write(nb, 'x.ipynb')",
    "%%markdown\nhi",
    "for item in items:\n!ls\n",
    "Markdown('" + LONG + "')",
    "\n\n\nz = )",
]


@pytest.mark.parametrize("code", CORPUS)
@pytest.mark.parametrize("kernel", [None, (3, 11)])
def test_messages_quote_code_not_line_numbers(code: str, kernel: Any) -> None:
    report = lint(code, kernel_python=kernel)
    for issue in report.errors + report.hints:
        text = f"{issue.message} {issue.fix}"
        assert not re.search(r"\b(?:line|lines|row)\s+\d", text, re.I), text
        assert "nh-" not in text
        assert issue.severity in ("error", "hint")
        assert issue.fix.endswith(".")
