"""Scaffold an nh project: folders, harness.toml, .nh/, NOTEBOOK.md, env file, first notebook.

Every write is create-only; an existing file is reported as kept and left alone. Three
edits are deliberate exceptions: the nh block appended to ``.gitignore`` (and a
``DATA_URL`` line appended to ``.env``); the data URL's host merged into
``.nh/state/approved_hosts.json`` (design §6.4); and, only when the caller passes
``add_dev_deps=True`` after the user agreed to the shown diff, the Jupyter packages
added to an existing env file.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import shutil
import string
import subprocess
import urllib.parse
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import hosts, secrets
from ..paths import Layout

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
DEFAULTS_TOML = HERE.parents[1] / "defaults.toml"

PROBLEM_TYPES = ("eda", "classification", "regression", "clustering", "forecasting", "other")
DATA_MODES = ("auto", "in-place", "copy")
ENV_MANAGERS = ("auto", "uv", "conda")
PROJECT_KEYS = ("name", "goal", "problem_type", "data_source", "notebook", "env_manager")

DEFAULT_NOTEBOOK = "notebooks/01_eda.ipynb"
FOLDERS = ("data/raw", "data/processed", "notebooks", "reports/figures", "src")
GITKEEP_FOLDERS = ("data/raw", "data/processed", "reports/figures", "src")
IN_PLACE_BYTES = 100 * 2**20

GITIGNORE_MARKER = "# Notebook Harness (nh)"
NH_ARTIFACT_IGNORES = (
    ".venv/", ".conda/", ".env", ".ipynb_checkpoints/", "*jupyter_ystore.db",
    ".jupyter/",  # jupyter-collaboration keeps .jupyter/collaboration_sessions.json in the root
)  # fmt: skip
GITIGNORE_LINES = NH_ARTIFACT_IGNORES + ("data/raw/*", "data/processed/*", "!data/*/.gitkeep")
NH_GITIGNORE = "*\n!.gitignore\n!README.md\n"

BASE_PACKAGES = ("pandas>=2.2", "numpy>=2.0", "matplotlib>=3.9")
DEV_PACKAGES = ("jupyterlab>=4.6,<5", "jupyter-collaboration>=5,<6", "ipykernel>=6.29")
READER_PACKAGES = {
    "parquet": "pyarrow>=17",
    "feather": "pyarrow>=17",
    "excel": "openpyxl>=3.1",
    "sql": "sqlalchemy>=2.0",
}
_READER_BY_SUFFIX = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".parquet": "parquet", ".pq": "parquet",
    ".feather": "feather", ".arrow": "feather",
    ".xlsx": "excel", ".xlsm": "excel",
    ".json": "json", ".jsonl": "json", ".ndjson": "json",
    ".sql": "sql", ".sqlite": "sql", ".sqlite3": "sql", ".db": "sql",
}  # fmt: skip
_COMPRESSION = (".gz", ".bz2", ".xz", ".zip", ".zst")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_LOADER_CALL = re.compile(r"\b(?:read_[a-z_]+|scan_[a-z_]+|load_dataset|loadtxt|genfromtxt)\s*\(")

UV_FALLBACK_DIRS = ("~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin")
CONDA_FALLBACK_DIRS = (
    "~/miniforge3/bin", "~/mambaforge/bin", "~/miniconda3/bin", "~/anaconda3/bin",
    "/opt/homebrew/Caskroom/miniforge/base/bin", "/opt/miniconda3/bin", "/opt/conda/bin",
)  # fmt: skip

HARNESS_HEADER = (
    "# Notebook Harness (nh) settings for this project. Commit this file.\n"
    "# Every setting outside [project] is shown commented out at nh's built-in default;\n"
    "# uncomment a line to override it. [approval] applies after /mcp -> Reconnect.\n"
)

_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]")
_STRING_PAIR = re.compile(
    r'^(?P<key>[A-Za-z0-9_\-]+)(?P<eq>\s*=\s*)"(?:[^"\\]|\\.)*"\s*(?P<comment>#.*)?$'
)


class ScaffoldError(Exception):
    """A request scaffold refuses before writing anything."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


# --------------------------------------------------------------------------- tools


