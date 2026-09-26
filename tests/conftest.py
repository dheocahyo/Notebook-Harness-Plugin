"""Shared pytest setup: make the gateway sources importable without installing them."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "nh"
SERVER_SRC = PLUGIN / "server" / "src"

if str(SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(SERVER_SRC))
