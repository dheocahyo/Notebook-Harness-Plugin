"""config.load: [lint.rules] levels, "ask" among them for the rules that can ask (design
§6.4), and headless(), with NH_HEADLESS forwarded by the plugin's .mcp.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nh_gateway import config
from nh_gateway._meta_rules import tool_meta

MCP_JSON = Path(__file__).resolve().parents[2] / "plugins" / "nh" / ".mcp.json"


def project(tmp_path: Path, toml: str) -> Path:
    (tmp_path / "harness.toml").write_text(f"version = 1\n{toml}")
    return tmp_path


RULE_KEYS = sorted(config.DEFAULTS["lint"]["rules"])
OTHER_KEYS = [key for key in RULE_KEYS if key not in config.ASK_RULES]


def test_the_levels() -> None:
    assert config.RULE_LEVELS == ("off", "hint", "error", "ask")
    assert {"package_install", "network", "outside_write"} == config.ASK_RULES
    assert set(RULE_KEYS) >= config.ASK_RULES
    assert config.DEFAULTS["lint"]["rules"]["package_install"] == "ask"
    assert config.DEFAULTS["lint"]["rules"]["network"] == "ask"
    assert config.DEFAULTS["lint"]["rules"]["outside_write"] == "ask"
    for key, level in config.DEFAULTS["lint"]["rules"].items():
        assert level in config.rule_levels(key), key
    assert config.rule_levels("package_install") == ("off", "hint", "error", "ask")
    assert config.rule_levels("network") == ("off", "hint", "error", "ask")
    assert config.rule_levels("outside_write") == ("off", "hint", "error", "ask")
    assert config.rule_levels("long_line") == ("off", "hint", "error")


@pytest.mark.parametrize("key", RULE_KEYS)
@pytest.mark.parametrize("level", ["off", "hint", "error"])
def test_every_rule_takes_off_hint_and_error(tmp_path: Path, key: str, level: str) -> None:
    cfg = config.load(project(tmp_path, f'[lint.rules]\n{key} = "{level}"\n'))
    assert cfg.problems == [] and cfg.rule(key) == level


@pytest.mark.parametrize("key", sorted(config.ASK_RULES))
def test_a_rule_that_can_ask_takes_ask(tmp_path: Path, key: str) -> None:
    cfg = config.load(project(tmp_path, f'[lint.rules]\n{key} = "ask"\n'))
    assert cfg.problems == [] and cfg.rule(key) == "ask"


@pytest.mark.parametrize("key", OTHER_KEYS)
def test_any_other_rule_refuses_ask_and_keeps_its_default(tmp_path: Path, key: str) -> None:
    """Its finding has no words for the user's question, and the cell key leaves out the note
    (design §6.4)."""
    cfg = config.load(project(tmp_path, f'[lint.rules]\n{key} = "ask"\n'))
    assert cfg.problems == [f"lint.rules.{key} must be off|hint|error"]
    assert cfg.rule(key) == config.DEFAULTS["lint"]["rules"][key] != "ask"


@pytest.mark.parametrize("bad", ["maybe", "ASK", "", "warn"])
@pytest.mark.parametrize(
    ("key", "levels"),
    [
        ("package_install", "off|hint|error|ask"),
        ("network", "off|hint|error|ask"),
        ("outside_write", "off|hint|error|ask"),
        ("long_line", "off|hint|error"),
    ],
)
def test_a_bad_level_falls_back_to_the_default_with_a_problem(
    tmp_path: Path, key: str, levels: str, bad: str
) -> None:
    cfg = config.load(project(tmp_path, f'[lint.rules]\n{key} = "{bad}"\n'))
    assert cfg.problems == [f"lint.rules.{key} must be {levels}"]
    assert cfg.rule(key) == config.DEFAULTS["lint"]["rules"][key]


def test_a_level_of_the_wrong_type_is_a_problem_too(tmp_path: Path) -> None:
    cfg = config.load(project(tmp_path, "[lint.rules]\npackage_install = 1\n"))
    assert cfg.problems == ["lint.rules.package_install must be str"]
    assert cfg.rule("package_install") == "ask"


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("0", False), ("", False), ("true", False)]
)
def test_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("NH_HEADLESS", value)
    assert config.headless() is expected
    root = project(tmp_path, "[approval]\napprove_before_run = true\n")
    assert config.load(root)["approval"]["approve_before_run"] is (not expected)


def test_headless_turns_approval_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project(tmp_path, "[approval]\napprove_before_run = true\n")
    monkeypatch.delenv("NH_HEADLESS", raising=False)
    assert config.headless() is False
    assert config.load(root)["approval"]["approve_before_run"] is True
    monkeypatch.setenv("NH_HEADLESS", "1")
    assert config.load(root)["approval"]["approve_before_run"] is False


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("", False)])
def test_mcp_json_forwards_nh_headless_to_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """Claude Code can pass a stdio server only an allowlisted environment plus the server's own
    `env` (CLAUDE_CODE_MCP_ALLOWLIST_ENV=1, the local-agent entrypoint), so .mcp.json forwards
    NH_HEADLESS itself; unset, `${NH_HEADLESS:-}` expands to "", which isn't headless in the
    gateway or its tools' _meta (design §6.4, "Spike V13")."""
    env = json.loads(MCP_JSON.read_text())["mcpServers"]["nh"]["env"]
    assert env["NH_HEADLESS"] == "${NH_HEADLESS:-}"
    monkeypatch.setenv("NH_HEADLESS", value)
    assert config.headless() is expected
    (tmp_path / ".nh").mkdir()
    meta = tool_meta(tmp_path, approve_before_run=True)
    assert ("anthropic/requiresUserInteraction" in meta["nh_add_cell"]) is (not expected)


# --- [turn] max_batch (design §6.3) ----------------------------------------------------------


def test_max_batch_defaults_to_5() -> None:
    assert config.DEFAULTS["turn"]["max_batch"] == 5
    assert config.load(None)["turn"]["max_batch"] == 5


@pytest.mark.parametrize("value", [1, 2, 7, 999])
def test_max_batch_takes_an_integer_of_at_least_1(tmp_path: Path, value: int) -> None:
    cfg = config.load(project(tmp_path, f"[turn]\nmax_batch = {value}\n"))
    assert cfg.problems == [] and cfg["turn"]["max_batch"] == value


@pytest.mark.parametrize(
    ("written", "problems"),
    [
        ("0", ["turn.max_batch must be an integer >= 1"]),
        ("-2", ["turn.max_batch must be an integer >= 1"]),
        ("2.5", ["turn.max_batch must be an integer >= 1"]),
        ("3.0", ["turn.max_batch must be an integer >= 1"]),
        ('"3"', ["turn.max_batch must be int"]),
        ("true", ["turn.max_batch must be int"]),
    ],
)
def test_any_other_max_batch_reads_as_5_with_a_problem(
    tmp_path: Path, written: str, problems: list[str]
) -> None:
    cfg = config.load(project(tmp_path, f"[turn]\nmax_batch = {written}\n"))
    assert cfg.problems == problems
    assert cfg["turn"]["max_batch"] == 5