def find_tool(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Find uv/conda/mamba on PATH, then in the usual install locations.

    ``NH_FALLBACK_ROOT`` (for tests) is put in front of the absolute install locations, so a
    machine's own /opt/homebrew/bin or /usr/local/bin can't leak into a test.
    """
    env = os.environ if env is None else env
    found = shutil.which(name, path=env.get("PATH", os.defpath))
    if found:
        return found
    candidates = []
    if name == "conda" and env.get("CONDA_EXE"):
        candidates.append(env["CONDA_EXE"])
    dirs = UV_FALLBACK_DIRS if name == "uv" else CONDA_FALLBACK_DIRS
    home = env.get("HOME", "")
    root = env.get("NH_FALLBACK_ROOT", "")
    for folder in dirs:
        if folder.startswith("~"):
            if not home:
                continue
            folder = home + folder[1:]
        else:
            folder = root + folder
        candidates.append(os.path.join(folder, name))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass
class Tools:
    uv: str | None = None
    conda: str | None = None
    mamba: str | None = None

    @classmethod
    def find(cls, env: Mapping[str, str] | None = None) -> Tools:
        return cls(find_tool("uv", env), find_tool("conda", env), find_tool("mamba", env))

    @property
    def conda_like(self) -> str | None:
        return self.mamba or self.conda


@dataclass
class EnvChoice:
    manager: str | None  # "uv" | "conda" | None when neither tool exists
    file: str | None  # env file, relative to the project
    existing: bool  # the file was already there (kept, never rewritten)
    reason: str


def existing_environment_yml(project: Path) -> str | None:
    for name in ("environment.yml", "environment.yaml"):
        if (project / name).is_file():
            return name
    return None


def choose_env(project: Path, tools: Tools, prefer: str = "auto") -> EnvChoice:
    """Plan D3: environment.yml means conda; else uv if found; else conda; else nothing."""
    yml = existing_environment_yml(project)
    if prefer == "conda" or (prefer == "auto" and yml):
        reason = f"{yml} exists" if yml and prefer == "auto" else "conda requested"
        return EnvChoice("conda", yml or "environment.yml", bool(yml), reason)
    has_pyproject = (project / "pyproject.toml").is_file()
    if prefer == "uv" or tools.uv:
        return EnvChoice(
            "uv", "pyproject.toml", has_pyproject, "uv requested" if prefer == "uv" else "uv found"
        )
    if tools.conda_like:
        return EnvChoice("conda", "environment.yml", False, "conda found, uv not found")
    return EnvChoice(None, None, False, "neither uv nor conda was found")


# ---------------------------------------------------------------------------- data


@dataclass
class DataPlan:
    given: str
    kind: str  # none | file | dir | url
    reader: str  # csv | parquet | feather | excel | json | sql | "" (unknown)
    mode: str  # none | in-place | copy | url
    source: str  # what harness.toml and NOTEBOOK.md record; never holds credentials
    path: Path | None = None
    dest: Path | None = None
    size: int | None = None
    secret_url: str | None = None  # full URL for .env when it may carry credentials
    warnings: list[str] = field(default_factory=list)


def is_url(text: str) -> bool:
    return bool(_URL.match(text.strip()))


def reader_for(name: str) -> str:
    lowered = name.lower()
    for suffix in _COMPRESSION:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return _READER_BY_SUFFIX.get(os.path.splitext(lowered)[1], "")


def split_secret_url(url: str) -> tuple[str, bool]:
    """Return (url safe to record, whether the original may carry credentials).

    Secrets hide in too many places to recognise (``?code=``, ``?jwt=``, ``#access_token=``,
    signed-URL parameters, ``/bot123:AAH…/`` path tokens), so any userinfo, query string or
    fragment counts, and so does a path segment that looks like a token
    (:func:`secrets.url_token_like`, which includes any ``@``): the committed files get
    ``scheme://host[:port]/path`` with those segments shown as ``…``, and the full URL goes to
    ``.env``. The redactor reads the same rules (:func:`secrets.url_may_hold_credentials`), so
    such a DATA_URL is hidden from Claude whole (design §6.8).

    SQLAlchemy reads a database password up to its ``@``, so it may hold ``/``, ``?`` or ``#``
    (``postgresql://me:ab/cd@db/sales``), which ``urlsplit`` would take for the path. For a
    database URL everything up to the last ``@`` is cut, and the record starts ``scheme://…@``
    when the cut text held one of those characters.
    """
    parts, cut = secrets.split_url(url)
    segments = parts.path.split("/")
    shown = ["…" if secrets.url_token_like(segment) else segment for segment in segments]
    secret = cut or "@" in parts.netloc or parts.query or parts.fragment or shown != segments
    if not secret:
        return url, False
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:  # a malformed port: record the host alone
        port = None
    if port:
        host = f"{host}:{port}"
    if set(cut) & set("/?#"):
        host = f"…@{host}"
    return f"{parts.scheme}://{host}{'/'.join(shown)}", True


def plan_data(project: Path, data: str, mode: str = "auto", adopt: bool = False) -> DataPlan:
    data = data.strip()
    if not data:
        return DataPlan("", "none", "", "none", "")
    if is_url(data):
        parts = urllib.parse.urlsplit(data)
        reader = "sql" if secrets.SQL_SCHEME.match(parts.scheme) else reader_for(parts.path)
        source, secret = split_secret_url(data)
        plan = DataPlan(data, "url", reader, "url", source, secret_url=data if secret else None)
        if mode == "copy":
            plan.warnings.append("URLs are not downloaded; the first cell reads the URL directly.")
        return plan

    path = Path(os.path.expanduser(data))
    path = (path if path.is_absolute() else project / path).resolve()
    if not path.exists():
        shown = display_path(project, path)
        plan = DataPlan(data, "file", reader_for(path.name), "in-place", shown, path=path)
        plan.warnings.append(f"No file at {data}; recorded it as given.")
        return plan

    kind = "dir" if path.is_dir() else "file"
    size = tree_size(path)
    reader = dir_reader(path) if kind == "dir" else reader_for(path.name)
    plan = DataPlan(
        data, kind, reader, "in-place", display_path(project, path), path=path, size=size
    )
    if adopt:
        if mode == "copy":
            plan.warnings.append("Adopt mode never copies data; it is used in place.")
        return plan
    if mode == "copy":
        copy = not is_within(path, project / "data" / "raw")
    elif mode == "auto":
        copy = not is_within(path, project) and size <= IN_PLACE_BYTES
    else:
        copy = False
    if copy:
        plan.mode = "copy"
        plan.dest = project / "data" / "raw" / path.name
        plan.source = display_path(project, plan.dest)
    return plan


def tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(folder, name))
    return total


def dir_reader(path: Path) -> str:
    kinds: Counter = Counter()
    for index, child in enumerate(path.rglob("*")):
        if index >= 200:
            break
        kind = reader_for(child.name) if child.is_file() else ""
        if kind:
            kinds[kind] += 1
    return kinds.most_common(1)[0][0] if kinds else ""


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def path_from_notebook(project: Path, notebook: str, plan: DataPlan) -> str | None:
    """The data path as the loader cell should write it: the kernel runs in the notebook's
    folder, so a project path is made relative to that folder; data outside the project
    stays absolute. None for URLs and when there is no data."""
    target = plan.dest if plan.mode == "copy" and plan.dest is not None else plan.path
    if plan.kind not in ("file", "dir") or target is None:
        return None
    target = target.resolve()
    if not is_within(target, project):
        return str(target)
    folder = (project / notebook).resolve().parent
    return Path(os.path.relpath(target, folder)).as_posix()


def display_path(project: Path, path: Path) -> str:
    """Project-relative POSIX path when inside the project, else absolute."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(project.resolve()).as_posix()
    except ValueError:
        return str(resolved)


# ----------------------------------------------------------------------- rendering


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "analysis"


def clean_line(text: str | None) -> str:
    """One line of user text: no newlines, no lone surrogates from odd argv bytes."""
    text = (text or "").encode("utf-8", "replace").decode("utf-8")
    return " ".join(text.split())


def toml_string(value: str) -> str:
    """A TOML string that nh's 3.9 fallback reader also parses.

    That reader doesn't track backslash escapes when it strips comments, so a double
    quote never appears escaped: a value with quotes becomes a literal string
    (``'...'``) when it can, else the quote is written as ``\\u0022``.
    """
    if '"' in value and "'" not in value and "\\" not in value and value.isprintable():
        return f"'{value}'"
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append("\\u0022")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_harness_toml(values: Mapping[str, str], defaults_text: str | None = None) -> str:
    """harness.toml = defaults.toml with [project] filled in and every other key commented out.

    Section headers and comments are kept, so the file lists every setting at its default
    without pinning it: a later change to nh's defaults still reaches the project. The
    defaults text is turned into a ``string.Template`` (existing ``$`` escaped) whose only
    placeholders are the [project] values, so user text is inserted verbatim and nothing is
    parsed and re-dumped.
    """
    if defaults_text is None:
        defaults_text = DEFAULTS_TOML.read_text(encoding="utf-8")
    literals = {key: toml_string(values[key]) for key in PROJECT_KEYS if key in values}
    lines = defaults_text.replace("$", "$$").splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)  # the defaults file's own header
    out, section = [], ""
    for line in lines:
        header = _HEADER.match(line)
        if header:
            section = header.group(1)
        elif section == "project":
            pair = _STRING_PAIR.match(line)
            key = pair.group("key") if pair else ""
            if pair and key in literals:
                line = f"{key}{pair.group('eq')}${{{key}}}"
                if pair.group("comment"):
                    visible = len(key) + len(pair.group("eq")) + len(literals[key])
                    gap = max(2, pair.start("comment") - visible)
                    line += " " * gap + pair.group("comment")
        elif line.strip() and not line.lstrip().startswith("#"):
            line = "# " + line  # a default, shown but not pinned
        out.append(line)
    body = HARNESS_HEADER + "\n".join(out).lstrip("\n").rstrip("\n") + "\n"
    return string.Template(body).substitute(literals)


def _template(name: str) -> string.Template:
    return string.Template((TEMPLATES / name).read_text(encoding="utf-8"))


def env_packages(reader: str) -> list[str]:
    packages: list[str] = list(BASE_PACKAGES)
    extra = READER_PACKAGES.get(reader)
    if extra and extra not in packages:
        packages.append(extra)
    return packages


def render_pyproject(name: str, description: str, reader: str) -> str:
    return _template("pyproject.toml.tmpl").substitute(
        name=toml_string(name),
        description=toml_string(description),
        dependencies="\n".join(f"    {toml_string(p)}," for p in env_packages(reader)),
        dev_dependencies="\n".join(f"    {toml_string(p)}," for p in DEV_PACKAGES),
    )


def render_environment_yml(name: str, reader: str) -> str:
    packages = env_packages(reader) + list(DEV_PACKAGES)
    return _template("environment.yml.tmpl").substitute(
        name=name, dependencies="\n".join(f"  - {p}" for p in packages)
    )


def render_notebook_md(
    title: str, goal: str, data: str, problem_type: str, notebook: str, secret_url: bool
) -> str:
    if data and secret_url:
        data = f"`{data}` (the full URL, which may hold credentials, is in `.env` as `DATA_URL`)"
    elif data:
        data = f"`{data}`"
    return _template("NOTEBOOK.md.tmpl").substitute(
        title=title,
        goal=goal or "(not set yet)",
        data=data or "(not set yet)",
        problem_type=problem_type,
        notebook=notebook,
    )


def notebook_dict(title: str, goal: str) -> dict:
    """A new nbformat 4.5 notebook holding only nh's title cell."""
    source = f"# {title}" + (f"\n\nGoal: {goal}" if goal else "")
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "id": "nh-title",
                "metadata": {"nh": {"v": 1, "role": "title", "created_by": "nh-init"}},
                "source": source.splitlines(keepends=True),
            }
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            "nh": {"v": 1, "goal": goal},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_new_notebook(path: Path, title: str, goal: str) -> bool:
    """Create ``path`` with the title cell; False when it already exists (never overwritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(notebook_dict(title, goal), indent=1, ensure_ascii=False) + "\n"
    return create_file(path, text)


def create_file(path: Path, text: str, mode: int = 0o644) -> bool:
    """Write ``text`` only if ``path`` doesn't exist yet. Returns whether it was created."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


