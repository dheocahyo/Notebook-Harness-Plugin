"""nhctl lab start|status|stop: the project's own JupyterLab, from the project env.

``lab start`` generates the server token and hands it to jupyter through
``JUPYTER_TOKEN_FILE``, a private temp file deleted once the server is up, so jupyter
never prints it into ``.nh/logs/jupyterlab.log`` and the kernels never inherit it. nhctl
then reads it back from jupyter's ``jpserver-<pid>.json`` and only ever sends it in an
Authorization header: it is never on argv, never printed and never in lab.json; the
browser gets it from jupyter's own ``jpserver-<pid>-open.html``.

A live JupyterLab that already serves the project (one the user started) is adopted:
lab.json is written for it instead of starting a second server on the same folder.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import common
import envsync
from common import NhctlError, Result

from nh_gateway._shared import paths
from nh_gateway._shared.scaffold import core

START_TIMEOUT_S = 30
STOP_TIMEOUT_S = 10
PROBE_TIMEOUT_S = 3
LOG_FILE = "jupyterlab.log"
BARE_PYTHONS = ("python", "python3")
SERVER_FILE = re.compile(r"jpserver-\d+\.json$")
# Environment the launched server must not inherit: nhctl passes its own token.
TOKEN_VARS = ("JUPYTER_TOKEN", "JUPYTER_TOKEN_FILE")


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    lab = sub.add_parser("lab", parents=[common_opts], help="the project's JupyterLab")
    lab_sub = lab.add_subparsers(dest="action", required=True)
    start = lab_sub.add_parser("start", parents=[common_opts], help="start it (detached)")
    start.add_argument("--notebook", help="notebook to open (default: harness.toml's)")
    start.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    start.add_argument("--timeout", type=float, default=START_TIMEOUT_S)
    start.set_defaults(func=cmd_start)
    status = lab_sub.add_parser("status", parents=[common_opts], help="is it running?")
    status.set_defaults(func=cmd_status)
    stop = lab_sub.add_parser("stop", parents=[common_opts], help="stop it")
    stop.set_defaults(func=cmd_stop)


# ----------------------------------------------------------------------------- REST


def api(server: dict, method: str, path: str, body: dict | None = None, timeout: float = 10):
    """Call the Jupyter REST API. Proxies are bypassed: the server is on localhost."""
    url = urllib.parse.urljoin(server["url"], path.lstrip("/"))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"token {server.get('token', '')}")
    request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def ensure_session(server: dict, notebook: str, kernel_name: str) -> dict:
    """One kernel for the notebook: reuse its session, else create it before the browser opens."""
    for session in api(server, "GET", "api/sessions") or []:
        if session.get("path") == notebook:
            return {"created": False, "kernel": (session.get("kernel") or {}).get("name")}
    body = {
        "path": notebook,
        "name": os.path.basename(notebook),
        "type": "notebook",
        "kernel": {"name": kernel_name},
    }
    session = api(server, "POST", "api/sessions", body) or {}
    return {"created": True, "kernel": (session.get("kernel") or {}).get("name")}


def kernelspec_warning(
    server: dict, kernel_name: str, prefix: Path, asked_by: str = "The notebook"
) -> str | None:
    """Warn when the notebook's kernel would run a Python outside the project env."""
    specs = api(server, "GET", "api/kernelspecs") or {}
    entry = (specs.get("kernelspecs") or {}).get(kernel_name)
    if entry is None:
        return f'{asked_by} asks for kernel "{kernel_name}", which this JupyterLab doesn\'t have.'
    argv = (entry.get("spec") or {}).get("argv") or []
    exe = argv[0] if argv else ""
    if exe in BARE_PYTHONS:  # resolved on PATH, which starts with <prefix>/bin
        return None
    if exe and _under(exe, prefix):
        return None
    return (
        f'Kernel "{kernel_name}" runs {exe or "an unknown program"}, outside this project\'s '
        f"environment ({prefix}). Pick the project's Python kernel in JupyterLab."
    )


