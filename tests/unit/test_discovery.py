"""backend/discovery: runtime dirs, stale pids, /api/status with the token, rejecting old or
collaboration-less servers (E130), another server holding the notebook (E138), notebook paths (E132).
Jupyter servers are simulated with a tiny local HTTP server; runtime dirs are temp folders.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from urllib3.util import parse_url

from nh_gateway import config
from nh_gateway.backend import discovery
from nh_gateway.policy.errors import NhError


@dataclass
class FakeJupyter:
    token: str = "secret-token"
    version: str = "2.21.1"
    collaboration: bool = True
    sessions: list[dict] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def start(self) -> FakeJupyter:
        spec = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: object) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != f"token {spec.token}":
                    return self._send(403, {"message": "Forbidden"})
                path = self.path.split("?")[0]
                if path == "/api/status":
                    return self._send(200, {"started": "now"})
                if path == "/api":
                    return self._send(200, {"version": spec.version})
                if path.startswith("/api/collaboration/session/"):
                    return self._send(405 if spec.collaboration else 404, {"message": "no"})
                if path == "/api/sessions":
                    return self._send(200, spec.sessions)
                return self._send(404, {"message": "Not Found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def servers():
    started: list[FakeJupyter] = []

    def make(**kwargs) -> FakeJupyter:
        server = FakeJupyter(**kwargs).start()
        started.append(server)
        return server

    yield make
    for server in started:
        server.stop()


@pytest.fixture
def project(tmp_path, monkeypatch):
    for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN", "JUPYTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "work" / "proj"
    (root / ".nh" / "state").mkdir(parents=True)
    (root / "notebooks").mkdir()
    (root / "notebooks" / "01_eda.ipynb").write_text("{}")
    return Path(os.path.realpath(root))


@pytest.fixture
def runtime(tmp_path):
    folder = tmp_path / "runtime"
    folder.mkdir()
    return folder


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_info(
    folder: Path,
    server: FakeJupyter,
    root: Path,
    *,
    pid: int | None = None,
    name: str | None = None,
    token: str | None = None,
) -> Path:
    pid = os.getpid() if pid is None else pid
    path = folder / (name or f"jpserver-{pid}-{server.server.server_address[1]}.json")
    path.write_text(
        json.dumps(
            {
                "url": server.url,
                "token": server.token if token is None else token,
                "root_dir": str(root),
                "pid": pid,
                "version": server.version,
            }
        )
    )
    return path


def cfg(project: Path, **jupyter) -> config.Config:
    loaded = config.load(project)
    loaded.data["jupyter"].update(jupyter)
    return loaded


def test_runtime_dirs_cover_the_four_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    dirs = discovery.runtime_dirs()
    real = Path(os.path.realpath(tmp_path))
    assert dirs == [
        real / "custom",
        real / "Library/Jupyter/runtime",
        real / "Library/Application Support/Jupyter/runtime",
        real / "xdg/jupyter/runtime",
    ]


def test_scan_skips_stale_pids_and_never_deletes(runtime, project, servers):
    server = servers()
    alive = write_info(runtime, server, project)
    stale = write_info(runtime, server, project, pid=dead_pid(), name="jpserver-1-stale.json")
    (runtime / "jpserver-2-broken.json").write_text("{not json")
    found = discovery.scan([runtime])
    assert [item.source for item in found] == [str(alive)]
    assert stale.exists() and (runtime / "jpserver-2-broken.json").exists()


def test_prefers_the_project_root_then_the_deepest(runtime, project, servers):
    home_server, parent_server, project_server = servers(), servers(), servers()
    write_info(runtime, home_server, project.parent.parent, name="jpserver-1.json")
    write_info(runtime, parent_server, project.parent, name="jpserver-2.json")
    assert discovery.discover(project, cfg(project), dirs=[runtime]).url == parent_server.url
    write_info(runtime, project_server, project, name="jpserver-3.json")
    info = discovery.discover(project, cfg(project), dirs=[runtime])
    assert (
        info.url == project_server.url
        and info.token == project_server.token
        and info.root_dir == project
    )


def test_ignores_servers_that_do_not_serve_the_project(runtime, project, servers, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_info(runtime, servers(), elsewhere)
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), dirs=[runtime])
    assert info.value.code == "E130"


def test_requires_the_token_to_work(runtime, project, servers):
    write_info(runtime, servers(), project, token="wrong")
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), dirs=[runtime])
    assert info.value.code == "E130" and "rejected nh's token" in str(info.value)
    assert "secret-token" not in str(info.value) and "wrong" not in str(info.value)


def test_rejects_an_old_global_jupyter_server_without_advising_an_upgrade(
    runtime, project, servers
):
    write_info(runtime, servers(version="1.24.0"), project.parent.parent)
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), dirs=[runtime])
    message = str(info.value)
    assert info.value.code == "E130"
    assert "jupyter_server 1.24.0" in message and "not this project's JupyterLab" in message
    assert "nhctl lab start" in message and "upgrade" not in message.lower()


def test_rejects_a_server_without_collaboration_but_uses_a_good_one(runtime, project, servers):
    plain = servers(collaboration=False)
    write_info(runtime, plain, project, name="jpserver-1.json")
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), dirs=[runtime])
    assert "no real-time collaboration" in str(info.value)
    good = servers()
    write_info(runtime, good, project.parent, name="jpserver-2.json")
    assert discovery.discover(project, cfg(project), dirs=[runtime]).url == good.url


def test_nothing_running(runtime, project):
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), dirs=[runtime])
    assert info.value.code == "E130" and "No running Jupyter server" in str(info.value)


def test_explicit_url_from_env_and_config(runtime, project, servers, monkeypatch):
    server = servers()
    write_info(runtime, server, project.parent)
    monkeypatch.setenv("NH_JUPYTER_URL", server.url)
    monkeypatch.setenv("NH_JUPYTER_TOKEN", server.token)
    info = discovery.discover(project, cfg(project), dirs=[runtime])
    assert (
        info.url == server.url and info.root_dir == project.parent
    )  # root taken from its runtime file
    monkeypatch.delenv("NH_JUPYTER_URL")
    info = discovery.discover(project, cfg(project, url=server.url), dirs=[])
    assert (
        info.url == server.url and info.root_dir == project
    )  # no runtime file: assume the project root
    monkeypatch.delenv("NH_JUPYTER_TOKEN")
    info = discovery.discover(project, cfg(project, url=server.url), dirs=[runtime])
    assert info.token == server.token  # a loopback [jupyter].url takes its own runtime file's token
    monkeypatch.setenv("NH_JUPYTER_TOKEN", "wrong")
    with pytest.raises(NhError):
        discovery.discover(project, cfg(project, url=server.url), dirs=[])


def test_is_loopback():
    for url in (
        "http://127.0.0.1:8888/",
        "http://127.3.4.5/",
        "https://localhost:9/lab",
        "http://[::1]:8888/",
    ):
        assert discovery.is_loopback(url), url
    for url in (
        "http://10.0.0.5:8888/",
        "http://example.com/",
        "http://127.0.0.1.nip.io/",
        "ftp://127.0.0.1/",
        "http://localhost.evil.test/",
        "not a url",
    ):
        assert not discovery.is_loopback(url), url
    # requests (urllib3) ends the host at a backslash where urllib.parse reads 127.0.0.1
    for url in (
        "http://evil.example\\@127.0.0.1:8888/",
        "http://evil.example:8888\\@127.0.0.1:8888/",
        "http://evil.example:80\\@127.0.0.1/",
        "http://127.0.0.1:8888\\@evil.example/",
        "http://127.0.0.1:8888 @evil.example/",
        "http://127.0.0.1\t.evil.example/",
        "http://evil.example\n@127.0.0.1/",
    ):
        assert not discovery.is_loopback(url), url
        assert discovery.local_url(url) is None, url
        assert discovery.clean_url(url) is None, url


@dataclass
class Capture:
    """Records every request (with its Authorization header) and answers like a Jupyter server."""

    seen: list[dict] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def start(self) -> Capture:
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _any(self):
                seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
                data = json.dumps({"version": "2.14.0"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_POST = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self


@pytest.fixture
def capture():
    spy = Capture().start()
    yield spy
    assert spy.server is not None
    spy.server.shutdown()
    spy.server.server_close()


def sent_to_loopback(url: str) -> bool:
    """Where requests would really connect (urllib3's parse), independent of nh's own check."""
    host = (parse_url(url).host or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture
def outgoing(monkeypatch):
    """Every URL nh's REST client asks for (requests is patched; nothing leaves the machine)."""
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


async def test_committed_harness_toml_never_sends_an_env_var_anywhere(
    project, capture, outgoing, monkeypatch
):
    """Finding 2: harness.toml is committed; its token_env and a remote url must not leak a token."""
    from nh_gateway._shared.paths import Layout
    from nh_gateway.backend.rtc_backend import RtcBackend
    from nh_gateway.config import ConfigCache

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRET_FROM_USER_SHELL")
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(project / "no-runtime"))
    for url in (capture.url, "http://nh-exfil.invalid:9/"):
        (project / "harness.toml").write_text(
            f'version = 1\n[jupyter]\nurl = "{url}"\ntoken_env = "GITHUB_TOKEN"\n'
        )
        backend = RtcBackend(Layout(project), ConfigCache(project))
        try:
            ref = await backend.resolve_notebook("notebooks/01_eda.ipynb")
            with pytest.raises(NhError):
                await backend.kernel_status(ref)
            status = await backend.describe(ref)
        finally:
            await backend.aclose()
        if url == capture.url:
            assert capture.seen, "a loopback [jupyter].url is still used"
        else:
            assert "is not on this machine" in status["config"]
    assert all("ghp_SECRET" not in str(call) for call in [*capture.seen, *outgoing]), (
        "an environment variable named in harness.toml was sent to a server"
    )
    assert all(item["auth"] is None for item in capture.seen)
    assert not any("nh-exfil.invalid" in url for url, _ in outgoing), "a remote url was contacted"


