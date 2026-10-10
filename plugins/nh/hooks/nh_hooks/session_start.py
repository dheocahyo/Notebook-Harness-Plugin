"""SessionStart: tell Claude how nh works in this project (also re-injected after compaction)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from common import PLUGIN_ROOT, Payload, context, setting, settings

from nh_gateway._shared import harness_toml, secrets
from nh_gateway._shared.paths import Layout, read_json

GOAL_MAX_CHARS = 200
SERVER_FILE = re.compile(r"jpserver-\d+\.json")
# The senior preset's explanation depth (design §6.9): SessionStart context only, never the
# per-turn reminder. Junior gets no line: it is the depth the skills default to. It names the
# reply contract, which comes later and says more, so the contract shrinks rather than wins.
SENIOR_LINE = (
    "Preset: senior. Keep explanations short. After a cell runs, answer the reply contract in a "
    "single short paragraph of a few plain sentences, not a paragraph per part, without headings, "
    "labels or bullet lists: what changed, what to check in the output (the numbers that matter, "
    "surprises first) and the proposed next cell; skip a part with nothing to say. Don't explain "
    "what common pandas methods do; /nh:explain still walks through every part in numbered steps, "
    "without defining methods."
)
LAB_NOT_FOUND = (
    "JupyterLab: no running server found for this project. Before notebook work, call "
    'mcp__plugin_nh_nh__nh_inspect(view="status"); only if it reports no JupyterLab (E130), '
    "ask the user whether to start it, then run `nhctl lab start`."
)


def handle(layout: Layout, payload: Payload) -> Payload | None:
    config = settings(layout)
    # The project's redactor (design §6.8): the goal before its cut, then the whole context.
    redactor = secrets.install(secrets.Redactor.for_project(layout.project))
    goal = " ".join(redactor.redact(str(setting(config, "project", "goal", ""))).split())
    notebook = str(setting(config, "project", "notebook", "")).strip()
    facts = []
    if goal:
        if len(goal) > GOAL_MAX_CHARS:
            goal = goal[: GOAL_MAX_CHARS - 1].rstrip() + "…"
        facts.append(f"Goal: {goal}.")
    if notebook:
        facts.append(f"Main notebook: {notebook}.")
    lines = [
        "Notebook Harness (nh) is active in this project: the notebook grows by one reviewed "
        "cell per user message.",
        " ".join(facts),
        SENIOR_LINE if harness_toml.preset_level(config)[0] == "senior" else None,
        "Before any notebook work, load the skill nh:notebook, and call "
        "mcp__plugin_nh_nh__nh_inspect before your first write.",
        lab_status(read_json(layout.lab_json), str(layout.project)),
        None if eval_run() else runtime_status(os.environ.get("CLAUDE_PLUGIN_DATA", "")),
        "Never Read, Write, Edit or NotebookEdit .ipynb files in this project, and never change "
        "them from Bash: notebooks change only through the nh tools. Never edit .nh/ (nh's own "
        "state); settings live in harness.toml.",
    ]
    return context("SessionStart", redactor.redact("\n".join(line for line in lines if line)))


def lab_status(lab: Any, project: str) -> str:
    """lab.json's server when its pid and jpserver file are alive, else any live Jupyter
    server whose root contains the project (the gateway uses those too)."""
    if isinstance(lab, dict) and pid_alive(lab.get("pid")):
        runtime_file = lab.get("runtime_file")
        if not isinstance(runtime_file, str) or not runtime_file or os.path.exists(runtime_file):
            return running_line(lab.get("url"), lab["pid"])
    found = project_server(project)
    if found is not None:
        return running_line(*found)
    return LAB_NOT_FOUND


def running_line(url: Any, pid: int) -> str:
    where = f" at {public_url(url)}" if isinstance(url, str) and url else ""
    return f"JupyterLab: running{where} (pid {pid})."


def runtime_dirs() -> list[str]:
    """Where Jupyter servers write ``jpserver-<pid>.json`` (as the gateway's discovery)."""
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    dirs: list[str] = []
    for item in (
        os.environ.get("JUPYTER_RUNTIME_DIR"),
        os.path.join(home, "Library", "Jupyter", "runtime"),
        os.path.join(home, "Library", "Application Support", "Jupyter", "runtime"),
        os.path.join(xdg, "jupyter", "runtime"),
    ):
        if item and os.path.realpath(item) not in dirs:
            dirs.append(os.path.realpath(item))
    return dirs


def project_server(project: str) -> tuple[str, int] | None:
    """(url, pid) of the live jupyter_server >= 2 serving ``project``, preferring the one
    whose root is the project itself, then the deepest root, then the newest file."""
    project = os.path.realpath(project)
    best: tuple[tuple[bool, int, float], str, int] | None = None
    for folder in runtime_dirs():
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not SERVER_FILE.fullmatch(name):
                continue
            path = os.path.join(folder, name)
            record = read_json(Path(path))
            if not isinstance(record, dict):
                continue
            url, pid = record.get("url"), record.get("pid")
            root = record.get("root_dir") or record.get("notebook_dir")
            if not isinstance(url, str) or not url or not isinstance(root, str) or not root:
                continue
            if too_old(record.get("version")) or not pid_alive(pid):
                continue
            root = os.path.realpath(root)
            if project != root and not project.startswith(root.rstrip(os.sep) + os.sep):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            key = (root == project, root.count(os.sep), mtime)
            if best is None or key > best[0]:
                best = (key, url, pid)
    return None if best is None else (best[1], best[2])


def too_old(version: Any) -> bool:
    """jupyter_server 1.x: the gateway refuses it (E130)."""
    try:
        return int(str(version).split(".")[0]) < 2
    except ValueError:
        return False


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def public_url(url: str) -> str:
    """Drop the query and fragment: a token must never reach the transcript."""
    return url.split("#", 1)[0].split("?", 1)[0]


def eval_run() -> bool:
    """`claude plugin eval` sets CLAUDE_CODE_EVAL_CONFINED=1: the nh tools are mocked and no
    runtime is built (nh-hook skips nh-sync), so the runtime line would only mislead."""
    return os.environ.get("CLAUDE_CODE_EVAL_CONFINED", "") not in ("", "0", "false")


def runtime_status(data: str) -> str | None:
    if not data:
        return None
    venv = runtime_venv(data)
    if venv and os.path.isfile(os.path.join(venv, ".nh-ready")):
        return "nh runtime: ready."
    log = os.path.join(data, "logs", "sync.log")
    return (
        "nh runtime: installing in the background (the first install takes about a minute; "
        f"log: {log}). If the nh tools are missing, ask the user to run /mcp, select "
        "plugin:nh:nh and choose Reconnect once it finishes."
    )


def runtime_venv(data: str) -> str | None:
    """The venv nh-sync builds for this plugin version: venv-<12 hex of sha256(uv.lock)>."""
    digest = hashlib.sha256()
    try:
        with open(os.path.join(PLUGIN_ROOT, "server", "uv.lock"), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError:
        return None
    return os.path.join(data, "venv-" + digest.hexdigest()[:12])
