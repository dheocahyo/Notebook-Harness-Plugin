"""Turn a cell's nbformat outputs into what the agent reads: a text budget, a few images, the error.

Nothing here talks to the kernel or the notebook. When the text had to be cut or an image was
downsampled, the text and the original images go to ``.nh/outputs/``. Past ``TRIM_CHARS`` of text
in one call, only each long text's head and tail are cleaned, redacted and kept (design §6.8).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import string
import tempfile
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePath
from typing import Any

from PIL import Image

from .._shared import secrets
from .._shared.text import _ANSI, _CONTROL, clip, strip_ansi, terminal_text
from ..backend.base import ErrorInfo, OutputSummary
from ..render import error_summary

ERROR_SHARE = 0.7  # of the text budget kept for the traceback when other output competes
MIN_SHARE = 200  # characters every shown output gets before any output is left out
HEAD_SHARE = 0.3  # head+tail cuts keep 30% head, 70% tail
PNG_LIMIT = 150 * 1024  # larger PNGs are re-encoded as JPEG q80
MAX_DECODE_PIXELS = 40_000_000
MAX_HTML_CHARS = 1_000_000
TRIM_CHARS = 8_000_000  # the most raw output text one call cleans, redacts and keeps (§6.8, Trim)
TRIM_MIN_SHARE = 256 * 1024  # what each text gets before texts from the middle are left out
PART_KEEP = 4096  # what each long part of a trimmed error keeps at least, when its share allows
HTML_CUT = "\n[… HTML cut …]"

# A bare object repr: "<Figure size 640x480 with 1 Axes>", "<IPython.core.display.HTML object>".
_OBJECT_REPR = re.compile(r"^<[^\n<>]*>$")
_SPACE = re.compile(r"\s*")  # what str.strip() strips, found without copying a long text
_CHAIN = re.compile(
    r"\n(?:The above exception was the direct cause of the following exception:"
    r"|During handling of the above exception, another exception occurred:)\n"
)
_CELL_LINE = re.compile(r"Cell In\s*\[\d*\],\s*line (\d+)")
_EVALUE_LINE = re.compile(r"\bline (\d+)\)?\s*$")
_PSEUDO_FILE_LINE = re.compile(r'File "<[^>]*>", line (\d+)')
_USER_FRAME = re.compile(r"(?m)^[ \t]*Cell In\s*\[\d*\], line \d+.*\n(?:[ \t-].*\n)*")
_DASH_LINE = re.compile(r"(?m)^-{20,}$\n?")
_PACKAGES_PATH = re.compile(r"(?<=File )\S*?/((?:site|dist)-packages)/")
# The trim (§6.8). A char that ends every run a pattern judges past a window's edge: a value
# (``_VALUE``, ``_WORD``), the code and skip rules' runs (``tokenizer.eos_token``, ``${X}``,
# ``***``, ``none.``), a number and an ``sk-`` key's tail.
_TERMINATOR = re.compile(r"[\s\"'<>&]")
_CUT_CHARS = (" ", "\t", '"', "'", "<", ">", "&")
_KEYED_LITERALS = ("token", "pass", "secret", "key", "authorization", "sk-")
# An unterminated private key's body (secrets._key_run), and a backslash: its escaped \n.
_KEY_BODY_CHARS = string.ascii_letters + string.digits + "+/=\n\\"
_KEY_BODY = re.compile(r"[A-Za-z0-9+/=\n\\]*")
_PEM_BEGIN = "-----BEGIN "  # how every private key's BEGIN line starts (secrets' pattern)
_COMMENT_CLOSERS = ("-->", "--!>")  # what ends an HTML comment (html.parser's commentclose)

_IMAGE_MIMES = ("image/png", "image/jpeg")
_PLOTLY = "application/vnd.plotly.v1+json"
_WIDGET = "application/vnd.jupyter.widget-view+json"
_BUNDLE_TYPES = ("execute_result", "display_data", "update_display_data")


@dataclass
class ShapedImage:
    data: bytes
    mime: str
    width: int
    height: int
    orig: tuple[int, int]


@dataclass
class Shaped:
    text: str
    images: list[ShapedImage]
    dropped_images: int
    truncated: bool
    full_path: str | None
    error: ErrorInfo | None
    summary: OutputSummary


@dataclass
class _Image:
    number: int
    raw: bytes
    fmt: str
    orig: tuple[int, int] | None = None  # None: unreadable
    sent: ShapedImage | None = None
    skipped: str = ""  # why a readable image was not sent
    saved_as: str | None = None

    @property
    def keep_original(self) -> bool:
        return self.orig is not None and (self.sent is None or self.sent.data != self.raw)

    def line(self, *, full: bool) -> str:
        if self.orig is None:
            return f"[image {self.number}: could not be read]"
        text = f"[image {self.number}: {self.orig[0]}x{self.orig[1]} {self.fmt}"
        if self.sent is None:
            text += f", not sent ({self.skipped})"
        elif (self.sent.width, self.sent.height) != self.orig:
            text += f", sent at {self.sent.width}x{self.sent.height}"
        if full and self.saved_as:
            text += f"; original: {self.saved_as}"
        return text + "]"


@dataclass
class _Section:
    label: str | None  # rendered as "[label] "; None for image lines and left-out runs
    body: str = ""
    is_error: bool = False
    image: _Image | None = None
    trims: list[tuple[str, int]] = field(default_factory=list)  # (a trim's marker, chars it cut)
    left_out: int | None = None  # raw chars of a text the trim left out whole
    count: int = 1  # outputs it stands for (a run of left-out texts is one line)

    @property
    def overhead(self) -> int:
        return len(self.label) + 3 if self.label else 0

    def render(self, body: str) -> str:
        if not self.label:
            return body
        sep = "\n" if "\n" in body else " "
        return f"[{self.label}]{sep}{body}"


@dataclass
class _Text:
    """One output's raw text, cleaned once the whole call's texts are planned (§6.8, Trim)."""

    section: _Section
    kind: str  # "text", "html" or "error" (``raw`` is then the output)
    raw: Any
    size: int  # what the plan counts: its length (HTML: at most MAX_HTML_CHARS)
    chars: int  # its whole raw length


def redact(text: str) -> str:
    """FR-14 (design §6.8): the installed redactor's ``redact``. Every text Claude reads from
    an output passes here once, before any cut; image base64 and bytes never do."""
    return secrets.current().redact(text)


def shape_outputs(
    outputs: list[dict],
    *,
    max_chars: int,
    max_images: int,
    image_max_px: int,
    save_dir: Path | None,
) -> Shaped:
    max_images, image_max_px = max(0, max_images), max(16, image_max_px)
    sections: list[_Section] = []
    texts: list[_Text] = []
    images: list[_Image] = []

    def add_text(section: _Section, kind: str, raw: Any, size: int, chars: int) -> None:
        sections.append(section)
        texts.append(_Text(section, kind, raw, size, chars))

    for out in _coalesce_streams(outputs):
        otype = out.get("output_type")
        if otype == "stream":
            raw = _text(out.get("text"))
            add_text(_Section(str(out.get("name") or "stdout")), "text", raw, len(raw), len(raw))
        elif otype == "error":
            size = _error_size(out)
            add_text(_Section("error", is_error=True), "error", out, size, size)
        elif otype in _BUNDLE_TYPES:
            data = _bundle(out)
            mime = next((m for m in _IMAGE_MIMES if data.get(m)), None)
            found = _bundle_text(data, has_image=mime is not None)
            if found is not None:
                kind, raw = found
                section = _Section("out" if otype == "execute_result" else "display")
                if kind == "fixed":
                    section.body = raw
                    sections.append(section)
                else:
                    size = min(len(raw), MAX_HTML_CHARS) if kind == "html" else len(raw)
                    add_text(section, kind, raw, size, len(raw))
            if mime is not None:
                send = sum(1 for i in images if i.sent) < max_images
                image = _take_image(
                    data[mime], mime, len(images) + 1, send, max_images, image_max_px
                )
                images.append(image)
                sections.append(_Section(None, image=image))
        else:
            sections.append(_Section(None, f"[{otype or 'unknown'} output]"))

    error = _clean_texts(texts)
    # within the cap, the summary's error is cleaned whole, as the texts are (§6.8, Unchanged)
    within = None if sum(t.size for t in texts) <= TRIM_CHARS else TRIM_MIN_SHARE
    sections = _join_left_out(sections)
    trimmed = any(section.trims for section in sections)
    saved_images = _save_originals(images, save_dir)
    for section in sections:
        if section.image is not None:
            section.body = section.image.line(full=False)
    plain = "\n".join(s.render(s.body) for s in sections)

    full_path = None
    if save_dir is not None and (len(plain) > max_chars or saved_images or trimmed):
        full = plain  # the same text unless an image line names its original
        if saved_images:
            full = "\n".join(
                s.render(s.image.line(full=True) if s.image else s.body) for s in sections
            )
        data = full.encode("utf-8", "replace")
        full_path = _write_once(save_dir, _sha_name(data, "txt"), data)
        written = [i.saved_as for i in images if i.saved_as] + ([full_path] if full_path else [])
        prune_outputs_dir(save_dir, keep={PurePath(path).name for path in written})

    footer = f"[full output: {full_path}]" if full_path else ""
    budget = max_chars - (len(footer) + 1 if footer else 0)
    truncated = len(plain) > budget
    text = _fit(sections, budget) if truncated else plain
    if footer:
        text = f"{text}\n{footer}" if text else footer
    if len(text) > max_chars:  # only reachable with a budget smaller than the fixed labels
        text, truncated = text[: max(0, max_chars)], True

    sent = [i.sent for i in images if i.sent is not None]
    return Shaped(
        text=text,
        images=sent,
        dropped_images=len(images) - len(sent),
        truncated=truncated or trimmed,
        full_path=full_path,
        error=error,
        summary=summarize_outputs(outputs, error_within=within),
    )


def summarize_outputs(
    outputs: list[dict], *, error_within: int | None = TRIM_MIN_SHARE
) -> OutputSummary:
    """A one-glance summary for outlines and logs. Never decodes images.

    The error's name and value are each cleaned within ``error_within`` raw chars (past it, the
    head and tail the trim keeps, §6.8), or whole when it is None, as ``shape_outputs`` asks for
    a call within ``TRIM_CHARS``."""
    summary = OutputSummary(count=len(outputs))
    for out in outputs:
        if not isinstance(out, dict):
            continue
        otype = out.get("output_type")
        text = ""
        if otype == "stream":
            kind = str(out.get("name") or "stdout")
            text = _text(out.get("text"))
        elif otype == "error":
            kind = "error"
            if summary.error is None:
                ename = str(out.get("ename") or "Error")
                evalue = str(out.get("evalue") or "")
                if error_within is None:
                    ename, evalue = _clean(ename), _clean(evalue)
                else:
                    ename = _clean_within(ename, error_within)
                    evalue = _clean_within(evalue, error_within)
                summary.error = error_summary(ename, evalue)
        elif otype in _BUNDLE_TYPES:
            data = _bundle(out)
            kind = _primary_mime(data)
            if any(data.get(m) for m in _IMAGE_MIMES):
                summary.images += 1
            plain = _text(data.get("text/plain"))
            text = "" if _object_repr(plain) else plain
        else:
            kind = str(otype or "unknown")
        if kind not in summary.types:
            summary.types.append(kind)
        if not summary.text_head and text and not text.isspace():
            summary.text_head = _first_line(text, 80)
    return summary


def prune_outputs_dir(
    save_dir: Path,
    max_files: int = 200,
    max_bytes: int = 50 * 2**20,
    *,
    keep: Collection[str] = (),
) -> None:
    """Delete the least recently written files beyond ``max_files`` or ``max_bytes``.

    The files named in ``keep``, what the current call just wrote and its result names, count
    first and are never deleted: a call's own copies outlive at least that call (design §6.13).
    """
    try:
        entries = list(os.scandir(save_dir))
    except OSError:
        return
    files: list[tuple[bool, float, int, str]] = []
    for entry in entries:
        if entry.name.startswith("."):  # in-flight temp files
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                files.append((entry.name in keep, stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            continue
    files.sort(reverse=True)  # kept first, then newest first
    total = 0
    for index, (kept, _mtime, size, path) in enumerate(files):
        total += size
        if not kept and (index >= max_files or total > max_bytes):
            with contextlib.suppress(OSError):
                os.unlink(path)


# --- text -----------------------------------------------------------------------------------


def _text(value: Any) -> str:
    """nbformat stores multi-line strings either whole or as a list of lines."""
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return value if isinstance(value, str) else ""


def _bundle(out: dict) -> dict:
    data = out.get("data")
    return data if isinstance(data, dict) else {}


def _clean(text: str) -> str:
    return redact(terminal_text(text).rstrip("\n"))


# --- trim (design §6.8) ---------------------------------------------------------------------


def _trim_margin() -> int:
    """How far a trim's windows reach past its cuts: the redactor's margin (past the longest
    value), and at least a private key's reach (its END line is looked for within
    PEM_MAX_CHARS of its BEGIN line)."""
    return max(secrets.current().margin, secrets.PEM_MAX_CHARS + secrets.MIN_MARGIN)


def _trim_plan(sizes: list[int], keep: Collection[int]) -> list[int | None]:
    """Each text's share of TRIM_CHARS, planned before any text is cleaned; None: left out.

    All whole when they fit. Else short texts stay whole and long ones split the rest
    (``_water_fill``); if even TRIM_MIN_SHARE each is too much, texts from the middle are left
    out: the first and last ones stay, and those in ``keep`` (the error).
    """
    if sum(sizes) <= TRIM_CHARS:
        return list(sizes)
    need = [min(size, TRIM_MIN_SHARE, TRIM_CHARS) for size in sizes]
    kept = list(range(len(sizes)))
    if sum(need) > TRIM_CHARS:
        chosen, spent = set(keep), sum(need[i] for i in keep)
        for i in _front_back([i for i in kept if i not in chosen]):
            if spent + need[i] > TRIM_CHARS:
                break
            chosen.add(i)
            spent += need[i]
        kept = sorted(chosen)
    plan: list[int | None] = [None] * len(sizes)
    shares = _water_fill([sizes[i] for i in kept], TRIM_CHARS)
    for i, share in zip(kept, shares, strict=True):
        plan[i] = share
    return plan


def _clean_texts(texts: list[_Text]) -> ErrorInfo | None:
    """Clean every text within its planned share (bodies and trims go on the sections); the
    first error's info."""
    plan = _trim_plan(
        [t.size for t in texts], [i for i, t in enumerate(texts) if t.kind == "error"]
    )
    margin = 0
    if any(share is not None and share < t.size for t, share in zip(texts, plan, strict=True)):
        margin = _trim_margin()
    error: ErrorInfo | None = None
    for text, share in zip(texts, plan, strict=True):
        section = text.section
        if share is None:
            section.left_out = text.chars
        elif text.kind == "error":
            info, section.trims = _error_info(text.raw, share, margin)
            error = error or info
            section.body = _traceback_body(info)
        elif text.kind == "html" and share < text.size:
            section.body = _html_within(text.raw, share, margin)
        elif text.kind == "html":
            section.body = _clean(_html_to_text(text.raw))
        else:
            section.body, trim = _clean_share(text.raw, share, margin)
            section.trims = [trim] if trim else []
    return error