def test_runtime_files_must_be_loopback(runtime, project, servers, outgoing, monkeypatch):
    local = servers()
    record = write_info(runtime, local, project, name="jpserver-1.json")
    remote = json.loads(record.read_text())
    remote["url"] = "http://nh-remote.invalid:8888/"
    (runtime / "jpserver-2.json").write_text(json.dumps({**remote, "root_dir": str(project)}))
    info = discovery.discover(project, cfg(project), dirs=[runtime])
    assert info.url == local.url
    assert not any("nh-remote.invalid" in url for url, _ in outgoing)
    (runtime / "jpserver-1.json").unlink()
    with pytest.raises(NhError) as refused:
        discovery.discover(project, cfg(project), dirs=[runtime])
    assert "isn't on this machine's loopback address" in str(refused.value)
    assert not any("nh-remote.invalid" in url for url, _ in outgoing)
    # a server listening on every interface writes the hostname: nh reaches it on 127.0.0.1
    port = local.server.server_address[1]
    remote["url"] = f"http://{socket.gethostname()}:{port}/"
    (runtime / "jpserver-2.json").write_text(json.dumps({**remote, "root_dir": str(project)}))
    info = discovery.discover(project, cfg(project), dirs=[runtime])
    assert info.url == f"http://127.0.0.1:{port}/" and info.token == local.token


