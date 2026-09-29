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
# through main(): what reaches stdout and stderr (design §6.8; review of C3). The "d199",
# "net" and "tail" modes take away the later scrubs, so each site is the only one that could
# pass its test.
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
def control(args):
    return common.Result({"detail": "login with " + os.environ["CTRL_TOKEN"]}, "", 0)
lab.cmd_status = {"fail": fail, "d199": fail, "control": control}.get(sys.argv[2], data)
if sys.argv[2] == "d199":  # what main hands print_result, before print_result's own scrubs
    common.print_result = lambda result, as_json: print(json.dumps(result.data))
if sys.argv[2] == "net":  # print_result's whole-line scrub on its own
    common.scrub_data = lambda value: value
if sys.argv[2] == "tail":
    secrets.install(secrets.Redactor.for_project(None))
    path = os.path.join(os.environ["NH_TEST_DIR"], "x.log")
    with open(path, "w") as handle:
        handle.write(f"one\\napi_token={secret}\\nthree\\n")
    print(common.tail(path, 2))
    sys.exit(0)
if sys.argv[2] == "scrub_data":
    secrets.install(secrets.Redactor.for_project(None))
    shown = common.scrub_data({f"token={secret}": [1, None, 2.5, ValueError(f"password={secret}")]})
    print(json.dumps(shown))
    sys.exit(0)
sys.exit(main.main(["lab", "status", "--json"]))
"""
PW = "Wx9Kp2Lm" + "7Qz4Rt8V"  # fake
CTRL = "Qw\x01" + "Er7Ty9Ui2Op"  # fake; json.dumps writes \u0001, which no redactor form is
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


def test_a_value_json_writes_as_a_unicode_escape_is_redacted_before_json_dumps(python):
    """scrub_data's own case: the line net can't match ``\\u0001``, the form json.dumps gives
    a control character."""
    proc = drive(python, "control", CTRL_TOKEN=CTRL)
    assert json.loads(proc.stdout) == {"detail": "login with [redacted:CTRL_TOKEN]"}, proc.stderr
    assert CTRL[3:] not in proc.stdout


def test_the_printed_line_is_redacted_without_scrub_data(python):
    """print_result's whole-line scrub on its own, with scrub_data taken away."""
    proc = drive(python, "net")
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout.count("[redacted:SMTP_PASSWORD]") == 2
    assert "w0rd99" not in proc.stdout


def test_d199s_message_is_redacted_before_print_result(python):
    """main scrubs D199's message itself: print_result, patched to print what it gets, shows it
    already redacted."""
    proc = drive(python, "d199")
    error = json.loads(proc.stdout)["error"]
    line = "could not reach postgresql://[redacted:url-userinfo]@db/x, api_token=[redacted:token]"
    assert error["code"] == "D199", proc.stderr
    assert error["message"] == f"nhctl hit an internal error: ConnectionError: {line}"
    assert PW[:4] not in proc.stdout + proc.stderr


def test_a_log_tail_is_redacted(python, tmp_path):
    """common.tail scrubs what it returns: envsync writes it to env-sync.json unprinted."""
    proc = drive(python, "tail", NH_TEST_DIR=str(tmp_path))
    assert proc.stdout == "api_token=[redacted:token]\nthree\n", proc.stderr