def _clean_share(raw: str, share: int, margin: int) -> tuple[str, tuple[str, int] | None]:
    """``raw`` as ``_clean`` makes it when it fits ``share``. Else only a head window and a
    tail window are cleaned, ``share`` raw chars in all, with nh's cut marker between (returned
    too, with the chars it cut: the raw chars between the windows, and what each window
    cleaned and redacted but did not keep; no line break on a side that kept nothing). The head
    keeps at most 30% and the tail at most 70% of ``share - 2 * margin`` (``_trim_head``,
    ``_trim_tail``); each window reaches ``margin`` past that.
    """
    if len(raw) <= share:
        return _clean(raw), None
    keep = max(0, share - 2 * margin)
    head_len = int(keep * HEAD_SHARE)
    tail_len = keep - head_len
    head, head_end, cut = "", 0, 0
    if head_len:
        head_end = head_len + margin
        head, cut = _trim_head(terminal_text(raw[:head_end]), head_len, margin)
    tail, ending, tail_start = "", 0, len(raw)
    if tail_len:
        tail, ending, tail_start, tail_left = _trim_tail(raw, tail_len, margin)
        cut += tail_left
    cut += tail_start - head_end  # the raw chars no window holds
    marker = _cut_marker(cut)
    marker = marker if head else marker.lstrip("\n")
    marker = marker if tail else marker.rstrip("\n")
    return head + marker + tail, (marker, cut)