def test_lab_json_runtime_file_must_be_loopback(runtime, project, servers, outgoing):
    elsewhere = runtime / "committed-runtime.json"
    elsewhere.write_text(
        json.dumps(
            {
                "url": "http://nh-remote.invalid:8888/",
                "token": "t",
                "root_dir": str(project),
                "pid": os.getpid(),
            }
        )
    )
    (project / ".nh" / "state" / "lab.json").write_text(
        json.dumps({"pid": os.getpid(), "runtime_file": str(elsewhere)})
    )
    with pytest.raises(NhError) as refused:
        discovery.discover(project, cfg(project), dirs=[])
    assert "isn't on this machine's loopback address" in str(refused.value)
    assert outgoing == []


def test_lab_json_wins_over_the_scan(runtime, project, servers):
    scanned, launched = servers(), servers()
    write_info(runtime, scanned, project, name="jpserver-1.json")
    lab_file = write_info(runtime, launched, project, name="jpserver-2.json")
    (project / ".nh" / "state" / "lab.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "url": launched.url,
                "runtime_file": str(lab_file),
                "env_prefix": "/x",
            }
        )
    )
    assert discovery.discover(project, cfg(project), dirs=[runtime]).url == launched.url


def test_another_server_with_a_session_for_the_notebook_is_e138(runtime, project, servers):
    ours = servers()
    write_info(runtime, ours, project, name="jpserver-1.json")
    other = servers(
        version="1.24.0",
        collaboration=False,
        sessions=[
            {"path": "proj/notebooks/01_eda.ipynb", "type": "notebook", "kernel": {"id": "k"}}
        ],
    )
    write_info(runtime, other, project.parent, name="jpserver-2.json")
    notebook = project / "notebooks" / "01_eda.ipynb"
    with pytest.raises(NhError) as info:
        discovery.discover(project, cfg(project), notebook=notebook, dirs=[runtime])
    assert info.value.code == "E138" and other.url in str(info.value)
    other.sessions = [{"path": "proj/notebooks/other.ipynb", "type": "notebook"}]
    assert (
        discovery.discover(project, cfg(project), notebook=notebook, dirs=[runtime]).url == ours.url
    )


