"""Read harness.toml on any Python >= 3.9.

Uses ``tomllib`` when available. On 3.9/3.10 (hooks, nhctl on the system Python) it falls back
to a small reader that reads a text as ``tomllib`` does or refuses it (design §6.9): ``[a.b]``
and ``[[a.b]]`` headers and dotted keys with bare or quoted parts, one-line strings of all four
kinds, TOML's integers and floats, booleans, one-line arrays of those, and ``#`` comments.
Anything else (an inline table, a date, a multi-line value, a line it can't read, a key or table
defined twice) raises ``ValueError`` naming the line and at most the key, never a value: the
doctor prints it, and a value may be a password.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on 3.9/3.10 only
    _tomllib = None

Key = tuple[str, ...]

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
# Control characters TOML refuses in a string or a comment: all but the tab.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_DEC = r"[0-9](?:_?[0-9])*"
_INTEGER = re.compile(
    r"[+-]?(?:0|[1-9](?:_?[0-9])*)"
    r"|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*"
)
_FLOAT = re.compile(
    rf"[+-]?(?:(?:0|[1-9](?:_?[0-9])*)(?:\.{_DEC}(?:[eE][+-]?{_DEC})?|[eE][+-]?{_DEC})|inf|nan)"
)


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


def basic_string(body: str, multiline: bool = False) -> str:
    """The value of a basic string from its body (the text between its quotes), with TOML 1.0's
    escapes. Raises ValueError on an escape TOML lacks, an unescaped quote that would end the
    string, or a control character."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            code = body[i + 1 : i + 2]
            if code in _ESCAPES:
                out.append(_ESCAPES[code])
                i += 2
                continue
            width = {"u": 4, "U": 8}.get(code, 0)
            digits = body[i + 2 : i + 2 + width]
            if width and len(digits) == width and set(digits) <= _HEX:
                point = int(digits, 16)
                if point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF:
                    out.append(chr(point))
                    i += 2 + width
                    continue
            raise ValueError("an escape TOML doesn't have")
        if ch == '"' and (not multiline or body.startswith('"""', i)):
            raise ValueError("a quote inside the string")
        if _CONTROL.match(ch):
            raise ValueError("a control character in the string")
        out.append(ch)
        i += 1
    return "".join(out)


def key_parts(text: str, start: int = 0) -> tuple[list[tuple[str | None, bool]], int] | None:
    """The dotted key at ``text[start:]``: ([(name, quoted), …], the index after it and the
    spaces after it), or None when no key starts there. A quoted name that doesn't decode (an
    escape TOML 1.0 lacks, a control character) is None."""
    parts: list[tuple[str | None, bool]] = []
    i, n = start, len(text)
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] in "\"'":
            end = _string_end(text, i, triple=False)
            if end > n:
                return None
            body = text[i + 1 : end - 1]
            name: str | None
            try:
                name = basic_string(body) if text[i] == '"' else _literal(body)
            except ValueError:
                name = None
            parts.append((name, True))
            i = end
        else:
            match = _BARE_KEY.match(text, i)
            if not match:
                return None
            parts.append((match.group(0), False))
            i = match.end()
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] == ".":
            i += 1
            continue
        return parts, i


def _literal(body: str, multiline: bool = False) -> str:
    if ("'''" if multiline else "'") in body or _CONTROL.search(body):
        raise ValueError("a quote or a control character in the string")
    return body


def _string_end(line: str, i: int, triple: bool = True) -> int:
    """The index after the one-line string that starts at ``line[i]``, len(line) + 1 when it
    doesn't end on the line. ``triple``: ``'''`` and ``\"\"\"`` open a multi-line string (a key
    can't be one)."""
    quote, n = line[i], len(line)
    if triple and line.startswith(quote * 3, i):
        j = i + 3
        while j < n:
            if quote == '"' and line[j] == "\\":
                j += 2
            elif line.startswith(quote * 3, j):
                j += 3
                for _ in range(2):  # up to two quotes before the closing three are content
                    if j < n and line[j] == quote:
                        j += 1
                return j
            else:
                j += 1
        return n + 1
    j = i + 1
    while j < n:
        if quote == '"' and line[j] == "\\":
            j += 2
        elif line[j] == quote:
            return j + 1
        else:
            j += 1
    return n + 1


def _statement(line: str, number: int) -> str:
    """``line`` without its comment and the spaces around it."""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            if _CONTROL.search(line, i + 1):
                raise ValueError(f"Invalid statement (at line {number})")
            return line[:i].strip(" \t")
        i = _string_end(line, i) if ch in "\"'" else i + 1
    return line.strip(" \t")


def _string(raw: str) -> str:
    quote = raw[0] * 3 if raw.startswith(raw[0] * 3) and len(raw) >= 6 else raw[0]
    if len(raw) < 2 * len(quote) or not raw.endswith(quote):
        raise ValueError("a string that doesn't end")
    body = raw[len(quote) : -len(quote)]
    multiline = len(quote) == 3
    return basic_string(body, multiline) if quote[0] == '"' else _literal(body, multiline)


