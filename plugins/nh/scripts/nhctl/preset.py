"""nhctl preset junior|senior: set the project's [preset] level in harness.toml (design §6.9).

A line edit: comments, blank lines, line endings and every other key stay byte for byte. The
edited text must parse with tomlread to the old settings plus the new level before an atomic
write; a file nh can't edit that safely gets D172 and stays as it is.
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import re
from pathlib import Path
from typing import Any

import common
from common import NhctlError, Result

from nh_gateway._shared import harness_toml, paths, tomlread

SHOWN = paths.HARNESS_TOML
# The commented default `nhctl scaffold` writes under [preset]: `# level = "junior"   # …`.
_COMMENTED_LEVEL = re.compile(
    r"""(?P<ws>[ \t]*)#[ \t]*(?P<key>level[ \t]*=[ \t]*)(?P<value>"[^"\\]*"|'[^']*')"""
    r"""(?P<tail>[ \t]*(?:#.*)?)"""
)
FIX_BY_HAND = 'Set it by hand: level = "{level}" under [preset].'
# A top-level `preset = …` or `preset.… = …`: a [preset] table added by hand would clash with it.
TOP_LEVEL = "a top-level preset key, not a [preset] table"
FIX_TOP_LEVEL = "Delete the top-level preset line(s), then rerun: nhctl preset {level}."
UNREADABLE_KEY = "a quoted key nh can't read"


class Unsafe(Exception):
    """harness.toml holds the preset in a form this line edit doesn't handle; ``fix`` is the
    D172 fix, with ``{level}`` for the asked level."""

    def __init__(self, reason: str, fix: str = FIX_BY_HAND) -> None:
        super().__init__(reason)
        self.fix = fix


class Syntax(ValueError):
    """A line outside any value that is neither a header, a key nor a comment. Both readers
    refuse such a file (design §6.9); the scan says so too, rather than guess at it."""

    def __init__(self, index: int) -> None:
        super().__init__(f"Invalid statement (at line {index + 1})")


def add_parsers(sub: argparse._SubParsersAction, common_opts: argparse.ArgumentParser) -> None:
    preset = sub.add_parser(
        "preset",
        parents=[common_opts],
        help="set harness.toml's [preset] level: junior (the default) or senior",
        description="Set the project's preset in harness.toml, keeping every other line. "
        "senior allows 1 comment line per 16 code lines, junior 1 per 8, unless harness.toml "
        "sets [lint] comment_ratio itself.",
    )
    preset.add_argument("level", choices=harness_toml.PRESET_LEVELS, help="junior or senior")
    preset.set_defaults(func=cmd_preset)


# ------------------------------------------------------------------------- the scan


# The dotted key at text[start:]: ([(name, quoted)], the index after it), or None. The 3.9
# reader's own key reader: TOML 1.0's escapes; a quoted name it can't decode is None.
_key = tomlread.key_parts


def _skip_string(text: str, i: int) -> int:
    """The index after the one-line string starting at ``text[i]`` (or the line's end)."""
    if text[i] == "'":
        end = text.find("'", i + 1)
        return len(text) if end < 0 else end + 1
    i += 1
    while i < len(text) and text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _line_starts(lines: list[str]) -> list[bool]:
    """Per line: True when it starts outside any string, array or inline table, so TOML reads
    it as a header, a key line, a comment or a blank; False inside a multi-line value."""
    out: list[bool] = []
    quote: str | None = None  # the open multi-line string's delimiter
    depth = 0
    for line in lines:
        out.append(quote is None and depth == 0)
        i, n = 0, len(line)
        while i < n:
            if quote is not None:
                if quote == '"""' and line[i] == "\\":
                    i += 2
                elif line.startswith(quote, i):
                    i += 3
                    while i < n and line[i] == quote[0]:  # up to two quotes end the content
                        i += 1
                    quote = None
                else:
                    i += 1
                continue
            ch = line[i]
            if ch == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                quote = line[i : i + 3]
                i += 3
            elif ch in "\"'":
                i = _skip_string(line, i)
            else:
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth = max(0, depth - 1)
                i += 1
    return out


def _value_end(body: str, start: int) -> int:
    """Where the one-line value starting at ``body[start]`` ends, before spaces and a comment."""
    i, n = start, len(body)
    while i < n and body[i] != "#":
        if body.startswith('"""', i) or body.startswith("'''", i):
            close = body.find(body[i : i + 3], i + 3)
            if close < 0:
                raise Unsafe("a level value over several lines")
            i = close + 3
        elif body[i] in "\"'":
            i = _skip_string(body, i)
        else:
            i += 1
    return len(body[:i].rstrip(" \t"))