# ------------------------------------------------------------- dev deps (existing files)


def _package_pattern(spec: str, yaml: bool) -> re.Pattern:
    name = re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0]
    body = r"[-_.]".join(re.escape(part) for part in re.split(r"[-_.]", name))
    lead = r"(?m)^\s*-\s*[\"']?" if yaml else r"[\"']\s*"
    return re.compile(lead + body + r"(?![-_.\w])", re.I)


def missing_dev_packages(text: str, yaml: bool) -> list[str]:
    return [spec for spec in DEV_PACKAGES if not _package_pattern(spec, yaml).search(text)]


def add_to_pyproject(text: str, specs: list[str]) -> str:
    """Add ``specs`` to ``[dependency-groups] dev`` with a minimal text edit."""
    items = [toml_string(spec) for spec in specs]
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next(
        (i for i, ln in enumerate(lines) if re.match(r"\s*\[dependency-groups\]", ln)), None
    )
    if start is None:
        block = "\n[dependency-groups]\ndev = [\n" + "".join(f"    {i},\n" for i in items) + "]\n"
        return "".join(lines) + block
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"\s*\[[^\]]+\]\s*(#.*)?$", lines[i])),
        len(lines),
    )
    dev = next((i for i in range(start + 1, end) if re.match(r"\s*dev\s*=\s*\[", lines[i])), None)
    if dev is None:
        new = "dev = [\n" + "".join(f"    {i},\n" for i in items) + "]\n"
        return "".join(lines[: start + 1] + [new] + lines[start + 1 :])
    line = lines[dev]
    if line.split("#", 1)[0].rstrip().endswith("]"):  # single-line array
        head, rest = line.split("[", 1)
        inner, tail = rest.rsplit("]", 1)
        inner = inner.strip().rstrip(",")
        lines[dev] = head + "[" + ", ".join(([inner] if inner else []) + items) + "]" + tail
        return "".join(lines)
    close = next((i for i in range(dev + 1, len(lines)) if lines[i].lstrip().startswith("]")), None)
    if close is None:
        raise ScaffoldError("D112", "pyproject.toml has an unterminated dev dependency list.")
    last = next(
        (i for i in range(close - 1, dev, -1) if lines[i].strip()[:1] not in ("", "#")), None
    )
    indent = "    "
    if last is not None:
        indent = lines[last][: len(lines[last]) - len(lines[last].lstrip())]
        code = lines[last].rstrip("\n")
        if not code.split("#", 1)[0].rstrip().endswith(","):
            lines[last] = code.rstrip() + ",\n"
    new_lines = [f"{indent}{i},\n" for i in items]
    return "".join(lines[:close] + new_lines + lines[close:])


