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
    assert "preset" in proc.stdout  # design §6.9


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
    for args in (
        ("env", "wait"),
        ("lab", "status"),
        ("metrics", "summarize"),
        ("fresh-run",),
        ("preset", "senior"),
    ):
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


# --- nhctl preset junior|senior (design §6.9) ------------------------------------------------


def harness(project: Path, text: str | bytes) -> Path:
    """An nh project (a .nh/ folder) whose harness.toml holds ``text`` byte for byte."""
    (project / ".nh").mkdir(exist_ok=True)
    path = project / "harness.toml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def scaffolded() -> str:
    from nh_gateway._shared.scaffold import core

    return core.render_harness_toml({"name": "demo", "goal": "Why?", "notebook": "n.ipynb"})


def leftovers(project: Path) -> list[str]:
    """What else is in the project folder: a temp file left by the write would show here."""
    return sorted(p.name for p in project.iterdir() if p.name not in ("harness.toml", ".nh"))


def has_tomllib(python: str) -> bool:
    proc = subprocess.run([python, "-c", "import tomllib"], capture_output=True, check=False)
    return proc.returncode == 0


def test_preset_uncomments_the_scaffolded_level_and_keeps_every_other_byte(env, project):
    before = scaffolded()
    path = harness(project, before)
    commented = next(line for line in before.splitlines() if line.startswith("# level = "))
    assert commented.startswith('# level = "junior"  ')  # its comment follows
    tail = commented[len('# level = "junior"') :]
    report = env.json("preset", "senior", cwd=project)
    assert report == {
        "ok": True,
        "changed": True,
        "path": "harness.toml",
        "level": "senior",
        "was": "junior",
        "comment_ratio": 16,
        "comment_ratio_set_by": "preset",
    }
    after = path.read_text()
    assert after == before.replace(commented, 'level = "senior"' + tail)
    assert leftovers(project) == []
    again = env.json("preset", "senior", cwd=project)  # idempotent
    assert again == dict(report, changed=False, was="senior")
    assert path.read_text() == after
    back = env.json("preset", "junior", cwd=project)
    assert (back["changed"], back["was"], back["comment_ratio"]) == (True, "senior", 8)
    assert path.read_text() == before.replace(commented, commented[2:])  # pinned junior
    assert env.json("preset", "junior", cwd=project)["changed"] is False


def test_preset_pins_junior_from_the_commented_default(env, project):
    """An explicit `nhctl preset junior` writes the level, so a later default doesn't change
    the project (design §6.9); then it changes nothing."""
    before = scaffolded()
    path = harness(project, before)
    report = env.json("preset", "junior", cwd=project)
    assert (report["changed"], report["was"]) == (True, "junior")
    assert '\nlevel = "junior"  ' in path.read_text()
    assert env.json("preset", "junior", cwd=project)["changed"] is False