def _under(path: str, prefix: Path) -> bool:
    """Is ``path`` inside ``prefix``? A venv's python is a symlink to the base interpreter,
    so the unresolved path counts too."""
    roots = {os.path.abspath(prefix), os.path.realpath(prefix)}
    for candidate in {os.path.abspath(path), os.path.realpath(path)}:
        for root in roots:
            if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
                return True
    return False


# ---------------------------------------------------------------------- lab state


def runtime_dir(prefix: Path, env: dict) -> Path:
    code, out = common.run([str(prefix / "bin" / "jupyter"), "--runtime-dir"], timeout=30, env=env)
    lines = out.strip().splitlines()
    if code == 0 and lines:
        return Path(lines[-1].strip()).resolve()
    if env.get("JUPYTER_RUNTIME_DIR"):
        return Path(env["JUPYTER_RUNTIME_DIR"])
    base = "~/Library/Jupyter" if sys.platform == "darwin" else "~/.local/share/jupyter"
    return Path(os.path.expanduser(base)) / "runtime"


def read_server(runtime_file: str | Path) -> dict | None:
    info = paths.read_json(Path(runtime_file), None)
    return info if isinstance(info, dict) and info.get("url") else None


def current_lab(layout: paths.Layout) -> tuple[dict, dict | None]:
    """(lab.json, live server info or None)."""
    lab = paths.read_json(layout.lab_json, None)
    if not isinstance(lab, dict):
        return {}, None
    server = read_server(lab.get("runtime_file") or "")
    if (
        server is None
        or not common.pid_alive(lab.get("pid"))
        or server.get("pid") != lab.get("pid")
    ):
        return lab, None
    return lab, server


def runtime_dirs(extra: Path | None = None) -> list[Path]:
    """Where jupyter servers write ``jpserver-<pid>.json``: the dirs the gateway scans,
    plus ``extra`` (the project env's own runtime dir), without duplicates."""
    env = os.environ
    candidates = [str(extra or ""), env.get("JUPYTER_RUNTIME_DIR", "")]
    candidates += ["~/Library/Jupyter/runtime", "~/Library/Application Support/Jupyter/runtime"]
    data_home = env.get("XDG_DATA_HOME") or "~/.local/share"
    candidates.append(os.path.join(data_home, "jupyter", "runtime"))
    seen, out = set(), []
    for raw in candidates:
        if not raw:
            continue
        path = Path(os.path.expanduser(raw))
        key = os.path.realpath(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            out.append(path)
    return out


def server_files(dirs: list[Path]) -> list[tuple[Path, dict]]:
    """Every parseable ``jpserver-<pid>.json`` whose pid is alive. Read-only."""
    found, pids = [], set()
    for folder in dirs:
        try:
            files = sorted(folder.glob("jpserver-*.json"))
        except OSError:
            continue
        for info_file in files:
            if not SERVER_FILE.match(info_file.name):
                continue
            info = read_server(info_file)
            if info is None or not common.pid_alive(info.get("pid")) or info["pid"] in pids:
                continue
            pids.add(info["pid"])
            found.append((info_file, info))
    return found


def server_root(info: dict) -> Path | None:
    root = info.get("root_dir") or info.get("notebook_dir")
    return Path(os.path.realpath(root)) if isinstance(root, str) and root else None


def project_servers(project: Path, dirs: list[Path]) -> list[tuple[Path, dict]]:
    """Live jupyter_server >= 2 servers whose root contains ``project``, in the gateway's
    order: the project itself as root first, then the deepest root, then the newest."""
    project = Path(os.path.realpath(project))
    ranked = []
    for info_file, info in server_files(dirs):
        root = server_root(info)
        if root is None or not core.is_within(project, root):
            continue
        version = info.get("version")
        if version and common.version_tuple(str(version)) < (2,):
            continue  # doctor reports these (D149); nh never uses them
        try:
            mtime = info_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        ranked.append(((root == project, len(root.parts), mtime), info_file, info))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(info_file, info) for _, info_file, info in ranked]


