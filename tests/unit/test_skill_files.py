"""Skills and the eval suite: frontmatter, pre-approved tools, tool names and the eval fixture."""

from __future__ import annotations

import contextlib
import csv
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import nbformat
import pytest

from nh_gateway import config, meta
from nh_gateway._shared.stamp_spec import TOOL_PREFIX
from nh_gateway._shared.tool_defaults import TOOL_DEFAULTS, WRITE_TOOLS
from nh_gateway.policy.errors import CATALOGUE

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"
SKILLS = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
EVALS = PLUGIN / "evals"
CASES = sorted(p.parent for p in EVALS.glob("*/prompt.md"))
NH_TOOLS = set(TOOL_DEFAULTS)
NOT_TOOLS = {"nh_enabled"}  # `nhctl doctor --json` field named in the init skill

ADD_CELL_EXPECT = {
    "title": r"/^\W*(?:\S+\s+){0,7}\S+\W*$/",
    "intent": r"/\S/",
    "code": "string",
}
# Keys `claude plugin eval` accepts (code.claude.com/docs/en/plugin-evals); it rejects unknown ones.
PROMPT_KEYS = {
    "schema_version",
    "name",
    "description",
    "tags",
    "plugins",
    "runs",
    "expected_outcome",
    "model",
    "max_turns",
    "timeout_seconds",
    "allowed_tools",
    "append_system_prompt",
    "env",
}
GRADER_KEYS = {
    "regex": {"pattern", "flags", "match", "target"},
    "tool_used": {"tool", "input_match", "min", "max"},
    "tool_order": {"before", "after"},
    "file_exists": {"path", "exists"},
    "llm": {"criteria", "focus"},
    "baseline": {"baseline_file", "criteria"},
}
MOCK_KEYS = {"type", "expect", "error", "tools", "abort_when"}


def _scalar(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _mapping(lines: list[str]) -> dict:
    """The YAML subset these files use: scalars, flow and block lists, nested maps, folded text."""
    data: dict = {}
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        key, _, rest = lines[i].partition(":")
        child: list[str] = []
        i += 1
        while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
            child.append(lines[i])
            i += 1
        rest = rest.strip()
        items = [line.strip() for line in child if line.strip()]
        if rest in (">-", ">", "|"):
            data[key.strip()] = " ".join(items)
        elif rest:
            data[key.strip()] = _scalar(rest)
        elif items and all(item.startswith("- ") for item in items):
            data[key.strip()] = [_scalar(item[2:]) for item in items]
        else:
            data[key.strip()] = _mapping(textwrap.dedent("\n".join(child)).splitlines())
    return data


def split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path}: frontmatter is not closed"
    return _mapping(head.splitlines()), body


def is_model_invoked(front: dict) -> bool:
    return front.get("disable-model-invocation") is not True


def test_every_skill_has_name_and_short_description():
    assert {path.parent.name for path in SKILLS} == {"notebook", "init", "status"}
    for path in SKILLS:
        front, body = split_frontmatter(path)
        assert front["name"] == path.parent.name
        assert 0 < len(front["description"]) + len(front.get("when_to_use", "")) <= 1536
        assert body.strip()


def test_notebook_skill_is_short_and_preapproves_only_inspect():
    path = PLUGIN / "skills" / "notebook" / "SKILL.md"
    front, _ = split_frontmatter(path)
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 150
    assert front["allowed-tools"] == [TOOL_PREFIX + "nh_inspect"]
    assert is_model_invoked(front) and "paths" not in front


def test_model_invoked_skills_preapprove_no_nh_write_tool_or_settings():
    for path in SKILLS:
        front, _ = split_frontmatter(path)
        rules = front.get("allowed-tools", [])
        for rule in rules:
            bash = re.fullmatch(r"Bash\((.*)\)", rule)
            # A Bash rule that would match `nhctl settings` also turns off the prompt for the deny rule.
            assert not (
                bash and fnmatch.fnmatchcase("nhctl settings apply --yes --json", bash.group(1))
            ), rule
        if is_model_invoked(front):
            assert not [rule for rule in rules if any(tool in rule for tool in WRITE_TOOLS)], path