def test_preset_text_output(env, project):
    harness(project, scaffolded())
    proc = env.run("preset", "senior", cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (  # design §6.9 (C9b): the budget now, the depth from a new session
        "Preset: senior (was junior). harness.toml updated: the comment budget applies from nh's "
        "next tool call, the explanation depth from a new session or /clear.\n"
        "Comment budget: 1 comment line per 16 code lines.\n"
    )
    proc = env.run("preset", "senior", cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "Preset: senior already; harness.toml unchanged.\n"
        "Comment budget: 1 comment line per 16 code lines.\n"
    )
    proc = env.run("preset", "junior", cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "Preset: junior (was senior). harness.toml updated: the comment budget applies from nh's "
        "next tool call, the explanation depth from a new session or /clear.\n"
        "Comment budget: 1 comment line per 8 code lines.\n"
    )


# (harness.toml before, the level, harness.toml after): every other byte kept (design §6.9)
EDITS = [
    (  # an active level line: the value only; spacing and comment kept
        'version = 1\n[preset]\nlevel   =  "junior"   # mine\n\n[lint]\nmode = "strict"\n',
        "senior",
        'version = 1\n[preset]\nlevel   =  "senior"   # mine\n\n[lint]\nmode = "strict"\n',
    ),
    (  # a literal string, indented
        "[preset]\n  level = 'junior'\n",
        "senior",
        '[preset]\n  level = "senior"\n',
    ),
    (  # a bad level is replaced
        "[preset] # the team's\nlevel = 3 # odd\n",
        "junior",
        '[preset] # the team\'s\nlevel = "junior" # odd\n',
    ),
    (  # a header alone: the line after it
        '# mine\n[preset]\n[lint]\nmode = "strict"\n',
        "senior",
        '# mine\n[preset]\nlevel = "senior"\n[lint]\nmode = "strict"\n',
    ),
    (  # a header alone, last, with no final newline
        "version = 1\n[ preset ]",
        "senior",
        'version = 1\n[ preset ]\nlevel = "senior"',
    ),
    (  # no [preset]: appended after a blank line
        '# My settings\nversion = 1\n\n[lint]\nmode = "strict"   # keep\n',
        "senior",
        '# My settings\nversion = 1\n\n[lint]\nmode = "strict"   # keep\n'
        '\n[preset]\nlevel = "senior"\n',
    ),
    (  # no [preset] and no final newline: one is added first
        "version = 1\n[turn]\nmax_retries = 1",
        "senior",
        'version = 1\n[turn]\nmax_retries = 1\n\n[preset]\nlevel = "senior"\n',
    ),
    (  # ends with a blank line already
        "version = 1\n\n",
        "senior",
        'version = 1\n\n[preset]\nlevel = "senior"\n',
    ),
    ("", "senior", '[preset]\nlevel = "senior"\n'),  # an empty file
    (  # CRLF kept, also for the new lines
        'version = 1\r\n[lint]\r\nmode = "strict"\r\n',
        "senior",
        'version = 1\r\n[lint]\r\nmode = "strict"\r\n\r\n[preset]\r\nlevel = "senior"\r\n',
    ),
    (
        'version = 1\r\n[preset]\r\n# level = "junior"\r\n[lint]\r\n',
        "senior",
        'version = 1\r\n[preset]\r\nlevel = "senior"\r\n[lint]\r\n',
    ),
    (
        "version = 1\r\n[preset]",
        "senior",
        'version = 1\r\n[preset]\r\nlevel = "senior"',
    ),
    (  # the table's first commented level; one in another table is left alone
        '[lint]\n# level = "x"\n[preset]\n# level = "junior"  # a\n# level = "senior"  # b\n',
        "senior",
        '[lint]\n# level = "x"\n[preset]\nlevel = "senior"  # a\n# level = "senior"  # b\n',
    ),
    (  # a NaN elsewhere doesn't fail the check
        '[exec]\nprobe_budget_s = nan\n[preset]\nlevel = "junior"\n',
        "senior",
        '[exec]\nprobe_budget_s = nan\n[preset]\nlevel = "senior"\n',
    ),
    (  # a commented level in the table after [preset] isn't [preset]'s: the header gets one
        '[preset]\n[lint]\n# level = "x"\n',
        "senior",
        '[preset]\nlevel = "senior"\n[lint]\n# level = "x"\n',
    ),
    (  # a quoted key with a \\U escape elsewhere: TOML's escapes decode (review of C9a)
        '"smile\\U0001F600" = 1\n["x\\U0001F600"]\nk = 1\n[preset]\n"y\\U0001F600" = 2\n',
        "senior",
        '"smile\\U0001F600" = 1\n["x\\U0001F600"]\nk = 1\n[preset]\nlevel = "senior"\n'
        '"y\\U0001F600" = 2\n',
    ),
]


def test_preset_edits(env, project):
    path = harness(project, "")
    for before, level, after in EDITS:
        path.write_bytes(before.encode())
        report = env.json("preset", level, cwd=project)
        assert report["changed"] is True, before
        assert path.read_bytes() == after.encode(), before
        inode = path.stat().st_ino
        assert env.json("preset", level, cwd=project)["changed"] is False, before
        assert path.read_bytes() == after.encode(), before
        assert path.stat().st_ino == inode, before  # nothing written, not even the same text
    assert leftovers(project) == []


# Multi-line values: Python 3.9's reader refuses both (D172 there), tomllib reads them.
MULTILINE_EDITS = [
    (  # "[preset]" inside a multi-line string and a multi-line array is not a header
        '[project]\ngoal = """\n[preset]\nlevel = "x"\n"""\nnames = [\n  "[preset]",\n]\n',
        '[project]\ngoal = """\n[preset]\nlevel = "x"\n"""\nnames = [\n  "[preset]",\n]\n'
        '\n[preset]\nlevel = "senior"\n',
    ),
    (  # a header after a multi-line literal string still ends the table
        "[preset]\nlevel = 'junior'\n[project]\ngoal = '''\nlevel = 1\n'''\n",
        "[preset]\nlevel = \"senior\"\n[project]\ngoal = '''\nlevel = 1\n'''\n",
    ),
]


def test_preset_edits_around_multiline_values(env, project):
    path = harness(project, "")
    for before, after in MULTILINE_EDITS:
        path.write_bytes(before.encode())
        if has_tomllib(env.python):
            assert env.json("preset", "senior", cwd=project)["changed"] is True
            assert path.read_text() == after
        else:
            report = env.json("preset", "senior", cwd=project, expect=1)
            assert report["error"]["code"] == "D172" and path.read_text() == before


def test_preset_writes_atomically_and_keeps_the_files_mode(env, project):
    path = harness(project, scaffolded())
    path.chmod(0o640)  # not mkstemp's own 0o600
    inode = path.stat().st_ino
    env.json("preset", "senior", cwd=project)
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.stat().st_ino != inode  # a new file renamed over the old one, not rewritten
    assert leftovers(project) == []


def test_preset_d172_for_a_read_only_file(env, project):
    """A harness.toml with no write bit is refused, not replaced (``os.replace`` would ignore its
    mode; as root ``os.access`` does too). Already at the level, it needs no write: fine."""
    path = harness(project, '[preset]\nlevel = "junior"\n')
    path.chmod(0o444)
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"] == {
        "code": "D172",
        "message": "harness.toml is read-only, so nh didn't change it.",
        "fix": "Make it writable (chmod u+w harness.toml), then rerun.",
    }
    assert path.read_text() == '[preset]\nlevel = "junior"\n'
    assert path.stat().st_mode & 0o777 == 0o444
    assert env.json("preset", "junior", cwd=project)["changed"] is False
    assert leftovers(project) == []


