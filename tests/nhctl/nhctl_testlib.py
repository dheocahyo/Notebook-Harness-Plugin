"""Shared helpers for the nhctl tests (imported by conftest.py and the test modules).

Every CLI test runs the real ``bin/nhctl`` in a subprocess with a hermetic environment:
PATH = a per-test fake bin dir + /usr/bin and /bin without uv, conda or mamba, HOME inside tmp,
nh's absolute fallback dirs moved into tmp (``NH_FALLBACK_ROOT``), no conda/venv vars.
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "nh"
NHCTL = PLUGIN / "bin" / "nhctl"
SERVER = PLUGIN / "server"
VENV_PYTHON = SERVER / ".venv" / "bin" / "python"
SYSTEM_PYTHON = "/usr/bin/python3"
# The tools a test fakes or removes. A runner's own copies must stay out of sight: GitHub's
# Ubuntu image has conda in /usr/bin.
HIDDEN_TOOLS = frozenset({"uv", "uvx", "conda", "mamba", "micromamba"})


@functools.cache
def system_bin() -> str:
    """/usr/bin and /bin as one dir of symlinks, without HIDDEN_TOOLS."""
    farm = tempfile.mkdtemp(prefix="nh-sysbin-")
    atexit.register(shutil.rmtree, farm, ignore_errors=True)
    for folder in ("/usr/bin", "/bin"):
        for entry in os.scandir(folder):
            link = os.path.join(farm, entry.name)
            if entry.name not in HIDDEN_TOOLS and not os.path.lexists(link):
                os.symlink(entry.path, link)
    return farm


def _system_python_ok() -> bool:
    if not os.path.exists(SYSTEM_PYTHON):
        return False
    out = subprocess.run(
        [SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info >= (3, 9))"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() == "True"


PYTHONS = [
    pytest.param(
        SYSTEM_PYTHON,
        id="py39",
        marks=pytest.mark.skipif(not _system_python_ok(), reason="no system python3 >= 3.9"),
    ),
    pytest.param(sys.executable, id="py3"),
]


class Env:
    """A hermetic environment for one test, with a fake bin dir first on PATH."""

    def __init__(self, tmp: Path, python: str) -> None:
        self.tmp = tmp
        self.python = python
        self.bin = tmp / "fakebin"
        self.bin.mkdir()
        self.home = tmp / "home"
        self.home.mkdir()
        self.runtime = tmp / "jupyter-runtime"
        self.runtime.mkdir()
        self.vars = {
            "PATH": f"{self.bin}:{system_bin()}",
            "HOME": str(self.home),
            "NH_FALLBACK_ROOT": str(tmp / "root"),
            "NH_PYTHON": python,
            "JUPYTER_RUNTIME_DIR": str(self.runtime),
            "JUPYTER_DATA_DIR": str(tmp / "jupyter-data"),
            "JUPYTER_CONFIG_DIR": str(tmp / "jupyter-config"),
            "LANG": "en_US.UTF-8",
        }

    def script(self, name: str, body: str, python: bool = False) -> Path:
        """Write an executable fake tool into the fake bin dir (sh, or the test's Python)."""
        path = self.bin / name
        shebang = f"#!{sys.executable}" if python else "#!/bin/sh"
        path.write_text(shebang + "\n" + textwrap.dedent(body).lstrip("\n"))
        path.chmod(0o755)
        return path

    def run(
        self, *args: str, cwd: Path, extra: dict | None = None, nhctl: Path = NHCTL, timeout=120
    ):
        env = dict(self.vars, **(extra or {}))
        return subprocess.run(
            ["/bin/sh", str(nhctl), *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def json(self, *args: str, cwd: Path, expect: int | None = 0, **kw) -> dict:
        proc = self.run(*args, "--json", cwd=cwd, **kw)
        assert proc.stdout.count("\n") == 1, (proc.stdout, proc.stderr)
        data = json.loads(proc.stdout)
        if expect is not None:
            assert proc.returncode == expect, (proc.returncode, data, proc.stderr)
        return data


ENV_VERSIONS = {"python": "3.13.8", "jupyterlab": "4.6.4", "jupyter_collaboration": "5.0.4"}


def env_builder(tool: str, prefix: str, versions: dict | None, exit_code: int, delay: float) -> str:
    """Body of a fake uv/conda/mamba: log argv and env, then build <prefix>/bin/python,
    a stand-in that prints the versions nhctl's importlib.metadata probe asks for."""
    report = json.dumps(versions or ENV_VERSIONS)
    return f"""
        if [ "$1" = "--version" ]; then echo "{tool} 0.10.6 (fake)"; exit 0; fi
        echo "{tool}-args: $*" >> "$PWD/{tool}-calls.log"
        echo "VIRTUAL_ENV=${{VIRTUAL_ENV:-unset}} UV_PROJECT_ENVIRONMENT=${{UV_PROJECT_ENVIRONMENT:-unset}} CONDA_ALWAYS_YES=${{CONDA_ALWAYS_YES:-unset}}" >> "$PWD/{tool}-calls.log"
        sleep {delay}
        if [ {exit_code} -ne 0 ]; then echo "error: resolution failed (fake)" >&2; exit {exit_code}; fi
        mkdir -p {prefix}/bin {prefix}/conda-meta
        cat > {prefix}/bin/python <<'PY'
        #!/bin/sh
        echo '{report}'
        PY
        chmod +x {prefix}/bin/python
        echo "Installed 42 packages (fake)"
        """


def fake_uv(env: Env, versions: dict | None = None, exit_code: int = 0, delay: float = 0) -> Path:
    return env.script("uv", env_builder("uv", ".venv", versions, exit_code, delay))


def fake_conda(env: Env, tmp: Path, version: str = "23.7.4", libmamba: bool = True) -> Path:
    """A conda install at <tmp>/miniconda (so nhctl can inspect its conda-meta), on PATH."""
    base = tmp / "miniconda"
    (base / "bin").mkdir(parents=True)
    (base / "conda-meta").mkdir()
    if libmamba:
        (base / "conda-meta" / "conda-libmamba-solver-23.5.0-py311_0.json").write_text("{}")
    body = env_builder("conda", ".conda", None, 0, 0).replace("0.10.6 (fake)", version)
    conda = base / "bin" / "conda"
    conda.write_text("#!/bin/sh\n" + textwrap.dedent(body).lstrip("\n"))
    conda.chmod(0o755)
    (env.bin / "conda").symlink_to(conda)
    return conda


def plugin_copy(dest: Path) -> Path:
    """A copy of the plugin's nhctl with server/ symlinked, so libexec/ can be controlled."""
    root = dest / "nh"
    (root / "bin").mkdir(parents=True)
    shutil.copy2(NHCTL, root / "bin" / "nhctl")
    shutil.copytree(
        PLUGIN / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    (root / "server").symlink_to(SERVER)
    (root / "libexec").mkdir()
    return root
