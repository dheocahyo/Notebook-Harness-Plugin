"""Stdlib IPython-syntax masker (D12): magic lines become ``pass`` so tokenize and ast see Python.

Only lines that start a logical line are masked, as IPython does: a ``%`` or ``!=`` that
continues an expression inside brackets, a string or after a backslash is left alone.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# First characters that no Python statement can start with; IPython reads them as escapes
# (magic, shell, help, autocall).
_ESCAPES = ("%", "!", "?", "/", ",", ";")
_ASSIGN_MAGIC = re.compile(r"^(\s*)([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*=\s*[!%]")
_TIME_MAGIC = re.compile(r"^(\s*)%time\s+(\S.*)$")
_CELL_MAGIC = re.compile(r"^\s*%%(\S*)")
_TRIPLE = ('"""', "'''")


@dataclass
class Masked:
    text: str  # same number of lines; magic lines replaced by "pass" + spaces
    cell_magic: str | None  # "%%time" -> "time"; the whole cell then skips AST rules
    magic_lines: set[int]  # 1-based


def lines_of(source: str) -> list[str]:
    """Split like ``mask`` does, so line numbers agree with ``Masked.text``."""
    return source.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def mask(source: str) -> Masked:
    lines = lines_of(source or "")
    out = list(lines)
    magic: set[int] = set()
    cell_magic = None
    start = 0
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and (match := _CELL_MAGIC.match(lines[first])):
        cell_magic = match.group(1)
        out[first] = _pad("pass", lines[first])
        magic.add(first + 1)
        start = first + 1

    scan = _Scan()
    continued_magic = False
    for i in range(start, len(lines)):
        line = lines[i]
        if continued_magic:
            # A backslash-continued magic runs on; blank keeps any indentation valid.
            out[i] = " " * len(line)
            magic.add(i + 1)
            continued_magic = line.endswith("\\")
            continue
        if not scan.at_start:
            scan.feed(line)
            continue
        replacement = _replace_magic(line)
        if replacement is None:
            scan.feed(line)
            if not (scan.tail == "?" and scan.at_start):
                continue
            scan.reset()  # `df?` / `df.head??`: help lookup
            replacement = _pad(_indent(line) + "pass", line)
        out[i] = replacement
        magic.add(i + 1)
        continued_magic = line.endswith("\\")
    return Masked("\n".join(out), cell_magic, magic)


def _replace_magic(line: str) -> str | None:
    """The masked form of a line that starts a logical line, or None when it is plain Python."""
    stripped = line.lstrip()
    if not stripped:
        return None
    indent = _indent(line)
    if stripped.startswith(_ESCAPES):
        timed = _TIME_MAGIC.match(line)
        if timed and not line.endswith("\\") and _parses(timed.group(2)):
            # `%time stmt` runs stmt in the user namespace, so keep it for dataflow.
            return _pad(indent + timed.group(2), line)
        return _pad(indent + "pass", line)
    assign = _ASSIGN_MAGIC.match(line)
    if assign:  # `files = !ls`, `env = %env`
        return _pad(f"{indent}{assign.group(2)} = None", line)
    return None


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _pad(text: str, original: str) -> str:
    return text + " " * max(0, len(original) - len(text))


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


class _Scan:
    """Tracks open brackets, strings and backslash continuations across lines."""

    __slots__ = ("cont", "depth", "quote", "tail")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.depth = 0
        self.quote = ""
        self.cont = False
        self.tail = ""  # last code character of the line fed last, outside strings and comments

    @property
    def at_start(self) -> bool:
        return self.depth == 0 and not self.quote and not self.cont

    def feed(self, line: str) -> None:
        self.cont = False
        self.tail = ""
        i, n = 0, len(line)
        while i < n:
            if self.quote:
                end = self._close(line, i)
                if end < 0:
                    # A one-quote string ends at the newline unless a backslash continues it.
                    if len(self.quote) == 1 and not line.endswith("\\"):
                        self.quote = ""
                    return
                self.quote = ""
                i = end
                continue
            char = line[i]
            if char == "#":
                return
            if not char.isspace():
                self.tail = char
            if char in "\"'":
                self.quote = line[i : i + 3] if line.startswith(_TRIPLE, i) else char
                i += len(self.quote)
                continue
            if char in "([{":
                self.depth += 1
            elif char in ")]}":
                self.depth = max(0, self.depth - 1)
            i += 1
        self.cont = line.endswith("\\")

    def _close(self, line: str, i: int) -> int:
        """Index just past the closing quote, or -1 when the string stays open."""
        while i < len(line):
            if line[i] == "\\":
                i += 2
            elif line.startswith(self.quote, i):
                return i + len(self.quote)
            else:
                i += 1
        return -1
