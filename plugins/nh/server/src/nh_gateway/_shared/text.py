"""Word counting and note normalisation shared by lint, render, metrics and evals."""

from __future__ import annotations

import re

_WORD = re.compile(r"\S+")
_HAS_WORD_CHAR = re.compile(r"\w")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+")
_TITLE_PREFIX = re.compile(r"^\s*#+\s*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`(\"'])")
# JupyterLab reads `$…$` as inline math and `~…~` as strikethrough; a backslash keeps them literal.
_MARKDOWN_SPECIAL = re.compile(r"(?<!\\)([$~])")
_ESCAPED_SPECIAL = re.compile(r"\\([$~])")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.S)


def count_words(text: str) -> int:
    """Count whitespace-separated tokens that contain at least one word character."""
    return sum(1 for token in _WORD.findall(text or "") if _HAS_WORD_CHAR.search(token))


def normalize_title(title: str) -> str:
    """The title without leading ``#`` and without the escapes ``render_note`` added."""
    return unescape_markdown(_TITLE_PREFIX.sub("", (title or "").strip()).strip())


def normalize_bullet(bullet: str) -> str:
    return _BULLET_PREFIX.sub("", (bullet or "").strip(), count=1).strip()


def split_notes(notes: list[str] | str | None) -> list[str]:
    """Turn the ``notes`` argument into bullets (raw text: the note cell escapes, metadata doesn't).

    A list is taken as-is. A string is split on lines when it has several,
    otherwise on sentence boundaries, which is the common model mistake of
    passing prose instead of a list. When some lines of a string carry list
    markers, a line without one continues the bullet above it (a wrapped line).
    """
    if notes is None:
        return []
    if isinstance(notes, str):
        lines = [line for line in notes.splitlines() if line.strip()]
        parts = _join_wrapped(lines) if len(lines) > 1 else _SENTENCE_SPLIT.split(notes.strip())
    else:
        parts = [str(item) for item in notes]
    return [normalize_bullet(part) for part in parts if normalize_bullet(part)]


def _join_wrapped(lines: list[str]) -> list[str]:
    if not any(_BULLET_PREFIX.match(line) for line in lines):
        return lines
    parts: list[str] = []
    for line in lines:
        if parts and not _BULLET_PREFIX.match(line):
            parts[-1] = f"{parts[-1].rstrip()} {line.strip()}"
        else:
            parts.append(line)
    return parts


def escape_markdown(text: str) -> str:
    """Escape ``$`` and ``~`` outside code spans, so JupyterLab shows them as typed.

    Idempotent: an already escaped character is left alone.
    """
    out: list[str] = []
    last = 0
    for span in _CODE_SPAN.finditer(text):
        out.append(_MARKDOWN_SPECIAL.sub(r"\\\1", text[last : span.start()]))
        out.append(span.group(0))
        last = span.end()
    out.append(_MARKDOWN_SPECIAL.sub(r"\\\1", text[last:]))
    return "".join(out)


def unescape_markdown(text: str) -> str:
    """Undo ``escape_markdown`` (outside code spans)."""
    out: list[str] = []
    last = 0
    for span in _CODE_SPAN.finditer(text):
        out.append(_ESCAPED_SPECIAL.sub(r"\1", text[last : span.start()]))
        out.append(span.group(0))
        last = span.end()
    out.append(_ESCAPED_SPECIAL.sub(r"\1", text[last:]))
    return "".join(out)


def render_note(title: str, bullets: list[str], level: int = 3) -> str:
    """The note cell's Markdown. ``$`` and ``~`` are escaped here only; metadata keeps raw text."""
    heading = "#" * max(1, min(level, 6))
    head = f"{heading} {escape_markdown(normalize_title(title))}"
    body = "\n".join(f"- {escape_markdown(bullet)}" for bullet in bullets)
    return f"{head}\n\n{body}" if body else head


def clip(text: str, limit: int) -> str:
    """``text`` on one line, cut at a word boundary to at most ``limit`` characters, ending in '…'."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if text[limit - 1] != " " and space >= (limit - 1) * 0.5:  # the cut splits a word
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"
