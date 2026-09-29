"""nhctl doctor: read-only checks for /nh:init and /nh:status.

Each problem is {code, message, fix, blocking}; ``ok`` is false when any problem blocks.
Nothing here writes to disk or talks to a server beyond reading jupyter runtime files.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

import common
import envsync
import lab
import settings
from common import Result

from nh_gateway._shared import paths, tomlread
from nh_gateway._shared.scaffold import core

RESERVED_SECTIONS = {"preset", "guardrails", "secrets", "libraries", "comprehension"}
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages"}
SCAN_DEPTH = 3
SCAN_BUDGET = 5000  # directory entries; keeps doctor fast in a huge folder
MAX_LISTED = 20


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    doctor = sub.add_parser("doctor", parents=[common_opts], help="check nh's prerequisites")
    doctor.set_defaults(func=cmd_doctor)


class Problems(list):
    def add(self, code: str, message: str, fix: str, blocking: bool = False) -> None:
        self.append({"code": code, "message": message, "fix": fix, "blocking": blocking})


# ---------------------------------------------------------------------- machine


def check_claude(problems: Problems) -> dict:
    floor = ".".join(str(n) for n in common.MIN_CLAUDE)
    path = find_claude()
    info = {"path": path, "version": None, "min": floor, "ok": None}
    if not path:
        problems.add(
            "D102",
            "Can't find the claude command to check its version.",
            f"Make sure Claude Code is {floor} or newer (claude --version).",
        )
        return info
    code, out = common.run([path, "--version"], timeout=15)
    version = common.version_tuple(out) if code == 0 else ()
    info["version"] = ".".join(str(n) for n in version) or None
    if len(version) < 3:
        problems.add(
            "D102",
            "Couldn't read the Claude Code version.",
            f"Check that claude --version shows {floor} or newer.",
        )
        return info
    info["ok"] = version >= common.MIN_CLAUDE
    if not info["ok"]:
        problems.add(
            "D101",
            f"Claude Code {info['version']} is older than {floor}, which nh needs.",
            "Run: claude update, then restart Claude Code.",
            blocking=True,
        )
    return info


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("~/.local/bin/claude", "~/.claude/local/claude"):
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def check_tools(problems: Problems, tools: core.Tools, runtime_ready: bool | None) -> dict:
    uv_version = None
    if tools.uv:
        code, out = common.run([tools.uv, "--version"], timeout=15)
        uv_version = ".".join(str(n) for n in common.version_tuple(out)) if code == 0 else None
    conda_version = None
    if tools.conda:
        code, out = common.run([tools.conda, "--version"], timeout=30)
        conda_version = ".".join(str(n) for n in common.version_tuple(out)) if code == 0 else None
    if not tools.uv and not tools.conda_like:
        problems.add(
            "D110",
            "Neither uv nor conda is installed, so nh can't build a project environment.",
            "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            blocking=True,
        )
    elif not tools.uv and not runtime_ready:
        problems.add(
            "D124",
            "uv is not installed, and nh's own runtime is built with uv.",
            "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            blocking=True,
        )
    elif uv_version and common.version_tuple(uv_version) < common.MIN_UV:
        problems.add(
            "D113",
            f"uv {uv_version} is older than 0.10.",
            "Run: uv self update (or upgrade uv the way you installed it).",
            blocking=not runtime_ready,
        )
    return {
        "uv": {"path": tools.uv, "version": uv_version},
        "conda": {"path": tools.conda, "version": conda_version},
        "mamba": {"path": tools.mamba},
    }


def check_runtime(problems: Problems, explicit: str | None) -> dict:
    try:
        data = common.plugin_data_dir(explicit)
    except common.NhctlError:
        problems.add(
            "D121",
            "Can't locate nh's plugin data folder, so the runtime check was skipped.",
            'Pass --plugin-data "${CLAUDE_PLUGIN_DATA}".',
        )
        return {"data_dir": None, "venv": None, "ready": None}
    status = envsync.runtime_status(data)
    if not status["ready"]:
        problems.add(
            "D120",
            "nh's own Python runtime isn't installed yet, so its tools can't start.",
            'Run: nhctl runtime sync --plugin-data "${CLAUDE_PLUGIN_DATA}", then /mcp -> Reconnect.',
        )
    return status


# ---------------------------------------------------------------------- project


def scan(project: Path) -> tuple[list[dict], list[str]]:
    """(data candidates, notebooks) near the top of the project, within a small budget."""
    data: list[dict] = []
    notebooks: list[str] = []
    root_depth = len(project.parts)
    budget = SCAN_BUDGET
    for folder, dirs, files in os.walk(project):
        budget -= len(files) + len(dirs)
        if budget < 0:
            break
        here = Path(folder)
        depth = len(here.parts) - root_depth
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS and depth < SCAN_DEPTH
        )
        for name in sorted(files):
            path = here / name
            if name.endswith(".ipynb"):
                notebooks.append(common.rel(project, path))
            elif core.reader_for(name):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                data.append({"path": common.rel(project, path), "bytes": size})
    return data[:MAX_LISTED], notebooks[:MAX_LISTED]


def harness_problems(project: Path) -> list[str]:
    path = project / paths.HARNESS_TOML
    if not path.is_file():
        return []
    try:
        user = tomlread.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # tomllib and the 3.9 fallback raise different errors
        return [f"harness.toml can't be parsed: {exc}"]
    defaults = tomlread.load(core.DEFAULTS_TOML)
    return [f"unknown key {key}" for key in unknown_keys(defaults, user, "")]


def unknown_keys(base: dict, user: dict, prefix: str) -> list[str]:
    found = []
    for key, value in user.items():
        where = f"{prefix}.{key}" if prefix else key
        if key not in base:
            if not (not prefix and key in RESERVED_SECTIONS):
                found.append(where)
        elif isinstance(base[key], dict) and isinstance(value, dict):
            found.extend(unknown_keys(base[key], value, where))
    return found


def check_project(problems: Problems, start: Path, tools: core.Tools) -> tuple[dict, Path | None]:
    found = paths.find_project(str(start))
    project = found or start
    choice = core.choose_env(project, tools)
    unsafe = found is None and project.resolve() in (Path.home().resolve(), Path(project.anchor))
    data, notebooks = ([], []) if unsafe else scan(project)
    info = {
        "dir": str(project),
        "nh_enabled": found is not None,
        "harness_toml": (project / paths.HARNESS_TOML).is_file(),
        "env_manager": choice.manager,
        "env_file": choice.file,
        "env_file_exists": choice.existing,
        "notebooks": notebooks,
        "data_candidates": data,
    }
    if unsafe:
        problems.add(
            "D106",
            f"{project} is your home (or root) folder; nh won't set up a project there.",
            "Create a folder for this analysis and run /nh:init there.",
            blocking=True,
        )
    if choice.manager == "conda" and not tools.conda_like:
        problems.add(
            "D111",
            f"{choice.file} asks for conda, but neither conda nor mamba is installed.",
            "Install Miniforge (https://conda-forge.org/download/), or remove it to use uv.",
            blocking=True,
        )
    config = harness_problems(project)
    if config:
        problems.add(
            "D131",
            "harness.toml has problems: " + "; ".join(config) + ".",
            "Fix or delete those lines; nh uses its defaults meanwhile.",
        )
    return info, found


def check_env(problems: Problems, layout: paths.Layout) -> dict | None:
    env = common.read_env_json(layout)
    prefix = env.get("prefix")
    if not env or not prefix or not (Path(prefix) / "bin" / "python").exists():
        problems.add(
            "D140",
            "The project environment isn't built yet.",
            "Run: nhctl env sync --background, then nhctl env wait.",
        )
        return env or None
    problem = envsync.env_problem(env)
    if problem:
        problems.add(
            "D141",
            f"The project environment is too old for nh: {problem}",
            "Raise the versions in the env file (jupyterlab>=4.6,<5, jupyter-collaboration>=5,<6), "
            "then run nhctl env sync.",
            blocking=True,
        )
    return env


def check_lab(problems: Problems, layout: paths.Layout, servers: list[dict]) -> dict:
    record, server = lab.current_lab(layout)
    if server is not None:
        return {"running": True, "url": lab.public_url(server), "pid": record.get("pid")}
    # No lab.json: a live server the user started on the project is still what the
    # gateway uses (doctor only reads runtime files, so it isn't probed here).
    found = next((s for s in servers if s["serves_project"] and s["usable"]), None)
    if found is not None:
        problems.add(
            "D148",
            f"JupyterLab at {found['url']} (pid {found['pid']}) serves this project but was "
            "started outside nh.",
            "Run: nhctl lab start (it records that server instead of starting another).",
        )
        return {"running": True, "url": found["url"], "pid": found["pid"], "registered": False}
    problems.add("D148", "The project's JupyterLab is not running.", "Run: nhctl lab start")
    return {"running": False}


def check_servers(problems: Problems, project: Path) -> list[dict]:
    servers = []
    for _, info in lab.server_files(lab.runtime_dirs()):
        root = lab.server_root(info) or Path("/")
        serves = core.is_within(project, root)
        version = str(info.get("version") or "")
        old = bool(version) and common.version_tuple(version) < (2,)
        servers.append(
            {
                "url": lab.public_url(info),
                "pid": info.get("pid"),
                "root_dir": str(root),
                "version": info.get("version"),
                "serves_project": serves,
                "usable": not old,
            }
        )
        if serves and old:
            problems.add(
                "D149",
                f"An older JupyterLab at {lab.public_url(info)} (jupyter_server "
                f"{info.get('version')}) serves this folder; nh doesn't use it.",
                "Use the project's own JupyterLab: nhctl lab start.",
            )
    return servers


def check_settings(problems: Problems, project: Path) -> dict:
    rules: set[str] = set()
    for name in ("settings.json", "settings.local.json"):
        data = paths.read_json(project / ".claude" / name, {})
        perms = data.get("permissions") if isinstance(data, dict) else None
        deny = perms.get("deny") if isinstance(perms, dict) else None
        if isinstance(deny, list):
            rules.update(r for r in deny if isinstance(r, str))
    present = set(settings.DENY_RULES) <= rules
    if not present:
        problems.add(
            "D160",
            "Optional: .claude/settings.json doesn't deny raw edits to notebooks and .nh/.",
            "/nh:init offers to add the two deny rules (it shows the diff first).",
        )
    return {"deny_rules": present}


def nbstripout_findings(project: Path) -> list[str]:
    roots = [project]
    code, out = common.run(["git", "rev-parse", "--show-toplevel"], timeout=10, cwd=project)
    if code == 0 and out.strip() and Path(out.strip()) != project:
        roots.append(Path(out.strip()))
    findings = []
    for root in roots:
        attributes = root / ".gitattributes"
        try:
            text = attributes.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if re.search(r"filter\s*=\s*nbstripout", text):
            code, clean = common.run(
                ["git", "config", "--get", "filter.nbstripout.clean"], timeout=10, cwd=root
            )
            if code == 0 and "--keep-id" not in clean:
                findings.append(f"the git filter from {common.rel(project, attributes)}")
        config = root / ".pre-commit-config.yaml"
        try:
            hooks = config.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in re.split(r"(?m)^\s*-\s+(?=id\s*:)", hooks)[1:]:
            hook_id = re.match(r"id\s*:\s*['\"]?([\w.-]+)", block)
            if hook_id and hook_id.group(1) == "nbstripout" and "--keep-id" not in block:
                findings.append(f"the pre-commit hook in {common.rel(project, config)}")
    return sorted(set(findings))


def check_nbstripout(problems: Problems, project: Path) -> None:
    findings = nbstripout_findings(project)
    if findings:
        problems.add(
            "D170",
            "nbstripout runs without --keep-id (" + ", ".join(findings) + "), so commits "
            "renumber cell ids and nh falls back to its own metadata to find cells.",
            "Add --keep-id: args: [--keep-id] in .pre-commit-config.yaml, or rerun "
            "nbstripout --install --keep-id for the git filter.",
        )


# ---------------------------------------------------------------------- command


def cmd_doctor(args: argparse.Namespace) -> Result:
    # Installs the project's redactor first: a check's message may echo a .env value (§6.8).
    common.project_root(getattr(args, "project", None), required=False)
    problems = Problems()
    if sys.platform.startswith(("win", "cygwin")):
        problems.add(
            "E137", "nh v0.1 does not support Windows.", "Run Claude Code under WSL.", True
        )
    start = Path(os.path.abspath(getattr(args, "project", None) or os.getcwd()))
    claude = check_claude(problems)
    runtime = check_runtime(problems, getattr(args, "plugin_data", None))
    tools = core.Tools.find()
    tool_info = check_tools(problems, tools, runtime.get("ready"))
    project_info, found = check_project(problems, start, tools)
    project = found or start
    env = lab_info = settings_info = None
    servers = check_servers(problems, project)
    if found is not None:
        layout = paths.Layout(found)
        env = check_env(problems, layout)
        lab_info = check_lab(problems, layout, servers)
        settings_info = check_settings(problems, found)
    check_nbstripout(problems, project)

    blocking = any(p["blocking"] for p in problems)
    data = {
        "ok": not blocking,
        "platform": sys.platform,
        "python": ".".join(str(n) for n in sys.version_info[:3]),
        "plugin_root": str(common.ROOT),
        "claude_code": claude,
        **tool_info,
        "runtime": runtime,
        "project": project_info,
        "env": env,
        "lab": lab_info,
        "jupyter": {"servers": servers},
        "settings": settings_info,
        "problems": list(problems),
    }
    return Result(data, doctor_text(data), 0 if not blocking else 1)


RUNTIME_STATES = {True: "ready", False: "not ready", None: "unknown (pass --plugin-data)"}


def doctor_text(data: dict) -> str:
    cc = data["claude_code"]
    project = data["project"]
    lines = [
        f"nh doctor: {'OK' if data['ok'] else 'BLOCKED'}",
        f"  Claude Code: {cc['version'] or 'unknown'} (needs {cc['min']})",
        f"  uv: {data['uv']['version'] or 'not found'}   conda: {data['conda']['version'] or 'not found'}",
        f"  nh runtime: {RUNTIME_STATES[data['runtime'].get('ready')]}",
        f"  project: {project['dir']} ({'nh project' if project['nh_enabled'] else 'not set up yet'}; "
        f"env: {project['env_manager'] or 'none'})",
    ]
    if data["lab"]:
        state = f"running at {data['lab']['url']}" if data["lab"]["running"] else "not running"
        lines.append(f"  JupyterLab: {state}")
    for problem in data["problems"]:
        tag = "BLOCKING" if problem["blocking"] else "warning"
        lines.append(f"{tag} {problem['code']}: {problem['message']}")
        if problem["fix"]:
            lines.append(f"  fix: {problem['fix']}")
    return "\n".join(lines)