class Scan:
    """Where harness.toml keeps its preset: the `[preset]` header's line, the `level = …` line
    in that table, and the first commented `# level = …` line there (indexes into the lines)."""

    def __init__(self, lines: list[str]) -> None:
        self.header: int | None = None
        self.level: int | None = None
        self.commented: int | None = None
        section: tuple | None = None  # None: the top level, before any header
        starts = _line_starts(lines)
        for index, line in enumerate(lines):
            if not starts[index]:
                continue
            body = line.rstrip("\r").lstrip(" \t")
            if not body:
                continue
            in_preset = section == ("preset",) and self.header is not None
            if body.startswith("#"):
                if in_preset and self.commented is None and _COMMENTED_LEVEL.fullmatch(body):
                    self.commented = index
                continue
            if body.startswith("["):
                section = self._header(body, index)
                continue
            key = _key(body, 0)
            if key is None or not body[key[1] :].startswith("="):
                raise Syntax(index)
            if any(name is None for name, _ in key[0]):
                raise Unsafe(UNREADABLE_KEY)
            (name, quoted), dotted = key[0][0], len(key[0]) > 1
            if section is None and name == "preset":
                raise Unsafe(TOP_LEVEL, FIX_TOP_LEVEL)
            if not in_preset or name != "level":
                continue
            if quoted or dotted:
                raise Unsafe("a dotted or quoted level key")
            if self.level is not None:
                raise Unsafe("two level lines")
            if index + 1 < len(lines) and not starts[index + 1]:
                raise Unsafe("a level value over several lines")
            self.level = index

    def _header(self, body: str, index: int) -> tuple:
        array = body.startswith("[[")
        parsed = _key(body, 2 if array else 1)
        if parsed is None:
            raise Syntax(index)
        parts, end = parsed
        close = "]]" if array else "]"
        rest = body[end:]
        if not rest.startswith(close) or rest[len(close) :].strip(" \t")[:1] not in ("", "#"):
            raise Syntax(index)
        names = tuple(name for name, _ in parts)
        if None in names:
            raise Unsafe(UNREADABLE_KEY)
        if names[0] == "preset":
            plain = not array and len(parts) == 1 and not parts[0][1]
            if not plain:
                raise Unsafe("a [preset.…], [[preset]] or quoted preset header")
            if self.header is not None:
                raise Unsafe("two [preset] tables")
            self.header = index
        return names


# ------------------------------------------------------------------------- the edit


def edit(text: str, level: str) -> tuple[str, bool, str]:
    """(the new text, changed, the level it held) with ``[preset] level = "<level>"``. Raises
    ValueError when the text doesn't parse (Syntax among them), Unsafe when nh can't edit it
    safely."""
    old = tomlread.loads(text)  # ValueError and the 3.11+ TOMLDecodeError (a ValueError)
    newline = text.find("\n")
    cr = "\r" if newline > 0 and text[newline - 1] == "\r" else ""
    final_newline = text.endswith("\n") or not text
    lines = text.split("\n")
    if final_newline:
        lines.pop()  # the "" after the last line break ("" itself: no lines)
    scan = Scan(lines)
    literal = f'"{level}"'
    # The level line the scan found, read on its own (design §6.9): no reader's view of the
    # rest of the file decides that nothing needs writing.
    current = None if scan.level is None else _line_value(lines[scan.level])
    was = current if current in harness_toml.PRESET_LEVELS else harness_toml.DEFAULT_LEVEL
    if scan.level is not None:
        if current == level:
            return text, False, was
        line = lines[scan.level]
        body = line.rstrip("\r")
        start = len(body) - len(body.lstrip(" \t"))
        _, after_key = _key(body, start) or ([], start)
        equals = body.index("=", after_key)
        value_start = equals + 1
        while value_start < len(body) and body[value_start] in " \t":
            value_start += 1
        value_end = _value_end(body, value_start)
        lines[scan.level] = body[:value_start] + literal + body[value_end:] + line[len(body) :]
    elif scan.commented is not None:
        line = lines[scan.commented]
        body = line.rstrip("\r")
        match = _COMMENTED_LEVEL.fullmatch(body.lstrip(" \t"))
        assert match is not None  # Scan matched it
        indent = body[: len(body) - len(body.lstrip(" \t"))]
        new = indent + match.group("ws") + match.group("key") + literal + match.group("tail")
        lines[scan.commented] = new + line[len(body) :]
    elif scan.header is not None:
        last = scan.header == len(lines) - 1
        if last:
            lines[scan.header] = _terminated(lines[scan.header], cr, final_newline)
        ending = "" if last and not final_newline else cr  # the new last line keeps no break
        lines.insert(scan.header + 1, f"level = {literal}{ending}")
    else:
        if lines:
            lines[-1] = _terminated(lines[-1], cr, final_newline)
            if lines[-1].strip():
                lines.append(cr)
        lines += ["[preset]" + cr, f"level = {literal}" + cr]
        final_newline = True
    new_text = "\n".join(lines) + ("\n" if final_newline and lines else "")
    _check(old, new_text, level)
    return new_text, True, was