# The write, watched (design §6.9): the temp file sits in the project folder, it is fsynced
# before the replace, and when mkstemp or the replace fails, D172 says so and no temp file stays.
WRITE_DRIVER = """
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import main
mode, project = sys.argv[2], sys.argv[3]
seen = {"dirs": [], "fsyncs": 0}
real_mkstemp, real_fsync, real_replace = tempfile.mkstemp, os.fsync, os.replace
def mkstemp(*args, **kwargs):
    seen["dirs"].append(kwargs.get("dir"))
    if mode == "mkstemp":
        raise PermissionError(13, "Permission denied", kwargs.get("dir"))
    return real_mkstemp(*args, **kwargs)
def fsync(fd):
    seen["fsyncs"] += 1
    return real_fsync(fd)
def replace(src, dst):
    seen["replaced_after_fsync"] = seen["fsyncs"] > 0
    if mode == "replace":
        raise OSError(30, "Read-only file system", src)
    return real_replace(src, dst)
tempfile.mkstemp, os.fsync, os.replace = mkstemp, fsync, replace
code = main.main(["preset", "senior", "--json", "--project", project])
sys.stderr.write(json.dumps(seen))
sys.exit(code)
"""


def test_preset_write_uses_the_project_folder_fsync_and_cleans_up(python, tmp_path):
    for mode, code in (("ok", 0), ("replace", 1), ("mkstemp", 1)):
        project = tmp_path / mode
        project.mkdir()
        path = harness(project, scaffolded())
        before = path.read_bytes()
        proc = subprocess.run(
            [python, "-c", WRITE_DRIVER, str(SCRIPTS), mode, str(project)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert proc.returncode == code, proc.stderr
        seen = json.loads(proc.stderr)
        assert [Path(folder).resolve() for folder in seen["dirs"]] == [project.resolve()], mode
        assert leftovers(project) == [], mode
        if mode == "ok":
            assert seen["fsyncs"] == 1 and seen["replaced_after_fsync"] is True
            assert path.read_bytes() != before
            continue
        reason = "Read-only file system" if mode == "replace" else "Permission denied"
        assert json.loads(proc.stdout)["error"] == {
            "code": "D172",
            "message": f"harness.toml can't be written ({reason}), so nh didn't change it.",
            "fix": "Make harness.toml and its folder writable, then rerun.",
        }, mode
        assert str(project) not in proc.stdout, mode  # no temp path in the message
        assert path.read_bytes() == before, mode


# A concurrent change between nhctl's read and its write: D172, and the other writer's text kept.
RACE_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import main
import preset
real_edit = preset.edit
def edit(text, level):
    result = real_edit(text, level)
    with open(sys.argv[2] + "/harness.toml", "a") as handle:
        handle.write("# typed meanwhile\\n")
    return result
preset.edit = edit
sys.exit(main.main(["preset", "senior", "--json", "--project", sys.argv[2]]))
"""


def test_preset_d172_when_the_file_changes_while_it_edits(python, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = harness(project, scaffolded())
    before = path.read_text()
    proc = subprocess.run(
        [python, "-c", RACE_DRIVER, str(SCRIPTS), str(project)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    assert json.loads(proc.stdout)["error"] == {
        "code": "D172",
        "message": "harness.toml changed while nh was editing it, so nh didn't change it.",
        "fix": "Rerun the command.",
    }
    assert path.read_text() == before + "# typed meanwhile\n"
    assert leftovers(project) == []


def test_preset_reports_an_explicit_comment_ratio(env, project):
    path = harness(project, '[preset]\nlevel = "junior"\n[lint]\ncomment_ratio = 8\n')
    report = env.json("preset", "senior", cwd=project)
    assert (report["comment_ratio"], report["comment_ratio_set_by"]) == (8, "harness.toml")
    assert path.read_text() == '[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = 8\n'
    proc = env.run("preset", "junior", cwd=project)
    assert proc.returncode == 0, proc.stderr
    # design §6.9 (C9b): the preset doesn't set the budget, so the first line names only the depth
    assert proc.stdout == (
        "Preset: junior (was senior). harness.toml updated: the explanation depth applies from a "
        "new session or /clear.\n"
        "Comment budget: harness.toml's [lint] comment_ratio = 8 sets it, not the preset.\n"
    )
    proc = env.run("preset", "senior", cwd=project)
    assert proc.stdout == (
        "Preset: senior (was junior). harness.toml updated: the explanation depth applies from a "
        "new session or /clear.\n"
        "Comment budget: harness.toml's [lint] comment_ratio = 8 sets it, not the preset.\n"
    )
    proc = env.run("preset", "senior", cwd=project)
    assert proc.stdout == (
        "Preset: senior already; harness.toml unchanged.\n"
        "Comment budget: harness.toml's [lint] comment_ratio = 8 sets it, not the preset.\n"
    )


FIX_BY_HAND = 'Set it by hand: level = "{level}" under [preset].'
UNSAFE_FIX = FIX_BY_HAND.format(level="senior")
HEADERS = "a [preset.…], [[preset]] or quoted preset header"
TOP = "a top-level preset key, not a [preset] table"
FIX_TOP_LEVEL = "Delete the top-level preset line(s), then rerun: nhctl preset {level}."
TOP_FIX = FIX_TOP_LEVEL.format(level="senior")

# (harness.toml, why): forms the line edit doesn't handle (design §6.9)
UNSAFE = [
    ('version = 1\npreset.level = "junior"\n', TOP),
    ('version = 1\npreset = "senior"\n', TOP),  # a plain value: no dotted key, no table
    ('preset = { level = "junior" }\n', TOP),
    ('"preset" = { level = "junior" }\n', TOP),
    ("[preset.extra]\nx = 1\n", HEADERS),
    ("[[preset]]\nx = 1\n", HEADERS),
    ('["preset"]\nlevel = "junior"\n', HEADERS),
    ('[preset]\n"level" = "junior"\n', "a dotted or quoted level key"),
    ("[preset]\nlevel.x = 1\n", "a dotted or quoted level key"),
]


def test_preset_d172_for_a_form_it_cant_edit_safely(env, project):
    path = harness(project, "")
    for text, why in UNSAFE:
        path.write_bytes(text.encode())
        report = env.json("preset", "senior", cwd=project, expect=1)
        if "{" in text and not has_tomllib(env.python):
            # 3.9's reader has no inline tables: it can't parse the file at all
            assert report["error"]["code"] == "D172"
            assert report["error"]["message"].startswith("harness.toml can't be parsed (")
            continue
        assert report["error"] == {
            "code": "D172",
            "message": f"harness.toml sets the preset in a form nh can't edit safely ({why}), "
            "so nh didn't change it.",
            "fix": TOP_FIX if why == TOP else UNSAFE_FIX,
        }, text
        assert path.read_bytes() == text.encode()
    proc = env.run("preset", "senior", cwd=project)
    assert proc.returncode == 1 and proc.stdout.startswith("error: harness.toml sets the preset")
    assert f"\nfix: {UNSAFE_FIX}\n" in proc.stdout
    assert leftovers(project) == []


def test_preset_top_level_key_fix_works_when_followed(env, project):
    """D172's fix for a top-level ``preset`` line, followed literally, leads to a working edit;
    adding a [preset] table beside the line, the generic fix, would break the file (review of
    C9a)."""
    for line in ('preset = "senior"', 'preset.level = "expert"'):
        path = harness(project, f'version = 1\n{line}\n[lint]\nmode = "strict"\n')
        report = env.json("preset", "junior", cwd=project, expect=1)
        assert report["error"]["fix"] == (
            "Delete the top-level preset line(s), then rerun: nhctl preset junior."
        )
        path.write_text(path.read_text().replace(line + "\n", ""))  # the fix, followed
        report = env.json("preset", "junior", cwd=project)
        assert (report["changed"], report["was"]) == (True, "junior"), line
        assert path.read_text() == (
            'version = 1\n[lint]\nmode = "strict"\n\n[preset]\nlevel = "junior"\n'
        )


def test_preset_d172_for_a_file_toml_refuses(env, project):
    """Two [preset] tables, two level lines, a level over several lines, a broken header: D172
    from the parser or the form checks, whichever reader runs, and nothing written."""
    path = harness(project, "")
    for text in (
        '[preset]\nlevel = "junior"\n[preset]\n',
        '[preset]\nlevel = "junior"\nlevel = "senior"\n',
        '[preset]\nlevel = """\njunior"""\n',
        "version = 1\n[lint\nmode = 1\n",  # 3.9's reader skips a broken header: the scan
        "version = 1\n[lint]\nmode\n",  # and a key without a value
        "version = 1\n[lint]\n= 1\n",
    ):
        path.write_bytes(text.encode())
        report = env.json("preset", "senior", cwd=project, expect=1)
        assert report["error"]["code"] == "D172", text
        assert report["error"]["message"].endswith("so nh didn't change it."), text
        assert path.read_bytes() == text.encode()
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"]["message"].startswith("harness.toml can't be parsed (")
    assert report["error"]["fix"] == "Fix the file, then rerun."
    assert leftovers(project) == []


def test_preset_d172_for_a_missing_or_unreadable_file(env, project):
    (project / ".nh").mkdir()
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"] == {
        "code": "D172",
        "message": "harness.toml is missing, so there is no preset to set.",
        "fix": "Run /nh:init in this folder (it writes harness.toml), then rerun.",
    }
    (project / "harness.toml").mkdir()
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"]["message"] == (
        "harness.toml can't be read (not a regular file), so nh didn't change it."
    )
    (project / "harness.toml").rmdir()
    (project / "real.toml").write_text('[preset]\nlevel = "junior"\n')
    (project / "harness.toml").symlink_to("real.toml")
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"]["message"] == (
        "harness.toml can't be read (a symbolic link), so nh didn't change it."
    )
    assert (project / "real.toml").read_text() == '[preset]\nlevel = "junior"\n'
    (project / "harness.toml").unlink()
    raw = b'[preset]\nlevel = "junior"\n# \xff\n'
    (project / "harness.toml").write_bytes(raw)
    report = env.json("preset", "senior", cwd=project, expect=1)
    assert report["error"]["message"] == (
        "harness.toml can't be read (it isn't UTF-8), so nh didn't change it."
    )
    assert (project / "harness.toml").read_bytes() == raw


def test_preset_usage(env, project):
    harness(project, scaffolded())
    proc = env.run("preset", "expert", "--json", cwd=project)
    assert proc.returncode == 2 and json.loads(proc.stdout)["error"]["code"] == "D100"
    proc = env.run("preset", cwd=project)
    assert proc.returncode == 2 and "junior" in proc.stderr and "senior" in proc.stderr
    proc = env.run("preset", "--help", cwd=project)
    assert proc.returncode == 0 and "{junior,senior}" in proc.stdout


# preset.edit and its checks under Python 3.9's reader (tomlread without tomllib), in a
# subprocess: nhctl's modules import each other as top-level names. Modes: "edit" runs
# preset.edit; "scan" runs the form checks alone; "misplaced" runs edit with a scan that puts
# [preset]'s header on the next table's line, which only the validation can catch.
EDIT_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import main  # puts the shared package on the path
import preset
from nh_gateway._shared import tomlread
tomlread._tomllib = None
real_scan = preset.Scan
class Misplaced(real_scan):
    def __init__(self, lines):
        super().__init__(lines)
        self.header = next(i for i, line in enumerate(lines) if line == "[lint]")
out = []
for mode, text, level in json.loads(sys.stdin.read()):
    preset.Scan = Misplaced if mode == "misplaced" else real_scan
    try:
        if mode == "scan":
            scan = real_scan(text.split("\\n"))
            out.append(["ok", scan.header, scan.level, scan.commented])
        else:
            out.append(["ok", *preset.edit(text, level)])
    except preset.Unsafe as exc:
        out.append(["unsafe", str(exc), exc.fix])
    except ValueError as exc:
        out.append(["parse", str(exc), None])
print(json.dumps(out))
"""


def run_edits(cases: list[tuple[str, str]], mode: str = "edit") -> list[list]:
    proc = subprocess.run(
        [sys.executable, "-c", EDIT_DRIVER, str(SCRIPTS)],
        input=json.dumps([(mode, text, level) for text, level in cases]),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_preset_edit_under_the_39_reader():
    """On 3.9 and 3.10 tomlread's own reader reads what tomllib reads or refuses the file
    (design §6.9): every edit row comes out the same, and each form nh doesn't edit is refused,
    by the reader or by the form checks."""
    results = run_edits([(before, level) for before, level, _ in EDITS])
    assert [result[:3] for result in results] == [["ok", after, True] for _, _, after in EDITS]
    [(kind, new, changed, was)] = run_edits([(scaffolded(), "senior")])
    assert kind == "ok" and changed and '\nlevel = "senior"  ' in new and was == "junior"
    results = run_edits([(text, "senior") for text, _ in UNSAFE])
    for (kind, why, fix), (text, expected) in zip(results, UNSAFE, strict=True):
        if "{" in text:  # this reader has no inline tables: D172 as well
            assert kind == "parse", text
            continue
        fix_wanted = FIX_TOP_LEVEL if expected == TOP else FIX_BY_HAND
        assert (kind, why, fix) == ("unsafe", expected, fix_wanted), text
    results = run_edits(
        [
            ('[preset]\nlevel = "junior"\n[preset]\n', "senior"),  # tomllib refuses these too
            ('[preset]\nlevel = "junior"\nlevel = "senior"\n', "senior"),
            ('[preset]\nlevel = """\njunior"""\n', "senior"),
            ("version = 1\n[lint\nmode = 1\n", "senior"),
            ("version = 1\n[lint]\nmode\n", "senior"),
        ]
    )
    assert [kind for kind, _, _ in results] == ["parse"] * 5
    assert [message for _, message, _ in results] == [
        "Cannot declare a table twice (at line 3)",
        "Cannot overwrite a value (at line 3)",
        "Invalid value for level (at line 2)",
        "Invalid statement (at line 2)",
        "Invalid statement (at line 3)",
    ]


def test_preset_decides_already_from_the_level_line():
    """The "already" check and ``was`` read [preset]'s level line, not the whole file: before
    the 3.9 reader read ``[[x]]`` headers, it filed this file's second ``level`` under [preset],
    and nhctl reported ``changed: false`` (review of C9a)."""
    text = '[preset]\nlevel = "junior"\n\n[[guardrails]]\nlevel = "senior"\n'
    [result] = run_edits([(text, "senior")])
    assert result == ["ok", text.replace('"junior"', '"senior"'), True, "junior"]
    for line, was in (("level = 3", "junior"), ("level = 'senior'", "senior"), ("", "junior")):
        [result] = run_edits([(f"[preset]\n{line}\n", "junior")])
        assert result[0] == "ok" and result[3] == was, line


def test_preset_form_checks_and_validation_on_their_own():
    """The form checks refuse what the line edit doesn't handle even where both readers would
    refuse it first, and the validation catches an edit the scan placed wrong."""
    scans = run_edits(
        [
            ('[preset]\nlevel = "junior"\n[preset]\n', ""),
            ('[preset]\nlevel = "junior"\nlevel = "senior"\n', ""),
            ('[preset]\nlevel = """\njunior"""\n', ""),
            ('"\\x41" = 1\n', ""),  # an escape TOML 1.0 lacks
            ('["\\x41"]\n', ""),
            ("[lint\n", ""),
            ('[preset]\n# level = "junior"  # c\nx = 1\n', ""),
        ],
        mode="scan",
    )
    assert scans == [
        ["unsafe", "two [preset] tables", FIX_BY_HAND],
        ["unsafe", "two level lines", FIX_BY_HAND],
        ["unsafe", "a level value over several lines", FIX_BY_HAND],
        ["unsafe", "a quoted key nh can't read", FIX_BY_HAND],
        ["unsafe", "a quoted key nh can't read", FIX_BY_HAND],
        ["parse", "Invalid statement (at line 1)", None],
        ["ok", 0, None, 1],
    ]
    [result] = run_edits([("[preset]\n[lint]\nmode = 1\n", "senior")], mode="misplaced")
    assert result == ["unsafe", "the edit didn't check out", FIX_BY_HAND]
