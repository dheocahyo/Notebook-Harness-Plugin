"""Effective configuration: packaged defaults < <project>/harness.toml < NH_* environment."""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

RESERVED_SECTIONS = {"preset", "guardrails", "secrets", "libraries", "comprehension"}


def _packaged_defaults() -> dict[str, Any]:
    text = resources.files("nh_gateway").joinpath("defaults.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


DEFAULTS: dict[str, Any] = _packaged_defaults()


@dataclass
class Config:
    data: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    source_mtime: float | None = None

    def get(self, section: str, key: str) -> Any:
        return self.data[section][key]

    def rule(self, key: str) -> str:
        return self.data["lint"]["rules"].get(key, "hint")

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]


def _merge(base: dict[str, Any], override: dict[str, Any], path: str, problems: list[str]) -> None:
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in base:
            if not path and key in RESERVED_SECTIONS:
                continue
            problems.append(f"unknown key {where}")
            continue
        current = base[key]
        if isinstance(current, dict):
            if isinstance(value, dict):
                _merge(current, value, where, problems)
            else:
                problems.append(f"{where} must be a table")
        elif isinstance(current, bool) or isinstance(value, bool):
            if isinstance(current, bool) and isinstance(value, bool):
                base[key] = value
            else:
                problems.append(f"{where} must be {type(current).__name__}")
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
            base[key] = type(current)(value) if isinstance(current, float) else value
        elif isinstance(current, str) and isinstance(value, str):
            base[key] = value
        else:
            problems.append(f"{where} must be {type(current).__name__}")


def load(project: Path | None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    problems: list[str] = []
    mtime = None
    if project is not None:
        path = project / "harness.toml"
        try:
            mtime = path.stat().st_mtime
            user = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            user = {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            user = {}
            problems.append(f"harness.toml unreadable: {exc}")
        _merge(data, user, "", problems)
    _apply_env(data)
    for key, value in data["lint"]["rules"].items():
        if value not in ("off", "hint", "error"):
            problems.append(f"lint.rules.{key} must be off|hint|error")
            data["lint"]["rules"][key] = DEFAULTS["lint"]["rules"].get(key, "hint")
    return Config(data=data, problems=problems, source_mtime=mtime)


def _apply_env(data: dict[str, Any]) -> None:
    if os.environ.get("NH_HEADLESS") == "1":
        data["approval"]["approve_before_run"] = False
    if url := os.environ.get("NH_JUPYTER_URL"):
        data["jupyter"]["url"] = url


class ConfigCache:
    """Reload harness.toml when its mtime changes."""

    def __init__(self, project: Path | None) -> None:
        self.project = project
        self._config = load(project)

    def current(self) -> Config:
        if self.project is None:
            return self._config
        try:
            mtime = (self.project / "harness.toml").stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._config.source_mtime:
            self._config = load(self.project)
        return self._config