def add_to_environment_yml(text: str, specs: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    deps = next((i for i, ln in enumerate(lines) if re.match(r"dependencies\s*:\s*$", ln)), None)
    if deps is None:
        return "".join(lines) + "dependencies:\n" + "".join(f"  - {s}\n" for s in specs)
    indent = "  "
    for ln in lines[deps + 1 :]:
        match = re.match(r"(\s*)-\s", ln)
        if match:
            indent = match.group(1)
            break
    new_lines = [f"{indent}- {spec}\n" for spec in specs]
    return "".join(lines[: deps + 1] + new_lines + lines[deps + 1 :])


@dataclass
class DevDeps:
    missing: list[str]
    diff: str
    new_text: str | None


def dev_deps_change(project: Path, choice: EnvChoice) -> DevDeps:
    """What adding the Jupyter packages to the existing env file would change."""
    if not choice.existing or not choice.file:
        return DevDeps([], "", None)
    path = project / choice.file
    old = path.read_text(encoding="utf-8")
    yaml = choice.manager == "conda"
    missing = missing_dev_packages(old, yaml)
    if not missing:
        return DevDeps([], "", None)
    new = add_to_environment_yml(old, missing) if yaml else add_to_pyproject(old, missing)
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{choice.file}",
            tofile=f"b/{choice.file}",
        )
    )
    return DevDeps(missing, diff, new)