def test_resolve_notebook(project, tmp_path):
    info = discovery.ServerInfo(url="http://127.0.0.1:1/", token="t", root_dir=project.parent)
    ref = discovery.resolve_notebook(project, info, None, cfg(project))
    assert (
        ref.rel_path == "notebooks/01_eda.ipynb" and ref.api_path == "proj/notebooks/01_eda.ipynb"
    )
    assert ref.abs_path == project / "notebooks" / "01_eda.ipynb"
    assert (
        discovery.resolve_notebook(project, None, "./notebooks/01_eda.ipynb", cfg(project)).api_path
        == "notebooks/01_eda.ipynb"
    )
    with pytest.raises(NhError) as missing:
        discovery.resolve_notebook(project, info, "notebooks/nope.ipynb", cfg(project))
    assert missing.value.code == "E132" and "notebooks/01_eda.ipynb" in str(missing.value)
    with pytest.raises(NhError):
        discovery.resolve_notebook(project, info, "../outside.ipynb", cfg(project))
    elsewhere = discovery.ServerInfo(
        url="http://127.0.0.1:1/", token="t", root_dir=tmp_path / "other"
    )
    with pytest.raises(NhError) as outside:
        discovery.resolve_notebook(project, elsewhere, None, cfg(project))
    assert outside.value.code == "E130"


