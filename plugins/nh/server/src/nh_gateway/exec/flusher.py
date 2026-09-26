"""Loop-side output writer for a running cell (plan §4.4): at most one CRDT transaction every 200 ms.

Kernel messages arrive from the worker thread via ``call_soon_threadsafe`` and are folded into a
local nbformat output list. Each flush resolves the cell by id, writes only outputs that changed
since the last flush (a growing stream gets just its new text appended), and stores doc-safe,
capped copies (see ``docsafe``). After a reconnect the room is a new document, so the next flush
rewrites every output.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Protocol

from ..backend.kernel import output_hook
from .docsafe import doc_outputs, stream_start, strip_transient

log = logging.getLogger("nh_gateway.flusher")

PERIOD_S = 0.2


class OutputTarget(Protocol):
    generation: int

    @property
    def synced(self) -> bool: ...

    async def ensure(self) -> Any: ...

    def write_outputs(
        self, cell_id: str, outputs: list[dict[str, Any]], previous: list[dict[str, Any]] | None
    ) -> str: ...


class OutputFlusher:
    def __init__(self, target: OutputTarget, cell_id: str, period: float = PERIOD_S) -> None:
        self.target = target
        self.cell_id = cell_id
        self.period = period
        self.outputs: list[dict[str, Any]] = []
        self.missing = False  # the cell was deleted in JupyterLab: stop writing
        self._written: list[dict[str, Any]] | None = None
        # per output position: (the output dict, where its saved stream tail starts)
        self._starts: list[tuple[dict[str, Any], int] | None] = []
        self._generation = target.generation
        self._dirty = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"nh-flush:{self.cell_id}")

    def apply(self, message: dict[str, Any]) -> None:
        if output_hook(self.outputs, message):
            self._dirty = True

    def snapshot(self) -> list[dict[str, Any]]:
        """The outputs so far, as nbformat dicts (uncapped; what the tools layer shapes)."""
        return strip_transient(
            [o for o in self.outputs if o.get("output_type") != "_pending_clear"]
        )

    def flush(self, final: bool = False) -> bool:
        """Write pending outputs now. False when the room is disconnected (retry after reconnecting).

        ``final`` trims the last output, if it is a stream, to exactly its cap (while running, a
        capped stream may run up to ``STREAM_SLACK_CHARS`` over it so that flushes only append).
        Earlier streams keep their tail: JupyterLab would move a replaced output to the end.
        """
        if self.target.generation != self._generation:
            self._dirty = True  # a new room document: rewrite everything once
        current = [o for o in self.outputs if o.get("output_type") != "_pending_clear"]
        known = self._starts[-1] if self._starts else None
        if (
            final
            and known is not None
            and known[1] != stream_start(len(known[0].get("text") or ""))
        ):
            self._dirty = True
        if self.missing or not self._dirty:
            return True
        same_room = self.target.generation == self._generation
        desired = doc_outputs(current, self._stream_starts(current, final))
        state = self.target.write_outputs(
            self.cell_id, desired, self._written if same_room else None
        )
        if state == "unsynced":
            return False
        if state == "missing":
            self.missing = True
        else:
            self._written = desired
            self._generation = self.target.generation
        self._dirty = False
        return True

    def _stream_starts(
        self, outputs: list[dict[str, Any]], final: bool = False
    ) -> list[int | None]:
        """Where each stream's saved tail starts; it moves only when the stream outgrows the slack.

        ``final`` puts the last output's start back to exactly its cap.
        """
        starts: list[int | None] = []
        kept: list[tuple[dict[str, Any], int] | None] = []
        last = len(outputs) - 1
        for index, output in enumerate(outputs):
            if output.get("output_type") != "stream":
                starts.append(None)
                kept.append(None)
                continue
            known = self._starts[index] if index < len(self._starts) else None
            exact = final and index == last
            previous = known[1] if known is not None and known[0] is output and not exact else None
            text = output.get("text") or ""
            start = stream_start(len(text), previous)
            starts.append(start)
            kept.append((output, start))
        self._starts = kept
        return starts

    async def _reconnect(self) -> None:
        try:
            await self.target.ensure()
        except Exception as exc:  # the room may come back; keep the outputs and retry next period
            log.info("output flush for %s waiting for the room: %s", self.cell_id, exc)
            await asyncio.sleep(1.0)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.period)
            if not self.flush():
                await self._reconnect()

    async def close(self, reconnect_timeout: float = 10.0) -> None:
        """Stop the periodic writer and make a final flush (reconnecting once if needed)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.flush(final=True):
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.target.ensure(), reconnect_timeout)
        if not self.flush(final=True):
            log.warning(
                "final outputs of %s could not be saved: the room is disconnected", self.cell_id
            )