# ------------------------------------------------------------------------ scaffold


@dataclass
class Report:
    project: str
    mode: str  # new | adopt
    paths: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    harness: dict = field(default_factory=dict)
    notebook: str = ""
    adopt: dict = field(default_factory=dict)

    def add(self, path: str, action: str) -> None:
        self.paths.append({"path": path, "action": action})

    def to_dict(self) -> dict:
        return asdict(self)


def cell_source(cell: dict) -> str:
    source = cell.get("source") or ""
    return "".join(source) if isinstance(source, list) else str(source)


def adopt_info(project: Path, notebook: str) -> tuple[str, dict, list[str]]:
    path = Path(os.path.expanduser(notebook))
    path = (path if path.is_absolute() else project / path).resolve()
    if path.suffix != ".ipynb" or not path.is_file():
        raise ScaffoldError("D113", f"No notebook at {notebook}.", "Pass an existing .ipynb file.")
    if not is_within(path, project):
        raise ScaffoldError(
            "D114",
            f"{notebook} is outside the project folder {project}.",
            "Run nhctl scaffold from the folder that contains the notebook.",
        )
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScaffoldError("D113", f"{notebook} is not a readable notebook: {exc}") from exc
    if not isinstance(nb, dict) or nb.get("nbformat") != 4:
        raise ScaffoldError("E136", f"{notebook} is not an nbformat 4 notebook; nh can't use it.")
    warnings = []
    minor = nb.get("nbformat_minor") or 0
    if minor < 5:
        warnings.append(
            f"{notebook} is nbformat 4.{minor}; nh upgrades it to 4.5 (cell ids) on its first write."
        )
    code = [
        cell_source(cell)
        for cell in nb.get("cells") or []
        if isinstance(cell, dict) and cell.get("cell_type") == "code"
    ]
    has_loader = any(_LOADER_CALL.search(src) for src in code)
    info = {"code_cells": len(code), "has_loader": has_loader, "nbformat_minor": minor}
    return display_path(project, path), info, warnings


