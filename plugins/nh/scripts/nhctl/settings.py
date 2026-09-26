"""nhctl settings apply: the optional hard backstop in <proj>/.claude/settings.json.

Only adds ``permissions.deny`` rules; every other key is kept as is. Without --yes it
prints the diff and exits 2 so the user can decide.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import tempfile
from pathlib import Path

import common
from common import NhctlError, Result

DENY_RULES = ("Edit(/**/*.ipynb)", "Edit(/.nh/**)")


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    settings = sub.add_parser("settings", parents=[common_opts], help="Claude Code settings")
    settings_sub = settings.add_subparsers(dest="action", required=True)
    apply = settings_sub.add_parser(
        "apply",
        parents=[common_opts],
        help="deny raw .ipynb and .nh/ edits in .claude/settings.json",
    )
    apply.add_argument("--yes", action="store_true", help="write it (otherwise show the diff)")
    apply.set_defaults(func=cmd_apply)


def merged(settings: dict) -> dict:
    result = dict(settings)
    permissions = result.get("permissions")
    if permissions is None:
        permissions = {}
    elif not isinstance(permissions, dict):
        raise NhctlError(
            "D161", '.claude/settings.json has a "permissions" value that is not an object.'
        )
    permissions = dict(permissions)
    deny = permissions.get("deny", [])
    if not isinstance(deny, list):
        raise NhctlError(
            "D161", '.claude/settings.json has a "permissions.deny" that is not a list.'
        )
    permissions["deny"] = deny + [rule for rule in DENY_RULES if rule not in deny]
    result["permissions"] = permissions
    return result


def dump(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def cmd_apply(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    path = project / ".claude" / "settings.json"
    shown = ".claude/settings.json"
    try:
        old_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    try:
        current = json.loads(old_text) if old_text.strip() else {}
    except ValueError as exc:
        raise NhctlError(
            "D161",
            f"{shown} is not valid JSON ({exc}); nh won't rewrite it.",
            "Fix the file, then rerun.",
        ) from exc
    if not isinstance(current, dict):
        raise NhctlError("D161", f"{shown} does not hold a JSON object; nh won't rewrite it.")
    new = merged(current)
    new_text = dump(new)
    if new == current:
        data = {"ok": True, "changed": False, "path": shown, "deny": list(DENY_RULES)}
        return Result(data, f"{shown} already denies raw .ipynb and .nh/ edits.")
    before = old_text if old_text.endswith("\n") or not old_text else old_text + "\n"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{shown}",
            tofile=f"b/{shown}",
        )
    )
    if not args.yes:
        data = {"ok": False, "changed": False, "needs_confirmation": True, "path": shown}
        data["diff"] = diff
        text = f"{diff.rstrip()}\n\nNothing written. Rerun with --yes to apply."
        return Result(data, text, 2)
    write_keeping_mode(path, new_text)
    data = {"ok": True, "changed": True, "path": shown, "diff": diff, "deny": list(DENY_RULES)}
    return Result(data, f"Updated {shown}:\n{diff.rstrip()}")


def write_keeping_mode(path: Path, text: str) -> None:
    """Atomic replace that keeps an existing file's permissions (0644 for a new one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
