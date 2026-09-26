"""Find this project's JupyterLab (plan §4.4) and map notebooks to its API paths.

Order: ``NH_JUPYTER_URL``/``NH_JUPYTER_TOKEN``, ``harness.toml [jupyter].url``, ``.nh/state/lab.json``,
then a read-only scan of ``jpserver-*.json`` runtime files. Unlike ``list_running_servers`` nothing
here deletes stale files. A server must be alive, answer ``/api/status`` with its token, run
jupyter_server >= 2 and serve real-time collaboration; older or collaboration-less servers are "not
this project's JupyterLab" (E130). Everything here blocks: call it off the event loop.

Tokens never leave this machine unless the user's own environment says so: ``harness.toml`` is
committed, so its ``url`` is used only when it is a loopback address, and it names no token
source. URLs from ``lab.json`` and runtime files must be loopback too (a server bound to all
interfaces is reached through 127.0.0.1). Only ``NH_JUPYTER_URL`` may point at another host. Each
token goes only to the server it belongs to. Every URL is rebuilt by :func:`clean_url` before nh
checks or contacts it, so the host nh checks is the host ``requests`` connects to.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from urllib3.util import parse_url

from .._shared.paths import Layout, read_json
from ..config import Config
from ..policy.errors import NhError
from .base import NotebookRef, ServerInfo
from .rest import Rest, RestError, ServerGone

STATUS_TIMEOUT = 2.0


def runtime_dirs() -> list[Path]:
    """Where Jupyter servers write ``jpserver-<pid>.json``, most specific first, without duplicates."""
    home = Path.home()
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    raw = [
        os.environ.get("JUPYTER_RUNTIME_DIR"),
        str(home / "Library" / "Jupyter" / "runtime"),
        str(home / "Library" / "Application Support" / "Jupyter" / "runtime"),
        str(Path(xdg) / "jupyter" / "runtime"),
    ]
    dirs: list[Path] = []
    for item in raw:
        if not item:
            continue
        path = Path(os.path.realpath(item))
        if path not in dirs:
            dirs.append(path)
    return dirs


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, but owned by someone else
        return True
    except OSError:
        return False
    return True


@dataclass
class Found:
    """A candidate server and what probing it revealed."""

    info: ServerInfo
    source: str
    mtime: float = 0.0
    problem: str | None = None


def clean_url(url: str) -> str | None:
    """``url`` rebuilt as ``scheme://host[:port]/path``, or None when nh can't read it safely.

    nh checks a URL with ``urllib.parse`` but ``requests`` (urllib3) sends it, and the two split
    some URLs differently: ``http://evil.example\\@127.0.0.1:8888/`` is host 127.0.0.1 to the
    first and evil.example to the second. So a backslash, whitespace, a control or non-ASCII
    character is refused, both parsers must agree on the host, the host is a plain name or IP
    address (requests would percent-encode anything else), and nh uses only the rebuilt string
    (no user info, query or fragment) for REST calls and websockets.
    """
    if "\\" in url or not all(32 < ord(char) < 127 for char in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        sent_to = parse_url(url).host  # the host requests connects to
    except ValueError:  # urllib3's LocationParseError is a ValueError too
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if (sent_to or "").strip("[]").lower() != host or not _PLAIN_HOST.fullmatch(host):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}{parts.path or '/'}"


# a DNS name (jupyter_server writes the hostname for --ip 0.0.0.0, and some have "_") or IPv4,
# or an IPv6 address
_PLAIN_HOST = re.compile(r"[a-z0-9._-]+|[0-9a-f:.]+")


def is_loopback(url: str) -> bool:
    """True for an http(s) URL whose host is ``localhost``, 127.0.0.0/8 or ``::1`` (see :func:`clean_url`)."""
    clean = clean_url(url)
    if clean is None:
        return False
    host = urlsplit(clean).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host(url: str) -> str:
    """``host:port`` of a URL, without any user info (for messages)."""
    clean = clean_url(url)
    return urlsplit(clean).netloc if clean else "?"


def local_url(url: str) -> str | None:
    """A runtime file's URL as a clean loopback URL, or None when the server isn't reachable on loopback.

    jupyter_server writes the machine's hostname when it listens on every interface
    (``--ip 0.0.0.0``); that server also answers on 127.0.0.1, so nh talks to it there.
    """
    clean = clean_url(url)
    if clean is None or is_loopback(clean):
        return clean
    parts = urlsplit(clean)
    host, port = parts.hostname or "", parts.port
    names = {"0.0.0.0", "::", socket.gethostname().lower()}
    if host not in names and host.split(".")[0] != socket.gethostname().lower().split(".")[0]:
        return None
    netloc = f"127.0.0.1:{port}" if port else "127.0.0.1"
    return f"{parts.scheme}://{netloc}{parts.path}"


_UNCLEAN = (
    "A Jupyter server URL isn't a plain http(s) address (it has a backslash, a space or another "
    "unusual character), so nh doesn't use that server."
)


def _not_local(url: str) -> str:
    if clean_url(url) is None:
        return _UNCLEAN
    return (
        f"The Jupyter server at {_host(url)} isn't on this machine's loopback address; nh only "
        "uses such a server when NH_JUPYTER_URL and NH_JUPYTER_TOKEN are set."
    )


def _info(record: dict[str, Any]) -> ServerInfo | None:
    url = record.get("url")
    root = record.get("root_dir") or record.get("notebook_dir")
    if not isinstance(url, str) or not url or not isinstance(root, str):
        return None
    return ServerInfo(
        url=url,
        token=record.get("token") or None,
        root_dir=Path(os.path.realpath(root)),
        pid=record.get("pid") if isinstance(record.get("pid"), int) else None,
        version=str(record.get("version")) if record.get("version") else None,
    )


def scan(dirs: list[Path] | None = None) -> list[Found]:
    """Every parseable ``jpserver-*.json`` whose pid is alive. Read-only."""
    found: list[Found] = []
    for folder in dirs if dirs is not None else runtime_dirs():
        try:
            files = sorted(folder.glob("jpserver-*.json"))
        except OSError:
            continue
        for path in files:
            record = read_json(path)
            info = _info(record) if isinstance(record, dict) else None
            if info is None or not pid_alive(info.pid):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            found.append(_localized(Found(info=info, source=str(path), mtime=mtime)))
    return found


def _localized(found: Found) -> Found:
    """Point a runtime record at loopback, or mark it unusable (its token must not leave the machine)."""
    url = local_url(found.info.url)
    if url is None:
        found.problem = _not_local(found.info.url)
    elif url != found.info.url:
        found.info = ServerInfo(
            url=url,
            token=found.info.token,
            root_dir=found.info.root_dir,
            pid=found.info.pid,
            version=found.info.version,
        )
    return found


def _same_server(a: str, b: str, *, exact: bool = False) -> bool:
    """Same host, port and base path, compared on the rebuilt URLs. Loopback names count as one
    host unless ``exact``, which is used before lending a token."""
    clean_a, clean_b = clean_url(a), clean_url(b)
    if clean_a is None or clean_b is None:
        return False
    x, y = urlsplit(clean_a), urlsplit(clean_b)
    local = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    hosts_match = x.hostname == y.hostname or (
        not exact and x.hostname in local and y.hostname in local
    )
    return hosts_match and x.port == y.port and x.path.rstrip("/") == y.path.rstrip("/")


def _major(version: str | None) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return None


def check(found: Found) -> Found:
    """Probe a candidate: answers with the token, jupyter_server >= 2, collaboration installed."""
    if found.problem:  # never contact a server already ruled out
        return found
    url = clean_url(found.info.url)
    if url is None:
        found.problem = _UNCLEAN
        return found
    found.info = replace(found.info, url=url)  # the only form nh sends requests to
    api = Rest(found.info)
    where = found.info.url
    try:
        api.status(timeout=STATUS_TIMEOUT)
        version = found.info.version or api.version(timeout=STATUS_TIMEOUT)
        if version and found.info.version != version:
            found.info = ServerInfo(
                url=found.info.url,
                token=found.info.token,
                root_dir=found.info.root_dir,
                pid=found.info.pid,
                version=version,
            )
        major = _major(version)
        if major is not None and major < 2:
            found.problem = (
                f"The Jupyter server at {where} (jupyter_server {version}) is an older one, not this "
                "project's JupyterLab; nh doesn't use it."
            )
        elif not api.has_collaboration(timeout=STATUS_TIMEOUT):
            found.problem = (
                f"The Jupyter server at {where} has no real-time collaboration, so it is not this "
                "project's JupyterLab; nh doesn't use it."
            )
    except ServerGone:
        found.problem = f"The Jupyter server at {where} doesn't answer."
    except RestError as exc:
        if exc.status in (401, 403) and not found.info.token:
            reason = (
                "needs a token nh doesn't have (set NH_JUPYTER_TOKEN, or remove [jupyter].url "
                "so nh finds the server itself)"
            )
        elif exc.status in (401, 403):
            reason = "rejected nh's token"
        else:
            reason = f"answered {exc.status}"
        found.problem = f"The Jupyter server at {where} {reason}."
    return found


def _contains(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def config_url_problem(cfg: Config) -> str | None:
    """Why ``harness.toml [jupyter].url`` is ignored (not a loopback URL), or None."""
    if os.environ.get("NH_JUPYTER_URL"):
        return None
    url = str(cfg["jupyter"].get("url") or "")
    if not url or is_loopback(url):
        return None
    where = f"({_host(url)}) is not on this machine" if clean_url(url) else "is not a plain URL"
    return (
        f"harness.toml [jupyter].url {where}, so nh ignored it; "
        "for a JupyterLab on another host set NH_JUPYTER_URL and NH_JUPYTER_TOKEN."
    )


def _token_for(url: str, scanned: list[Found]) -> str | None:
    """The token for an explicitly chosen server: NH_JUPYTER_TOKEN, its own runtime file, JUPYTER_TOKEN.

    A runtime file's token is lent only to exactly its own host and port.
    """
    token = os.environ.get("NH_JUPYTER_TOKEN")
    if token:
        return token
    for item in scanned:
        if not item.problem and item.info.token and _same_server(item.info.url, url, exact=True):
            return item.info.token
    return os.environ.get("JUPYTER_TOKEN") or None


def _local_twin(url: str, scanned: list[Found]) -> Found | None:
    """The scanned runtime file of a loopback ``url`` spelled with another loopback name
    (``127.0.0.1`` for a lab started as ``localhost``), when nh has no token of its own for it."""
    if os.environ.get("NH_JUPYTER_TOKEN") or not is_loopback(url):
        return None
    for item in scanned:
        if item.problem or not item.info.token:
            continue
        if _same_server(item.info.url, url) and not _same_server(item.info.url, url, exact=True):
            return item
    return None


def _explicit(cfg: Config) -> str | None:
    """The URL the user chose: ``NH_JUPYTER_URL`` (any host), else a loopback ``[jupyter].url``."""
    url = os.environ.get("NH_JUPYTER_URL")
    if url:
        return url
    url = str(cfg["jupyter"].get("url") or "")
    if url and is_loopback(url):
        return url
    return None


def _root_for(url: str, project: Path, scanned: list[Found]) -> Path:
    for item in scanned:
        if _same_server(item.info.url, url):
            return item.info.root_dir
    return project


def _from_lab_json(project: Path) -> Found | None:
    lab = read_json(Layout(project).lab_json)
    if not isinstance(lab, dict) or not pid_alive(lab.get("pid")):
        return None
    runtime = lab.get("runtime_file")
    record = read_json(Path(runtime)) if isinstance(runtime, str) and runtime else None
    info = _info(record) if isinstance(record, dict) else None
    if info is None:
        return None
    return _localized(Found(info=info, source="lab.json"))


def _refuse(problems: list[str]) -> NhError:
    detail = (
        "\n".join(dict.fromkeys(problems))
        if problems
        else "No running Jupyter server serves this project."
    )
    return NhError("E130", detail=detail)


def discover(
    project: Path, cfg: Config, *, notebook: Path | None = None, dirs: list[Path] | None = None
) -> ServerInfo:
    """The project's JupyterLab. With ``notebook``, also refuse (E138) if another server has it open."""
    if sys.platform == "win32":
        raise NhError("E137")
    project = Path(os.path.realpath(project))
    scanned = scan(dirs)
    problems: list[str] = []
    ignored = config_url_problem(cfg)
    if ignored:
        problems.append(ignored)
    chosen: Found | None = None
    url = _explicit(cfg)
    if url is not None:
        twin = _local_twin(url, scanned)
        info = (
            twin.info  # its own URL: the token still goes only to the host it belongs to
            if twin is not None
            else ServerInfo(
                url=url, token=_token_for(url, scanned), root_dir=_root_for(url, project, scanned)
            )
        )
        chosen = check(Found(info=info, source="config"))
        if chosen.problem:
            raise _refuse([chosen.problem])
    if chosen is None:
        lab = _from_lab_json(project)
        if lab is not None and _contains(lab.info.root_dir, project):
            lab = check(lab)
            if lab.problem:
                problems.append(lab.problem)
            else:
                chosen = lab
    if chosen is None:
        candidates = [item for item in scanned if _contains(item.info.root_dir, project)]
        candidates.sort(
            key=lambda item: (
                item.info.root_dir == project,
                len(item.info.root_dir.parts),
                item.mtime,
            ),
            reverse=True,
        )
        for item in candidates:
            check(item)
            if item.problem:
                problems.append(item.problem)
                continue
            chosen = item
            break
    if chosen is None:
        raise _refuse(problems)
    if notebook is not None:
        _refuse_other_servers(chosen.info, scanned, Path(os.path.realpath(notebook)))
    return chosen.info