def test_symlinked_project_is_realpathed(runtime, project, servers, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(project)
    server = servers()
    write_info(runtime, server, project)
    assert discovery.discover(link, cfg(project), dirs=[runtime]).url == server.url
    ref = discovery.resolve_notebook(
        link,
        discovery.ServerInfo(url=server.url, token=None, root_dir=project),
        "notebooks/01_eda.ipynb",
        cfg(project),
    )
    assert ref.api_path == "notebooks/01_eda.ipynb"


# ---------------------------------------------------------------------------- the notebook's session kernel


class StubRest:
    """Just enough of rest.Rest for kernel.attach_session."""

    def __init__(
        self,
        sessions: list[dict],
        *,
        unknown: tuple[str, ...] = (),
        after_create: list[dict] | None = None,
        renamed_after: int | None = None,
        api_path: str = "notebooks/01_eda.ipynb",
    ) -> None:
        self._sessions = sessions
        self.unknown = unknown
        self.after_create = after_create
        self.renamed_after = renamed_after
        self.api_path = api_path
        self.calls = 0
        self.created: list[str | None] = []

    def sessions(self) -> list[dict]:
        self.calls += 1
        if self.renamed_after is not None and self.calls > self.renamed_after:
            for session in self._sessions:
                session["path"] = self.api_path
        return [dict(s) for s in self._sessions]

    def create_session(self, api_path: str, kernel_name: str | None) -> dict:
        from nh_gateway.backend.rest import RestError

        self.created.append(kernel_name)
        if kernel_name in self.unknown:
            raise RestError(501, f"The '{kernel_name}' kernel is not available.")
        session = {
            "id": "new",
            "path": api_path,
            "type": "notebook",
            "kernel": {"id": "k-new", "name": kernel_name},
        }
        self._sessions = (
            self.after_create if self.after_create is not None else self._sessions + [session]
        )
        return session


def session(path: str, kernel_id: str, connections: int = 0, name: str = "01_eda.ipynb") -> dict:
    return {
        "id": kernel_id,
        "path": path,
        "name": name,
        "type": "notebook",
        "kernel": {
            "id": kernel_id,
            "name": "python3",
            "connections": connections,
            "execution_state": "idle",
        },
    }


def test_attach_uses_the_existing_session_with_most_connections():
    from nh_gateway.backend import kernel

    api = StubRest(
        [
            session("notebooks/01_eda.ipynb", "a", 1),
            session("notebooks/01_eda.ipynb", "b", 3),
            session("notebooks/other.ipynb", "c", 9),
        ]
    )
    chosen, created = kernel.attach_session(api, "notebooks/01_eda.ipynb", ["python3"])
    assert chosen["kernel"]["id"] == "b" and created is False and api.created == []


def test_attach_waits_for_a_uuid_session_to_be_renamed():
    from nh_gateway.backend import kernel

    api = StubRest(
        [session("notebooks/0b6f7a57-3f25-4d4c-9f7e-3a2b1c0d9e8f", "ui", 1)], renamed_after=3
    )
    chosen, created = kernel.attach_session(
        api, "notebooks/01_eda.ipynb", ["python3"], uuid_wait_s=5
    )
    assert chosen["kernel"]["id"] == "ui" and created is False and api.created == []


def test_attach_gives_up_waiting_and_creates_a_session():
    from nh_gateway.backend import kernel

    api = StubRest(
        [session("notebooks/0b6f7a57-3f25-4d4c-9f7e-3a2b1c0d9e8f", "ui", 1, name="other.ipynb")]
    )
    chosen, created = kernel.attach_session(
        api, "notebooks/01_eda.ipynb", ["python3"], uuid_wait_s=0.3
    )
    assert created is True and api.created == ["python3"] and chosen["kernel"]["id"] == "k-new"


def test_attach_falls_back_through_kernel_names_on_501():
    from nh_gateway.backend import kernel

    api = StubRest([], unknown=("ir", "custom"))
    chosen, created = kernel.attach_session(
        api, "notebooks/01_eda.ipynb", ["ir", "custom", "ir", "", "python3"]
    )
    assert created is True and api.created == ["ir", "custom", "python3"]
    api = StubRest([], unknown=("ir", "python3"))
    with pytest.raises(NhError) as info:
        kernel.attach_session(api, "notebooks/01_eda.ipynb", ["ir", "python3"])
    assert info.value.code == "E134"


def test_attach_prefers_the_session_jupyterlab_raced_in():
    from nh_gateway.backend import kernel

    racing = [
        session("notebooks/01_eda.ipynb", "ours", 0),
        session("notebooks/01_eda.ipynb", "ui", 2),
    ]
    api = StubRest([], after_create=racing)
    chosen, created = kernel.attach_session(api, "notebooks/01_eda.ipynb", ["python3"])
    assert chosen["kernel"]["id"] == "ui" and created is True


def test_attach_without_create_refuses_instead_of_starting_a_kernel():
    from nh_gateway.backend import kernel

    api = StubRest([])
    with pytest.raises(NhError) as info:
        kernel.attach_session(api, "notebooks/01_eda.ipynb", ["python3"], create=False)
    assert info.value.code == "E134" and api.created == []


def test_configured_kernel_name_is_tried_first():
    """Finding 34: [jupyter].kernel_name comes before the notebook's own kernelspec."""
    from nh_gateway.backend.kernel import attach_session
    from nh_gateway.backend.rtc_backend import kernel_candidates

    assert kernel_candidates("python3", "myenv") == ["myenv", "python3", "python3"]
    assert kernel_candidates("ir", "") == ["", "ir", "python3"]
    api = StubRest([])
    attach_session(api, "notebooks/01_eda.ipynb", kernel_candidates("python3", "myenv"))
    assert api.created == ["myenv"]
    api = StubRest([], unknown=("myenv",))
    attach_session(api, "notebooks/01_eda.ipynb", kernel_candidates("python3", "myenv"))
    assert api.created == ["myenv", "python3"]