def _clean_within(text: str, share: int) -> str:
    """``text`` cleaned for a one-line summary, within ``share`` raw chars: past it, the head
    and tail the trim keeps, a line apart, without the trim's marker (the summary's first line
    is then the head's, or the tail's when the head kept nothing)."""
    if len(text) <= share:
        return _clean(text)
    kept, trim = _clean_share(text, share, _trim_margin())
    return kept.replace(trim[0], "\n", 1) if trim else kept


def _trim_head(window: str, target: int, margin: int) -> tuple[str, int]:
    """The redacted head, at most ``target`` cleaned chars, of a text whose cleaned start is
    ``window``, and the chars of ``window`` it leaves out (what was redacted with it past its
    cut, counted as redacted, and the rest). The text goes on past ``window``, so the window's
    last line may differ from the text's (a ``\\r`` past the window rewrites it); the lines
    before it are the text's own.

    A secret on one line (every pattern but a private key) is judged on that line alone, so a
    head that ends at a line break is redacted as the whole text would be. A head that ends
    inside a line also needs the line to end, or a char that ends every run a pattern judges,
    within the ``margin`` that is redacted with it (``_head_cut``). A secret that may span
    lines is judged whole on the lines before the last: with a private key's BEGIN line in the
    window, the head ends ``margin`` before the last line (an END line is looked for within
    ``PEM_MAX_CHARS``); a ``.env`` value with a line break that may run on into the last line
    from before it is left out (``Redactor.spill_back``). A window of one line holds neither
    across the cut. The cut never splits a marker (``redact_at``).
    """
    redactor = secrets.current()
    last_line = window.rfind("\n") + 1
    limit = target
    if last_line and _PEM_BEGIN in window:
        limit = min(limit, last_line - margin)
    elif last_line:
        limit = min(limit, redactor.spill_back(window, last_line))
    cut = _head_cut(window, limit, margin) if limit > 0 else 0
    if cut <= 0:
        return "", len(window)
    redacted = window[: cut + margin]
    out, lo, _ = redactor.redact_at(redacted, cut)
    return out[:lo], len(out) - lo + len(window) - len(redacted)


