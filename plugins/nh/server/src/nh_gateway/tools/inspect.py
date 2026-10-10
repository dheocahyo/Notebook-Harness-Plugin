"""nh_inspect: a read-only view of the live notebook and kernel (FR-4). It never writes."""

from __future__ import annotations

import sys
import time
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult

from .. import meta
from .._shared import secrets
from .._shared.paths import read_json
from .._shared.text import unescape_markdown
from ..backend.base import CellView, NotebookRef
from ..exec import shaping
from ..policy.errors import NhError
from .common import (
    Services,
    check_drift,
    config_lines,
    drift_lead,
    drift_names,
    kernel_notes,
    label,
    look_at_kernel,
    normalize_uids,
    note_for,
    prune_drift,
    safe_probe,
    text_result,
    title_of,
    uid_of,
    vars_args,
)

VIEWS = ("status", "overview", "outline", "vars", "var", "cell", "intents")


def _clip(text: str, limit: int) -> str:
    """A view's text, redacted whole and then cut (design §6.8): the cell view's source, notes
    and outputs, the var views, outline rows and the status lines."""
    text = secrets.current().redact(text)
    if len(text) <= limit:
        return text
    return (
        text[: limit - 60].rstrip() + f"\n… [{len(text) - limit + 60} more chars; narrow the view]"
    )


def _first_line(source: str, width: int = 80) -> str:
    for line in source.splitlines():
        if line.strip():
            line = secrets.current().redact(line.strip())  # before the cut
            return line if len(line) <= width else line[: width - 1] + "…"
    return ""


def _author(cell: CellView) -> str:
    nh = meta.nh_meta(cell.metadata)
    if nh.get("role") != "code":
        return "human"
    if nh.get("created_by") == "human":
        return "human+nh"
    sha = nh.get("source_sha")
    return "agent*" if sha and sha != meta.source_sha(cell.source) else "agent"


def _status(cell: CellView, stale: dict[str, dict]) -> str:
    if cell.running:
        return "RUN"
    info = stale.get(uid_of(cell))
    if info is not None and cell.execution_count == info.get("exec_count"):
        return "STALE"  # not re-run since it was marked (a cleared count stays None)
    if cell.summary and cell.summary.error:
        return "ERR " + cell.summary.error.split(":", 1)[0]
    if cell.execution_count is None:
        return "-"
    return "ok"


