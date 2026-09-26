"""bin/nhctl itself: symlinked invocation, option placement, usage errors, stdlib only."""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

from nhctl_testlib import NHCTL, PLUGIN

SCRIPTS = PLUGIN / "scripts" / "nhctl"
SCAFFOLD = PLUGIN / "server" / "src" / "nh_gateway" / "_shared" / "scaffold"


def test_runs_through_a_relative_symlink(env, project):
    link_dir = env.tmp / "links"
    link_dir.mkdir()
    link = link_dir / "nhctl"
    link.symlink_to(os.path.relpath(NHCTL, link_dir))
    proc = env.run("--help", cwd=project, nhctl=link)
    assert proc.returncode == 0, proc.stderr
    assert "scaffold" in proc.stdout and "fresh-run" in proc.stdout


def test_json_flag_before_or_after_the_command(env, project):
    env.script("uv", 'echo "uv 0.10.6"')
    before = env.run("--json", "doctor", "--plugin-data", str(env.tmp), cwd=project)
    after = env.run("doctor", "--plugin-data", str(env.tmp), "--json", cwd=project)
    assert json.loads(before.stdout)["project"] == json.loads(after.stdout)["project"]


def test_usage_errors(env, project):
    proc = env.run("nope", "--json", cwd=project)
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"]["code"] == "D100"
    human = env.run("nope", cwd=project)
    assert human.returncode == 2 and "invalid choice" in human.stderr


def test_not_an_nh_project(env, project):
    for args in (("env", "wait"), ("lab", "status"), ("metrics", "summarize"), ("fresh-run",)):
        report = env.json(*args, cwd=project, expect=1)
        assert report["error"]["code"] == "D105", args


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_stdlib_and_shared_only():
    siblings = {p.stem for p in SCRIPTS.glob("*.py")}
    for path in [*SCRIPTS.glob("*.py"), *SCAFFOLD.glob("*.py")]:
        foreign = _imports(path) - set(sys.stdlib_module_names) - siblings - {"nh_gateway"}
        assert not foreign, (path.name, foreign)
        tree = ast.parse(path.read_text())
        shared = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nh_gateway")
        ]
        assert all(module.startswith("nh_gateway._shared") for module in shared), path.name
