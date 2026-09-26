"""nhctl scaffold and nhctl notebook new."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from common import NhctlError, Result, harness_project, project_root, rel

from nh_gateway._shared.scaffold import core


def add_parsers(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    parser = sub.add_parser(
        "scaffold", parents=[common], help="create (or adopt) an nh project in this folder"
    )
    parser.add_argument("--goal", default="", help="what the analysis should answer")
    parser.add_argument("--data", default="", help="data file, folder or URL")
    parser.add_argument("--data-mode", choices=core.DATA_MODES, default="auto")
    parser.add_argument("--problem-type", choices=core.PROBLEM_TYPES, default="eda")
    parser.add_argument("--env-manager", choices=core.ENV_MANAGERS, default="auto")
    parser.add_argument("--adopt", metavar="NB", help="use this existing notebook")
    parser.add_argument("--name", help="project name (default: the folder name)")
    parser.add_argument(
        "--add-dev-deps",
        action="store_true",
        help="add JupyterLab to an existing env file (only after the user agreed to the diff)",
    )
    parser.set_defaults(func=cmd_scaffold)

    notebook = sub.add_parser("notebook", parents=[common], help="notebook files")
    nb_sub = notebook.add_subparsers(dest="action", required=True)
    new = nb_sub.add_parser("new", parents=[common], help="create a new notebook")
    new.add_argument("path")
    new.add_argument("--title", help="title cell text (default: from the file name)")
    new.set_defaults(func=cmd_notebook_new)


def cmd_scaffold(args: argparse.Namespace) -> Result:
    project = Path(os.path.abspath(getattr(args, "project", None) or os.getcwd()))
    enclosing = project_root(str(project), required=False)
    if enclosing is not None and enclosing != project.resolve():
        raise NhctlError(
            "D117",
            f"{project} is inside the nh project {enclosing}.",
            f"Run nhctl scaffold in {enclosing}, or pass --project to choose explicitly.",
        )
    data = args.data.strip()
    if data and not core.is_url(data):
        data = os.path.abspath(os.path.expanduser(data))
    adopt = os.path.abspath(os.path.expanduser(args.adopt)) if args.adopt else None
    try:
        report = core.scaffold(
            project,
            goal=args.goal,
            data=data,
            problem_type=args.problem_type,
            data_mode=args.data_mode,
            env_manager=args.env_manager,
            adopt=adopt,
            name=args.name,
            add_dev_deps=args.add_dev_deps,
        )
    except core.ScaffoldError as exc:
        raise NhctlError(exc.code, exc.message, exc.fix) from exc
    data_out = dict(report.to_dict(), ok=True)
    return Result(data_out, _scaffold_text(report))


def _scaffold_text(report: core.Report) -> str:
    lines = [f"nh project {'adopted' if report.mode == 'adopt' else 'ready'}: {report.project}"]
    width = max((len(p["path"]) for p in report.paths), default=0)
    lines += [f"  {p['path']:<{width}}  {p['action']}" for p in report.paths]
    env = report.env
    lines.append(f"env: {env['manager']} ({env['file']}; {env['reason']})")
    if env["missing_dev"]:
        lines.append(f"{env['file']} lacks {', '.join(env['missing_dev'])}. Proposed change:")
        lines.append(env["dev_diff"].rstrip("\n"))
        lines.append("Rerun with --add-dev-deps once the user agrees.")
    data = report.data
    if data["kind"] != "none":
        where = data["source"] + (
            " (full URL in .env as DATA_URL)" if data["secret_in_env"] else ""
        )
        lines.append(f"data: {where} [{data['mode']}]")
        if data["path_from_notebook"] and data["path_from_notebook"] != data["source"]:
            lines.append(f"  read it from the notebook as: {data['path_from_notebook']}")
    lines += [f"warning: {w}" for w in report.warnings]
    return "\n".join(lines)


def cmd_notebook_new(args: argparse.Namespace) -> Result:
    project = project_root(getattr(args, "project", None))
    assert project is not None
    raw = Path(os.path.expanduser(args.path))
    path = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
    if path.suffix != ".ipynb":
        raise NhctlError(
            "D118", f"{args.path} is not an .ipynb path.", "Use a name ending in .ipynb."
        )
    if not core.is_within(path, project):
        raise NhctlError(
            "D114",
            f"{args.path} is outside the nh project {project}.",
            "Create notebooks inside the project folder.",
        )
    title = core.clean_line(args.title) or path.stem.replace("_", " ").replace("-", " ").strip()
    goal = harness_project(project).get("goal")
    goal = goal if isinstance(goal, str) else ""
    shown = rel(project, path)
    if not core.write_new_notebook(path, title or path.stem, goal):
        raise NhctlError(
            "D119", f"{shown} already exists; nh never overwrites notebooks.", "Pick another name."
        )
    return Result({"ok": True, "notebook": shown, "created": True}, f"created {shown}")
