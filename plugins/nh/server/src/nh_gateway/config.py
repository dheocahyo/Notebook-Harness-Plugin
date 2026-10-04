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
# A [lint.rules] level; "ask" holds the cell for the user's yes (design §6.4).
RULE_LEVELS = ("off", "hint", "error", "ask")
# The rules that can ask: each one's finding carries the user's question (design §6.4).
ASK_RULES = frozenset({"package_install", "network", "outside_write"})
HEADLESS_ENV = "NH_HEADLESS"


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
        levels = rule_levels(key)
        if value not in levels:
            problems.append(f"lint.rules.{key} must be {'|'.join(levels)}")
            data["lint"]["rules"][key] = DEFAULTS["lint"]["rules"].get(key, "hint")
    # The most steps one approved batch writes (design §6.3): an integer of at least 1.
    max_batch = data["turn"]["max_batch"]
    if isinstance(max_batch, bool) or not isinstance(max_batch, int) or max_batch < 1:
        problems.append("turn.max_batch must be an integer >= 1")
        data["turn"]["max_batch"] = DEFAULTS["turn"]["max_batch"]
    return Config(data=data, problems=problems, source_mtime=mtime)


def rule_levels(key: str) -> tuple[str, ...]:
    """The levels a [lint.rules] key takes: "ask" only for a rule that can ask (design §6.4)."""
    return RULE_LEVELS if key in ASK_RULES else tuple(v for v in RULE_LEVELS if v != "ask")


def headless() -> bool:
    """``NH_HEADLESS=1``: a run with nobody to answer. Set it for an unattended ``claude -p``
    run: nothing sets it for you, and nh doesn't treat ``-p`` as headless by itself, since a
    print-mode run can still carry the user's next message (design §6.4, spike V13). No approval
    prompt, and no yes can arrive for a question nh asks, so E122 stands (design §6.4).
    ``.mcp.json`` forwards it; unset, it arrives as "", which isn't headless."""
    return os.environ.get(HEADLESS_ENV) == "1"


def _apply_env(data: dict[str, Any]) -> None:
    if headless():
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