def ensure_gitignore(project: Path, wanted: tuple[str, ...]) -> str:
    path = project / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in wanted if line not in present]
    if not missing:
        return "kept"
    prefix = ""
    if existing:
        prefix = ("" if existing.endswith("\n") else "\n") + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(prefix + GITIGNORE_MARKER + "\n" + "\n".join(missing) + "\n")
    return "updated" if existing else "created"


def git_tracked(project: Path, rel: str) -> bool:
    """Whether git tracks ``rel`` in ``project`` (False outside a repo or without git)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(project), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def ensure_env_var(project: Path, name: str, value: str) -> tuple[str, str | None]:
    """Append ``NAME='value'`` to .env unless NAME is already set there."""
    path = project / ".env"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    current = re.search(rf"(?m)^\s*(?:export\s+)?{name}\s*=\s*(.*)$", existing)
    if current:
        same = current.group(1).strip().strip("'\"") == value
        return "kept", None if same else f".env already sets {name}; it was left unchanged."
    if "'" in value:
        line = f'{name}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"\n'
    else:
        line = f"{name}='{value}'\n"
    if not existing:
        create_file(path, line, mode=0o600)
        return "created", None
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(("" if existing.endswith("\n") else "\n") + line)
    return "updated", None


def _mkdir(project: Path, rel: str, report: Report, gitkeep: bool) -> None:
    folder = project / rel
    action = "kept" if folder.is_dir() else "created"
    folder.mkdir(parents=True, exist_ok=True)
    if gitkeep:
        create_file(folder / ".gitkeep", "")
    report.add(rel + "/", action)


def _write(project: Path, rel: str, text: str, report: Report) -> None:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    report.add(rel, "created" if create_file(path, text) else "kept")


def _copy_data(plan: DataPlan, project: Path, report: Report) -> None:
    if plan.path is None or plan.dest is None:
        return
    rel = display_path(project, plan.dest)
    if plan.dest.exists():
        if tree_size(plan.dest) == plan.size:
            report.add(rel, "kept")
            return
        # A different file already has that name: keep it and read the original in place.
        plan.mode, plan.dest = "in-place", None
        plan.source = display_path(project, plan.path)
        plan.warnings.append(f"{rel} already exists and differs; the data is used in place.")
        return
    plan.dest.parent.mkdir(parents=True, exist_ok=True)
    if plan.kind == "dir":
        shutil.copytree(plan.path, plan.dest)
    else:
        shutil.copy2(plan.path, plan.dest)
    report.add(rel, "copied")


def data_host(plan: DataPlan) -> str | None:
    """The host key a URL plan's data comes from (``data.example.org``, ``s3://trips-bucket``),
    when it is on the network: a network scheme and a host nh can read that isn't this machine
    (never a database URL, ``file://`` or localhost)."""
    if plan.kind != "url":
        return None
    is_url, host = hosts.network_url(plan.given)
    return host if is_url and host and not hosts.is_loopback(host) else None


def _approve_data_host(project: Path, plan: DataPlan, report: Report, warnings: list[str]) -> str:
    """Merge the data URL's host into ``.nh/state/approved_hosts.json``, so the first cell
    reads it with no question (L012, design §6.4). Only the host: never the URL or its
    credentials. A file that isn't a JSON list is left alone, with a warning."""
    host = data_host(plan)
    if host is None:
        return ""
    path = Layout(project).approved_hosts
    action = hosts.approve(path, [host])
    report.add(display_path(project, path), action)
    if action == "skipped":
        warnings.append(
            f"{display_path(project, path)} is not a JSON list of hosts, so nh left it alone and "
            f"did not approve {host}: the first cell will ask before reading the data."
        )
        return ""
    return host


def scaffold(
    project: Path,
    *,
    goal: str = "",
    data: str = "",
    problem_type: str = "eda",
    data_mode: str = "auto",
    env_manager: str = "auto",
    adopt: str | None = None,
    name: str | None = None,
    add_dev_deps: bool = False,
    tools: Tools | None = None,
    home: Path | None = None,
) -> Report:
    """Scaffold (or adopt) ``project``. Idempotent: a re-run reports every path as kept."""
    if problem_type not in PROBLEM_TYPES:
        raise ScaffoldError(
            "D115",
            f"Unknown problem type {problem_type!r}.",
            "Use one of: " + ", ".join(PROBLEM_TYPES) + ".",
        )
    if data_mode not in DATA_MODES:
        raise ScaffoldError(
            "D115", f"Unknown data mode {data_mode!r}.", "Use auto, in-place or copy."
        )
    if env_manager not in ENV_MANAGERS:
        raise ScaffoldError(
            "D115", f"Unknown env manager {env_manager!r}.", "Use auto, uv or conda."
        )
    project = project.resolve()
    home = (home or Path.home()).resolve()
    if not project.is_dir():
        raise ScaffoldError("D116", f"{project} is not a folder.")
    if project in (home, Path(project.anchor)):
        raise ScaffoldError(
            "D106",
            f"nh won't set up a project in {project}.",
            "Create a folder for this analysis and run /nh:init there.",
        )

    tools = tools or Tools.find()
    choice = choose_env(project, tools, env_manager)
    if choice.manager is None:
        raise ScaffoldError(
            "D110",
            "Neither uv nor conda is installed, so nh can't build the project environment.",
            "Install uv (https://docs.astral.sh/uv/getting-started/installation/), then rerun.",
        )
    warnings: list[str] = []
    missing_tool = (choice.manager == "uv" and not tools.uv) or (
        choice.manager == "conda" and not tools.conda_like
    )
    if missing_tool and env_manager == "auto":
        raise ScaffoldError(
            "D111",
            f"{choice.file} asks for conda, but neither conda nor mamba is installed.",
            "Install Miniforge (https://conda-forge.org/download/), or remove "
            f"{choice.file} to use uv instead.",
        )
    if missing_tool:
        warnings.append(f"{choice.manager} is not installed; nhctl env sync will need it.")

    report = Report(str(project), "adopt" if adopt else "new")
    if adopt:
        notebook, report.adopt, adopt_warnings = adopt_info(project, adopt)
        warnings.extend(adopt_warnings)
    else:
        notebook = DEFAULT_NOTEBOOK

    data = (data or "").strip().encode("utf-8", "replace").decode("utf-8")
    plan = plan_data(project, data, data_mode, adopt=bool(adopt))
    if not adopt:
        for rel in FOLDERS:
            _mkdir(project, rel, report, gitkeep=rel in GITKEEP_FOLDERS)
        if plan.mode == "copy":
            _copy_data(plan, project, report)

    goal = clean_line(goal)
    if not goal and plan.given:
        where = urllib.parse.urlsplit(plan.source).path if plan.kind == "url" else plan.source
        goal = f"Explore {os.path.basename(where.rstrip('/')) or plan.source}"
    display_name = clean_line(name) or project.name
    values = {
        "name": slugify(display_name),
        "goal": goal,
        "problem_type": problem_type,
        "data_source": plan.source,
        "notebook": notebook,
        "env_manager": choice.manager,
    }

    (project / ".nh").mkdir(exist_ok=True)
    _write(project, ".nh/README.md", (TEMPLATES / "nh_README.md.tmpl").read_text("utf-8"), report)
    _write(project, ".nh/.gitignore", NH_GITIGNORE, report)
    approved_host = _approve_data_host(project, plan, report, warnings)
    _write(project, "harness.toml", render_harness_toml(values), report)
    notebook_md = render_notebook_md(
        display_name, goal, plan.source, problem_type, notebook, bool(plan.secret_url)
    )
    _write(project, "NOTEBOOK.md", notebook_md, report)

    env_file = choice.file or ""
    if not choice.existing:
        # Adopt mode also needs an env file to build JupyterLab from, so it writes one when none exists.
        text = (
            render_pyproject(values["name"], goal or display_name, plan.reader)
            if choice.manager == "uv"
            else render_environment_yml(values["name"], plan.reader)
        )
        _write(project, env_file, text, report)
        dev = DevDeps([], "", None)
    else:
        dev = dev_deps_change(project, choice)
        if dev.new_text is not None and add_dev_deps:
            (project / env_file).write_text(dev.new_text, encoding="utf-8")
            report.add(env_file, "updated")
        else:
            report.add(env_file, "kept")

    secret_stored = False
    if plan.secret_url and git_tracked(project, ".env"):
        report.add(".env", "skipped")  # a committed .env would publish the credentials
        warnings.append(
            ".env is committed to git, so nh did not put the data URL (it holds credentials) "
            "there. Untrack it with `git rm --cached .env` (the file stays; .gitignore then keeps "
            "it out), then run /nh:init again to store DATA_URL."
        )
    elif plan.secret_url:
        action, note = ensure_env_var(project, "DATA_URL", plan.secret_url)
        secret_stored = True
        report.add(".env", action)
        if note:
            warnings.append(note)
    ignores = NH_ARTIFACT_IGNORES if adopt else GITIGNORE_LINES
    report.add(".gitignore", ensure_gitignore(project, ignores))

    if not adopt:
        title = f"{display_name}: exploratory analysis"
        created = write_new_notebook(project / DEFAULT_NOTEBOOK, title, goal)
        report.add(DEFAULT_NOTEBOOK, "created" if created else "kept")

    applied = add_dev_deps and dev.new_text is not None
    report.env = {
        "manager": choice.manager,
        "file": env_file,
        "existing": choice.existing,
        "reason": choice.reason,
        "tool": (tools.uv if choice.manager == "uv" else tools.conda_like),
        "missing_dev": [] if applied else dev.missing,
        "dev_diff": "" if applied else dev.diff,
        "dev_added": dev.missing if applied else [],
    }
    report.data = {
        "given": "" if plan.secret_url else plan.given,
        "kind": plan.kind,
        "mode": plan.mode,
        "source": plan.source,
        "path_from_notebook": path_from_notebook(project, notebook, plan),
        "reader": plan.reader,
        "bytes": plan.size,
        "secret_in_env": secret_stored,
        "approved_host": approved_host,
    }
    report.harness = values
    report.notebook = notebook
    report.warnings = warnings + plan.warnings
    return report
