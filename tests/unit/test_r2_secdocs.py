"""Round 2 (V4/V16): a URL that urllib.parse and requests read differently never gets a token.

``http://evil.example\\@127.0.0.1:8888/`` is host 127.0.0.1 to urllib.parse but evil.example to
requests (urllib3). nh refuses such URLs and sends requests only to the URL it rebuilt, and a
runtime file's token is lent only to exactly its own host and port.

Also (V15): every status the gateway writes to last_cell.json has plain words in the reminder and
in the notebook skill, and E117 is documented.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from urllib3.util import parse_url

from nh_gateway import config
from nh_gateway.backend import discovery
from nh_gateway.backend.base import ServerInfo
from nh_gateway.policy.errors import NhError

TOKEN = "LOCAL_JUPYTER_TOKEN_abc123"


def sent_to_loopback(url: str) -> bool:
    """Where requests really connects (urllib3's parse), independent of nh's own check."""
    host = (parse_url(url).host or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture
def outgoing(monkeypatch):
    """Every request nh makes; only loopback ones (by urllib3's reading) go through."""
    import requests

    calls: list[tuple[str, dict]] = []
    real = requests.request

    def record(method, url, headers=None, **kwargs):
        calls.append((url, dict(headers or {})))
        if sent_to_loopback(url):
            return real(method, url, headers=headers, **kwargs)
        raise requests.ConnectionError("blocked by the test")

    monkeypatch.setattr(requests, "request", record)
    return calls


def off_machine(calls: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return [call for call in calls if not sent_to_loopback(call[0])]


@pytest.fixture
def jupyter():
    """A loopback server that answers like a collaboration-enabled jupyter_server 2."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != f"token {TOKEN}":
                status, body = 403, {"message": "Forbidden"}
            elif self.path.startswith("/api/collaboration/session/"):
                status, body = 405, {"message": "no"}
            else:
                status, body = 200, {"version": "2.14.0"}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """No token variables, and runtime dirs that hold only what the test writes."""
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN", "JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg"))
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(runtime))
    return runtime


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "cloned"
    (root / ".nh" / "state").mkdir(parents=True)
    (root / "notebooks").mkdir()
    (root / "notebooks" / "01_eda.ipynb").write_text(
        json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    )
    return Path(os.path.realpath(root))


def runtime_record(folder: Path, url: str, root: Path, name: str = "jpserver-1.json") -> Path:
    path = folder / name
    path.write_text(
        json.dumps(
            {
                "url": url,
                "token": TOKEN,
                "root_dir": str(root),
                "pid": os.getpid(),
                "version": "2.14.0",
            }
        )
    )
    return path


def test_clean_url_rebuilds_the_url_requests_will_use():
    assert (
        discovery.clean_url("http://user:pw@127.0.0.1:08888/lab?token=x#top")
        == "http://127.0.0.1:8888/lab"
    )
    assert discovery.clean_url("HTTP://LOCALHOST:8888") == "http://localhost:8888/"
    assert discovery.clean_url("http://[::1]:8888/") == "http://[::1]:8888/"
    assert discovery.clean_url("http://127.0.0.1:0/") == "http://127.0.0.1:0/"
    assert discovery.clean_url("https://jupyter.example.com/base/") == (
        "https://jupyter.example.com/base/"
    )
    for url in (
        "http://evil.example\\@127.0.0.1:8888/",
        "http://evil.example:9\\@127.0.0.1:8888/",
        "http://127.0.0.1 /",
        "http://127.0.0.1 /",
        "http://127.0.0.1%2e/",
        "http://evil{127.0.0.1:8888/",  # requests would percent-encode the host it looks up
        "http://[fe80::1%25en0]:8888/",
        "http://127.0.0.1:99999/",
        "http://[::1/",
        "ftp://127.0.0.1/",
        "http:///x",
        "",
    ):
        assert discovery.clean_url(url) is None, url
        assert not discovery.is_loopback(url), url


def test_a_runtime_token_is_lent_only_to_its_exact_host_and_port(home, tmp_path):
    scanned = [
        discovery.Found(
            info=ServerInfo(url="http://127.0.0.1:8888/", token=TOKEN, root_dir=tmp_path),
            source="jpserver-1.json",
        )
    ]
    assert discovery._token_for("http://127.0.0.1:8888", scanned) == TOKEN
    for url in (
        "http://localhost:8888/",
        "http://[::1]:8888/",
        "http://127.0.0.1:8889/",
        "http://127.0.0.1/",
        "http://evil.example\\@127.0.0.1:8888/",
        "http://evil.example:8888\\@127.0.0.1:8888/",
    ):
        assert discovery._token_for(url, scanned) is None, url


@pytest.mark.parametrize("with_env_token", [True, False])
async def test_backslash_config_url_sends_no_token_off_the_machine(
    home, project, jupyter, outgoing, monkeypatch, with_env_token
):
    """The verify repro: a committed harness.toml with a backslash URL and nh_inspect(status)."""
    from nh_gateway._shared.paths import Layout
    from nh_gateway.backend.rtc_backend import RtcBackend
    from nh_gateway.config import ConfigCache

    port, seen = jupyter
    # an unrelated local JupyterLab, whose runtime file the config URL tries to borrow a token from
    runtime_record(home, f"http://127.0.0.1:{port}/", project.parent / "some_other_project")
    if with_env_token:
        monkeypatch.setenv("JUPYTER_TOKEN", "jt_SECRET_ENV")
    for url in (
        f"http://nh-exfil.invalid:{port}\\@127.0.0.1:{port}/",
        f"http://nh-exfil.invalid\\@127.0.0.1:{port}/",
    ):
        (project / "harness.toml").write_text(f"version = 1\n[jupyter]\nurl = '{url}'\n")
        backend = RtcBackend(Layout(project), ConfigCache(project))
        try:
            ref = await backend.resolve_notebook("notebooks/01_eda.ipynb")
            with pytest.raises(NhError):
                await backend.kernel_status(ref)
            status = await backend.describe(ref)
        finally:
            await backend.aclose()
        assert "[jupyter].url is not a plain URL, so nh ignored it" in status["config"]
    assert off_machine(outgoing) == [], "a request left the machine"
    assert all(item["auth"] is None for item in seen), "a token was sent"


def test_lab_json_with_a_backslash_url_is_refused(home, project, outgoing):
    """verify/security/labjson_vector.py: a committed lab.json and runtime record."""
    record = runtime_record(
        project / ".nh" / "state", "http://nh-exfil.invalid:9\\@127.0.0.1:8888/", project, "rt.json"
    )
    (project / ".nh" / "state" / "lab.json").write_text(
        json.dumps({"pid": os.getpid(), "runtime_file": str(record)})
    )
    with pytest.raises(NhError) as refused:
        discovery.discover(project, config.load(project), dirs=[])
    assert refused.value.code == "E130"
    assert "isn't a plain http(s) address" in str(refused.value)
    assert outgoing == []


def test_runtime_file_with_a_backslash_url_is_never_contacted(home, project, outgoing):
    runtime_record(home, "http://nh-exfil.invalid\\@127.0.0.1:8888/", project)
    with pytest.raises(NhError) as refused:
        discovery.discover(project, config.load(project), dirs=[home])
    assert "isn't a plain http(s) address" in str(refused.value)
    assert outgoing == []


def test_env_url_with_a_backslash_is_refused(home, project, outgoing, monkeypatch):
    monkeypatch.setenv("NH_JUPYTER_URL", "http://nh-exfil.invalid\\@127.0.0.1:8888/")
    monkeypatch.setenv("NH_JUPYTER_TOKEN", "nh_SECRET")
    with pytest.raises(NhError) as refused:
        discovery.discover(project, config.load(project), dirs=[])
    assert "isn't a plain http(s) address" in str(refused.value)
    assert outgoing == []


def test_discover_returns_the_rebuilt_url(home, project, jupyter, outgoing):
    port, seen = jupyter
    runtime_record(home, f"http://nh@127.0.0.1:{port}/?token=zzz#frag", project)
    info = discovery.discover(project, config.load(project), dirs=[home])
    assert info.url == f"http://127.0.0.1:{port}/" and info.token == TOKEN
    assert seen and all(url.startswith(f"http://127.0.0.1:{port}/api") for url, _ in outgoing)


# ---------------------------------------------------------------------------- V15: statuses

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"
GATEWAY = PLUGIN / "server" / "src" / "nh_gateway"
REFS = PLUGIN / "skills" / "notebook" / "reference"


def status_text() -> dict[str, str]:
    """prompt_submit.STATUS_TEXT, read without importing the hook package."""
    tree = ast.parse((PLUGIN / "hooks" / "nh_hooks" / "prompt_submit.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "STATUS_TEXT" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("prompt_submit.py has no STATUS_TEXT")


def written_statuses() -> set[str]:
    from nh_gateway.tools import common

    tools = "\n".join(p.read_text("utf-8") for p in (GATEWAY / "tools").glob("*.py"))
    common_source = (GATEWAY / "tools" / "common.py").read_text("utf-8")
    computed = {"running", "queued", "deleted"}  # common's status for an unfinished or gone cell
    assert all(f'"{status}"' in common_source for status in computed)
    return set(re.findall(r'status="(\w+)"', tools)) | set(common.FINAL_STATUSES) | computed


def test_every_written_status_has_plain_words_everywhere():
    statuses = written_statuses()
    assert {"queued", "deleted", "conflict", "interrupted", "lost"} <= statuses
    words = status_text()
    tools_md = (REFS / "tools.md").read_text("utf-8")
    statuses_line = tools_md[tools_md.index("Statuses: ") :]
    replies = (REFS / "replies.md").read_text("utf-8")
    for status in statuses - {"ok"}:
        assert status in words and words[status] != status, status
    for status in statuses:
        assert f"`{status}`" in statuses_line, status
    for status in statuses - {"undone"}:
        assert re.search(rf"^\| {status}\b", replies, flags=re.MULTILINE), status


def test_queued_is_treated_like_running_and_e117_is_documented():
    from nh_gateway.app import INSTRUCTIONS
    from nh_gateway.policy.errors import CATALOGUE

    skill = (PLUGIN / "skills" / "notebook" / "SKILL.md").read_text("utf-8")
    assert "RUNNING or QUEUED" in skill and "RUNNING or QUEUED" in INSTRUCTIONS
    assert len(INSTRUCTIONS) <= 2048
    assert "E117" in CATALOGUE
    assert "| E117 |" in (REFS / "errors.md").read_text("utf-8")
    assert "| `E117` |" in (REFS / "replies.md").read_text("utf-8")
