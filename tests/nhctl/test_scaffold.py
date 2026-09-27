"""nhctl scaffold / notebook new, and the _shared/scaffold library behind them."""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import nbformat
import pytest

from nh_gateway._shared import tomlread
from nh_gateway._shared.scaffold import core

DEFAULTS = tomllib.loads(core.DEFAULTS_TOML.read_text(encoding="utf-8"))


def tools(uv=False, conda=False, mamba=False) -> core.Tools:
    return core.Tools(
        uv="/fake/uv" if uv else None,
        conda="/fake/conda" if conda else None,
        mamba="/fake/mamba" if mamba else None,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def assert_commented_defaults(text: str, project_values: dict) -> None:
    """Parsed as written, harness.toml holds only [project] (plus empty section tables);
    with every '# key = value' line uncommented it is exactly the defaults."""
    for parse in (tomllib.loads, tomlread._mini_loads):  # the 3.9 fallback reader too
        written = parse(text)
        assert written["project"] == project_values
        leaves = {k for k, v in written.items() if not isinstance(v, dict)}
        assert leaves == set()
    uncommented = "\n".join(
        line[2:] if line.startswith("# ") and "=" in line else line for line in text.splitlines()
    )
    assert tomllib.loads(uncommented) == dict(DEFAULTS, project=project_values)


# ------------------------------------------------------------------------ CLI


def test_fresh_scaffold(env, project, tmp_path):
    env.script("uv", "exit 0")
    data = tmp_path / "sales.csv"
    data.write_text("region,price\nnorth,1.5\n")
    report = env.json(
        "scaffold", "--goal", "Why did sales drop?", "--data", str(data),
        "--problem-type", "regression", cwd=project,
    )  # fmt: skip
    actions = {p["path"]: p["action"] for p in report["paths"]}
    for folder in ("data/raw/", "data/processed/", "notebooks/", "reports/figures/", "src/"):
        assert actions[folder] == "created"
    for path in (".nh/README.md", ".nh/.gitignore", "harness.toml", "NOTEBOOK.md"):
        assert actions[path] == "created"
    assert actions["pyproject.toml"] == "created"
    assert actions["notebooks/01_eda.ipynb"] == "created"
    assert actions["data/raw/sales.csv"] == "copied"
    assert actions[".gitignore"] == "created"
    for keep in ("data/raw", "data/processed", "reports/figures", "src"):
        assert (project / keep / ".gitkeep").is_file()
    assert (project / "data/raw/sales.csv").read_bytes() == data.read_bytes()
    assert not (project / ".python-version").exists()
    assert report["env"]["manager"] == "uv"
    assert report["data"]["source"] == "data/raw/sales.csv"
    assert report["data"]["path_from_notebook"] == "../data/raw/sales.csv"
    assert (project / ".nh/.gitignore").read_text() == "*\n!.gitignore\n!README.md\n"
    gitignore = (project / ".gitignore").read_text().splitlines()
    assert gitignore[1:] == [
        ".venv/", ".conda/", ".env", ".ipynb_checkpoints/", "*jupyter_ystore.db", ".jupyter/",
        "data/raw/*", "data/processed/*", "!data/*/.gitkeep",
    ]  # fmt: skip

    harness = tomllib.loads((project / "harness.toml").read_text())
    assert harness["project"] == {
        "name": "sales-study",
        "goal": "Why did sales drop?",
        "problem_type": "regression",
        "data_source": "data/raw/sales.csv",
        "notebook": "notebooks/01_eda.ipynb",
        "env_manager": "uv",
    }
    # Everything outside [project] is a commented-out default: nothing is pinned.
    assert "version" not in harness
    for section in DEFAULTS:
        if section not in ("project", "version"):
            assert not any(v for v in harness[section].values() if not isinstance(v, dict))
    assert_commented_defaults((project / "harness.toml").read_text(), harness["project"])

    notebook = project / "notebooks/01_eda.ipynb"
    raw = json.loads(notebook.read_text())
    nbformat.validate(nbformat.reads(notebook.read_text(), as_version=4))
    assert (raw["nbformat"], raw["nbformat_minor"]) == (4, 5)
    title = raw["cells"][0]
    assert title["id"] == "nh-title" and title["cell_type"] == "markdown"
    assert title["metadata"] == {"nh": {"v": 1, "role": "title", "created_by": "nh-init"}}
    assert raw["metadata"]["kernelspec"]["name"] == "python3"
    assert raw["metadata"]["nh"] == {"v": 1, "goal": "Why did sales drop?"}

    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert pyproject["project"]["dependencies"] == ["pandas>=2.2", "numpy>=2.0", "matplotlib>=3.9"]
    assert pyproject["dependency-groups"]["dev"] == [
        "jupyterlab>=4.6,<5", "jupyter-collaboration>=5,<6", "ipykernel>=6.29",
    ]  # fmt: skip
    assert pyproject["tool"]["uv"]["package"] is False
    notes = (project / "NOTEBOOK.md").read_text()
    for part in ("Why did sales drop?", "data/raw/sales.csv", "regression", "## Decisions"):
        assert part in notes


def test_rerun_is_idempotent(env, project, tmp_path):
    env.script("uv", "exit 0")
    data = tmp_path / "sales.csv"
    data.write_text("a\n1\n")
    args = ("scaffold", "--goal", "g", "--data", str(data))
    env.json(*args, cwd=project)
    (project / "harness.toml").write_text("# edited by the user\nversion = 1\n")
    before = snapshot(project)
    again = env.json(*args, cwd=project)
    assert {p["action"] for p in again["paths"]} == {"kept"}
    assert snapshot(project) == before


def test_human_output(env, project):
    env.script("uv", "exit 0")
    proc = env.run("scaffold", cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert "nh project ready" in proc.stdout and "harness.toml" in proc.stdout


def test_adopt_mode(env, project):
    env.script("uv", "exit 0")
    nb = {
        "cells": [
            {"cell_type": "code", "metadata": {}, "source": ["import pandas as pd\n", "df = pd.read_csv('x.csv')"],
             "outputs": [], "execution_count": 1},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 4,
    }  # fmt: skip
    (project / "analysis").mkdir()
    (project / "analysis/old.ipynb").write_text(json.dumps(nb))
    (project / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["pandas"]\n'
    )
    report = env.json("scaffold", "--adopt", "analysis/old.ipynb", cwd=project)
    created = {p["path"] for p in report["paths"] if p["action"] != "kept"}
    assert created == {
        ".nh/README.md",
        ".nh/.gitignore",
        "harness.toml",
        "NOTEBOOK.md",
        ".gitignore",
    }
    assert not (project / "data").exists() and not (project / "notebooks").exists()
    assert report["adopt"] == {"code_cells": 1, "has_loader": True, "nbformat_minor": 4}
    assert any("4.5" in w for w in report["warnings"])
    harness = tomllib.loads((project / "harness.toml").read_text())
    assert harness["project"]["notebook"] == "analysis/old.ipynb"
    # The existing env file is only proposed a change, never edited without --add-dev-deps.
    assert report["env"]["missing_dev"] == list(core.DEV_PACKAGES)
    assert '+    "jupyterlab>=4.6,<5",' in report["env"]["dev_diff"]
    assert "jupyterlab" not in (project / "pyproject.toml").read_text()
    # The adopted project ignores nh's own artifacts, not a data layout it doesn't have.
    assert "data/raw/*" not in (project / ".gitignore").read_text()

    applied = env.json("scaffold", "--adopt", "analysis/old.ipynb", "--add-dev-deps", cwd=project)
    assert {"path": "pyproject.toml", "action": "updated"} in applied["paths"]
    assert applied["env"]["dev_added"] == list(core.DEV_PACKAGES)
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    assert pyproject["dependency-groups"]["dev"] == list(core.DEV_PACKAGES)
    assert pyproject["project"]["dependencies"] == ["pandas"]
    third = env.json("scaffold", "--adopt", "analysis/old.ipynb", cwd=project)
    assert third["env"]["missing_dev"] == []


def test_adopt_rejects_missing_or_outside_notebook(env, project, tmp_path):
    env.script("uv", "exit 0")
    missing = env.json("scaffold", "--adopt", "nope.ipynb", cwd=project, expect=1)
    assert missing["error"]["code"] == "D113"
    outside = tmp_path / "elsewhere.ipynb"
    outside.write_text(
        json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    )
    report = env.json("scaffold", "--adopt", str(outside), cwd=project, expect=1)
    assert report["error"]["code"] == "D114"
    assert not (project / ".nh").exists()


def test_url_with_credentials_goes_to_dotenv(env, project):
    env.script("uv", "exit 0")
    url = "https://data.example.com/exports/sales.parquet?X-Amz-Signature=s3cr3t&v=2"
    proc = env.run("scaffold", "--data", url, "--json", cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert "s3cr3t" not in proc.stdout
    report = json.loads(proc.stdout)
    assert report["data"]["source"] == "https://data.example.com/exports/sales.parquet"
    assert report["data"]["secret_in_env"] is True
    dotenv = project / ".env"
    assert dotenv.read_text() == f"DATA_URL='{url}'\n"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    for name in ("harness.toml", "NOTEBOOK.md", "pyproject.toml"):
        assert "s3cr3t" not in (project / name).read_text()
    assert (
        "pyarrow>=17"
        in tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"]
    )
    assert ".env" in (project / ".gitignore").read_text().splitlines()
    # A second run keeps the existing DATA_URL line.
    again = env.json("scaffold", "--data", url, cwd=project)
    assert {"path": ".env", "action": "kept"} in again["paths"]
    assert dotenv.read_text().count("DATA_URL") == 1


def test_url_without_credentials_is_recorded_whole(tmp_path):
    report = core.scaffold(tmp_path, data="https://example.com/a.csv", tools=tools(uv=True))
    assert report.data["source"] == "https://example.com/a.csv"
    assert report.data["secret_in_env"] is False and report.data["path_from_notebook"] is None
    assert not (tmp_path / ".env").exists()
    assert report.harness["goal"] == "Explore a.csv"


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://myfn.azurewebsites.net/api/export.csv?code=AZFUNCKEY123", "AZFUNCKEY123"),
        ("https://api.example.com/export.csv?jwt=eyJhbGciOi.x.y", "eyJhbGciOi"),
        ("https://api.example.com/export.csv?hmac=deadbeef99&ts=1", "deadbeef99"),
        ("https://api.example.com/export.csv?format=csv", "format=csv"),
        ("https://app.example.com/export.csv#access_token=frag5ecret", "frag5ecret"),
        ("https://robot@files.example.com/export.csv", "robot"),
    ],
)
def test_any_query_fragment_or_userinfo_goes_to_dotenv(tmp_path, url, secret):
    """Finding 48: secrets hide in too many forms to recognise, so none of these are kept."""
    report = core.scaffold(tmp_path, data=url, tools=tools(uv=True))
    safe = url.split("?")[0].split("#")[0].replace("robot@", "")
    assert report.data["source"] == safe and report.data["secret_in_env"] is True
    assert report.data["given"] == ""
    assert (tmp_path / ".env").read_text() == f"DATA_URL='{url}'\n"
    for name in ("harness.toml", "NOTEBOOK.md"):
        text = (tmp_path / name).read_text()
        assert secret not in text and safe in text
    assert tomllib.loads((tmp_path / "harness.toml").read_text())["project"]["data_source"] == safe
    assert "DATA_URL" in (tmp_path / "NOTEBOOK.md").read_text()


def test_userinfo_url_is_secret(tmp_path):
    report = core.scaffold(
        tmp_path, data="postgresql://me:pw@db.local:5432/sales", tools=tools(uv=True)
    )
    assert report.data["source"] == "postgresql://db.local:5432/sales"
    assert report.data["reader"] == "sql"
    assert "pw" not in (tmp_path / "harness.toml").read_text()
    assert "sqlalchemy>=2.0" in (tmp_path / "pyproject.toml").read_text()


def test_notebook_new(env, project):
    env.script("uv", "exit 0")
    env.json("scaffold", "--goal", "Forecast demand", cwd=project)
    made = env.json("notebook", "new", "notebooks/02_model-fit.ipynb", cwd=project)
    assert made == {"ok": True, "notebook": "notebooks/02_model-fit.ipynb", "created": True}
    nb = json.loads((project / "notebooks/02_model-fit.ipynb").read_text())
    assert nb["cells"][0]["source"][0] == "# 02 model fit\n"
    assert nb["metadata"]["nh"]["goal"] == "Forecast demand"
    again = env.json("notebook", "new", "notebooks/02_model-fit.ipynb", cwd=project, expect=1)
    assert again["error"]["code"] == "D119"
    outside = env.json("notebook", "new", "../x.ipynb", cwd=project, expect=1)
    assert outside["error"]["code"] == "D114"


def test_refuses_nested_project(env, project):
    env.script("uv", "exit 0")
    env.json("scaffold", cwd=project)
    report = env.json("scaffold", cwd=project / "notebooks", expect=1)
    assert report["error"]["code"] == "D117"


def test_neither_uv_nor_conda_writes_nothing(env, project):
    report = env.json("scaffold", cwd=project, expect=1)
    assert report["error"]["code"] == "D110"
    assert list(project.iterdir()) == []


def test_bad_usage_is_json(env, project):
    report = env.json("scaffold", "--problem-type", "vibes", cwd=project, expect=2)
    assert report["ok"] is False and "vibes" in report["error"]["message"]


# ------------------------------------------------------------ env detection (D3)


@pytest.mark.parametrize(
    ("files", "found", "expect"),
    [
        (
            {"environment.yml": "dependencies:\n  - pandas\n"},
            {"uv": 1, "conda": 1},
            ("conda", True),
        ),
        ({}, {"uv": 1, "conda": 1}, ("uv", False)),
        ({"pyproject.toml": '[project]\nname = "x"\n'}, {"uv": 1}, ("uv", True)),
        ({"pyproject.toml": '[project]\nname = "x"\n'}, {"conda": 1}, ("conda", False)),
        ({}, {"mamba": 1}, ("conda", False)),
        ({}, {}, (None, False)),
    ],
)
def test_choose_env(tmp_path, files, found, expect):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    choice = core.choose_env(tmp_path, tools(**found))
    assert (choice.manager, choice.existing) == expect


def test_environment_yml_without_conda_stops(tmp_path):
    (tmp_path / "environment.yml").write_text("dependencies:\n  - pandas\n")
    with pytest.raises(core.ScaffoldError) as err:
        core.scaffold(tmp_path, tools=tools(uv=True))
    assert err.value.code == "D111"
    assert not (tmp_path / ".nh").exists()


@pytest.mark.parametrize(
    ("fake", "expect"),
    [(("uv",), "uv"), (("conda",), "conda"), (("mamba",), "conda"), (("uv", "conda"), "uv")],
)
def test_env_detection_from_path(env, project, fake, expect):
    for name in fake:
        env.script(name, "exit 0")
    report = env.json("scaffold", "--data-mode", "in-place", cwd=project)
    assert report["env"]["manager"] == expect
    env_file = "pyproject.toml" if expect == "uv" else "environment.yml"
    assert (project / env_file).is_file()


def test_uv_off_path_is_found_in_its_install_locations(env, project):
    brew = Path(env.vars["NH_FALLBACK_ROOT"], "opt", "homebrew", "bin")
    brew.mkdir(parents=True)
    uv = env.script("uv", "exit 0").rename(brew / "uv")
    report = env.json("scaffold", "--data-mode", "in-place", cwd=project)
    assert (report["env"]["manager"], report["env"]["tool"]) == ("uv", str(uv))


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("uv", "~/.local/bin"),
        ("uv", "/usr/local/bin"),
        ("conda", "~/miniforge3/bin"),
        ("conda", "/opt/conda/bin"),
        ("mamba", "/opt/homebrew/Caskroom/miniforge/base/bin"),
    ],
)
def test_find_tool_looks_in_the_install_locations(tmp_path, name, where):
    home, root = tmp_path / "home", tmp_path / "root"
    folder = home / where[2:] if where.startswith("~") else Path(f"{root}{where}")
    folder.mkdir(parents=True)
    tool = folder / name
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    env = {"PATH": str(tmp_path / "empty"), "HOME": str(home), "NH_FALLBACK_ROOT": str(root)}
    assert core.find_tool(name, env) == str(tool)
    tool.chmod(0o644)
    assert core.find_tool(name, env) is None