def refuse_other_servers(
    server: ServerInfo, notebook: Path, dirs: list[Path] | None = None
) -> None:
    """E138 when a different live server has a kernel session for the same notebook file."""
    _refuse_other_servers(server, scan(dirs), Path(os.path.realpath(notebook)))


def _refuse_other_servers(server: ServerInfo, scanned: list[Found], notebook: Path) -> None:
    for item in scanned:
        if (
            item.problem
            or _same_server(item.info.url, server.url)
            or not _contains(item.info.root_dir, notebook)
        ):
            continue
        try:
            sessions = Rest(item.info).sessions()
        except (ServerGone, RestError, ValueError):
            continue
        for session in sessions:
            path = session.get("path")
            if (
                isinstance(path, str)
                and Path(os.path.realpath(item.info.root_dir / path)) == notebook
            ):
                raise NhError("E138", url=item.info.url)


# ---------------------------------------------------------------------------- notebooks


def candidates(project: Path, limit: int = 8) -> str:
    found: list[str] = []
    skip = {".nh", ".ipynb_checkpoints", ".venv", ".conda", "node_modules", ".git"}
    for path in sorted(project.rglob("*.ipynb")):
        rel = path.relative_to(project)
        if skip.intersection(rel.parts):
            continue
        found.append(rel.as_posix())
        if len(found) >= limit:
            break
    return ", ".join(found) or "none"


def resolve_notebook(
    project: Path, server: ServerInfo | None, rel_or_none: str | None, cfg: Config
) -> NotebookRef:
    """Map a project-relative notebook path to the server's API path. E132 if it isn't a notebook here."""
    project = Path(os.path.realpath(project))
    rel = (rel_or_none or str(cfg["project"].get("notebook") or "")).replace("\\", "/").strip()
    if not rel:
        raise NhError("E132", notebook="(none)", candidates=candidates(project))
    path = Path(os.path.realpath(project / rel))
    if not _contains(project, path) or path.suffix != ".ipynb" or not path.is_file():
        raise NhError("E132", notebook=rel, candidates=candidates(project))
    rel_path = path.relative_to(project).as_posix()
    if server is None:
        return NotebookRef(abs_path=path, api_path=rel_path, rel_path=rel_path)
    if not _contains(server.root_dir, path):
        raise NhError(
            "E130",
            detail=f"The JupyterLab at {server.url} serves {server.root_dir}, which doesn't "
            f"contain {rel_path}.",
        )
    return NotebookRef(
        abs_path=path, api_path=path.relative_to(server.root_dir).as_posix(), rel_path=rel_path
    )
