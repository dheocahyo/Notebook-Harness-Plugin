"""Fixtures for the hook and launcher tests (helpers live in hookenv.py)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from hookenv import Sandbox


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: longer than 30 s")


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    box = Sandbox(tmp_path)
    yield box
    # Let a background sync started by a test finish before its directory goes away.
    lock = box.data / "sync.lock"
    deadline = time.monotonic() + 15
    while lock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
