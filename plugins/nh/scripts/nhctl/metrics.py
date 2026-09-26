"""nhctl metrics summarize|sample: pilot metrics from .nh/log.jsonl.

The log never holds code or outputs. ``sample`` reads agent code cells straight from
the project's notebooks for the comprehension check (code only, no notes).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import statistics
from collections import Counter
from pathlib import Path

import common
from common import NhctlError, Result

from nh_gateway._shared import paths, tomlread
from nh_gateway._shared.scaffold import core

SKIP_DIRS = {"node_modules", "__pycache__", "site-packages"}


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    metrics = sub.add_parser("metrics", parents=[common_opts], help="pilot metrics")
    metrics_sub = metrics.add_subparsers(dest="action", required=True)
    summarize = metrics_sub.add_parser(
        "summarize", parents=[common_opts], help="summarize .nh/log.jsonl"
    )
    summarize.add_argument("--since", help="only events from this date or ISO time on")
    summarize.set_defaults(func=cmd_summarize)
    sample = metrics_sub.add_parser(
        "sample", parents=[common_opts], help="random agent code cells, for a comprehension check"
    )
    sample.add_argument("--n", type=int, default=5)
    sample.add_argument("--seed", type=int, help="make the sample repeatable")
    sample.set_defaults(func=cmd_sample)


# --------------------------------------------------------------------------- events


def parse_time(value: object) -> float | None:
    """Epoch seconds from a number or an ISO-8601 string (naive = local time)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def load_events(layout: paths.Layout, since: float | None) -> tuple[list[dict], list[str]]:
    """Events from the rotated log and the current one, oldest first."""
    events: list[dict] = []
    used: list[str] = []
    for path in (layout.nh / "log.1.jsonl", layout.log_file):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        used.append(common.rel(layout.project, path))
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a torn or foreign line
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                continue
            if since is not None:
                ts = parse_time(event.get("ts"))
                if ts is None or ts < since:
                    continue
            events.append(event)
    return events, used


def _uid(event: dict) -> str | None:
    for key in ("cell_uid", "uid", "cell_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _number(value: object) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else value


def harness_ms(event: dict) -> float | None:
    """Gateway overhead of one add: ``harness_ms``, else ``ms.harness``, else total - exec."""
    value = _number(event.get("harness_ms"))
    ms = event.get("ms")
    if value is None and isinstance(ms, dict):
        value = _number(ms.get("harness"))
        total, run = _number(ms.get("total")), _number(ms.get("exec"))
        if value is None and total is not None:
            value = total - (run or 0)
    return value


def rule_ids(reason: object) -> list[str]:
    """``cell_rejected.reason``: a list of rule ids, or (older logs) one comma-joined string."""
    if isinstance(reason, list):
        items = [str(item).strip() for item in reason if item is not None]
    elif isinstance(reason, str):
        items = [part.strip() for part in reason.split(",")]
    else:
        items = []
    return [item for item in items if item] or ["unknown"]


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def spread(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "median": None, "p90": None, "max": None}
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p90": nearest_rank(values, 0.9),
        "max": max(values),
    }