def unusable_reason(server: dict) -> str | None:
    """None when nh can use ``server``: it answers with its token and has real-time
    collaboration (jupyter-collaboration answers a GET on its session route with 405)."""
    try:
        api(server, "GET", "api/status", timeout=PROBE_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        return f"answered {exc.code}"
    except (OSError, ValueError):
        return "doesn't answer"
    try:
        api(server, "GET", "api/collaboration/session/nh-probe.ipynb", timeout=PROBE_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        return None if exc.code == 405 else "has no real-time collaboration"
    except (OSError, ValueError):
        return "doesn't answer"
    return "has no real-time collaboration"


def find_project_server(project: Path, dirs: list[Path]) -> tuple[Path, dict, list[str]]:
    """The best live server nh can use for ``project`` -> (its jpserver file, its info, why
    other candidates were passed over). The file is ``Path()`` and info ``{}`` when none."""
    skipped = []
    for info_file, info in project_servers(project, dirs):
        reason = unusable_reason(info)
        if reason is None:
            return info_file, info, skipped
        skipped.append(
            f"A JupyterLab at {public_url(info)} (pid {info.get('pid')}) serves this folder but "
            f"{reason}, so nh doesn't use it. Close its browser tabs so the notebook is edited "
            "in one place only."
        )
    return Path(), {}, skipped


def api_path(server: dict, path: Path, fallback: str) -> str:
    """``path`` as the server's API sees it (relative to its root_dir)."""
    root = server_root(server)
    if root is None:
        return fallback
    try:
        return Path(os.path.realpath(path)).relative_to(root).as_posix()
    except ValueError:
        return fallback


def notebook_kernel(path: Path) -> str:
    nb = paths.read_json(path, {})
    spec = (nb.get("metadata") or {}).get("kernelspec") if isinstance(nb, dict) else None
    name = spec.get("name") if isinstance(spec, dict) else None
    return name if isinstance(name, str) and name else "python3"


def open_browser(open_file: Path) -> bool:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [opener, str(open_file)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def public_url(server: dict) -> str:
    """The server URL without any query string, so a token can never ride along."""
    return urllib.parse.urlsplit(str(server.get("url", "")))._replace(query="").geturl()


# ------------------------------------------------------------------------ commands


def cmd_start(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    prefix = common.env_prefix(layout)
    jupyter = prefix / "bin" / "jupyter"
    nb_path = common.resolve_notebook(project, args.notebook)
    notebook = common.rel(project, nb_path)
    warnings: list[str] = []
    too_old = envsync.env_problem(common.read_env_json(layout))
    if too_old:
        warnings.append(f"{too_old} nh can't write cells until the env is upgraded.")
    if not nb_path.is_file():
        warnings.append(f"{notebook} doesn't exist yet; JupyterLab opens without it.")

    lab, server = current_lab(layout)
    status = "running"
    if server is None:
        env = common.env_with(prefix, drop=TOKEN_VARS, JUPYTER_PREFER_ENV_PATH="1")
        rt_dir = runtime_dir(prefix, env)
        info_file, server, skipped = find_project_server(project, runtime_dirs(rt_dir))
        warnings.extend(skipped)
        if server:
            status = "adopted"
            lab = _lab_record(server["pid"], server, info_file, None, notebook, adopted=True)
        else:
            if not jupyter.exists():
                raise NhctlError(
                    "D144", f"JupyterLab isn't installed in {prefix}.", "Run nhctl env sync first."
                )
            status = "started"
            cmd = [
                str(jupyter), "lab", "--no-browser", "--ip=127.0.0.1",
                f"--ServerApp.root_dir={project}",
                f"--SQLiteYStore.db_path={layout.nh / 'jupyter_ystore.db'}",
            ]  # fmt: skip
            if nb_path.is_file():
                cmd.append(f"--LabApp.default_url=/lab/tree/{urllib.parse.quote(notebook)}")
            server, pid = _launch(cmd, project, env, layout, rt_dir, args.timeout)
            info_file = rt_dir / f"jpserver-{pid}.json"
            lab = _lab_record(pid, server, info_file, prefix, notebook)
        paths.atomic_write_json(layout.lab_json, lab)

    session: dict = {}
    if nb_path.is_file():
        configured = common.configured_kernel(project)
        kernel = configured or notebook_kernel(nb_path)
        asked_by = "harness.toml [jupyter].kernel_name" if configured else "The notebook"
        try:
            session = ensure_session(server, api_path(server, nb_path, notebook), kernel)
            warning = kernelspec_warning(server, kernel, prefix, asked_by)
            if warning:
                warnings.append(warning)
            running = session.get("kernel")
            if configured and not session.get("created") and running and running != configured:
                warnings.append(
                    f"The notebook's kernel is already running \"{running}\", not harness.toml's "
                    f'"{configured}". Switch it in JupyterLab: Kernel -> Change Kernel.'
                )
        except Exception as exc:  # best effort: the lab is up either way
            warnings.append(common.scrub(f"Couldn't prepare the notebook's kernel: {exc}"))

    opened = False
    if not args.no_browser:
        open_file = Path(lab["runtime_file"]).with_name(f"jpserver-{lab['pid']}-open.html")
        opened = open_file.is_file() and open_browser(open_file)
        if not opened:
            warnings.append(
                f"Open {public_url(server)} in your browser (the token is in {open_file})."
            )

    data = {
        "ok": True,
        "status": status,
        "url": public_url(server),
        "pid": lab["pid"],
        "notebook": notebook,
        "session": session,
        "browser_opened": opened,
        "log": None if lab.get("adopted") else f".nh/logs/{LOG_FILE}",
        "warnings": warnings,
    }
    verb = {
        "running": "already running",
        "adopted": "already running (started outside nh; nh uses it)",
        "started": "started",
    }[status]
    text = f"JupyterLab {verb} at {data['url']} (pid {lab['pid']}); notebook {notebook}"
    text += "".join(f"\nwarning: {w}" for w in warnings)
    return Result(data, text)


def _lab_record(
    pid: int,
    server: dict,
    info_file: Path,
    prefix: Path | None,
    notebook: str,
    adopted: bool = False,
) -> dict:
    """lab.json: never the token. ``env_prefix`` is None for a server nh didn't start."""
    record = {
        "pid": pid,
        "url": public_url(server),
        "runtime_file": str(info_file),
        "env_prefix": str(prefix) if prefix is not None else None,
        "notebook": notebook,
        "started_at": time.time(),
    }
    if adopted:
        record["adopted"] = True
    return record


def _launch(
    cmd: list[str], project: Path, env: dict, layout: paths.Layout, rt_dir: Path, timeout: float
) -> tuple[dict, int]:
    """Start jupyter detached, with a token nhctl generates. The token reaches jupyter
    through ``JUPYTER_TOKEN_FILE`` (a 0600 temp file, deleted once the server is up):
    not on argv, not in the log (jupyter prints only tokens it generated itself) and not
    in the environment the kernels inherit."""
    layout.logs.mkdir(parents=True, exist_ok=True)
    log_path = layout.logs / LOG_FILE
    log_fd = _open_log(log_path)
    token_fd, token_file = tempfile.mkstemp(prefix="nh-lab-", suffix=".token")
    try:
        with os.fdopen(token_fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(24))
        with os.fdopen(log_fd, "ab") as log:
            log.write(f"\n== nhctl lab start {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                cmd, cwd=str(project), env=dict(env, JUPYTER_TOKEN_FILE=token_file),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )  # fmt: skip
        return _wait_for_server(proc, rt_dir / f"jpserver-{proc.pid}.json", log_path, timeout)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(token_file)


def _open_log(path: Path) -> int:
    """The JupyterLab log, owner-only (it may hold jupyter's startup messages); a log from an
    older nhctl that still holds a ``token=`` is scrubbed first."""
    try:
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        old = ""
    clean = common.scrub(old)
    if clean != old:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(clean)
        os.replace(tmp, path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _wait_for_server(
    proc: subprocess.Popen, runtime_file: Path, log_path: Path, timeout: float
) -> tuple[dict, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server = read_server(runtime_file)
        if server is not None:
            return server, proc.pid
        if proc.poll() is not None:
            raise NhctlError(
                "D145",
                f"JupyterLab exited during startup (exit {proc.returncode}).",
                f"See .nh/logs/{LOG_FILE}.",
                data={"log_tail": common.tail(log_path)},
            )
        time.sleep(0.2)
    _terminate(proc.pid)
    raise NhctlError(
        "D146",
        f"JupyterLab didn't start within {timeout:.0f}s.",
        f"See .nh/logs/{LOG_FILE}, then retry nhctl lab start.",
        data={"log_tail": common.tail(log_path)},
    )


def _terminate(pid: int) -> bool:
    """SIGTERM (jupyter shuts its kernels down), then SIGKILL after STOP_TIMEOUT_S."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        if _reap(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(0.2)
    return _reap(pid)


def _reap(pid: int) -> bool:
    """True once ``pid`` is gone (collecting it if it is our own child)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except ChildProcessError:
        pass
    return not common.pid_alive(pid)


def cmd_status(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    lab, server = current_lab(layout)
    if server is None:
        _, found, _ = find_project_server(project, runtime_dirs())
        if found:
            # Not in lab.json, but the gateway finds and uses it all the same.
            data = {
                "ok": True,
                "running": True,
                "reachable": True,
                "registered": False,
                "url": public_url(found),
                "pid": found.get("pid"),
                "notebook": None,
                "env_prefix": None,
                "version": found.get("version"),
            }
            text = (
                f"JupyterLab: running at {data['url']} (pid {data['pid']}), started outside nh; "
                "nh uses it (nhctl lab start records it)"
            )
            return Result(data, text)
        data = {"ok": True, "running": False, "stale_lab_json": bool(lab)}
        return Result(data, "JupyterLab: not running (start it with nhctl lab start)")
    reachable = True
    try:
        api(server, "GET", "api/status", timeout=5)
    except (OSError, ValueError, urllib.error.URLError):
        reachable = False
    data = {
        "ok": True,
        "running": True,
        "reachable": reachable,
        "registered": True,
        "url": public_url(server),
        "pid": lab.get("pid"),
        "notebook": lab.get("notebook"),
        "env_prefix": lab.get("env_prefix"),
        "version": server.get("version"),
    }
    state = "running" if reachable else "running but not answering"
    return Result(data, f"JupyterLab: {state} at {data['url']} (pid {data['pid']})")


def cmd_stop(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    lab, server = current_lab(layout)
    if server is None:
        layout.lab_json.unlink(missing_ok=True)
        return Result({"ok": True, "stopped": False}, "JupyterLab: not running")
    pid = int(lab["pid"])
    root = server_root(server)
    if lab.get("adopted") and root is not None and root != Path(os.path.realpath(project)):
        # A server the user started on a parent folder serves more than this project.
        data = {"ok": True, "stopped": False, "pid": pid, "adopted": True, "root_dir": str(root)}
        text = (
            f"JupyterLab (pid {pid}) was started outside nh and serves {root}, not just this "
            "project, so nhctl leaves it running. Stop it where it was started."
        )
        return Result(data, text)
    stopped = _terminate(pid)
    if not stopped:
        raise NhctlError("D147", f"JupyterLab (pid {pid}) didn't stop.", f"Run: kill -9 {pid}")
    layout.lab_json.unlink(missing_ok=True)
    return Result({"ok": True, "stopped": True, "pid": pid}, f"JupyterLab stopped (pid {pid})")
