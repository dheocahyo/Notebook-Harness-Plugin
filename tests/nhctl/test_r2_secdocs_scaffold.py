"""Round 2 (V5, X2): data-URL secrets beyond the query string, and the .jupyter/ ignore line."""

from __future__ import annotations

import stat
import tomllib

import pytest

from nh_gateway._shared.scaffold import core


def tools() -> core.Tools:
    return core.Tools(uv="/fake/uv", conda=None, mamba=None)


@pytest.mark.parametrize(
    ("url", "recorded", "secret"),
    [
        # a token in the path (Telegram bot API)
        (
            "https://api.telegram.org/bot123456:ABCdefSECRETtoken/getFile",
            "https://api.telegram.org/…/getFile",
            "ABCdefSECRETtoken",
        ),
        # a password with "/": urlsplit reads the path from there on, SQLAlchemy doesn't
        (
            "postgresql://analyst:ab/cdPW@db.example.com:5432/sales",
            "postgresql://…@db.example.com:5432/sales",
            "cdPW",
        ),
        (
            "mysql://analyst:12345#x@db.example.com/sales",
            "mysql://…@db.example.com/sales",
            "12345",
        ),
        (
            "postgresql://analyst:pw?x@db.example.com/sales",
            "postgresql://…@db.example.com/sales",
            "pw?x",
        ),
        (
            "postgresql://me:pw@db.local:5432/sales",
            "postgresql://db.local:5432/sales",
            "me:pw",
        ),
        # an "@" in an http path, and long random-looking path tokens
        (
            "https://files.example.com/share/me:s3cret@x/data.csv",
            "https://files.example.com/share/…/data.csv",
            "s3cret",
        ),
        (
            "https://hooks.slack.com/services/T0000/B0000/aBcDeFgHiJkLmNoPqRsTuVwX",
            "https://hooks.slack.com/services/T0000/B0000/…",
            "aBcDeFgHiJkLmNoPqRsTuVwX",
        ),
        (
            "https://files.example.com/s/123e4567-e89b-12d3-a456-426614174000/data.csv",
            "https://files.example.com/s/…/data.csv",
            "426614174000",
        ),
        (
            "https://example.com/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptl/export.csv",
            "https://example.com/d/…/export.csv",
            "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptl",
        ),
    ],
)
def test_secret_url_forms_are_not_recorded(tmp_path, url, recorded, secret):
    assert core.split_secret_url(url) == (recorded, True)
    report = core.scaffold(tmp_path, data=url, tools=tools())
    assert report.data["source"] == recorded and report.data["secret_in_env"] is True
    for name in ("harness.toml", "NOTEBOOK.md"):
        assert secret not in (tmp_path / name).read_text(encoding="utf-8"), name
    project = tomllib.loads((tmp_path / "harness.toml").read_text(encoding="utf-8"))["project"]
    assert project["data_source"] == recorded
    dotenv = tmp_path / ".env"
    assert url in dotenv.read_text(encoding="utf-8")
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/exports/orders_2023-01-01.parquet",
        "https://example.com/monthly_sales_by_region_2024/data.csv",
        "https://huggingface.co/datasets/u/n/resolve/main/data/train-00000-of-00001.parquet",
        "https://data.cityofnewyork.us/api/views/kku6-nxdu/rows.csv",
        "sqlite:///data/sales.db",
        "s3://bucket/2024/01/data.parquet",
    ],
)
def test_ordinary_urls_are_recorded_whole(url):
    assert core.split_secret_url(url) == (url, False)


def test_a_sqlite_url_keeps_its_three_slashes():
    assert core.split_secret_url("sqlite:///data/x.db?mode=ro") == ("sqlite:///data/x.db", True)


@pytest.mark.parametrize("adopt", [False, True])
def test_scaffold_ignores_the_collaboration_sessions_folder(tmp_path, adopt):
    """jupyter-collaboration writes .jupyter/collaboration_sessions.json into the server root."""
    if adopt:
        (tmp_path / "analysis.ipynb").write_text(
            '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}'
        )
    core.scaffold(tmp_path, tools=tools(), adopt="analysis.ipynb" if adopt else None)
    assert ".jupyter/" in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
