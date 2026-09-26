"""Jupyter Server REST calls (``requests``, blocking), run on a small dedicated thread pool.

The token travels in the Authorization header, never in a URL, and errors never carry it.
"""

from __future__ import annotations

import os


def allow_direct(host: str | None = None) -> None:
    """Keep localhost (and ``host``) traffic, and the token, away from any configured HTTP proxy."""
    wanted = ["localhost", "127.0.0.1", "::1"] + ([host] if host else [])
    for var in ("NO_PROXY", "no_proxy"):
        current = [item for item in os.environ.get(var, "").split(",") if item]
        missing = [item for item in wanted if item not in current]
        if missing:
            os.environ[var] = ",".join(current + missing)


allow_direct()  # before requests (and, via rtc/kernel, websockets) is imported

import asyncio  # noqa: E402
from collections.abc import Callable  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from typing import Any, TypeVar  # noqa: E402
from urllib.parse import quote, urlsplit  # noqa: E402

import requests  # noqa: E402

from ..policy.errors import scrub  # noqa: E402
from .base import ServerInfo  # noqa: E402

T = TypeVar("T")
TIMEOUT = 5.0


class ServerGone(Exception):
    """The server did not answer at all (refused, reset, timed out)."""


class RestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Rejected(RestError):
    """401/403: the server doesn't accept nh's token (typically it restarted with a new one)."""


def api_quote(path: str) -> str:
    return quote(path, safe="/")


class Rest:
    """REST client for one Jupyter server. Methods block; use :meth:`call` from the event loop."""

    def __init__(self, server: ServerInfo, pool: ThreadPoolExecutor | None = None) -> None:
        self.server = server
        self.base = server.url.rstrip("/")
        self._pool = pool
        # requests.Session isn't thread-safe; the pool runs several calls at once, so no session.
        self._headers = {"Authorization": f"token {server.token}"} if server.token else {}
        allow_direct(urlsplit(self.base).hostname)

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run a blocking call on the pool (never the loop's default executor, which long runs could fill)."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = TIMEOUT,
        ok: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> requests.Response:
        try:
            response = requests.request(
                method, self.base + path, headers=self._headers, timeout=timeout, **kwargs
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ServerGone(scrub(f"{type(exc).__name__}: {exc}")) from None
        if response.status_code >= 400 and response.status_code not in ok:
            try:
                message = response.json().get("message") or response.reason
            except ValueError:
                message = response.reason
            kind = Rejected if response.status_code in (401, 403) else RestError
            raise kind(response.status_code, scrub(str(message)))
        return response

    # ------------------------------------------------------------------ endpoints

    def status(self, timeout: float = 3.0) -> dict[str, Any]:
        return self.request("GET", "/api/status", timeout=timeout).json()

    def version(self, timeout: float = 3.0) -> str | None:
        return self.request("GET", "/api", timeout=timeout).json().get("version")

    def has_collaboration(self, timeout: float = 3.0) -> bool:
        """jupyter-collaboration serves PUT /api/collaboration/session/<path>; a GET there is 405, not 404."""
        response = self.request(
            "GET", "/api/collaboration/session/nh-probe.ipynb", timeout=timeout, ok=(404, 405)
        )
        return response.status_code == 405

    def sessions(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/sessions").json()

    def create_session(self, api_path: str, kernel_name: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "path": api_path,
            "type": "notebook",
            "name": api_path.rsplit("/", 1)[-1],
        }
        if kernel_name:
            body["kernel"] = {"name": kernel_name}
        return self.request("POST", "/api/sessions", json=body, timeout=30.0).json()

    def kernel(self, kernel_id: str) -> dict[str, Any] | None:
        """The kernel model, or None when the kernel no longer exists."""
        response = self.request("GET", f"/api/kernels/{kernel_id}", ok=(404,))
        return None if response.status_code == 404 else response.json()

    def interrupt(self, kernel_id: str) -> None:
        self.request("POST", f"/api/kernels/{kernel_id}/interrupt")

    def kernelspecs(self) -> dict[str, Any]:
        return self.request("GET", "/api/kernelspecs").json()

    def contents_exists(self, api_path: str) -> bool:
        response = self.request(
            "GET", f"/api/contents/{api_quote(api_path)}", params={"content": "0"}, ok=(404,)
        )
        return response.status_code != 404

    def collab_session(self, api_path: str) -> dict[str, Any]:
        """Open (or look up) the document's collaboration room: ``{format, type, fileId, sessionId}``."""
        return self.request(
            "PUT",
            f"/api/collaboration/session/{api_quote(api_path)}",
            json={"format": "json", "type": "notebook"},
        ).json()

    def extensions(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Installed JupyterLab extensions (advisory only: the PyPI manager may be slow)."""
        return self.request(
            "GET", "/lab/api/extensions", params={"per_page": "100"}, timeout=timeout
        ).json()

    def room_url(self, room: dict[str, Any]) -> str:
        """The collaboration websocket URL for a :meth:`collab_session` answer (carries the token)."""
        scheme = "wss" if self.base.startswith("https") else "ws"
        rest = self.base.split("://", 1)[1]
        query = f"sessionId={quote(str(room['sessionId']))}"
        if self.server.token:
            query += f"&token={quote(self.server.token)}"
        return f"{scheme}://{rest}/api/collaboration/room/{room['format']}:{room['type']}:{room['fileId']}?{query}"
