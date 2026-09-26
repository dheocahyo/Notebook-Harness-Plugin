"""nhctl lab start|status|stop against a fake `jupyter` that writes jpserver files and serves
the few REST endpoints nhctl calls. No real JupyterLab is started here."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOKEN = "tok-s3cret-4f2a"

FAKE_JUPYTER = r"""
import http.server, json, os, signal, sys

args = sys.argv[1:]
runtime = os.environ["JUPYTER_RUNTIME_DIR"]
if args == ["--runtime-dir"]:
    print(runtime)
    sys.exit(0)
record = os.environ["FAKE_LAB_RECORD"]

def log(line):
    with open(record, "a") as handle:
        handle.write(line + "\n")

log("ARGV " + json.dumps(args))
names = ("JUPYTER_PREFER_ENV_PATH", "PATH", "JUPYTER_TOKEN", "JUPYTER_TOKEN_FILE")
log("ENV " + json.dumps({k: os.environ.get(k) for k in names}))
if os.environ.get("FAKE_LAB_FAIL"):
    print("boom: address already in use")
    sys.exit(1)
# Like jupyter_server's IdentityProvider: JUPYTER_TOKEN, then JUPYTER_TOKEN_FILE, else a
# generated token, which is the only kind it prints.
generated = False
if os.environ.get("JUPYTER_TOKEN"):
    TOKEN = os.environ["JUPYTER_TOKEN"]
elif os.environ.get("JUPYTER_TOKEN_FILE"):
    with open(os.environ["JUPYTER_TOKEN_FILE"]) as handle:
        TOKEN = handle.read()
else:
    TOKEN, generated = os.environ["FAKE_LAB_TOKEN"], True
prefix = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
exe = os.environ.get("FAKE_KERNEL_EXE") or os.path.join(prefix, "bin", "python")
sessions = []

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("Authorization") == "token " + TOKEN

    def do_GET(self):
        log("GET " + self.path)
        if not self.authorized():
            return self.send(403, {"message": "Forbidden"})
        if self.path == "/api/sessions":
            return self.send(200, sessions)
        if self.path == "/api/kernelspecs":
            spec = {"argv": [exe, "-m", "ipykernel_launcher", "-f", "{connection_file}"]}
            specs = {name: {"name": name, "spec": spec} for name in ("python3", "myenv")}
            return self.send(200, {"default": "python3", "kernelspecs": specs})
        if self.path == "/api/status":
            return self.send(200, {"connections": 0})
        if self.path.startswith("/api/collaboration/session/"):
            return self.send(404 if os.environ.get("FAKE_NO_COLLAB") else 405, {})
        self.send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        log("POST " + self.path + " " + json.dumps(body, sort_keys=True))
        if not self.authorized():
            return self.send(403, {})
        session = {"id": "s1", "path": body["path"], "kernel": {"id": "k1", "name": body["kernel"]["name"]}}
        sessions.append(session)
        self.send(201, session)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
root = next(a.split("=", 1)[1] for a in args if a.startswith("--ServerApp.root_dir="))
pid = os.getpid()
info_file = os.path.join(runtime, "jpserver-%d.json" % pid)
open_file = os.path.join(runtime, "jpserver-%d-open.html" % pid)

def stop(*_):
    for path in (info_file, open_file):
        if os.path.exists(path):
            os.remove(path)
    log("STOPPED")
    os._exit(0)

signal.signal(signal.SIGTERM, stop)
url = "http://127.0.0.1:%d/" % server.server_address[1]
with open(open_file, "w") as handle:
    handle.write('<meta http-equiv="refresh" content="1;url=%s?token=%s" />' % (url, TOKEN))
with open(info_file + ".tmp", "w") as handle:
    json.dump({"url": url, "token": TOKEN, "pid": pid, "root_dir": root, "version": "2.17.0"}, handle)
