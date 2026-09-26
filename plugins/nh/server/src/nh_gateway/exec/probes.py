"""Read-only kernel probes (plan §4.5): stdlib scripts from ``nh_gateway/probes`` run silently in the user's kernel.

Each probe is sent base64-encoded and ``exec``'d in throwaway globals, with ``silent=True`` and
``store_history=False``: no history entry, no execution count, no names bound in ``user_ns``.
It answers with one ``display({PROBE_MIME: payload}, raw=True)``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from collections.abc import Callable
from functools import cache
from importlib import resources
from typing import Any

from ..backend import kernel

PROBE_MIME = "application/vnd.nh.probe+json"
PROBES = ("attach", "vars", "var")
LIBRARIES = {"var": ("vars",)}  # probes whose source needs another probe's helpers first
INTERRUPT_SETTLE_S = 5.0


@cache
def probe_source(name: str) -> str:
    if name not in PROBES:
        raise ValueError(f"unknown probe {name!r}")
    return resources.files("nh_gateway.probes").joinpath(f"{name}.py").read_text(encoding="utf-8")


def _exec_expr(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return f'exec(__import__("base64").b64decode("{encoded}").decode("utf-8"), _g)'


def probe_code(name: str, args: dict[str, Any]) -> str:
    """The one-statement kernel code that runs probe ``name`` with ``args`` bound as ``_A``."""
    probe_source(name)  # validates the name
    parts = []
    for library in LIBRARIES.get(name, ()):
        library_args = {**args, "_as_library": True}
        parts.append(f"_g.update(_A=__import__('json').loads({json.dumps(library_args)!r}))")
        parts.append(_exec_expr(probe_source(library)))
    parts.append(f"_g.update(_A=__import__('json').loads({json.dumps(args)!r}))")
    parts.append(_exec_expr(probe_source(name)))
    body = ", ".join(parts)
    # A lambda keeps `_g` out of the user namespace: the whole probe is one expression statement.
    return f"(lambda _g: ({body}, None)[-1])({{}})"


def extract_payload(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    for output in outputs:
        data = output.get("data") or {}
        if PROBE_MIME in data:
            payload = data[PROBE_MIME]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return (
                payload if isinstance(payload, dict) else {"error": "ProbeError: malformed answer"}
            )
    for output in outputs:
        if output.get("output_type") == "error":
            return {"error": f"{output.get('ename', 'Error')}: {output.get('evalue', '')}"}
    return {"error": "ProbeError: the kernel sent no answer"}


def run_probe(
    client: Any,
    name: str,
    args: dict[str, Any],
    timeout: float,
    lock: threading.Lock | None = None,
    interrupt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run a probe on a connected kernel websocket client. Blocking; call it off the event loop.

    When the probe itself is still running at the timeout, ``interrupt`` (the kernel's REST
    interrupt) stops it, so a look never keeps the user's kernel busy. A probe still queued
    behind someone else's cell is left alone: interrupting then would stop their cell.
    """
    try:
        code = probe_code(name, args)
    except (TypeError, ValueError) as exc:
        return {"error": f"ValueError: {exc}"}
    if lock is not None and not lock.acquire(timeout=timeout):
        return {"error": "KernelBusy: another probe is still running"}
    try:
        outputs: list[dict[str, Any]] = []
        request = kernel.KernelRequest(
            client, code, silent=True, on_message=lambda msg: kernel.output_hook(outputs, msg)
        )
        request.send()
        state = request.pump(lambda: request.sent_at + timeout)
        if state == "timeout" and request.started:
            grace = time.monotonic() + 0.1  # its answer may be arriving right now
            state = request.pump(lambda: grace)
        if state == "timeout":
            if request.started and interrupt is not None:
                with contextlib.suppress(Exception):
                    interrupt()
                settle = time.monotonic() + INTERRUPT_SETTLE_S
                request.pump(lambda: settle)  # swallow our KeyboardInterrupt and idle
                return {
                    "error": f"TimeoutError: no answer within {timeout:.1f}s; nh stopped its look"
                }
            return {
                "error": f"TimeoutError: no answer within {timeout:.1f}s (the kernel may be busy)"
            }
        if state != "idle":
            return {"error": f"KernelLost: {request.lost_reason or 'the kernel went away'}"}
        return extract_payload(outputs)
    finally:
        if lock is not None:
            lock.release()