def _visible_indexes(total: int, limit: int, center: int | None) -> list[int]:
    if total <= limit:
        return list(range(total))
    if center is not None:
        lo = max(0, min(center - limit // 2, total - limit))
        return list(range(lo, lo + limit))
    return list(range(5)) + list(range(total - (limit - 5), total))


def _outline(
    cells: list[CellView], stale: dict[str, dict], limit: int, around: str | None
) -> list[str]:
    center = None
    if around:
        center = next((c.index for c in cells if c.id == around or uid_of(c) == around), None)
    rows: list[str] = []
    previous = -1
    for index in _visible_indexes(len(cells), limit, center):
        if index - previous > 1:
            rows.append(
                f"… {index - previous - 1} cells not shown (pass cell_id to center the outline)"
            )
        previous = index
        rows.append(_outline_row(cells, cells[index], stale))
    if cells and previous < len(cells) - 1:
        rows.append(f"… {len(cells) - 1 - previous} later cells not shown")
    return rows


def _outline_row(cells: list[CellView], cell: CellView, stale: dict[str, dict]) -> str:
    if cell.cell_type != "code":
        kind = "note" if meta.is_note(cell.metadata) else "md"
        detail = _first_line(cell.source)
        if kind == "note":  # as the user reads it in JupyterLab: '$', not '\$'
            detail = unescape_markdown(detail)
        if kind == "note":
            pair = meta.nh_meta(cell.metadata).get("pair_uid")
            nxt = cells[cell.index + 1] if cell.index + 1 < len(cells) else None
            if nxt is None or uid_of(nxt) != pair:
                detail += "  (!) note is no longer above its cell"
        return f"{cell.index:>3} {cell.id:<16} {kind:<4}        {detail}"
    count = f"[{cell.execution_count}]" if cell.execution_count is not None else "[ ]"
    title = title_of(cells, cell) or _first_line(cell.source)
    out = ""
    if cell.summary and cell.summary.count:
        parts = []
        if cell.summary.error:
            parts.append(cell.summary.error)
        elif cell.summary.text_head:
            parts.append(cell.summary.text_head[:60].replace("\n", " "))
        if cell.summary.images:
            parts.append(f"{cell.summary.images} image(s)")
        if parts:
            out = "  → " + "; ".join(parts)
    return (
        f"{cell.index:>3} {cell.id:<16} code {count:<6} {_status(cell, stale):<10} "
        f"{_author(cell):<8} {title}{out}"
    )


def _var_line(name: str, info: dict[str, Any]) -> str:
    kind = info.get("kind")
    if kind == "DataFrame":
        shape = info.get("shape") or ["?", "?"]
        nulls = info.get("nulls") or {}
        null_text = (
            ", nulls " + ", ".join(f"{k} {v}" for k, v in list(nulls.items())[:5]) if nulls else ""
        )
        cols = info.get("columns") or []
        col_text = ", ".join(cols[:12]) + (" …" if len(cols) > 12 else "")
        return f"{name}: {info.get('lib', '')} DataFrame {shape[0]}x{shape[1]}{null_text}; columns: {col_text}"
    if kind == "Series":
        return (
            f"{name}: Series len {info.get('len')} {info.get('dtype')}, nulls {info.get('nulls')}"
        )
    if kind == "ndarray":
        return f"{name}: ndarray {tuple(info.get('shape') or ())} {info.get('dtype')}"
    if kind == "scalar":
        return f"{name}: {info.get('type')} = {info.get('repr')}"
    if kind == "container":
        return f"{name}: {info.get('type')} len {info.get('len')}"
    return f"{name}: {info.get('type', kind)}"


def _vars_block(payload: dict[str, Any] | None) -> list[str]:
    if payload is None:
        return ["(variables unavailable: kernel busy or not attached)"]
    lines = [_var_line(name, info) for name, info in (payload.get("vars") or {}).items()]
    if payload.get("truncated"):
        lines.append("… more variables not shown")
    packages = payload.get("packages") or {}
    if packages:
        lines.append(
            "installed: "
            + ", ".join(
                f"{name} {version}" if version else f"{name} (not imported)"
                for name, version in packages.items()
            )
        )
    return lines or ["(no user variables yet)"]


def _young(folder, seconds: float = 600.0) -> bool:
    try:
        return time.time() - folder.stat().st_mtime < seconds
    except OSError:
        return False


async def _status_lines(svc: Services, ref: NotebookRef | None) -> list[str]:
    lines = [
        f"project: {svc.project}",
        f"gateway: python {sys.version.split()[0]} ({sys.executable})",
    ]
    describe = getattr(svc.backend, "describe", None)
    if describe is not None:
        try:
            for key, value in (await describe(ref)).items():
                lines.append(f"{key}: {value}")
        except NhError as exc:
            lines.append(str(exc).splitlines()[0])
    env = read_json(svc.layout.env_json)
    if isinstance(env, dict):
        lines.append("project env: " + ", ".join(f"{k}={v}" for k, v in env.items()))
    else:
        lines.append("project env: unknown (run `nhctl env sync`)")
    stamps = (
        sorted(svc.layout.stamps.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if svc.layout.stamps.is_dir()
        else []
    )
    turns = (
        sorted(svc.layout.turns.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if svc.layout.turns.is_dir()
        else []
    )
    if turns:
        age = time.time() - turns[-1].stat().st_mtime
        lines.append(
            f"hooks: last turn record {age:.0f}s ago"
            + (f", {len(stamps)} pending stamps" if stamps else "")
        )
    elif _young(svc.layout.nh):
        lines.append("hooks: no turn record yet (expected in the message that ran /nh:init)")
    else:
        lines.append(
            "hooks: no turn records yet (hooks may not be running; needs Claude Code >= 2.1.196)"
        )
    if svc.runs:
        lines.append("running: " + ", ".join(sorted(svc.runs)))
    return lines


async def inspect_notebook(
    svc: Services,
    ctx: Context | None,
    *,
    view: str = "overview",
    name: str | None = None,
    cell_id: str | None = None,
    rows: int | None = None,
    notebook: str | None = None,
) -> ToolResult:
    cfg = svc.config()
    limit = int(cfg["inspect"]["max_chars"])
    if view not in VIEWS:
        raise NhError("E120", detail=f"- view must be one of {', '.join(VIEWS)} (got {view!r}).")
    wanted = int(cfg["inspect"]["head_rows"]) if rows is None else int(rows)
    rows = max(0, min(wanted, int(cfg["inspect"]["max_rows"])))

    if view == "status":
        try:
            ref = await svc.resolve(notebook, activate=False)
        except NhError as exc:
            lines = await _status_lines(svc, None)
            lines.append(str(exc).splitlines()[0])
            return text_result(
                _clip("\n".join(["nh status"] + lines + config_lines(cfg, svc.layout)), limit)
            )
        lines = await _status_lines(svc, ref)
        return text_result(
            _clip(
                "\n".join(
                    [f"nh status — notebook {ref.rel_path}"] + lines + config_lines(cfg, svc.layout)
                ),
                limit,
            )
        )

    ref = await svc.resolve(notebook, activate=False)
    # Every view looks at the kernel: a restart found here clears 'Kernel ≠ notebook' and is
    # news the agent must see ("NEW kernel"), whichever view noticed it first.
    kernel = await look_at_kernel(svc, ref)
    lead = kernel_notes(svc, ref, kernel)[0] if kernel is not None else []
    payload = None
    if view in ("vars", "overview"):
        payload = await safe_probe(svc, ref, "vars", vars_args(cfg, None))
    complete = payload is not None and not payload.get("truncated")
    if complete:
        prune_drift(svc, ref, payload, drift_names(svc, ref))
    idle = kernel is not None and kernel.execution_state != "busy"
    await check_drift(svc, ref, probe=idle and not complete)
    header = lead + drift_lead(svc, ref)

    if view == "var":
        if not name:
            raise NhError("E120", detail='- view="var" needs name=<variable>.')
        payload = await safe_probe(svc, ref, "var", {"name": name, "rows": rows})
        if payload is None:
            return text_result(
                "\n".join(
                    header
                    + [f"`{name}`: unavailable (kernel busy, not attached, or no such variable)."]
                )
            )
        body = [_var_line(name, payload.get("summary") or {})]
        if payload.get("head"):
            body += ["--- head ---", payload["head"]]
        if payload.get("text"):
            body += ["--- value ---", payload["text"]]
        return text_result(_clip("\n".join(header + body), limit))

    if view == "vars":
        return text_result(_clip("\n".join(header + _vars_block(payload)), limit))

    cells = await svc.backend.snapshot(ref, outputs="full" if view == "cell" else "summary")
    normalize_uids(cells)
    stale = svc.stale.get(ref.rel_path)

    if view == "cell":
        if not cell_id:
            raise NhError("E120", detail='- view="cell" needs cell_id.')
        cell = next((c for c in cells if c.id == cell_id or uid_of(c) == cell_id), None)
        if cell is None:
            raise NhError("E140", cell_id=cell_id)
        nh = meta.nh_meta(cell.metadata)
        body = [
            f"{label(cells, cell)}  id={cell.id}  sha={meta.source_sha(cell.source)}  author={_author(cell)}"
        ]
        if nh.get("intent"):
            body.append(f"intent: {nh['intent']}")
        for bullet in nh.get("rationale") or []:
            body.append(f"- {bullet}")
        body += ["--- source ---"] + [f"{line}" for line in cell.source.splitlines()]
        if cell.outputs:
            shaped = shaping.shape_outputs(
                cell.outputs,
                max_chars=int(cfg["output"]["max_chars"]),
                max_images=0,
                image_max_px=int(cfg["output"]["image_max_px"]),
                save_dir=None,
            )
            body += ["--- outputs ---", shaped.text]
        return text_result(_clip("\n".join(header + body), limit))

    if view == "intents":
        body = []
        for cell in cells:
            if cell.cell_type != "code":
                continue
            nh = meta.nh_meta(cell.metadata)
            if not nh:
                continue
            title = title_of(cells, cell) or _first_line(cell.source, 60)
            body.append(f"{label(cells, cell)} — {_author(cell)}")
            if nh.get("intent"):
                body.append(f"  intent: {nh['intent']}")
            for bullet in nh.get("rationale") or []:
                body.append(f"  - {bullet}")
            if not note_for(cells, cell) and title:
                body.append("  (no note above this cell)")
        return text_result(_clip("\n".join(header + (body or ["No nh-written cells yet."])), limit))

    outline = _outline(cells, stale, int(cfg["inspect"]["outline_limit"]), cell_id)
    head = [f"notebook {ref.rel_path}: {len(cells)} cells"]
    if view == "outline":
        return text_result(
            _clip("\n".join(header + head + outline + config_lines(cfg, svc.layout)), limit)
        )

    body = (
        header
        + head
        + outline
        + ["--- variables ---"]
        + _vars_block(payload)
        + config_lines(cfg, svc.layout)
    )
    return text_result(_clip("\n".join(body), limit))
