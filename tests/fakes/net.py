"""A network that serves only the pages a test names: ``urllib.request.urlopen`` (which pandas
reads URLs with) answering them and refusing any other URL, so no test reaches the network. The
eval mocks' drift test and the gateway's approval tests share it."""

from __future__ import annotations

import contextlib
import email.message
import io
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Iterator
from typing import Any


@contextlib.contextmanager
def serving(pages: dict[str, str]) -> Iterator[None]:
    """While open, ``urlopen`` answers each URL in ``pages`` with its text and refuses the rest."""
    real = urllib.request.urlopen

    def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url not in pages:
            raise urllib.error.URLError(f"the test serves no {url}")
        body = io.BytesIO(pages[url].encode("utf-8"))
        return urllib.response.addinfourl(body, email.message.Message(), url, 200)

    urllib.request.urlopen = urlopen  # type: ignore[assignment]
    try:
        yield
    finally:
        urllib.request.urlopen = real
