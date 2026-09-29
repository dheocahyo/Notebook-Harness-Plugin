"""bin/nhctl itself: symlinked invocation, option placement, usage errors, stdlib only."""

from __future__ import annotations

import ast
import json
import os
import subprocess
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


def test_printed_errors_are_redacted(env, project):
    """nhctl redacts what it prints (design §6.8): the environment's secrets from the start,
    the project's .env values once it found the project."""
    password = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; the project's .env holds it
    token = "wh-" + "tok-8c1f0e2d9a"  # fake; the environment holds it
    (project / ".nh").mkdir()
    (project / ".env").write_text(f"DB_PASSWORD={password}\n")
    extra = {"WAREHOUSE_TOKEN": token}
    name = f"nope-{password}-{token}.ipynb"
    report = env.json("fresh-run", name, cwd=project, expect=1, extra=extra)
    assert report["error"]["code"] == "D150"
    shown = "nope-[redacted:DB_PASSWORD]-[redacted:WAREHOUSE_TOKEN].ipynb"
    assert shown in report["error"]["message"]
    human = env.run("fresh-run", name, cwd=project, extra=extra)
    assert human.returncode == 1 and shown in human.stdout
    for out in (json.dumps(report), human.stdout, human.stderr):
        assert password not in out and token not in out
    # A usage error, which echoes its argument, outside any project: the environment's values.
    usage = env.json(f"nope-{token}", cwd=project.parent, expect=2, extra=extra)
    assert "'nope-[redacted:WAREHOUSE_TOKEN]'" in usage["error"]["message"]
    human = env.run(f"nope-{token}", cwd=project.parent, extra=extra)
    assert human.returncode == 2 and "'nope-[redacted:WAREHOUSE_TOKEN]'" in human.stderr
    assert token not in json.dumps(usage) + human.stdout + human.stderr


# `nhctl lab status` with its command swapped for one that fails or returns ``data``, run
# through main(): what reaches stdout and stderr (design §6.8; review of C3).
DRIVER = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import main  # puts the shared package on the path
import common, lab
from nh_gateway._shared import secrets
secret = os.environ["NH_TEST_VALUE"]  # a name the redactor ignores: patterns only
def fail(args):
    raise ConnectionError(f"could not reach postgresql://etl:{secret}@db/x, api_token={secret}")
def data(args):
    smtp = os.environ["SMTP_PASSWORD"]
    conf = "password=" + json.dumps(secret)
    return common.Result({"detail": f"login failed: {smtp}", f"conf {smtp}": conf}, "", 1)
lab.cmd_status = fail if sys.argv[2] == "fail" else data
if sys.argv[2] == "scrub_data":
    secrets.install(secrets.Redactor.for_project(None))
    shown = common.scrub_data({f"token={secret}": [1, None, 2.5, ValueError(f"password={secret}")]})
    print(json.dumps(shown))
    sys.exit(0)
sys.exit(main.main(["lab", "status", "--json"]))
"""
PW = "Wx9Kp2Lm" + "7Qz4Rt8V"  # fake
SMTP = 'Pa"ss' + "\\w0rd99"  # fake; JSON escapes its quote and backslash


def drive(python: str, mode: str, **extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, NH_TEST_VALUE=PW, SMTP_PASSWORD=SMTP, **extra)
    if "NHCTL_DEBUG" not in extra:
        env.pop("NHCTL_DEBUG", None)
    argv = [python, "-c", DRIVER, str(SCRIPTS), mode]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)


def test_a_debug_traceback_is_redacted(python):
    """NHCTL_DEBUG=1, which nh's own D199 fix line asks for, prints the traceback to stderr."""
    proc = drive(python, "fail", NHCTL_DEBUG="1")
    assert proc.returncode == 1 and "Traceback (most recent call last)" in proc.stderr
    line = "could not reach postgresql://[redacted:url-userinfo]@db/x, api_token=[redacted:token]"
    assert f"ConnectionError: {line}" in proc.stderr and line in proc.stdout
    assert PW[:4] not in proc.stdout + proc.stderr


def test_json_strings_are_redacted_before_json_escapes_them(python):
    proc = drive(python, "data")
    assert proc.returncode == 1, proc.stderr
    assert json.loads(proc.stdout) == {
        "detail": "login failed: [redacted:SMTP_PASSWORD]",
        "conf [redacted:SMTP_PASSWORD]": 'password="[redacted:password]"',
    }
    assert "w0rd99" not in proc.stdout and PW[:4] not in proc.stdout
    proc = drive(python, "scrub_data")  # keys, lists and other objects too
    shown = {"token=[redacted:token]": [1, None, 2.5, "password=[redacted:password]"]}
    assert json.loads(proc.stdout) == shown, proc.stderr