def _line_value(line: str) -> Any:
    """The value of a ``level = …`` line, None when it doesn't read on its own."""
    try:
        value = tomlread.loads(line.strip()).get("level")
    except Exception:  # tomllib and the 3.9 reader raise different errors
        return None
    return value if isinstance(value, str) else None


def _terminated(line: str, cr: str, final_newline: bool) -> str:
    """``line``, which was the last one, now followed by another: in a CRLF file it needs its
    "\\r" when the file had no final line break."""
    return line + cr if cr and not final_newline and not line.endswith("\r") else line


def _check(old: dict, new_text: str, level: str) -> None:
    """The edited text must read as the old settings with only ``[preset] level`` set."""
    try:
        new = tomlread.loads(new_text)
    except Exception as exc:
        raise Unsafe("the edit didn't check out") from exc
    expected = copy.deepcopy(old)
    table = expected.setdefault("preset", {})
    if not isinstance(table, dict):
        raise Unsafe(TOP_LEVEL, FIX_TOP_LEVEL)
    table["level"] = level
    if not _same(new, expected):
        raise Unsafe("the edit didn't check out")


def _same(a: Any, b: Any) -> bool:
    """Equal, with NaN equal to NaN (a TOML `nan` must not fail the check)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


# ------------------------------------------------------------------------- the command


def _error(message: str, fix: str = "Fix the file, then rerun.") -> NhctlError:
    return NhctlError("D172", message, fix)


def read_harness(path: Path) -> tuple[bytes, str]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        kind = "a symbolic link" if path.is_symlink() else "not a regular file"
        raise _error(
            f"{SHOWN} can't be read ({kind}), so nh didn't change it.",
            "Make harness.toml a plain file, then rerun.",
        )
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise _error(
            f"{SHOWN} is missing, so there is no preset to set.",
            "Run /nh:init in this folder (it writes harness.toml), then rerun.",
        ) from None
    except OSError as exc:
        raise _error(f"{SHOWN} can't be read ({exc.strerror}), so nh didn't change it.") from None
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _error(f"{SHOWN} can't be read (it isn't UTF-8), so nh didn't change it.") from None


def writable(path: Path) -> bool:
    """Someone may write the file (a write bit in its mode) and this user can. ``os.replace``
    would replace a read-only file anyway, and root's ``os.access`` ignores the mode."""
    try:
        return bool(path.stat().st_mode & 0o222) and os.access(path, os.W_OK)
    except OSError:
        return False


def write(path: Path, raw: bytes, new_text: str) -> None:
    """Replace harness.toml with ``new_text`` unless it changed since nh read ``raw`` or is
    read-only (design §6.9): D172 then, and for a write that fails."""
    try:
        unchanged = path.read_bytes() == raw
    except OSError:
        unchanged = False
    if not unchanged:
        raise _error(
            f"{SHOWN} changed while nh was editing it, so nh didn't change it.",
            "Rerun the command.",
        )
    if not writable(path):
        raise _error(
            f"{SHOWN} is read-only, so nh didn't change it.",
            f"Make it writable (chmod u+w {SHOWN}), then rerun.",
        )
    try:
        common.write_keeping_mode(path, new_text)
    except OSError as exc:  # a read-only folder, a full disk; the temp file is gone
        reason = exc.strerror or type(exc).__name__
        raise _error(
            f"{SHOWN} can't be written ({reason}), so nh didn't change it.",
            f"Make {SHOWN} and its folder writable, then rerun.",
        ) from None


def cmd_preset(args: argparse.Namespace) -> Result:
    project = common.project_root(getattr(args, "project", None))
    assert project is not None
    level = args.level
    path = project / paths.HARNESS_TOML
    raw, text = read_harness(path)
    try:
        new_text, changed, was = edit(text, level)
    except Unsafe as exc:
        raise _error(
            f"{SHOWN} sets the preset in a form nh can't edit safely ({exc}), so nh didn't "
            "change it.",
            exc.fix.format(level=level),
        ) from None
    except Exception as exc:  # tomllib, the 3.9 reader and the scan raise different errors
        raise _error(f"{SHOWN} can't be parsed ({exc}), so nh didn't change it.") from None
    if changed:
        write(path, raw, new_text)
    ratio, set_by = harness_toml.comment_ratio(tomlread.loads(new_text))
    data = {
        "ok": True,
        "changed": changed,
        "path": SHOWN,
        "level": level,
        "was": was,
        "comment_ratio": ratio,
        "comment_ratio_set_by": set_by,
    }
    if changed:
        head = f"Preset: {level} (was {was}). {SHOWN} updated; nh uses it from its next tool call."
    else:
        head = f"Preset: {level} already; {SHOWN} unchanged."
    if set_by == "preset":
        budget = f"Comment budget: 1 comment line per {ratio} code lines."
    else:
        budget = (
            f"Comment budget: {SHOWN}'s [lint] comment_ratio = {ratio} sets it, not the preset."
        )
    return Result(data, f"{head}\n{budget}")
