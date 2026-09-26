"""Fixtures for nhctl tests: each CLI test runs under the system Python 3.9 and the venv's 3.13."""

from __future__ import annotations

from pathlib import Path

import pytest
from nhctl_testlib import PYTHONS, Env


@pytest.fixture(params=PYTHONS)
def python(request) -> str:
    return request.param


@pytest.fixture
def env(tmp_path: Path, python: str) -> Env:
    return Env(tmp_path, python)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "sales_study"
    path.mkdir(parents=True)
    return path