def test_init_skill_follows_the_plan():
    front, body = split_frontmatter(PLUGIN / "skills" / "init" / "SKILL.md")
    assert front["disable-model-invocation"] is True
    assert front["allowed-tools"] == [
        "Bash(nhctl doctor *)",
        "Bash(nhctl scaffold *)",
        "Bash(nhctl env *)",
        "Bash(nhctl lab *)",
        "Bash(nhctl runtime *)",
        TOOL_PREFIX + "nh_inspect",
        TOOL_PREFIX + "nh_add_cell",
    ]
    for call in re.findall(r"`(nhctl runtime [^`]*)`", body):
        assert '--plugin-data "${CLAUDE_PLUGIN_DATA}"' in call
    assert (
        "select:" + ",".join(TOOL_PREFIX + t for t in ("nh_inspect", "nh_add_cell", "nh_edit_cell"))
        in body
    )
    assert "AskUserQuestion" in body and "600000" in body


def saved_tools() -> dict[str, dict]:
    listing = json.loads((EVALS / "mocks" / "nh" / "_tools.json").read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in listing["tools"]}


def text_files() -> list[Path]:
    roots = [PLUGIN / "skills", EVALS, PLUGIN / "agents", PLUGIN / "workflows"]
    return [
        p
        for root in roots
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".md", ".yaml", ".json", ".sh", ".js"}
    ]


def test_text_files_cover_the_agents_and_the_workflow():
    names = {p.relative_to(PLUGIN).as_posix() for p in text_files()}
    assert {"agents/cell-writer.md", "agents/cell-qa.md", "workflows/qa-cell.js"} <= names


def test_every_tool_name_mentioned_exists():
    for path in text_files():
        text = path.read_text(encoding="utf-8")
        assert set(re.findall(TOOL_PREFIX + r"(\w+)", text)) <= NH_TOOLS, path
        bare = set(re.findall(r"\b(nh_[a-z_]+)\b", text)) - NOT_TOOLS
        assert bare <= NH_TOOLS, (path, bare - NH_TOOLS)
    for mock in EVALS.rglob("mocks/nh/*.md"):
        assert mock.stem in NH_TOOLS, mock


def test_saved_tool_list_matches_the_tool_defaults():
    tools = saved_tools()
    assert set(tools) == NH_TOOLS
    for name, defaults in TOOL_DEFAULTS.items():
        properties = tools[name]["inputSchema"]["properties"]
        assert set(defaults) <= set(properties), name
        for key, value in defaults.items():
            assert properties[key].get("default") == value, (name, key)