def _head_cut(window: str, limit: int, margin: int) -> int:
    """Where a head of at most ``limit`` chars of ``window`` ends: a line break in its last 40%,
    else ``limit`` when what is redacted with it (``margin`` more) holds the line's end, or a
    char of ``_TERMINATOR`` after the cut, or no keyed pattern's literal on the line; else the
    last such char before ``limit``; else the line break before it (0: none)."""
    newline = window.rfind("\n", 0, limit + 1)
    if newline >= limit * 0.6:
        return newline
    line = newline + 1
    cut = limit
    if window.find("\n", cut, cut + margin) == -1:  # the line runs on past what is redacted
        cut = min(cut, len(window) - margin)  # a .env value crossing the cut is whole in it
        end = cut + margin
        if cut > line and not _TERMINATOR.search(window, cut, end):
            lowered = window[line:end].lower()
            if any(literal in lowered for literal in _KEYED_LITERALS):
                cut = max(window.rfind(char, line, cut) for char in _CUT_CHARS)
    return cut if cut > line else max(0, newline)


def _trim_tail(raw: str, tail_len: int, margin: int) -> tuple[str, int, int, int]:
    """The redacted tail of ``raw``, about ``tail_len`` cleaned chars of whole lines; the
    trailing newlines ``_clean`` drops (no count includes them); where its window starts in
    ``raw``; and the chars of the window, cleaned and redacted, it leaves out before the tail.

    The window starts ``margin`` before that, outside any terminal escape. Its first line may
    have begun before it, with a pattern's literal (``token=``, ``Bearer``, a quote), so the
    tail starts at a line break after it; or, when that line shows only what follows a ``\\r``
    in the window, at the window's start. Then it skips what a secret that spans lines, begun
    before that start, may still cover: a ``.env`` value with a line break or a cut piece of
    one (``Redactor.spill``); and, when a private key's BEGIN line may be before the start
    (``_begin_before``), the key's END line (looked for within ``PEM_MAX_CHARS``) and an
    unterminated key's body (when the text before the start may end in one). It starts at the
    next line.
    """
    redactor = secrets.current()
    start = _outside_escape(raw, len(raw) - tail_len - margin)
    shown = terminal_text(raw[start:])
    window = shown.rstrip("\n")
    ending = len(shown) - len(window)
    newline = raw.find("\n", start)
    first_end = newline if newline != -1 else len(raw)  # the window's first line, raw
    key_before = _begin_before(raw, start, first_end, margin)
    if _shows_after_cr(raw[start:first_end]):
        line = 0
        key_open = key_before and _key_body_before(raw, start, margin)
    else:
        line = window.find("\n") + 1
        if not line:
            return "", ending, start, len(window)
        key_before = key_before or _PEM_BEGIN in window[:line]
        run = len(window[:line].rstrip(_KEY_BODY_CHARS))
        key_open = key_before and (run == 0 or window[run - 1] == "-")
    skip = redactor.spill(window, line)
    key_end = window.find("-----END ", line, line + secrets.PEM_MAX_CHARS) if key_before else -1
    if key_end != -1:
        skip = max(skip, key_end + 1)
    if key_open:
        body = _KEY_BODY.match(window, line)
        skip = max(skip, body.end() if body else line)
    begin = max(skip, len(window) - tail_len)
    if begin >= len(window):
        return "", ending, start, len(window)
    if begin > line and window[begin - 1] != "\n":
        begin = window.find("\n", begin) + 1 or len(window)
        if begin == len(window):
            return "", ending, start, len(window)
    out, _, hi = redactor.redact_at(window[line:], begin - line)
    return out[hi:], ending, start, line + hi


def _begin_before(raw: str, start: int, first_end: int, margin: int) -> bool:
    """Whether a private key's BEGIN line may be before ``start`` once ``raw`` is cleaned (False:
    no key can be open in the window). The raw text holds ``-----BEGIN `` before it (a C-speed
    scan back), or cleaning may join one the raw text splits: that takes a terminal escape
    before ``first_end``, the end of the window's first line (an escape inside ``-----BEGIN``
    is dropped, review of C12), or a control character within two margins of ``start`` (a
    scan of the whole text for those would cost 5 ns a char)."""
    if raw.rfind(_PEM_BEGIN, 0, start + len(_PEM_BEGIN) - 1) != -1:
        return True
    if raw.find("\x1b", 0, first_end) != -1:
        return True
    return (
        _CONTROL.search(raw, max(0, start - 2 * margin), min(first_end, start + margin)) is not None
    )


def _shows_after_cr(line: str) -> bool:
    """Whether a raw line shows only what follows a ``\\r`` in it, as ``collapse_cr`` does."""
    line = line.rstrip("\r")
    if "\r" not in line:
        return False
    return "\x1b" not in line or "\r" in strip_ansi(line).rstrip("\r")


def _key_body_before(raw: str, start: int, margin: int) -> bool:
    """Whether the text just before the line ``start`` is in may end in an unterminated private
    key's body: key-shaped chars back to a window ``margin`` before, or to a ``-`` (its BEGIN
    line's end). A line break inside a terminal escape counts too (it joins the lines)."""
    newline = raw.rfind("\n", 0, start)
    if newline == -1:
        return False
    if _outside_escape(raw, newline) != newline:
        return True
    before = terminal_text(raw[_outside_escape(raw, max(0, newline - margin)) : newline])
    run = len(before.rstrip(_KEY_BODY_CHARS))
    return run == 0 or before[run - 1] == "-"


