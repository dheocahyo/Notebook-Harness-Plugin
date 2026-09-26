"""nhctl fresh-run: does the notebook run top to bottom in a fresh kernel? (FR-11 seam)

A copy (``.nh/tmp/fresh-<ts>.ipynb``) runs under the project env's nbclient, with
relative paths resolved against the original notebook's folder. The original is only
ever read. Each run is logged as a ``fresh_run`` event in ``.nh/log.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import common
from common import NhctlError, Result

from nh_gateway._shared import paths, text
from nh_gateway._shared.scaffold import core

KEEP_COPIES = 5
MARKER = "NH-FRESH-RUN "

# Runs inside the project env: argv = copy, working dir, per-cell timeout, kernel name
# (harness.toml [jupyter].kernel_name; "" = the notebook's kernelspec).
_RUNNER = r"""
import json, sys
import nbformat
from jupyter_client.kernelspec import NoSuchKernel
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError

copy, cwd, cell_timeout, configured = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
nb = nbformat.read(copy, as_version=4)
kernel = configured or (nb.metadata.get("kernelspec") or {}).get("name") or "python3"
state = {"index": None}
result = {"ok": False, "kernel": kernel}

def run(name):
    client = NotebookClient(nb, timeout=cell_timeout, kernel_name=name, allow_errors=False,
                            resources={"metadata": {"path": cwd}})
    client.on_cell_start = lambda cell, cell_index: state.update(index=cell_index)
    client.execute()

try:
    try:
        run(kernel)
    except NoSuchKernel:
        if kernel == "python3":
            raise
        result["kernel"] = "python3"
        run("python3")
    result["ok"] = True
except CellExecutionError as exc:
    result.update(ename=exc.ename, evalue=str(exc.evalue))
except (CellTimeoutError, DeadKernelError, NoSuchKernel) as exc:
    result.update(ename=type(exc).__name__, evalue=str(exc))
if not result["ok"]:
    result["failing_index"] = state["index"]
    result["evalue"] = result.get("evalue", "")[:500]
result["executed"] = sum(1 for c in nb.cells if c.cell_type == "code" and c.get("execution_count"))
nbformat.write(nb, copy)
print(__MARKER__ + json.dumps(result))
""".replace("__MARKER__", repr(MARKER))


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    fresh = sub.add_parser(
        "fresh-run", parents=[common_opts], help="run a copy of a notebook in a fresh kernel"
    )
    fresh.add_argument("notebook", nargs="?", help="default: harness.toml's notebook")
    fresh.add_argument("--cell-timeout", type=int, default=600, help="seconds per cell")
    fresh.add_argument("--timeout", type=float, default=1800, help="seconds for the whole run")
    fresh.set_defaults(func=cmd_fresh_run)


def _nh(cell: object) -> dict:
    meta = cell.get("metadata") if isinstance(cell, dict) else None
    value = meta.get("nh") if isinstance(meta, dict) else None
    return value if isinstance(value, dict) else {}


def cell_label(cells: list, index: int) -> str:
    """'"<note title>" [n]', falling back to the first code line."""
    cell = cells[index]
    uid = _nh(cell).get("uid")
    title = ""
    above = cells[index - 1] if index > 0 else {}
    note = _nh(above)
    is_note = above.get("cell_type") == "markdown" and note.get("role") == "note"
    if is_note and note.get("pair_uid") in (None, uid):
        first = core.cell_source(above).strip().splitlines()
        title = text.normalize_title(first[0]) if first else ""
    if not title:
        lines = [ln.strip() for ln in core.cell_source(cell).splitlines() if ln.strip()]
        title = lines[0] if lines else "an empty cell"
        title = title if len(title) <= 40 else title[:39] + "…"
    count = cell.get("execution_count")
    return f'"{title}"' + (f" [{count}]" if isinstance(count, int) else "")


def _prune(tmp: Path) -> None:
    copies = sorted(tmp.glob("fresh-*.ipynb"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[KEEP_COPIES:]:
        old.unlink(missing_ok=True)


def _run_isolated(cmd: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
    """Run in its own process group, so a timeout also stops what the runner started."""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )  # fmt: skip
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        proc.communicate()
        raise NhctlError(
            "D152",
            f"The fresh run took longer than {timeout:.0f}s and was stopped.",
            "Rerun with a larger --timeout, or check for a cell that waits forever.",
        ) from None
    return proc.returncode, out.decode("utf-8", "replace")


def cmd_fresh_run(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    layout = paths.Layout(project)
    original = common.resolve_notebook(project, args.notebook)
    shown = common.rel(project, original)
    if original.suffix != ".ipynb" or not original.is_file():
        raise NhctlError("D150", f"No notebook at {shown}.", "Pass the notebook's path.")
    nb = paths.read_json(original, None)
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise NhctlError("D150", f"{shown} is not a readable notebook.")
    prefix = common.env_prefix(layout)

    layout.tmp.mkdir(parents=True, exist_ok=True)
    copy = layout.tmp / f"fresh-{common.now_stamp()}-{os.getpid()}.ipynb"
    shutil.copyfile(original, copy)
    env = common.env_with(prefix, JUPYTER_PREFER_ENV_PATH="1")
    cmd = [str(prefix / "bin" / "python"), "-I", "-c", _RUNNER]
    cmd += [str(copy), str(original.parent), str(max(1, args.cell_timeout))]
    cmd.append(common.configured_kernel(project))
    started = time.monotonic()
    code, out = _run_isolated(cmd, original.parent, env, args.timeout)
    ms = int((time.monotonic() - started) * 1000)
    _prune(layout.tmp)

    lines = [ln for ln in out.splitlines() if ln.startswith(MARKER)]
    if not lines:
        tail = common.scrub("\n".join(out.strip().splitlines()[-15:]))
        fix = "Check that the project env has jupyterlab (it brings nbclient): nhctl env sync."
        if "No module named" in out:
            fix = "The project env lacks nbclient/ipykernel; run nhctl env sync."
        raise NhctlError(
            "D151", f"The fresh run didn't finish (exit {code}).", fix, data={"output_tail": tail}
        )
    result = json.loads(lines[-1][len(MARKER) :])
    cells = nb["cells"]
    code_cells = sum(1 for c in cells if isinstance(c, dict) and c.get("cell_type") == "code")
    failing = None
    if not result.get("ok"):
        index = result.get("failing_index")
        failing = {"index": index, "ename": result.get("ename"), "evalue": result.get("evalue")}
        if isinstance(index, int) and 0 <= index < len(cells):
            failing["label"] = cell_label(cells, index)
            failing["uid"] = _nh(cells[index]).get("uid")
    common.emit_event(
        layout, "fresh_run", nb=shown, ok=bool(result.get("ok")),
        failing_cell_uid=(failing or {}).get("uid"), n_cells=code_cells, ms=ms, via="nbclient",
    )  # fmt: skip
    data = {
        "ok": bool(result.get("ok")),
        "notebook": shown,
        "copy": common.rel(project, copy),
        "kernel": result.get("kernel"),
        "code_cells": code_cells,
        "executed": result.get("executed"),
        "ms": ms,
        "failing": failing,
    }
    if data["ok"]:
        return Result(data, f"Fresh run OK: {shown} ran top to bottom ({code_cells} code cells).")
    assert failing is not None
    where = failing.get("label") or "kernel startup"
    reason = f"{failing['ename']}: {failing['evalue']}".rstrip(": ")
    return Result(data, f"Fresh run FAILED in {shown} at {where}: {reason}", 1)