def summarize(events: list[dict], bullet_hint_words: int = 25) -> dict:
    turns: dict[tuple, set] = {}
    added: set[str] = set()
    undone: set[str] = set()
    reviews: dict[str, dict] = {}
    rejections: Counter = Counter()  # per rule id; one rejection can name several
    rejected = 0
    note_words: list[float] = []
    bullet_words: list[float] = []
    overhead: list[float] = []
    fresh = {"runs": 0, "passed": 0}
    for index, event in enumerate(events):
        kind = event["event"]
        turn = (event.get("session_id"), event.get("turn_id"))
        uid = _uid(event)
        if kind == "turn_open":
            turns.setdefault(turn, set())
        elif kind == "cell_added":
            cell = uid or f"#{index}"
            first = cell not in added
            turns.setdefault(turn, set()).add(cell)
            added.add(cell)
            if first:
                words = _number(event.get("note_words"))
                if words is not None:
                    note_words.append(words)
                bullets = event.get("bullet_words")
                if isinstance(bullets, list):
                    bullet_words.extend(b for b in bullets if _number(b) is not None)
            ms = harness_ms(event)
            if ms is not None:
                overhead.append(ms)
        elif kind == "cell_rejected":
            rejected += 1
            rejections.update(dict.fromkeys(rule_ids(event.get("reason")), 1))
        elif kind == "cell_undone" and uid:
            undone.add(uid)
        elif kind == "cell_review" and uid:
            reviews[uid] = event
        elif kind == "fresh_run":
            fresh["runs"] += 1
            fresh["passed"] += 1 if event.get("ok") is True else 0

    per_turn = Counter(len(cells) for cells in turns.values())
    accepted = sum(
        1
        for uid, review in reviews.items()
        if review.get("unedited") is True and not review.get("deleted") and uid not in undone
    )
    bullets = spread(bullet_words)
    bullets["over_hint"] = sum(1 for b in bullet_words if b > bullet_hint_words)
    return {
        "events": len(events),
        "turns": len(turns),
        "cells_added": len(added),
        "cells_per_turn": {str(k): per_turn[k] for k in sorted(per_turn)},
        "max_cells_per_turn": max(per_turn) if per_turn else 0,
        "rejections": {"total": rejected, "by_reason": dict(rejections)},
        "acceptance": {
            "reviewed": len(reviews),
            "accepted": accepted,
            "rate": _rate(accepted, len(reviews)),
            "unreviewed": len(added - set(reviews) - undone),
        },
        "undo": {"undone": len(undone), "rate": _rate(len(undone), len(added))},
        "note_words": spread(note_words),
        "bullet_words": bullets,
        "fresh_run": dict(fresh, rate=_rate(fresh["passed"], fresh["runs"])),
        "harness_ms": {
            "n": len(overhead),
            "p50": nearest_rank(overhead, 0.5) if overhead else None,
            "p95": nearest_rank(overhead, 0.95) if overhead else None,
        },
    }


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def summary_text(summary: dict) -> str:
    dist = ", ".join(f"{k} cell(s): {v}" for k, v in summary["cells_per_turn"].items()) or "none"
    acc, undo, fresh = summary["acceptance"], summary["undo"], summary["fresh_run"]
    notes, bullets, ms = summary["note_words"], summary["bullet_words"], summary["harness_ms"]
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(summary["rejections"]["by_reason"].items()))
    lines = [
        f"turns: {summary['turns']}   cells added: {summary['cells_added']}   events: {summary['events']}",
        f"cells per turn: {dist} (max {summary['max_cells_per_turn']})",
        f"rejections: {summary['rejections']['total']}" + (f" ({reasons})" if reasons else ""),
        f"acceptance: {_pct(acc['rate'])} of {acc['reviewed']} reviewed ({acc['unreviewed']} not reviewed yet)",
        f"undo rate: {_pct(undo['rate'])} ({undo['undone']} undone)",
        f"note words: median {notes['median']}, p90 {notes['p90']}, max {notes['max']}",
        f"bullet words: median {bullets['median']}, p90 {bullets['p90']}, "
        f"over the hint: {bullets['over_hint']}",
        f"fresh-run pass rate: {_pct(fresh['rate'])} of {fresh['runs']} runs",
        f"harness overhead: p50 {ms['p50']} ms, p95 {ms['p95']} ms",
    ]
    return "\n".join(lines)


def cmd_summarize(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    since = None
    if args.since:
        since = parse_time(args.since)
        if since is None:
            raise NhctlError(
                "D180", f"Can't read --since {args.since!r}.", "Use a date like 2026-09-25."
            )
    layout = paths.Layout(project)
    events, used = load_events(layout, since)
    markdown = tomlread.load(layout.harness_toml).get("markdown") or {}
    hint = markdown.get("bullet_hint_words") if isinstance(markdown, dict) else None
    summary = summarize(events, hint if isinstance(hint, int) else 25)
    data = dict(summary, ok=True, logs=used, since=args.since)
    if not used:
        return Result(data, "No nh activity logged yet (.nh/log.jsonl is missing).")
    return Result(data, summary_text(summary))


# --------------------------------------------------------------------------- sample


def project_notebooks(project: Path) -> list[Path]:
    found = []
    for folder, dirs, files in os.walk(project):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        found.extend(Path(folder) / name for name in sorted(files) if name.endswith(".ipynb"))
    return found


def agent_code_cells(project: Path, path: Path) -> list[dict]:
    nb = paths.read_json(path, {})
    cells = nb.get("cells") if isinstance(nb, dict) else None
    out = []
    for cell in cells or []:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        meta = (cell.get("metadata") or {}).get("nh")
        if not isinstance(meta, dict) or meta.get("role") != "code":
            continue
        code = core.cell_source(cell)
        if code.strip():
            out.append(
                {
                    "notebook": common.rel(project, path),
                    "exec": cell.get("execution_count"),
                    "code": code,
                }
            )
    return out


def cmd_sample(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    if args.n < 1:
        raise NhctlError("D181", "--n must be at least 1.")
    pool = [cell for nb in project_notebooks(project) for cell in agent_code_cells(project, nb)]
    rng = random.Random(args.seed)
    picked = rng.sample(pool, min(args.n, len(pool)))
    data = {"ok": True, "pool": len(pool), "cells": picked}
    if not picked:
        return Result(data, "No nh-written code cells in this project's notebooks yet.")
    blocks = []
    for cell in picked:
        label = f" [{cell['exec']}]" if cell["exec"] is not None else ""
        blocks.append(f"--- {cell['notebook']}{label} ---\n{cell['code'].rstrip()}")
    return Result(data, "\n\n".join(blocks))
