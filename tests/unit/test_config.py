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


@pytest.mark.parametrize("value", [2, 7, 20])
def test_max_batch_takes_an_integer_from_2_to_20(tmp_path: Path, value: int) -> None:
    """Design §6.3 (C6b): a batch is two or more steps, and the launch guard counts a batch's
    runs in the run registry, which keeps the session's last 20."""
    cfg = config.load(project(tmp_path, f"[turn]\nmax_batch = {value}\n"))
    assert cfg.problems == [] and cfg["turn"]["max_batch"] == value


RANGE = "turn.max_batch must be an integer from 2 to 20"


@pytest.mark.parametrize(
    ("written", "problems"),
    [
        ("0", [RANGE]),
        ("-2", [RANGE]),
        ("1", [RANGE]),
        ("21", [RANGE]),
        ("999", [RANGE]),
        ("2.5", [RANGE]),
        ("3.0", [RANGE]),
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


def test_a_harness_toml_that_isnt_utf8_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    """Before C6b's review the ``UnicodeDecodeError`` escaped ``load`` (it caught only
    ``OSError`` and TOML errors); now it reads as defaults with a problem, as a TOML error does."""
    (tmp_path / "harness.toml").write_bytes(b"version = 1\n[turn]\nmax_batch = 2\n# \xff\n")
    cfg = config.load(tmp_path)
    assert cfg["turn"]["max_batch"] == 5
    assert len(cfg.problems) == 1 and cfg.problems[0].startswith("harness.toml unreadable: ")
    assert "utf-8" in cfg.problems[0]


# --- [preset] level and its overlay (design §6.9) --------------------------------------------


def test_load_without_a_project_reads_the_junior_preset() -> None:
    """The load() trap (design §6.9): ``user`` was bound only for a project, and the overlay
    reads the level for ``load(None)`` too."""
    cfg = config.load(None)
    assert cfg.problems == []
    assert cfg["preset"]["level"] == "junior"
    assert cfg["lint"]["comment_ratio"] == 8


# defaults < the preset's overlay < explicit harness.toml keys < NH_* (design §6.0 e, §6.9):
# (harness.toml after `version = 1`, the level, comment_ratio).
PRECEDENCE = [
    ("", "junior", 8),
    ("[preset]\n", "junior", 8),
    ('[preset]\nlevel = "junior"\n', "junior", 8),
    ('[preset]\nlevel = "senior"\n', "senior", 16),
    ("[lint]\ncomment_ratio = 12\n", "junior", 12),
    ('[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = 8\n', "senior", 8),
    ('[preset]\nlevel = "junior"\n[lint]\ncomment_ratio = 16\n', "junior", 16),
    ('[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = 0\n', "senior", 0),
    ('[lint]\ncomment_ratio = 4\n[preset]\nlevel = "senior"\n', "senior", 4),
    ('[preset]\nlevel = "senior"\n[lint]\nmode = "strict"\n', "senior", 16),
]


@pytest.mark.parametrize(("toml", "level", "ratio"), PRECEDENCE)
def test_the_preset_overlay_sits_between_the_defaults_and_explicit_keys(
    tmp_path: Path, toml: str, level: str, ratio: int
) -> None:
    cfg = config.load(project(tmp_path, toml))
    assert cfg.problems == []
    assert cfg["preset"]["level"] == level
    assert cfg["lint"]["comment_ratio"] == ratio


def test_the_preset_changes_only_the_comment_ratio(tmp_path: Path) -> None:
    """The note stays a title plus 2-5 bullets in both presets (plan D9): the overlay touches
    [lint] comment_ratio and nothing else."""
    junior = config.load(project(tmp_path, '[preset]\nlevel = "junior"\n'))
    senior = config.load(project(tmp_path, '[preset]\nlevel = "senior"\n'))
    for section in config.DEFAULTS:
        if section in ("preset", "lint"):
            continue
        assert junior[section] == senior[section] == config.DEFAULTS[section], section
    assert {k: v for k, v in junior["lint"].items() if k != "comment_ratio"} == {
        k: v for k, v in senior["lint"].items() if k != "comment_ratio"
    }
    assert (junior["lint"]["comment_ratio"], senior["lint"]["comment_ratio"]) == (8, 16)


def test_nh_variables_stay_on_top_of_a_senior_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(
        tmp_path,
        '[preset]\nlevel = "senior"\n[approval]\napprove_before_run = true\n'
        '[jupyter]\nurl = "http://127.0.0.1:8890/"\n',
    )
    monkeypatch.setenv("NH_HEADLESS", "1")
    monkeypatch.setenv("NH_JUPYTER_URL", "http://10.0.0.5:8888/")
    cfg = config.load(root)
    assert cfg.problems == []
    assert cfg["approval"]["approve_before_run"] is False
    assert cfg["jupyter"]["url"] == "http://10.0.0.5:8888/"
    assert (cfg["preset"]["level"], cfg["lint"]["comment_ratio"]) == ("senior", 16)


LEVEL_PROBLEM = "preset.level must be junior|senior"


@pytest.mark.parametrize(
    "written",
    [
        '"expert"',
        '""',
        '"Senior"',
        '" senior"',
        '"junior "',
        "1",
        "1.5",
        "true",
        "16",
        "[]",
        "{}",  # a table: without _without_level, _merge would add "must be str" too
        '{ name = "senior" }',
        "1979-05-27",
        "1979-05-27T07:32:00Z",
    ],
)
def test_a_bad_level_reads_as_junior_with_one_problem(tmp_path: Path, written: str) -> None:
    """Any value but "junior" or "senior", non-strings too: one problem, not _merge's "must be
    str" as well (design §6.9)."""
    cfg = config.load(project(tmp_path, f"[preset]\nlevel = {written}\n"))
    assert cfg.problems == [LEVEL_PROBLEM]
    assert (cfg["preset"]["level"], cfg["lint"]["comment_ratio"]) == ("junior", 8)


def test_a_preset_level_table_reads_as_junior_with_one_problem(tmp_path: Path) -> None:
    cfg = config.load(project(tmp_path, "[preset.level]\nx = 1\n"))
    assert cfg.problems == [LEVEL_PROBLEM]
    assert (cfg["preset"]["level"], cfg["lint"]["comment_ratio"]) == ("junior", 8)


def test_a_bad_level_keeps_the_users_own_ratio(tmp_path: Path) -> None:
    cfg = config.load(project(tmp_path, '[preset]\nlevel = "expert"\n[lint]\ncomment_ratio = 3\n'))
    assert cfg.problems == [LEVEL_PROBLEM]
    assert (cfg["preset"]["level"], cfg["lint"]["comment_ratio"]) == ("junior", 3)


def test_a_preset_that_isnt_a_table_reads_as_junior(tmp_path: Path) -> None:
    cfg = config.load(project(tmp_path, 'preset = "senior"\n'))
    assert cfg.problems == ["preset must be a table"]
    assert (cfg["preset"]["level"], cfg["lint"]["comment_ratio"]) == ("junior", 8)


def test_preset_is_a_section_like_any_other_now(tmp_path: Path) -> None:
    """The preset left RESERVED_SECTIONS: another key in it is an unknown key."""
    cfg = config.load(project(tmp_path, '[preset]\nlevel = "senior"\nname = "x"\n'))
    assert cfg.problems == ["unknown key preset.name"]
    assert cfg["preset"]["level"] == "senior"
    reserved = config.load(project(tmp_path, '[guardrails]\nname = "x"\n'))
    assert reserved.problems == []


def test_reserved_sections_have_one_definition() -> None:
    from nh_gateway._shared import harness_toml

    assert config.RESERVED_SECTIONS is harness_toml.RESERVED_SECTIONS
    assert "preset" not in harness_toml.RESERVED_SECTIONS
    assert {"guardrails", "secrets", "libraries", "comprehension"} == harness_toml.RESERVED_SECTIONS


def test_the_overlay_sets_defaults_keys_of_the_same_type() -> None:
    from nh_gateway._shared import harness_toml

    assert config.DEFAULTS["preset"] == {"level": harness_toml.DEFAULT_LEVEL}
    assert harness_toml.PRESET_LEVELS == ("junior", "senior")
    assert set(harness_toml.PRESET_OVERLAY) == set(harness_toml.PRESET_LEVELS)
    for overlay in harness_toml.PRESET_OVERLAY.values():
        for section, keys in overlay.items():
            for key, value in keys.items():
                assert type(value) is type(config.DEFAULTS[section][key]), (section, key)
    assert harness_toml.PRESET_OVERLAY["junior"]["lint"]["comment_ratio"] == 8
    assert config.DEFAULTS["lint"]["comment_ratio"] == 8  # junior's is today's default
    assert harness_toml.PRESET_OVERLAY["senior"]["lint"]["comment_ratio"] == 16


@pytest.mark.parametrize(
    "toml",
    [toml for toml, _, _ in PRECEDENCE]
    + [
        '[preset]\nlevel = "expert"\n',
        "[preset]\nlevel = 2\n[lint]\ncomment_ratio = 2.5\n",
        '[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = true\n',
        '[preset]\nlevel = "senior"\n[lint]\ncomment_ratio = "4"\n',
        'preset = "senior"\n',
        'lint = 3\n[preset]\nlevel = "senior"\n',
    ],
)
def test_the_shared_reading_agrees_with_config_load(tmp_path: Path, toml: str) -> None:
    """``harness_toml.preset_level`` and ``comment_ratio`` (nhctl's doctor and preset, and C9b's
    SessionStart hook) read what ``config.load`` uses."""
    import tomllib

    from nh_gateway._shared import harness_toml

    root = project(tmp_path, toml)
    cfg = config.load(root)
    data = tomllib.loads((root / "harness.toml").read_text())
    level, valid = harness_toml.preset_level(data)
    assert level == cfg["preset"]["level"]
    assert valid is (
        LEVEL_PROBLEM not in cfg.problems and "preset must be a table" not in cfg.problems
    )
    ratio, set_by = harness_toml.comment_ratio(data)
    assert ratio == cfg["lint"]["comment_ratio"]
    explicit = isinstance(data.get("lint"), dict) and "comment_ratio" in data["lint"]
    taken = "lint.comment_ratio must be int" not in cfg.problems
    assert (set_by == "harness.toml") is (explicit and taken)