def _array(raw: str) -> list[Any]:
    if not raw.endswith("]"):
        raise ValueError("an array that doesn't end on its line")
    items: list[str] = []
    depth, start, i, last = 0, 1, 1, len(raw) - 1
    while i < last:
        ch = raw[i]
        if ch in "\"'":
            i = _string_end(raw, i)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValueError("an array closed early")
        elif ch == "," and depth == 0:
            items.append(raw[start:i])
            start = i + 1
        i += 1
    if depth or i > last:
        raise ValueError("an array that doesn't end on its line")
    parts = [part.strip(" \t") for part in [*items, raw[start:last]]]
    if not parts[-1]:
        parts.pop()  # a trailing comma, or no item at all
    if not all(parts):
        raise ValueError("an empty array item")
    return [_value(part) for part in parts]


def _value(raw: str) -> Any:
    if raw in ("true", "false"):
        return raw == "true"
    if raw[:1] in ("'", '"'):
        return _string(raw)
    if raw.startswith("["):
        return _array(raw)
    if _INTEGER.fullmatch(raw):
        return int(raw.replace("_", ""), 0)
    if _FLOAT.fullmatch(raw):
        return float(raw.replace("_", ""))
    raise ValueError("a value this reader doesn't read")  # an inline table, a date, …


def _key(stmt: str, start: int, number: int) -> tuple[Key, int]:
    parsed = key_parts(stmt, start)
    if parsed is None or any(name is None for name, _ in parsed[0]):
        raise ValueError(f"Invalid statement (at line {number})")
    return tuple(name for name, _ in parsed[0] if name is not None), parsed[1]


def _nest(root: dict[str, Any], key: Key, number: int) -> dict[str, Any]:
    """The table at ``key``, made where missing; an array of tables gives its last table."""
    node: Any = root
    for name in key:
        if name not in node:
            node[name] = {}
        node = node[name]
        if isinstance(node, list):
            node = node[-1] if node else None
        if not isinstance(node, dict):
            raise ValueError(f"Cannot overwrite a value (at line {number})")
    return node


def _frozen(frozen: set[Key], key: Key) -> bool:
    return any(key[:size] in frozen for size in range(1, len(key) + 1))


def _mini_loads(text: str) -> dict[str, Any]:
    """``tomllib.loads`` for what this reader reads, with ``tomllib``'s rules for defining a key
    or a table once: a ``[table]`` header once, never over a table dotted keys made; dotted keys
    never into a table a header made; a ``[[array]]`` header starts a fresh table."""
    root: dict[str, Any] = {}
    header: Key = ()
    explicit: set[Key] = set()  # tables a header or a finished section's dotted keys defined
    pending: set[Key] = set()  # this section's dotted-key tables: explicit at the next header
    frozen: set[Key] = set()  # keys holding an array value: no header goes into one
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        stmt = _statement(line, number)
        if not stmt:
            continue
        if stmt.startswith("["):
            explicit |= pending
            pending = set()
            array = stmt.startswith("[[")
            header, end = _key(stmt, 2 if array else 1, number)
            if stmt[end:] != ("]]" if array else "]"):
                raise ValueError(f"Invalid statement (at line {number})")
            if _frozen(frozen, header) or (not array and header in explicit):
                raise ValueError(f"Cannot declare a table twice (at line {number})")
            if array:  # a fresh table: what the last one defined is free again
                size = len(header)
                explicit = {key for key in explicit if key[:size] != header}
                frozen = {key for key in frozen if key[:size] != header}
                parent = _nest(root, header[:-1], number)
                tables = parent.setdefault(header[-1], [])
                if not isinstance(tables, list):
                    raise ValueError(f"Cannot overwrite a value (at line {number})")
                tables.append({})
            else:
                _nest(root, header, number)
            explicit.add(header)
            continue
        key, end = _key(stmt, 0, number)
        if not stmt.startswith("=", end):
            raise ValueError(f"Invalid statement (at line {number})")
        try:
            value = _value(stmt[end + 1 :].strip(" \t"))
        except (ValueError, IndexError):
            # names the line and the key, never the value: doctor prints this
            raise ValueError(f"Invalid value for {'.'.join(key)} (at line {number})") from None
        for size in range(1, len(key)):
            if header + key[:size] in explicit:
                raise ValueError(f"Cannot overwrite a value (at line {number})")
            pending.add(header + key[:size])
        if _frozen(frozen, header + key[:-1]):
            raise ValueError(f"Cannot overwrite a value (at line {number})")
        table = _nest(root, header + key[:-1], number)
        if key[-1] in table:
            raise ValueError(f"Cannot overwrite a value (at line {number})")
        if isinstance(value, list):
            frozen.add(header + key)
        table[key[-1]] = value
    return root
