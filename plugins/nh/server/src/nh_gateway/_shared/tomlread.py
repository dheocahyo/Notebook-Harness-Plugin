"""Read harness.toml on any Python >= 3.9.

Uses ``tomllib`` when available. On 3.9/3.10 (hooks, nhctl on the system
Python) it falls back to a small parser that understands what nh writes:
``[section]`` / ``[a.b]`` headers, ``key = value`` with strings, integers,
floats, booleans and flat arrays of those, plus ``#`` comments.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on 3.9/3.10 only
    _tomllib = None

_HEADER = re.compile(r"^\[\s*([A-Za-z0-9_.\-]+)\s*\]$")
_PAIR = re.compile(r"^([A-Za-z0-9_\-]+)\s*=\s*(.+)$")


def loads(text: str) -> dict[str, Any]:
    if _tomllib is not None:
        return _tomllib.loads(text)
    return _mini_loads(text)


def load(path: Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):  # missing, unreadable, or not UTF-8: no settings
        return {}
    try:
        return loads(text)
    except Exception:
        return {}


def _strip_comment(line: str) -> str:
    out, quote, escaped = [], None, False
    for ch in line:
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _value(raw: str) -> Any:
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_value(part) for part in _split_array(inner)]
    if raw.startswith("'"):
        return raw[1:-1]  # TOML literal string: no escapes
    if raw.startswith('"'):
        return ast.literal_eval(raw)
    try:
        return int(raw.replace("_", ""))
    except ValueError:
        return float(raw.replace("_", ""))


def _split_array(inner: str):
    parts, depth, quote, current = [], 0, None, []
    for ch in inner:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if "".join(current).strip():
        parts.append("".join(current))
    return [part for part in parts if part.strip()]


def _mini_loads(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    table = root
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line)
        if not line:
            continue
        header = _HEADER.match(line)
        if header:
            table = root
            for part in header.group(1).split("."):
                table = table.setdefault(part, {})
            continue
        pair = _PAIR.match(line)
        if pair:
            try:
                table[pair.group(1)] = _value(pair.group(2))
            except (ValueError, SyntaxError):
                # names the line, never the value: doctor prints this, and it may be a password
                raise ValueError(f"Invalid value for {pair.group(1)} (at line {number})") from None
    return root