def _outside_escape(text: str, at: int) -> int:
    """``at``, or the end of the terminal escape it falls inside: a window that starts inside
    one shows what ``terminal_text`` strips from the whole text."""
    escape = text.rfind("\x1b", 0, at)
    if escape == -1:
        return at
    match = _ANSI.match(text, escape)
    return match.end() if match and match.end() > at else at


def _join_left_out(sections: list[_Section]) -> list[_Section]:
    """Each run of texts the trim left out becomes one line: ``[… N chars cut …]``."""
    joined: list[_Section] = []
    for section in sections:
        last = joined[-1] if joined else None
        if section.left_out is None:
            joined.append(section)
        elif last is not None and last.label is None and last.left_out is not None:
            last.left_out += section.left_out
            last.count += 1
        else:
            joined.append(_Section(None, left_out=section.left_out))
    for section in joined:
        if section.label is None and section.left_out is not None:
            section.body = _cut_marker(section.left_out).strip("\n")
            section.trims = [(section.body, section.left_out)]
    return joined


FIRST_LINE_WINDOW = 4000  # the first window _first_line cleans, past its margin
FIRST_LINE_MAX = 1 << 20  # and the most it grows to


def _first_line(text: str, limit: int) -> str:
    """The first non-blank line, cleaned and redacted, clipped to ``limit``. Only a window is
    cleaned: it starts at the first non-blank character and grows until that line ends (or is
    longer than ``limit``) in its safe part, the part more than the redactor's margin before its
    raw edge, where a secret the edge cuts can't reach (review of C3)."""
    start = _leading_space(text)
    margin = secrets.current().margin
    size = FIRST_LINE_WINDOW + margin
    while True:
        end = start + size
        whole = end >= len(text) or size >= FIRST_LINE_MAX
        cleaned = _clean(text[start:end])
        safe = cleaned if whole else cleaned[: max(0, len(cleaned) - margin)]
        lines = safe.split("\n")
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                continue
            if whole or index < len(lines) - 1 or len(line) > limit:
                return clip(line, limit)
            break  # the line may go on past the safe part
        else:
            if whole:
                return ""
        size *= 2


def _leading_space(text: str) -> int:
    """How much whitespace ``text`` starts with (``len(text) - len(text.lstrip())``)."""
    match = _SPACE.match(text)
    return match.end() if match else 0


def _object_repr(text: str) -> bool:
    """``_OBJECT_REPR`` on ``text.strip()``, which is copied only when it starts with "<"."""
    return text.startswith("<", _leading_space(text)) and bool(_OBJECT_REPR.match(text.strip()))


def _coalesce_streams(outputs: Iterable[Any]) -> list[dict]:
    merged: list[dict] = []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        prev = merged[-1] if merged else None
        if (
            out.get("output_type") == "stream"
            and prev is not None
            and prev.get("output_type") == "stream"
            and prev.get("name") == out.get("name")
        ):
            merged[-1] = {**prev, "text": _text(prev.get("text")) + _text(out.get("text"))}
        else:
            merged.append(out)
    return merged


def _primary_mime(data: dict) -> str:
    for mime in (*_IMAGE_MIMES, _PLOTLY, _WIDGET):
        if data.get(mime):
            return mime
    plain = _text(data.get("text/plain"))
    if plain and not _object_repr(plain):
        return "text/plain"
    for mime in ("text/markdown", "text/latex", "text/html", "application/json", "image/svg+xml"):
        if mime in data:
            return mime
    return "text/plain" if plain else next(iter(data), "empty")


def _bundle_text(data: dict, *, has_image: bool) -> tuple[str, str] | None:
    """The text shown for one mime bundle, raw, and how it is cleaned: "text", "html", or
    "fixed" (nh's own placeholder, not cleaned). None when its image says it all."""
    if data.get(_PLOTLY) is not None:
        figure = data[_PLOTLY]
        traces = figure.get("data") if isinstance(figure, dict) else None
        count = len(traces) if isinstance(traces, list) else 0
        return "fixed", f"[plotly figure: {count} trace{'' if count == 1 else 's'}]"
    if data.get(_WIDGET) is not None:
        return "fixed", "[widget]"
    # A bare object repr says less than the richer mime types below it.
    plain = _text(data.get("text/plain"))
    if plain and not _object_repr(plain):
        return "text", plain
    for mime in ("text/markdown", "text/latex"):
        if data.get(mime):
            return "text", _text(data[mime])
    if has_image:
        return None
    if data.get("text/html"):
        return "html", _text(data["text/html"])
    if "application/json" in data:
        value = data["application/json"]
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        return "text", value
    if data.get("image/svg+xml"):
        return "fixed", "[SVG figure omitted]"
    if plain:
        return "text", plain
    mime = next(iter(data), None)
    return ("fixed", f"[{mime} output omitted]") if mime else None