def test_mocks_use_known_keys_and_the_planned_expect_guard():
    tools = saved_tools()
    for mock in EVALS.rglob("mocks/nh/*.md"):
        front, body = split_frontmatter(mock)
        assert set(front) <= MOCK_KEYS and body.strip(), mock
        expect = front.get("expect", {})
        assert set(expect) <= set(tools[mock.stem]["inputSchema"]["properties"]), mock
        if mock.stem == "nh_add_cell":
            assert expect == ADD_CELL_EXPECT, mock
    title = re.compile(ADD_CELL_EXPECT["title"][1:-1])
    assert title.match("Load raw data and check schema")
    assert not title.match("one two three four five six seven eight nine")


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_eval_case_files(case: Path):
    front, prompt = split_frontmatter(case / "prompt.md")
    assert set(front) <= PROMPT_KEYS and prompt.strip()
    assert [(case / p).resolve() for p in front["plugins"]] == [PLUGIN.resolve()]
    case_yaml = _mapping((case / "case.yaml").read_text(encoding="utf-8").splitlines())
    assert case_yaml["schema_version"] == "1.1" and case_yaml["name"] == case.name
    script = case / case_yaml["context"]["scaffold_script"]
    assert script.parent == case and script.is_file()
    graders = sorted((case / "graders").glob("*.md"))
    assert graders
    for grader in graders:
        spec, criteria = split_frontmatter(grader)
        kind = spec.pop("type")
        assert set(spec) - {"weight", "arm"} <= GRADER_KEYS[kind], grader
        if kind == "llm":
            assert "PASS" in criteria and "FAIL" in criteria
        if kind == "regex":
            re.compile(spec["pattern"])
        for tool in (spec.get("tool"), spec.get("before"), spec.get("after")):
            if tool and tool.startswith(TOOL_PREFIX):
                assert tool[len(TOOL_PREFIX) :] in NH_TOOLS, grader


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("case,cells", [("one-cell-per-turn", 3), ("undo-last", 5)])
def test_eval_scaffold_builds_a_consistent_project(tmp_path: Path, case: str, cells: int):
    subprocess.run(
        ["bash", str(EVALS / case / "scaffold.sh")],
        cwd=tmp_path,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    cfg = config.load(tmp_path)
    assert cfg.problems == [] and cfg["approval"]["approve_before_run"] is False
    with open(tmp_path / cfg["project"]["data_source"], newline="") as handle:
        rows = list(csv.DictReader(handle))
    priced = [row for row in rows if row["price"]]
    assert (len(rows), len(priced)) == (43, 37)  # the numbers the mocks report
    notebook = nbformat.read(tmp_path / cfg["project"]["notebook"], as_version=4)
    nbformat.validate(notebook)
    assert len(notebook.cells) == cells and "anaylsis" in notebook.cells[0].source
    for cell in notebook.cells:
        if meta.is_agent_code(cell.metadata):
            assert meta.nh_meta(cell.metadata)["source_sha"] == meta.source_sha(cell.source)
    assert (tmp_path / ".nh" / "state" / "last_cell.json").exists() == (case == "undo-last")
    assert "37" in (EVALS / "mocks" / "nh" / "nh_add_cell.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ eval mocks vs the real gateway
#
# Each mock's body must look like what the gateway really returns for the same call on the eval
# fixture. `run_mock_scenario` replays the call with FakeBackend and the real hooks. To refresh a
# mock, print `await run_mock_scenario("<mock path>")` and paste it under the mock's frontmatter,
# putting back `{{input.title}}` / `{{input.cell_id}}` where the mock echoes the call.

FIXTURE_LOADER = "nh-3b8f2a61c0"
DROP_CODE = 'df_clean = df.dropna(subset=["price"])\nprint(f"rows: {len(df)} -> {len(df_clean)}")\ndf_clean.head()'
DROP = {
    "title": "Drop rows with missing price",
    "notes": [
        "Keeps only the orders that have a price, in a new frame df_clean",
        "Filling 6 of 43 prices would invent 14% of the data, so they are dropped instead",
    ],
    "intent": "drop the rows where price is missing",
    "code": DROP_CODE,
}
DATES = {
    "title": "Count orders per month",
    "notes": [
        "Parses order_date as dates, in a new series order_dates",
        "Counts the orders in each calendar month, oldest first",
    ],
    "intent": "parse order_date as dates and count the orders per month",
    "code": 'order_dates = pd.to_datetime(df["order_date"])\n'
    'orders_per_month = order_dates.dt.to_period("M").value_counts().sort_index()\n'
    "orders_per_month",
}
DATES_FIXED = (
    'order_dates = pd.to_datetime(df["order_date"], errors="coerce")\n'
    'print("unparsed dates:", df.loc[order_dates.isna(), "order_date"].tolist())\n'
    'orders_per_month = order_dates.dt.to_period("M").value_counts().sort_index()\n'
    "orders_per_month"
)
HEATMAP = {
    "title": "Correlation heatmap of numeric columns",
    "notes": [
        "Correlates order_id, units and price, the numeric columns of df",
        "The heatmap colours each pair from -1 to 1, so strong links stand out",
    ],
    "intent": "correlation matrix of the numeric columns as a heatmap",
    "code": "import matplotlib.pyplot as plt\n\n"
    'corr_matrix = df.select_dtypes("number").corr().round(2)\n\n'
    "fig, ax = plt.subplots(figsize=(6, 5))\n"
    'heatmap = ax.imshow(corr_matrix, cmap="RdBu_r", vmin=-1, vmax=1)\n'
    "ax.set_xticks(range(len(corr_matrix.columns)), corr_matrix.columns)\n"
    "ax.set_yticks(range(len(corr_matrix.columns)), corr_matrix.columns)\n"
    "fig.colorbar(heatmap, ax=ax)\n"
    'ax.set_title("Correlation between numeric columns")\n'
    "corr_matrix",
}
# mock path (relative to evals/) -> (fixture env, calls); "$cell" is the id the previous call returned
MOCK_SCENARIOS: dict[str, tuple[dict[str, str], list[tuple[str, str, dict[str, Any]]]]] = {
    "mocks/nh/nh_add_cell.md": ({}, [("p1", "nh_add_cell", DROP)]),
    "mocks/nh/nh_edit_cell.md": (
        {},
        [
            (
                "p1",
                "nh_add_cell",
                {**DROP, "code": DROP_CODE.replace('"price"', '"prce"')},
            ),
            ("p1", "nh_edit_cell", {"cell_id": "$cell", "code": DROP_CODE}),
        ],
    ),
    "mocks/nh/nh_run.md": ({}, [("p1", "nh_run", {"cell_id": FIXTURE_LOADER})]),
    "mocks/nh/nh_undo.md": ({}, [("p1", "nh_add_cell", DROP), ("p2", "nh_undo", {})]),
    "mocks/nh/nh_inspect.md": ({}, [("p1", "nh_inspect", {"view": "overview"})]),
    "undo-last/mocks/nh/nh_inspect.md": (
        {"NH_EVAL_LAST_CELL": "drop"},
        [("p1", "nh_inspect", {"view": "overview"})],
    ),
    "error-retry/mocks/nh/nh_add_cell.md": ({}, [("p1", "nh_add_cell", DATES)]),
    "error-retry/mocks/nh/nh_edit_cell.md": (
        {},
        [
            ("p1", "nh_add_cell", DATES),
            ("p1", "nh_edit_cell", {"cell_id": "$cell", "code": DATES_FIXED}),
        ],
    ),
    "note-shape/mocks/nh/nh_add_cell.md": ({}, [("p1", "nh_add_cell", HEATMAP)]),
    "never-edits-ipynb/mocks/nh/nh_edit_cell.md": (
        {},
        [("p1", "nh_edit_cell", {"cell_id": "title", "code": "# Sales analysis"})],
    ),
}


def _plot_backend(project: Path):
    """FakeBackend that shows open matplotlib figures as PNG outputs, like IPython's inline backend."""
    from nh_gateway.backend.fake import FakeBackend

    class PlotBackend(FakeBackend):
        def _execute(self, code: str):
            outputs, count, status = super()._execute(code)
            import base64

            import matplotlib.pyplot as plt

            for number in plt.get_fignums():
                buffer = io.BytesIO()
                plt.figure(number).savefig(buffer, format="png", dpi=100)
                png = base64.b64encode(buffer.getvalue()).decode()
                outputs.append(
                    {
                        "output_type": "display_data",
                        "data": {"image/png": png},
                        "metadata": {},
                    }
                )
            plt.close("all")
            return outputs, count, status

    return PlotBackend(project)


@contextlib.contextmanager
def _eval_fixture(env: dict[str, str]):
    """The eval workspace from _scaffold/base.sh, with the kernel cwd in its notebooks/ folder."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        subprocess.run(
            ["bash", str(EVALS / "_scaffold" / "base.sh")],
            cwd=project,
            check=True,
            env={"PATH": "/usr/bin:/bin", **env},
        )
        saved_cwd, saved_env = os.getcwd(), dict(os.environ)
        os.environ.update(NH_PROJECT_DIR=str(project), MPLBACKEND="Agg")
        os.chdir(project / "notebooks")
        try:
            yield project, Path(tmp) / "plugin-data"
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)


async def run_mock_scenario(name: str) -> str:
    """The real gateway's text for the last call of MOCK_SCENARIOS[name]."""
    from fastmcp import Client

    from nh_gateway.app import create_server
    from tests.fakes.turns import Turns, text

    env, calls = MOCK_SCENARIOS[name]
    with _eval_fixture(env) as (project, data):
        backend = _plot_backend(project)
        notebook = nbformat.read(project / "notebooks" / "eda.ipynb", as_version=4)
        for cell in notebook.cells:  # the kernel ran the fixture's cells
            if cell.cell_type == "code":
                backend._execute(cell.source)
        turns, prompt, result = Turns(project, data), "", ""
        async with Client(create_server(project, backend)) as client:
            for prompt_id, tool, args in calls:
                if prompt_id != prompt:
                    turns.prompt(prompt_id)
                    prompt = prompt_id
                cell = re.search(r"\bcell=(\S+)", result)
                args = {k: (cell.group(1) if v == "$cell" and cell else v) for k, v in args.items()}
                result = text(await turns.call(client, tool, args, prompt_id))
        return result


def result_shape(text: str) -> dict[str, Any]:
    """What a mock must share with the real result: lead lines, first-line form, machine keys,
    sections. Titles, ids, numbers and the data-dependent headline are left out."""
    text = text.replace("{{input.title}}", "T").replace("{{input.cell_id}}", "nh-0")
    lines = text.strip("\n").splitlines()
    found = [i for i, line in enumerate(lines) if re.match(r"nh: (cell=|E\d{3})", line)]
    machine = found[0] if found else 1  # nh_inspect views have no machine line
    first = re.sub(r'"[^"]*"', '"T"', lines[machine - 1])
    first = re.sub(r"\d+(?:\.\d+)?", "N", first)
    clauses = [clause.split(":")[0].strip() for clause in first.split(";")]
    return {
        "lead": [line.split(":")[0] for line in lines[: machine - 1]],
        "first": clauses[:2],
        "headline": len(clauses) > 2,
        "machine": (re.findall(r"(\w+)=", lines[machine]) or lines[machine].split()[:2])
        if found
        else [],
        "sections": re.findall(r"^--- (.+) ---$", text, flags=re.MULTILINE),
        "next": any(line.startswith("Next: ") for line in lines),
    }


def test_result_shape_reads_the_documented_format():
    shape = result_shape(
        "Kernel ≠ notebook: `df_clean` (still holds results)\n"
        'Added "{{input.title}}" [2] at the bottom; ran ok in 0.4s; df_clean: new 37×6.\n'
        "nh: cell=nh-1 exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3\n"
        "--- output ---\nrows: 43 -> 37\n--- next ---\nReply."
    )
    assert shape == {
        "lead": ["Kernel ≠ notebook"],
        "first": ['Added "T" [N] at the bottom', "ran ok in Ns"],
        "headline": True,
        "machine": ["cell", "exec", "turn", "retries", "waits", "undos"],
        "sections": ["output", "next"],
        "next": False,
    }


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("name", sorted(MOCK_SCENARIOS))
async def test_mock_matches_the_real_gateway_result(name: str):
    pytest.importorskip("pandas")
    pytest.importorskip("matplotlib")
    mock = EVALS / name
    front, body = split_frontmatter(mock)
    real = await run_mock_scenario(name)
    assert result_shape(body) == result_shape(real), f"{name} drifted from the gateway:\n{real}"
    assert bool(front.get("error")) == bool(re.search(r"^nh: E\d{3}", real, flags=re.MULTILINE))


# ------------------------------------------------------------------ docs follow the gateway

REPO = PLUGIN.parents[1]
GATEWAY = PLUGIN / "server" / "src" / "nh_gateway"
NOTEBOOK_REFS = PLUGIN / "skills" / "notebook" / "reference"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _gateway_source() -> str:
    return "\n".join(_read(p) for p in sorted(GATEWAY.rglob("*.py")))


def test_mock_calls_graders_are_scored_in_both_arms():
    # A two-arm run drops mock_calls graders on the plugin's own server unless `arm: both`.
    graders = [g for case in CASES for g in (case / "graders").glob("*.md")]
    mock_graders = [g for g in graders if split_frontmatter(g)[0].get("target") == "mock_calls"]
    assert {g.parent.parent.name for g in mock_graders} >= {"note-shape"}
    for grader in mock_graders:
        assert split_frontmatter(grader)[0].get("arm") == "both", grader


def test_never_edits_ipynb_mock_refuses_markdown_edits_like_the_gateway():
    case = EVALS / "never-edits-ipynb"
    front, body = split_frontmatter(case / "mocks" / "nh" / "nh_edit_cell.md")
    assert front["error"] is True and "markdown cell" in body
    _, rubric = split_frontmatter(case / "graders" / "hands-back.md")
    assert "JupyterLab" in rubric and "already fixed" in rubric
    assert "used an nh tool" not in rubric


def test_kernel_signals_the_skill_names_are_emitted_by_the_gateway():
    docs = "\n".join(
        _read(p)
        for p in [
            PLUGIN / "skills" / "notebook" / "SKILL.md",
            *NOTEBOOK_REFS.glob("*.md"),
        ]
    )
    source = _gateway_source()
    for signal in (
        "Kernel ≠ notebook: ",
        "NEW kernel: earlier variables are gone",
        "not the project env",
    ):
        assert signal in docs, signal
        assert signal in source, f"the skill names {signal!r}, which the gateway never emits"
    # Both warnings lead the result, above its first line; nothing says they sit in --- kernel ---.
    assert "`--- kernel ---`: NEW kernel" not in docs
    assert '"Kernel ≠ notebook", lead with it' not in docs
    assert "Lines ABOVE it warn about the kernel" in _read(NOTEBOOK_REFS / "tools.md")


def test_every_result_section_the_gateway_emits_is_documented():
    tools_md = _read(NOTEBOOK_REFS / "tools.md")
    emitted = set(re.findall(r'section\(\s*"([a-z -]+)"', _gateway_source()))
    documented = set(re.findall(r"^\| `--- (.+?) ---`", tools_md, flags=re.MULTILINE))
    aliases = {
        "hints": "readability hints (advisory)",
        "readability hints": "readability hints (advisory)",
    }
    assert {aliases.get(name, name) for name in emitted} <= documented


def test_skill_rules_match_the_gateway_refusals():
    skill = _read(PLUGIN / "skills" / "notebook" / "SKILL.md")
    errors_md = _read(NOTEBOOK_REFS / "errors.md")
    tools_md = _read(NOTEBOOK_REFS / "tools.md")
    # base_sha is enforced for the user's cells (E144); markdown cells are refused (E145).
    assert "`base_sha` is REQUIRED" in skill and "E144" in skill
    assert "REQUIRED for a cell the user wrote" in tools_md
    for code in ("E144", "E145", "E107", "E108"):
        assert f"| {code} |" in errors_md and f'"{code}"' in _read(GATEWAY / "policy" / "errors.py")
    # Inside nh:qa-cell only nh:cell-writer writes; the main conversation waits for its report.
    assert "nh:cell-writer` inside `nh:qa-cell` may" in skill
    assert "(reference/qa-workflow.md)" in skill
    qa_md = _read(NOTEBOOK_REFS / "qa-workflow.md")
    assert '"name": "nh:qa-cell"' in qa_md
    for code in ("E103", "E107", "E108", "E110", "E112", "E141", "E144"):
        assert re.search(rf"^\| [^|]*\b{code}\b", qa_md, flags=re.MULTILINE), code
    e103 = next(line for line in errors_md.splitlines() if line.startswith("| E103 |"))
    assert CATALOGUE["E103"][0].rstrip(".").lower() in e103.lower()
    # E120's fixes for L002 and L009 no longer contradict "call again".
    assert (
        "| L002 | `# %%`, `# In[ ]`, `# <codecell>` or `# COMMAND` separators | Keep only the "
        "first step in this cell (no separators) and propose the rest in your reply |" in errors_md
    )
    l009 = next(line for line in errors_md.splitlines() if line.startswith("| L009 |"))
    assert (
        "don't call again yet" in l009 and "after a yes" in l009 and "then write the cell" in l009
    )
    assert "Ask first; after a yes, run `uv add <pkg>`" in skill
    # L121 (intent repeats the title) is gone.
    for text in (skill, errors_md, tools_md, _read(REPO / "docs" / "harness-toml.md")):
        assert "L121" not in text and "intent_repeats_title" not in text
        assert "not a copy of the title" not in text


def test_inspect_rows_default_is_the_configured_head_rows():
    assert TOOL_DEFAULTS["nh_inspect"]["rows"] is None
    assert "| `rows` | `[inspect].head_rows` (5) |" in _read(NOTEBOOK_REFS / "tools.md")
    assert config.DEFAULTS["inspect"]["head_rows"] == 5


def _documented_keys() -> dict[str, dict[str, str]]:
    """harness-toml.md's tables: {section: {key: default as written}}."""
    sections: dict[str, dict[str, str]] = {}
    section: str | None = None
    for line in _read(REPO / "docs" / "harness-toml.md").splitlines():
        if line.startswith("## "):
            heading = re.fullmatch(r"## `\[(.+)\]`", line)
            section = heading.group(1) if heading else ("" if line == "## Top level" else None)
            continue
        row = re.match(r"\| `(\w+)` \|(.*)", line)
        if section is None or not row:
            continue
        cells = [cell.strip() for cell in row.group(2).split("|")]
        default = cells[1] if section == "lint.rules" else cells[0]
        sections.setdefault(section, {})[row.group(1)] = default.strip("`")
    return sections


def _defaults_flat(table: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {prefix: {}}
    for key, value in table.items():
        if isinstance(value, dict):
            out.update(_defaults_flat(value, f"{prefix}.{key}" if prefix else key))
        else:
            out[prefix][key] = value
    return out


def test_harness_toml_doc_lists_every_default_key_and_nothing_else():
    import tomllib

    documented = _documented_keys()
    defaults = _defaults_flat(config.DEFAULTS)
    assert {s: set(keys) for s, keys in documented.items()} == {
        s: set(keys) for s, keys in defaults.items() if keys
    }
    for section, keys in documented.items():
        for key, written in keys.items():
            assert tomllib.loads(f"v = {written}")["v"] == defaults[section][key], (
                section,
                key,
            )
    assert documented["turn"]["max_revisions"] == str(config.DEFAULTS["turn"]["max_revisions"])
    text = _read(REPO / "docs" / "harness-toml.md")
    assert "token_env" not in "\n".join(
        line for line in text.splitlines() if line.startswith("| `")
    )
    assert "Only loopback addresses" in text and "`NH_JUPYTER_TOKEN`" in text
    assert "When set, nh tries it first" in text  # [jupyter] kernel_name
    assert "`clear-count`" in text  # [stale] mark
    assert "every other section is there with each of its keys\ncommented out" in text


def test_scaffolded_harness_toml_matches_the_documented_form(tmp_path: Path):
    from nh_gateway._shared.scaffold import core

    text = core.render_harness_toml({"name": "demo", "goal": "g", "notebook": "n.ipynb"})
    assert '\nname = "demo"' in text and "\n# max_retries = 2" in text
    assert "\nmax_retries = 2" not in text and "\n[turn]" in text


def test_install_docs_point_at_the_published_repo():
    marketplace = json.loads(_read(REPO / ".claude-plugin" / "marketplace.json"))
    install = f"nh@{marketplace['name']}"
    for readme in (REPO / "README.md", PLUGIN / "README.md"):
        text = _read(readme)
        assert "OWNER" not in text and "<github-owner>" not in text, readme
        assert "/plugin marketplace add dheocahyo/Notebook-Harness-Plugin\n" in text, readme
        assert "claude plugin marketplace add dheocahyo/Notebook-Harness-Plugin\n" in text, readme
        assert f"/plugin install {install}\n" in text and f"plugin install {install} " in text
        assert "claude plugin marketplace add <path-to-clone>" in text, readme  # working on nh
    ci = _read(REPO / ".github" / "workflows" / "ci.yml")
    guard = ci.split("release guard (install placeholders)", 1)[1].split("\n\n", 1)[0]
    assert "OWNER/" in guard and "pull_request" not in guard
    assert "github.ref == 'refs/heads/master'" in guard and "refs/tags/" in guard
    assert 'tags: ["v*"]' in ci


def test_plugin_and_gateway_versions_match():
    import tomllib

    plugin = json.loads(_read(PLUGIN / ".claude-plugin" / "plugin.json"))
    gateway = tomllib.loads(_read(PLUGIN / "server" / "pyproject.toml"))["project"]
    assert plugin["version"] == gateway["version"]


def test_readme_matches_ci_for_tests_and_evals():
    readme = _read(REPO / "README.md")
    ci = _read(REPO / ".github" / "workflows" / "ci.yml")
    assert "--all-groups" in ci
    sync = readme.index("uv sync --project plugins/nh/server --all-groups")
    assert sync < readme.index("-m integration") and sync < readme.index("-m e2e")
    evals = re.findall(r"^claude plugin eval .*$", readme, flags=re.MULTILINE)
    assert evals and all("--ablation none" in cmd for cmd in evals)
    assert "--ablation none" in ci


def test_dev_sandbox_is_a_seeded_nh_project():
    sandbox = REPO / "dev" / "sandbox"
    assert _read(sandbox / ".nh" / ".gitignore").split() == [
        "*",
        "!.gitignore",
        "!README.md",
    ]
    assert (sandbox / ".nh" / "README.md").is_file()
    cfg = config.load(sandbox)
    assert cfg.problems == [] and cfg["project"]["env_manager"] == "uv"
    with open(sandbox / cfg["project"]["data_source"], newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert 30 <= len(rows) <= 50 and "region" in rows[0]
    assert 0 < sum(1 for row in rows if not row["price"]) < len(rows)
    notebook = nbformat.read(sandbox / cfg["project"]["notebook"], as_version=4)
    nbformat.validate(notebook)
    assert (notebook.nbformat, notebook.nbformat_minor) == (4, 5)
    # Seeded with the title cell; cells written while trying the harness may follow it.
    assert notebook.cells and notebook.cells[0].cell_type == "markdown"
    assert notebook.cells[0].source.startswith("# ")
    # Like /nh:init writes it: [project] values, every other key commented out.
    live = [
        line
        for line in _read(sandbox / "harness.toml").splitlines()
        if line.strip() and not line.startswith(("#", "["))
    ]
    assert all(line.split("=")[0].strip() in config.DEFAULTS["project"] for line in live), live
    assert "[tool.uv]" in _read(sandbox / "pyproject.toml")  # `nhctl env sync` needs it
    readme = _read(REPO / "README.md")
    assert "nhctl lab start" in readme and "--ServerApp.root_dir=dev/sandbox" in readme


def test_first_cell_takes_the_data_path_from_the_scaffold():
    first_cell = _read(PLUGIN / "skills" / "init" / "reference" / "first-cell.md")
    init = _read(PLUGIN / "skills" / "init" / "SKILL.md")
    assert "data.path_from_notebook" in first_cell and "data.path_from_notebook" in init
    assert '"../<data.source>"' not in first_cell
    assert "path_from_notebook" in _read(GATEWAY / "_shared" / "scaffold" / "core.py")


def test_init_skill_expects_no_turn_record_in_its_own_message():
    _, body = split_frontmatter(PLUGIN / "skills" / "init" / "SKILL.md")
    step = body.split('Call `nh_inspect(view="status")`', 1)[1].split("\n3. ", 1)[0]
    assert "no turn" in step and "expected" in step and "Don't report it as a problem" in step