def test_conda_scaffold_template(tmp_path):
    yaml = pytest.importorskip("yaml")
    (tmp_path / "book.xlsx").write_bytes(b"x")
    core.scaffold(tmp_path, data=str(tmp_path / "book.xlsx"), tools=tools(conda=True))
    spec = yaml.safe_load((tmp_path / "environment.yml").read_text())
    assert spec["channels"] == ["conda-forge"]
    assert spec["dependencies"] == [
        "python>=3.11,<3.14", "pandas>=2.2", "numpy>=2.0", "matplotlib>=3.9", "openpyxl>=3.1",
        "jupyterlab>=4.6,<5", "jupyter-collaboration>=5,<6", "ipykernel>=6.29",
    ]  # fmt: skip
    assert (
        tomllib.loads((tmp_path / "harness.toml").read_text())["project"]["env_manager"] == "conda"
    )


def test_conda_add_dev_deps(tmp_path):
    yaml = pytest.importorskip("yaml")
    (tmp_path / "environment.yml").write_text(
        "name: x\nchannels:\n- conda-forge\ndependencies:\n- python=3.12\n- pip:\n  - rich\n"
    )
    report = core.scaffold(tmp_path, tools=tools(conda=True), add_dev_deps=True)
    assert report.env["dev_added"] == list(core.DEV_PACKAGES)
    deps = yaml.safe_load((tmp_path / "environment.yml").read_text())["dependencies"]
    assert deps[:3] == list(core.DEV_PACKAGES) and {"pip": ["rich"]} in deps