class _HtmlText(HTMLParser):
    """HTML to plain text; table rows become tab-separated lines."""

    _SKIP = frozenset({"script", "style", "head", "template"})
    _BLOCK = frozenset(
        {"p", "div", "pre", "section", "article", "blockquote", "hr", "caption", "table"}
        | {"li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.pre = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skip += 1
        elif tag in ("td", "th"):
            self._close_cell()
            self.row = [] if self.row is None else self.row
            self.cell = []
        elif tag == "tr":
            self._close_row()
            self.row = []
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCK:
            self.pre += tag == "pre"
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "table"):
            self._close_row()
        elif tag in self._BLOCK:
            self.pre = max(0, self.pre - (tag == "pre"))
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.row is None:  # inside a row but outside a cell is layout whitespace
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))

    def _close_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
        self.cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self.row is not None:
            self._newline()
            self.parts.append("\t".join(self.row) + "\n")
        self.row = None

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        self._close_row()
        lines = [line.rstrip(" ") for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _html_text(markup: str) -> str:
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _html_to_text(markup: str) -> str:
    """HTML as text, from at most MAX_HTML_CHARS of markup."""
    if len(markup) <= MAX_HTML_CHARS:
        return _html_text(markup)
    # redact before the cut, so no secret is cut in half
    return _html_text(secrets.current().redact_head(markup, MAX_HTML_CHARS)[:MAX_HTML_CHARS]) + (
        HTML_CUT
    )


def _html_within(markup: str, share: int, margin: int) -> str:
    """HTML over its trim share (§6.8), cleaned and redacted: only its first ``share`` chars of
    markup are read. Markup over MAX_HTML_CHARS is redacted as markup first, as it is when
    whole (``_html_to_text``), a margin past them (``redact_at``: a secret across the cut is
    left out whole), so a value whose line breaks or spaces the parser joins or collapses is
    still found (review of C12); markup of 1 MB or less is not, as it is not when whole
    (redacting it first would mark a token's part before a ``<wbr>``, a tag or an entity and
    show the rest). Then at most ``share`` chars of it (markers longer than their values grow it) are parsed, as far as
    the whole markup's parse agrees (``_html_prefix``); and the text they give keeps a head of
    at most ``share - margin`` chars as a stream's does (``_trim_head``: their last line may go
    on past them), then says it was cut. A secret that the parser joins (across tags,
    collapsed whitespace or entities) is judged whole there: the head's cut is in the parsed
    text, a margin inside its end."""
    if len(markup) > MAX_HTML_CHARS:
        redacted, lo, _ = secrets.current().redact_at(markup[: share + margin], share)
        markup = redacted[:lo]
    text = terminal_text(_html_prefix(markup[:share]))
    head, _ = _trim_head(text, share - margin, margin)
    return head + HTML_CUT if head else HTML_CUT.lstrip("\n")


def _html_prefix(markup: str) -> str:
    """The text of ``markup``, the start of a longer markup, but for its last line as the whole
    markup's parse gives it. html.parser is fed it without ``close()``, so a construct still
    open at its end (a tag, a comment, a declaration, a script's or a title's text) shows
    nothing, where ``close()`` shows it as text or ends it there. And it ends before a comment
    that no ``-->`` or ``--!>`` closes in it: the parser closes ``<!-->`` and ``<!--->`` at once
    only when no closer follows in what it was given (Python 3.11.15), and a later one in the
    whole markup hides the text between (review of C12). Ending before one can leave an
    earlier one open (its closer was in what is left out), so it ends before that too."""
    end = len(markup)
    while True:
        last = max(markup.rfind(closer, 0, end) for closer in _COMMENT_CLOSERS)
        # a "<!--" at s is closed only by a closer that starts at s + 4 or later
        open_comment = markup.find("<!--", max(0, last - 3), end)
        if open_comment == -1:
            break
        end = open_comment
    parser = _HtmlText()
    parser.feed(markup[:end])
    return parser.text()


# --- errors ---------------------------------------------------------------------------------


def _error_parts(out: dict) -> tuple[str, str, list[str]]:
    frames = out.get("traceback")
    frames = [frames] if isinstance(frames, str) else frames if isinstance(frames, list) else []
    return str(out.get("ename") or "Error"), str(out.get("evalue") or ""), [str(f) for f in frames]


def _error_size(out: dict) -> int:
    ename, evalue, frames = _error_parts(out)
    return len(ename) + len(evalue) + sum(len(frame) + 1 for frame in frames)


def _error_info(
    out: dict, share: int | None = None, margin: int = 0
) -> tuple[ErrorInfo, list[tuple[str, int]]]:
    """The error, cleaned: its name, value and each frame alone, as the end of a frame is the
    end of a text to the redactor (a cut piece of a value there is redacted). Over the trim's
    ``share``, they split it (``_part_shares``, a frame counting its line break) and each is
    trimmed alone (with the trims made)."""
    ename, evalue, frames = _error_parts(out)
    trims: list[tuple[str, int]] = []
    if share is None or share >= _error_size(out):
        ename, evalue = _clean(ename), _clean(evalue)
        traceback = "\n".join(_clean(frame) for frame in frames)
    else:
        sizes = [len(ename), len(evalue), *(len(frame) + 1 for frame in frames)]
        shares = _part_shares(sizes, share, 2 * margin + PART_KEEP)
        shares[2:] = [max(0, part_share - 1) for part_share in shares[2:]]
        cleaned = [
            _clean_share(part, part_share, margin)
            for part, part_share in zip((ename, evalue, *frames), shares, strict=True)
        ]
        ename, evalue = cleaned[0][0], cleaned[1][0]
        traceback = "\n".join(frame for frame, _ in cleaned[2:])
        trims = [trim for _, trim in cleaned if trim]
    info = ErrorInfo(
        ename=ename, evalue=evalue, traceback=traceback, line=_error_line(traceback, evalue)
    )
    return info, trims


def _part_shares(sizes: list[int], share: int, floor: int) -> list[int]:
    """An error's parts' shares of ``share`` (its name, its value, then its frames): by
    ``_water_fill``, unless that leaves a part it cuts under ``floor`` (two margins and
    PART_KEEP: under two margins a cut part keeps nothing, review of C12). Then the parts that
    get a share are chosen first: the name, the value, and the frames from both ends (the
    user's cell and where it was raised, ``_front_back``), each needing ``floor`` or its size,
    while the share holds them; they split it by ``_water_fill``, and the others keep nothing
    (a trim marker alone)."""
    shares = _water_fill(sizes, share)
    if all(part >= min(size, floor) for part, size in zip(shares, sizes, strict=True)):
        return shares
    chosen, spent = [], 0
    for index in [0, 1, *_front_back(list(range(2, len(sizes))))]:
        need = min(sizes[index], floor)
        if spent + need <= share:
            chosen.append(index)
            spent += need
    shares = [0] * len(sizes)
    for index, part in zip(chosen, _water_fill([sizes[i] for i in chosen], share), strict=True):
        shares[index] = part
    return shares


def _error_line(traceback: str, evalue: str) -> int | None:
    """The failing line in the cell: the outermost ``Cell In[n], line N`` of the last exception.

    Falls back to ``line N`` in a SyntaxError's value or in a ``File "<...>"`` pseudo-file frame.
    """
    last = _CHAIN.split(traceback)[-1]
    for pattern, text in (
        (_CELL_LINE, last),
        (_CELL_LINE, traceback),
        (_EVALUE_LINE, evalue),
        (_PSEUDO_FILE_LINE, last),
    ):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _traceback_body(info: ErrorInfo) -> str:
    """The traceback without its dashed rules, ending in 'ename: evalue' exactly once."""
    text = _PACKAGES_PATH.sub(r"…/\1/", _DASH_LINE.sub("", info.traceback))
    text = re.sub(r"\n{2,}", "\n", text).strip("\n")
    headline = f"{info.ename}: {info.evalue}" if info.evalue else info.ename
    if not text:
        return headline
    if not _ends_with_error(text, info.ename, headline):
        text += "\n" + headline
    return text


def _ends_with_error(text: str, ename: str, headline: str) -> bool:
    """True when the traceback already closes with the error, even a multi-line message."""
    squashed, message = " ".join(text.split()), " ".join(headline.split())
    if squashed.endswith(message) or text.rsplit("\n", 1)[-1].lstrip().startswith(ename):
        return True
    first = " ".join(headline.split("\n", 1)[0].split())
    tail = text[-(len(headline) + 200) :]
    return any(" ".join(line.split()) == first for line in tail.splitlines())


# --- budget ---------------------------------------------------------------------------------


def _cut_marker(count: int) -> str:
    return f"\n[… {count:,} chars cut …]\n"


def _cut(text: str, limit: int, trims: Sequence[tuple[str, int]] = ()) -> str:
    """Keep the head (30%) and tail (70%), snapped to line breaks where that costs little.

    ``trims``: the trim markers in ``text`` (§6.8, Trim). Part of one is never shown, and one
    the cut leaves out counts as the chars it stands for.
    """
    if len(text) <= limit:
        return text
    spans = _trim_spans(text, trims)
    total = len(text) + sum(count - (end - start) for start, end, count in spans)
    room = limit - len(_cut_marker(total))
    if room <= 0:
        return _cut_marker(total).strip()[: max(0, limit)]
    head_len = int(room * HEAD_SHARE)
    head, tail = text[:head_len], text[len(text) - (room - head_len) :]
    newline = head.rfind("\n")
    if newline >= head_len * 0.6:
        head = head[:newline]
    newline = tail.find("\n")
    if 0 <= newline <= len(tail) * 0.4:
        tail = tail[newline + 1 :]
    if not spans:
        return head + _cut_marker(len(text) - len(head) - len(tail)) + tail
    pieces = [(0, len(head)), (len(text) - len(tail), len(text))]
    [(_, head_end), (tail_start, _)], count = _shown(spans, pieces, len(text))
    return text[:head_end] + _cut_marker(count) + text[tail_start:]


def _cut_traceback(text: str, limit: int, trims: Sequence[tuple[str, int]] = ()) -> str:
    """Tracebacks keep the tail (the error) plus the frame in the user's cell when it fits."""
    if len(text) <= limit:
        return text
    spans = _trim_spans(text, trims)
    total = len(text) + sum(count - (end - start) for start, end, count in spans)
    room = limit - len(_cut_marker(total))
    if room <= 0:
        return _cut(text, limit, trims)
    chained = list(_CHAIN.finditer(text))
    last_exception = chained[-1].end() if chained else 0
    frame = _USER_FRAME.search(text, last_exception) or _USER_FRAME.search(text)
    head, head_start, tail_start = "", 0, len(text) - room
    if frame and frame.start() < tail_start:
        head, head_start = frame.group(0).rstrip("\n")[: int(limit * 0.45)], frame.start()
        tail_start = max(len(text) - (room - len(head)), frame.end())
    tail = text[tail_start:]
    newline = tail.find("\n")
    if 0 <= newline <= len(tail) * 0.3:
        tail = tail[newline + 1 :]
    count = len(text) - len(head) - len(tail)
    if spans:
        pieces = [(head_start, head_start + len(head)), (len(text) - len(tail), len(text))]
        [(head_start, head_end), (tail_start, _)], count = _shown(spans, pieces, len(text))
        head, tail = text[head_start:head_end], text[tail_start:]
    marker = _cut_marker(count)
    return head + marker + tail if head else marker.lstrip("\n") + tail


def _trim_spans(text: str, trims: Sequence[tuple[str, int]]) -> list[tuple[int, int, int]]:
    """Where each trim marker is in ``text`` (with its newlines), and the chars it stands for.
    Two the same (an error's frames trimmed alike) are found one after the other."""
    spans = []
    after: dict[str, int] = {}
    for marker, count in trims:
        start = text.find(marker, after.get(marker, 0))
        if start == -1:  # a traceback's blank lines were squeezed: the marker's own newlines too
            marker = marker.strip("\n")
            start = text.find(marker, after.get(marker, 0))
        if start != -1:
            spans.append((start, start + len(marker), count))
            after[marker] = start + len(marker)
    return spans


def _shown(
    spans: list[tuple[int, int, int]], pieces: list[tuple[int, int]], length: int
) -> tuple[list[tuple[int, int]], int]:
    """The kept ``pieces`` of a cut text, moved off any trim marker they would show in part, and
    the chars the cut leaves out: a trim marker it leaves out counts as the chars it stands for."""
    moved = []
    for start, end in pieces:
        for span_start, span_end, _ in spans:
            if start < span_end and span_start < end and not start <= span_start < span_end <= end:
                if span_start <= start:
                    start = min(span_end, end)
                else:
                    end = span_start
        moved.append((start, end))
    count = length - sum(end - start for start, end in moved)
    for span_start, span_end, chars in spans:
        if not any(start <= span_start and span_end <= end for start, end in moved):
            count += chars - (span_end - span_start)
    return moved, count


def _water_fill(lengths: list[int], budget: int) -> list[int]:
    """Max-min fair shares: short outputs stay whole, long ones split what is left."""
    shares = [0] * len(lengths)
    left = max(0, budget)
    order = sorted(range(len(lengths)), key=lengths.__getitem__)
    for rank, index in enumerate(order):
        shares[index] = min(lengths[index], left // (len(order) - rank))
        left -= shares[index]
    return shares


def _fit(sections: list[_Section], budget: int) -> str:
    """Cut sections so the joined text fits ``budget``; an error gets most of it when present.

    If even ``MIN_SHARE`` characters each is too much, outputs from the middle are left out.
    """
    errors = [i for i, s in enumerate(sections) if s.is_error]
    others = [i for i, s in enumerate(sections) if not s.is_error]
    cost = [s.overhead + 1 for s in sections]  # label plus the joining newline
    avail = budget - sum(cost[i] for i in errors)
    error_len = sum(len(sections[i].body) for i in errors)
    other_budget = avail - (min(error_len, int(avail * ERROR_SHARE)) if others else error_len)

    outputs = sum(sections[i].count for i in others)
    marker_cost = len(f"[… {outputs:,} more outputs not shown …]") + 1
    need = {i: cost[i] + min(len(sections[i].body), MIN_SHARE) for i in others}
    kept = others
    if sum(need.values()) > other_budget:
        kept, spent = [], marker_cost
        for i in _front_back(others):
            if spent + need[i] > other_budget:
                break
            kept.append(i)
            spent += need[i]
        kept.sort()
    kept_set = set(kept)
    omitted = [i for i in others if i not in kept_set]
    fixed = sum(cost[i] for i in kept) + (marker_cost if omitted else 0)
    lengths = [len(sections[i].body) for i in kept]
    shares = dict(zip(kept, _water_fill(lengths, other_budget - fixed), strict=True))
    error_budget = avail - fixed - sum(shares.values())
    lengths = [len(sections[i].body) for i in errors]
    shares.update(zip(errors, _water_fill(lengths, error_budget), strict=True))

    parts: list[str] = []
    for index, section in enumerate(sections):
        if index in shares:
            cut = _cut_traceback if section.is_error else _cut
            parts.append(section.render(cut(section.body, shares[index], section.trims)))
        elif omitted and index == omitted[0]:
            outputs = sum(sections[i].count for i in omitted)
            parts.append(f"[… {outputs:,} more outputs not shown …]")
    return "\n".join(parts)


def _front_back(indices: list[int]) -> list[int]:
    """First, last, second, second-to-last, …: what survives is the head and the tail."""
    order: list[int] = []
    lo, hi = 0, len(indices) - 1
    while lo <= hi:
        order.append(indices[lo])
        if hi != lo:
            order.append(indices[hi])
        lo, hi = lo + 1, hi - 1
    return order


# --- images ---------------------------------------------------------------------------------


def _take_image(
    value: Any, mime: str, number: int, send: bool, max_images: int, max_px: int
) -> _Image:
    fmt = mime.split("/", 1)[1]
    try:
        raw = base64.b64decode(_text(value))
        with Image.open(io.BytesIO(raw)) as img:
            image = _Image(number, raw, (img.format or fmt).lower(), orig=img.size)
            if not send:
                image.skipped = f"limit {max_images}"
            elif img.width * img.height > MAX_DECODE_PIXELS:
                image.skipped = "too large to decode"
            else:
                image.sent = _fit_image(img, raw, max_px)
            return image
    except Exception:  # corrupt or unsupported data: base64, Pillow and codec errors of many types
        return _Image(number, b"", fmt)


def _fit_image(img: Image.Image, raw: bytes, max_px: int) -> ShapedImage:
    """≤ max_px on the long side; PNG, or JPEG q80 when the PNG would exceed 150 KB."""
    orig, fmt = img.size, img.format
    if max(orig) <= max_px and len(raw) <= PNG_LIMIT and fmt in ("PNG", "JPEG"):
        return ShapedImage(raw, f"image/{fmt.lower()}", orig[0], orig[1], orig)
    work = _resizable(img)
    work.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    if fmt != "JPEG":
        # Default zlib level, not optimize=True: on matplotlib figures that was ~5x slower and
        # no smaller.
        png = _encode(work, "PNG")
        if len(png) <= PNG_LIMIT:
            return ShapedImage(png, "image/png", work.width, work.height, orig)
    jpeg = _encode(_flatten(work), "JPEG", quality=80, optimize=True)
    return ShapedImage(jpeg, "image/jpeg", work.width, work.height, orig)


def _resizable(img: Image.Image) -> Image.Image:
    """The image, converted when PNG or LANCZOS resampling can't take its mode.

    Resizing in place is fine: the image was opened from bytes for this call only, and an
    unloaded JPEG then decodes at reduced scale (Pillow's draft mode).
    """
    if img.mode in ("RGB", "RGBA", "L", "LA"):
        return img
    if img.mode in ("P", "PA") or "A" in img.getbands() or "transparency" in img.info:
        return img.convert("RGBA")
    return img.convert("L" if img.mode in ("1", "I", "I;16", "F") else "RGB")


def _flatten(img: Image.Image) -> Image.Image:
    """JPEG has no alpha: composite on white."""
    if img.mode not in ("RGBA", "LA"):
        return img
    background = Image.new("RGB", img.size, "white")
    background.paste(img.convert("RGB"), mask=img.getchannel("A"))
    return background


def _encode(img: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt, **options)
    return buffer.getvalue()


# --- files ----------------------------------------------------------------------------------


def _sha_name(data: bytes, ext: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def _save_originals(images: list[_Image], save_dir: Path | None) -> bool:
    if save_dir is None:
        return False
    for image in images:
        if image.keep_original:
            ext = "jpg" if image.fmt in ("jpeg", "jpg") else image.fmt
            image.saved_as = _write_once(save_dir, _sha_name(image.raw, ext), image.raw)
    return any(image.saved_as for image in images)


def _write_once(save_dir: Path, name: str, data: bytes) -> str | None:
    """Content-addressed write; an existing copy is only touched (LRU). None if the disk fails."""
    path = save_dir / name
    try:
        if path.exists():
            os.utime(path)
        else:
            save_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(save_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
    except OSError:
        return None
    return _project_relative(path)


def _project_relative(path: Path) -> str:
    """'.nh/outputs/<name>' when the path is inside an nh project, else the absolute path."""
    parts = path.parts
    if ".nh" in parts:
        start = len(parts) - 1 - parts[::-1].index(".nh")
        return "/".join(parts[start:])
    return path.as_posix()