os.replace(info_file + ".tmp", info_file)
print("[I ServerApp] %slab?token=%s" % (url, TOKEN if generated else "..."), flush=True)
server.serve_forever()
"""


@pytest.fixture
def lab_project(env, project):
    """A scaffolded project whose env prefix holds the fake jupyter; kills leftovers after."""
    env.script("uv", "exit 0")
    env.json("scaffold", cwd=project)
    prefix = project / ".venv"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").write_text("")
    jupyter = prefix / "bin/jupyter"
    jupyter.write_text(f"#!{sys.executable}\n{FAKE_JUPYTER}")
    jupyter.chmod(0o755)
    state = project / ".nh/state"
    state.mkdir(exist_ok=True)
    env_json = {"manager": "uv", "prefix": str(prefix.resolve()), "jupyterlab": "4.6.4",
                "jupyter_collaboration": "5.0.4"}  # fmt: skip
    (state / "env.json").write_text(json.dumps(env_json))
    for opener in ("open", "xdg-open"):
        env.script(opener, f'echo "{opener} $*" >> "{env.tmp}/opened.log"')
    env.vars.update(FAKE_LAB_RECORD=str(env.tmp / "lab-record.log"), FAKE_LAB_TOKEN=TOKEN)
    yield project
    lab = project / ".nh/state/lab.json"
    if lab.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(json.loads(lab.read_text())["pid"], signal.SIGKILL)


@pytest.fixture
def user_lab(env, lab_project):
    """Start a JupyterLab the way a user would, outside nhctl: user_lab(root, **env) -> pid.

    The shell backgrounds it, so it isn't this test's child (nhctl lab stop must be able to
    see it exit, which a zombie child would hide)."""
    started = []

    def start(root, **extra):
        jupyter = lab_project / ".venv/bin/jupyter"
        cmd = f'"{sys.executable}" "{jupyter}" lab "--ServerApp.root_dir={root}" >/dev/null 2>&1 &'
        out = subprocess.run(
            ["/bin/sh", "-c", cmd + " echo $!"], env=dict(env.vars, **extra),
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        pid = int(out.stdout.strip())
        started.append(pid)
        info = env.runtime / f"jpserver-{pid}.json"
        deadline = time.time() + 10
        while not info.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert info.exists(), "the user's fake JupyterLab didn't start"
        return pid

    yield start
    for pid in started:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(path, timeout=5.0):
    """The browser opener is detached, so its log may land just after nhctl returns."""
    deadline = time.time() + timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(0.05)
    return path.read_text()


def record(env) -> list[str]:
    path = env.tmp / "lab-record.log"
    return path.read_text().splitlines() if path.exists() else []


def test_start_status_stop(env, lab_project):
    project = lab_project.resolve()
    proc = env.run("lab", "start", "--json", cwd=lab_project)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    started = json.loads(proc.stdout)
    assert started["status"] == "started" and started["warnings"] == []
    assert started["url"].startswith("http://127.0.0.1:") and "token" not in started["url"]
    assert started["notebook"] == "notebooks/01_eda.ipynb"
    assert started["session"] == {"created": True, "kernel": "python3"}

    lab = json.loads((lab_project / ".nh/state/lab.json").read_text())
    token = json.loads(Path(lab["runtime_file"]).read_text())["token"]
    # nhctl generated the token (jupyter didn't), and it shows up nowhere nh writes.
    assert len(token) == 48 and token != TOKEN
    assert token not in proc.stdout + proc.stderr
    assert token not in json.dumps(lab)
    assert lab["pid"] == started["pid"] and lab["url"] == started["url"]
    assert lab["runtime_file"] == str(env.runtime / f"jpserver-{lab['pid']}.json")
    assert lab["env_prefix"] == str(project / ".venv")
    assert "adopted" not in lab

    lines = record(env)
    argv = json.loads(lines[0][len("ARGV ") :])
    assert argv == [
        "lab", "--no-browser", "--ip=127.0.0.1", f"--ServerApp.root_dir={project}",
        f"--SQLiteYStore.db_path={project}/.nh/jupyter_ystore.db",
        "--LabApp.default_url=/lab/tree/notebooks/01_eda.ipynb",
    ]  # fmt: skip
    launch_env = json.loads(lines[1][len("ENV ") :])
    assert launch_env["JUPYTER_PREFER_ENV_PATH"] == "1"
    assert launch_env["PATH"].startswith(f"{project}/.venv/bin:")
    # The token went in through a file the kernels can't read afterwards.
    assert launch_env["JUPYTER_TOKEN"] is None
    assert launch_env["JUPYTER_TOKEN_FILE"] and not os.path.exists(launch_env["JUPYTER_TOKEN_FILE"])
    assert token not in "\n".join(lines[:2])
    post = next(line for line in lines if line.startswith("POST /api/sessions"))
    assert json.loads(post.split(" ", 2)[2]) == {
        "kernel": {"name": "python3"}, "name": "01_eda.ipynb",
        "path": "notebooks/01_eda.ipynb", "type": "notebook",
    }  # fmt: skip
    opened = wait_for(env.tmp / "opened.log").split()
    assert opened[1] == str(env.runtime / f"jpserver-{lab['pid']}-open.html")
    assert token in (env.runtime / f"jpserver-{lab['pid']}-open.html").read_text()
    log = lab_project / ".nh/logs/jupyterlab.log"
    assert "lab?token=..." in log.read_text()  # jupyter masks a token it didn't generate
    assert token not in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    status = env.json("lab", "status", cwd=lab_project)
    assert status["running"] is True and status["reachable"] is True
    assert status["pid"] == lab["pid"] and status["registered"] is True

    again = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert again["status"] == "running" and again["pid"] == lab["pid"]
    assert again["session"] == {"created": False, "kernel": "python3"}
    assert sum(line.startswith("ARGV") for line in record(env)) == 1

    stopped = env.json("lab", "stop", cwd=lab_project)
    assert stopped == {"ok": True, "stopped": True, "pid": lab["pid"]}
    assert "STOPPED" in record(env)
    assert not (lab_project / ".nh/state/lab.json").exists()
    assert not (env.runtime / f"jpserver-{lab['pid']}.json").exists()
    assert env.json("lab", "status", cwd=lab_project)["running"] is False
    assert env.json("lab", "stop", cwd=lab_project)["stopped"] is False


def test_kernel_outside_env_warns(env, lab_project):
    env.vars["FAKE_KERNEL_EXE"] = "/opt/miniconda3/bin/python"
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert len(started["warnings"]) == 1
    assert "/opt/miniconda3/bin/python" in started["warnings"][0]
    assert not (env.tmp / "opened.log").exists()
    env.json("lab", "stop", cwd=lab_project)


def test_old_collaboration_warns(env, lab_project):
    env_json = lab_project / ".nh/state/env.json"
    env_json.write_text(
        json.dumps(dict(json.loads(env_json.read_text()), jupyter_collaboration="4.0.2"))
    )
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert any("jupyter-collaboration 4.0.2" in w for w in started["warnings"])
    env.json("lab", "stop", cwd=lab_project)


def test_startup_failure_reports_log(env, lab_project):
    env.vars["FAKE_LAB_FAIL"] = "1"
    report = env.json("lab", "start", cwd=lab_project, expect=1)
    assert report["error"]["code"] == "D145"
    assert "boom: address already in use" in report["log_tail"]
    assert not (lab_project / ".nh/state/lab.json").exists()


def test_start_needs_the_env(env, project):
    env.script("uv", "exit 0")
    env.json("scaffold", cwd=project)
    report = env.json("lab", "start", cwd=project, expect=1)
    assert report["error"]["code"] == "D140"


def test_stale_lab_json_is_not_running(env, lab_project):
    (lab_project / ".nh/state/lab.json").write_text(
        json.dumps({"pid": 999999, "url": "http://127.0.0.1:1/", "runtime_file": "/nope.json"})
    )
    assert env.json("lab", "status", cwd=lab_project)["running"] is False
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert started["status"] == "started"
    time.sleep(0.1)
    env.json("lab", "stop", cwd=lab_project)


def test_old_log_is_scrubbed_and_made_private(env, lab_project):
    """A log from an older nhctl (token printed by jupyter, mode 0644) is cleaned on start."""
    log = lab_project / ".nh/logs/jupyterlab.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[I ServerApp] http://127.0.0.1:8888/lab?token=0ld5ecret0ld5ecret\n")
    log.chmod(0o644)
    env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert "0ld5ecret" not in log.read_text() and "token=***" in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    env.json("lab", "stop", cwd=lab_project)


# ------------------------------------------------ a JupyterLab the user started (finding 10)


def test_adopts_a_lab_the_user_started(env, lab_project, user_lab):
    project = lab_project.resolve()
    user = user_lab(project)
    status = env.json("lab", "status", cwd=lab_project)
    assert status["running"] is True and status["registered"] is False
    assert status["pid"] == user

    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert started["status"] == "adopted" and started["pid"] == user
    assert started["session"] == {"created": True, "kernel": "python3"}
    assert started["log"] is None and started["warnings"] == []
    assert sum(line.startswith("ARGV") for line in record(env)) == 1  # no second server
    lab = json.loads((lab_project / ".nh/state/lab.json").read_text())
    assert lab["pid"] == user and lab["adopted"] is True and lab["env_prefix"] is None
    assert lab["runtime_file"] == str(env.runtime / f"jpserver-{user}.json")
    assert TOKEN not in json.dumps(lab)

    again = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert again["status"] == "running" and again["pid"] == user
    assert again["session"] == {"created": False, "kernel": "python3"}
    assert env.json("lab", "status", cwd=lab_project)["registered"] is True
    assert env.json("lab", "stop", cwd=lab_project) == {"ok": True, "stopped": True, "pid": user}
    assert "STOPPED" in record(env)


def test_adopts_a_parent_folder_lab_but_never_stops_it(env, lab_project, user_lab):
    parent = lab_project.resolve().parent
    user = user_lab(parent)
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert started["status"] == "adopted" and started["pid"] == user
    post = next(line for line in record(env) if line.startswith("POST /api/sessions"))
    assert json.loads(post.split(" ", 2)[2])["path"] == "sales_study/notebooks/01_eda.ipynb"
    stopped = env.json("lab", "stop", cwd=lab_project)
    assert stopped["stopped"] is False and stopped["adopted"] is True
    assert alive(user)


def test_lab_start_skips_servers_nh_cannot_use(env, lab_project, user_lab, tmp_path):
    other = tmp_path / "other_project"
    other.mkdir()
    user_lab(other)
    no_collab = user_lab(lab_project.resolve(), FAKE_NO_COLLAB="1")
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert started["status"] == "started"
    assert started["pid"] not in (no_collab,)
    assert len(started["warnings"]) == 1
    assert "has no real-time collaboration" in started["warnings"][0]
    assert f"pid {no_collab}" in started["warnings"][0]
    env.json("lab", "stop", cwd=lab_project)


# --------------------------------------------------- [jupyter].kernel_name (finding 34)


def set_kernel_name(project, name):
    harness = project / "harness.toml"
    text = harness.read_text()
    assert '# kernel_name = ""' in text
    harness.write_text(text.replace('# kernel_name = ""', f'kernel_name = "{name}"'))


def test_kernel_name_from_harness_toml(env, lab_project):
    set_kernel_name(lab_project, "myenv")
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert started["session"] == {"created": True, "kernel": "myenv"}
    assert started["warnings"] == []
    post = next(line for line in record(env) if line.startswith("POST /api/sessions"))
    assert json.loads(post.split(" ", 2)[2])["kernel"] == {"name": "myenv"}
    env.json("lab", "stop", cwd=lab_project)


def test_kernel_name_missing_or_not_running(env, lab_project):
    env.json("lab", "start", "--no-browser", cwd=lab_project)  # a python3 session
    set_kernel_name(lab_project, "myenv")
    again = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert again["session"] == {"created": False, "kernel": "python3"}
    assert len(again["warnings"]) == 1 and '"python3", not harness.toml' in again["warnings"][0]
    env.json("lab", "stop", cwd=lab_project)

    set_kernel_name_text = (lab_project / "harness.toml").read_text()
    (lab_project / "harness.toml").write_text(
        set_kernel_name_text.replace('kernel_name = "myenv"', 'kernel_name = "nope"')
    )
    started = env.json("lab", "start", "--no-browser", cwd=lab_project)
    assert any(
        'harness.toml [jupyter].kernel_name asks for kernel "nope"' in w
        for w in started["warnings"]
    )
    env.json("lab", "stop", cwd=lab_project)