# ----------------------------------------------------------------------- data


def test_data_modes(tmp_path, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    big = tmp_path / "big.parquet"
    big.write_bytes(b"x" * 50)
    small = tmp_path / "small.csv"
    small.write_bytes(b"x")
    inside = project / "inside.csv"
    inside.write_bytes(b"x")
    monkeypatch.setattr(core, "IN_PLACE_BYTES", 10)
    assert core.plan_data(project, str(big)).mode == "in-place"
    assert core.plan_data(project, str(big)).source == str(big.resolve())
    assert core.plan_data(project, str(big), "copy").mode == "copy"
    assert core.plan_data(project, str(small)).mode == "copy"
    assert core.plan_data(project, str(small), "in-place").mode == "in-place"
    assert core.plan_data(project, str(inside)).mode == "in-place"
    assert core.plan_data(project, str(inside)).source == "inside.csv"
    assert core.plan_data(project, str(small), "copy", adopt=True).mode == "in-place"
    missing = core.plan_data(project, "nope.csv")
    assert missing.warnings and missing.source == "nope.csv"


def test_path_from_notebook(tmp_path, monkeypatch):
    """Finding 42: the loader reads data relative to the notebook's folder (the kernel's cwd)."""
    monkeypatch.setattr(core, "IN_PLACE_BYTES", 10)
    project = tmp_path / "p"
    (project / "raw").mkdir(parents=True)
    (project / "raw/inside.csv").write_text("a\n1\n")
    big = tmp_path / "big.parquet"
    big.write_bytes(b"x" * 50)
    small = tmp_path / "small.csv"
    small.write_text("a\n1\n")

    def fresh(data):
        return core.scaffold(project, data=data, tools=tools(uv=True))

    assert fresh(str(project / "raw/inside.csv")).data["path_from_notebook"] == "../raw/inside.csv"
    assert fresh(str(small)).data["path_from_notebook"] == "../data/raw/small.csv"
    assert fresh(str(big)).data["path_from_notebook"] == str(big.resolve())
    assert fresh(str(project / "raw")).data["path_from_notebook"] == "../raw"
    assert (
        fresh(str(project / "raw/missing.csv")).data["path_from_notebook"] == "../raw/missing.csv"
    )
    assert fresh("").data["path_from_notebook"] is None


@pytest.mark.parametrize(
    ("notebook", "expect"),
    [("analysis.ipynb", "sales.csv"), ("work/nb/a.ipynb", "../../sales.csv")],
)
def test_path_from_notebook_in_adopt_mode(env, project, notebook, expect):
    env.script("uv", "exit 0")
    (project / notebook).parent.mkdir(parents=True, exist_ok=True)
    (project / notebook).write_text(
        json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    )
    (project / "sales.csv").write_text("a\n1\n")
    report = env.json("scaffold", "--adopt", notebook, "--data", "sales.csv", cwd=project)
    assert report["notebook"] == notebook and report["adopt"]["has_loader"] is False
    assert report["data"]["mode"] == "in-place" and report["data"]["source"] == "sales.csv"
    assert report["data"]["path_from_notebook"] == expect


def test_copy_conflict_uses_data_in_place(tmp_path):
    project = tmp_path / "p"
    (project / "data/raw").mkdir(parents=True)
    (project / "data/raw/sales.csv").write_text("someone else's file")
    source = tmp_path / "sales.csv"
    source.write_text("a\n1\n")
    report = core.scaffold(project, data=str(source), tools=tools(uv=True))
    assert report.data["mode"] == "in-place" and report.data["source"] == str(source.resolve())
    assert report.data["path_from_notebook"] == str(source.resolve())
    assert (project / "data/raw/sales.csv").read_text() == "someone else's file"
    assert any("differs" in w for w in report.warnings)
    harness = tomllib.loads((project / "harness.toml").read_text())
    assert harness["project"]["data_source"] == str(source.resolve())


def test_gitignore_appends_only_missing_lines(tmp_path):
    (tmp_path / ".gitignore").write_text("__pycache__/\n.env")
    core.scaffold(tmp_path, tools=tools(uv=True))
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[:3] == ["__pycache__/", ".env", ""]
    assert lines[3] == core.GITIGNORE_MARKER and lines.count(".env") == 1
    assert ".venv/" in lines and "!data/*/.gitkeep" in lines


def test_refuses_home(tmp_path):
    with pytest.raises(core.ScaffoldError) as err:
        core.scaffold(tmp_path, tools=tools(uv=True), home=tmp_path)
    assert err.value.code == "D106"


# ------------------------------------------------------------------ templates


@pytest.mark.parametrize(
    "goal",
    ['Why do "sales" drop?', "It's 50% off", 'path C:\\data "x"', "tab\there", "emoji 📈 #1"],
)
def test_harness_toml_values_round_trip(goal):
    values = {"name": "p", "goal": goal, "problem_type": "eda", "data_source": "d.csv",
              "notebook": "notebooks/01_eda.ipynb", "env_manager": "uv"}  # fmt: skip
    text = core.render_harness_toml(values)
    assert tomllib.loads(text)["project"] == values
    assert tomlread._mini_loads(text)["project"] == values  # the Python 3.9 fallback reader


def test_harness_toml_keeps_defaults_comments_and_dollars():
    defaults = (
        '# header\nversion = 1\n\n[project]\ngoal = ""    # the $goal\nname = ""\n\n'
        '[turn]\nmax_code_cells = 1   # one per $message\n# a note\n\n[lint.rules]  # per rule\nx = "hint"\n'
    )
    text = core.render_harness_toml({"goal": "cost in $USD", "name": "n"}, defaults)
    assert text.startswith(core.HARNESS_HEADER)
    assert "# header" not in text
    assert 'goal = "cost in $USD"  # the $goal' in text
    assert "# version = 1\n" in text
    assert "[turn]\n# max_code_cells = 1   # one per $message\n# a note\n" in text
    assert '[lint.rules]  # per rule\n# x = "hint"\n' in text
    assert tomllib.loads(text) == {
        "project": {"goal": "cost in $USD", "name": "n"}, "turn": {}, "lint": {"rules": {}},
    }  # fmt: skip


def test_rendered_harness_toml_is_the_commented_defaults():
    """Finding 35: only [project] is live; uncommenting any line restores that default."""
    values = {"name": "p", "goal": "g", "problem_type": "eda", "data_source": "d.csv",
              "notebook": "notebooks/01_eda.ipynb", "env_manager": "uv"}  # fmt: skip
    assert_commented_defaults(core.render_harness_toml(values), values)


@pytest.mark.parametrize(
    "before",
    [
        '[project]\nname = "x"\n',
        '[project]\nname = "x"\n\n[dependency-groups]\ntest = ["pytest"]\n',
        '[dependency-groups]\ndev = ["ruff"]  # lint\n\n[tool.uv]\npackage = false\n',
        '[dependency-groups]\ndev = [\n    "ruff",\n    "mypy[reports]"\n]\n[tool.x]\ny = 1\n',
        "[dependency-groups]\ndev = [\n]\n",
    ],
)
def test_add_to_pyproject(before):
    after = core.add_to_pyproject(before, list(core.DEV_PACKAGES))
    old = tomllib.loads(before)
    new = tomllib.loads(after)
    old_dev = old.get("dependency-groups", {}).get("dev", [])
    assert new["dependency-groups"]["dev"] == old_dev + list(core.DEV_PACKAGES)
    for key in old:
        if key != "dependency-groups":
            assert new[key] == old[key]
    assert core.missing_dev_packages(after, yaml=False) == []


def test_missing_dev_packages_ignores_lookalikes():
    text = '[project]\ndependencies = ["jupyterlab-git", "ipykernel>=6"]\n'
    assert core.missing_dev_packages(text, yaml=False) == [
        "jupyterlab>=4.6,<5",
        "jupyter-collaboration>=5,<6",
    ]
    yml = "dependencies:\n  - jupyter_collaboration=5.0\n  - JupyterLab>=4.6\n"
    assert core.missing_dev_packages(yml, yaml=True) == ["ipykernel>=6.29"]


def test_notebook_template_validates():
    nb = core.notebook_dict("A title", "")
    nbformat.validate(nbformat.from_dict(nb))
    assert nb["cells"][0]["source"] == ["# A title"]
