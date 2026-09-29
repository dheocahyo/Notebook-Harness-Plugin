"""Shared pytest setup: make the gateway sources importable without installing them."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "nh"
SERVER_SRC = PLUGIN / "server" / "src"

if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))


@pytest.fixture(autouse=True)
def _no_installed_redactor():
    """Each test starts with nothing installed (patterns only) and no added values: the
    gateway, the hooks and nhctl install one per project (design §6.8)."""
    from nh_gateway._shared import secrets

    secrets.reset()
    yield
    secrets.reset()
