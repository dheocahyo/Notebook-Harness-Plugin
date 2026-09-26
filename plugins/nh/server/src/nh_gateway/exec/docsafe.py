"""Make kernel outputs safe to write into the notebook's CRDT document, and summarize them cheaply.

pycrdt panics (a ``BaseException``) on ints outside int64 and on lone surrogates, and Yjs stores
ints beyond 2**53 as BigInt, which JupyterLab can't serialize. Large outputs are capped so one
update never approaches the server's websocket message limit.

A stream that outgrows its cap keeps its tail. While a cell runs, the saved tail only grows at
the end (so each flush appends just the new text) until it is ``STREAM_SLACK_CHARS`` over the
cap; then its head is trimmed back in one rewrite (see :func:`stream_start`).
"""

from __future__ import annotations

import math
from typing import Any

from ..backend.base import OutputSummary

MAX_SAFE_INT = 2**53 - 1
STREAM_KEEP_CHARS = 1 << 20
STREAM_SLACK_CHARS = 1 << 19
BUNDLE_MAX_CHARS = 5 << 20
CELL_MAX_CHARS = 8 << 20
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/svg+xml")


def safe_text(text: str) -> str:
    """Replace lone surrogates with U+FFFD; valid surrogate pairs are joined."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sanitize_for_doc(value: Any) -> Any:
    """Recursively turn ``value`` into JSON that pycrdt and JupyterLab both accept."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INT <= value <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(str(key)): sanitize_for_doc(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_doc(item) for item in value]
    return safe_text(str(value))


def approx_size(value: Any) -> int:
    """Rough serialized size in characters, without encoding anything."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(len(str(key)) + approx_size(item) + 4 for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(approx_size(item) + 1 for item in value)
    return 8


def _notice(text: str) -> dict[str, Any]:
    return {"output_type": "stream", "name": "stderr", "text": text}


def stream_start(length: int, start: int | None = None) -> int:
    """Where a running stream's saved tail starts, given where it started at the last flush.

    A new stream (``start`` None) keeps exactly its last ``STREAM_KEEP_CHARS``. After that the
    start stays put (the saved text only grows) until the tail is ``STREAM_SLACK_CHARS`` longer
    than the cap, then moves so exactly the last ``STREAM_KEEP_CHARS`` are kept again.
    """
    exact = max(0, length - STREAM_KEEP_CHARS)
    if start is None or length < start or length - start > STREAM_KEEP_CHARS + STREAM_SLACK_CHARS:
        return exact
    return start


def _cap_one(output: dict[str, Any], start: int | None = None) -> tuple[dict[str, Any], int]:
    kind = output.get("output_type")
    if kind == "stream":
        text = output.get("text") or ""
        if isinstance(text, list):
            text = "".join(text)
        if start is None:
            start = max(0, len(text) - STREAM_KEEP_CHARS)
        if start > 0:
            text = (
                f"[nh: {start:,} earlier characters of this output were not saved in the notebook]\n"
                + text[start:]
            )
        capped = {**output, "text": text}
        return capped, len(text) + 32
    size = approx_size(output)
    if size <= BUNDLE_MAX_CHARS:
        return output, size
    if kind == "error":
        traceback = list(output.get("traceback") or [])[-50:]
        capped = {**output, "traceback": [line[-4000:] for line in traceback]}
        return capped, approx_size(capped)
    note = f"[nh: this output ({size / 2**20:.1f} MB) is too large to save in the notebook]"
    capped = {key: value for key, value in output.items() if key not in ("data", "metadata")}
    capped["data"] = {"text/plain": note}
    capped["metadata"] = {}
    return capped, len(note) + 64


def cap_outputs_for_doc(
    outputs: list[dict[str, Any]], starts: list[int | None] | None = None
) -> list[dict[str, Any]]:
    """Cap each output (stream tail 1 MB, bundle 5 MB) and the cell total (8 MB). Returns a new list.

    ``starts`` gives, per output, where a stream's saved tail starts (see :func:`stream_start`);
    without it each stream keeps exactly its last ``STREAM_KEEP_CHARS``.
    """
    capped: list[dict[str, Any]] = []
    total = 0
    for position, output in enumerate(outputs):
        start = starts[position] if starts is not None and position < len(starts) else None
        item, size = _cap_one(output, start)
        if total + size > CELL_MAX_CHARS:
            rest = len(outputs) - position
            capped.append(
                _notice(
                    f"[nh: {rest} more output(s) were not saved; this cell's output is over 8 MB]\n"
                )
            )
            break
        capped.append(item)
        total += size
    return capped


def strip_transient(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the kernel-protocol ``transient`` field, which the nbformat schema doesn't allow."""
    return [
        {key: value for key, value in output.items() if key != "transient"} for output in outputs
    ]


def _as_shown(output: dict[str, Any]) -> dict[str, Any]:
    """A stream without the ``\\r`` and ``\\b`` left after progress-bar processing.

    JupyterLab drops them when it adds a stream output, then applies nh's in-place edits to its
    own copy by length ("remove n characters from the end, append"), so its copy and the
    notebook's text must be identical.
    """
    text = output.get("text") if output.get("output_type") == "stream" else None
    if not isinstance(text, str) or ("\r" not in text and "\b" not in text):
        return output
    return {**output, "text": text.replace("\r", "").replace("\b", "")}


def doc_outputs(
    outputs: list[dict[str, Any]], starts: list[int | None] | None = None
) -> list[dict[str, Any]]:
    """What the notebook stores for these kernel outputs (streams as JupyterLab shows them)."""
    capped = cap_outputs_for_doc(strip_transient(outputs), starts)
    return sanitize_for_doc([_as_shown(output) for output in capped])


def output_summary(outputs: list[dict[str, Any]]) -> OutputSummary:
    """Cheap summary for the outline: types, first error, first text line, image count. No decoding."""
    summary = OutputSummary(count=len(outputs))
    for output in outputs:
        kind = str(output.get("output_type") or "")
        if kind and kind not in summary.types:
            summary.types.append(kind)
        if kind == "error" and summary.error is None:
            summary.error = f"{output.get('ename', '')}: {output.get('evalue', '')}"[:160]
        text = ""
        if kind == "stream":
            text = output.get("text") or ""
        elif kind in ("execute_result", "display_data"):
            data = output.get("data") or {}
            summary.images += sum(1 for mime in IMAGE_MIMES if mime in data)
            text = data.get("text/plain") or ""
        if isinstance(text, list):
            text = "".join(text)
        if text.strip() and not summary.text_head:
            summary.text_head = text.strip()[:200]
    return summary
