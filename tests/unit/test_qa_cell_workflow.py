"""The nh:qa-cell workflow: its agents' contract, and qa-cell.js run under node with fake agents."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nh_gateway._shared.stamp_spec import TOOL_PREFIX

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nh"
AGENTS = PLUGIN / "agents"
SCRIPT = PLUGIN / "workflows" / "qa-cell.js"
WRITER = "nh:cell-writer"
QA = "nh:cell-qa"
PHASES = ["Write", "QA", "Revise"]
# Keys that would pin a model or effort, or widen what an agent may do, instead of inheriting.
FORBIDDEN_KEYS = {"model", "effort", "permissionMode", "hooks", "mcpServers"}
AGENT_OPTIONS = {"label", "phase", "agentType", "schema"}

TITLE = "Drop rows with missing price"
CELL_ID = "nh-7d41c9e2a5"
NEW_KERNEL = "NEW kernel: earlier variables are gone (the kernel restarted since nh's last call)."

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# Runs qa-cell.js the way Claude Code's workflow runtime does: the body in an async function with
# agent/phase/log/args/budget in scope, and no clock or randomness. Fake agents answer in order;
# "throw" makes that agent() call throw.
HARNESS = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...a) { if (a.length === 0) throw new Error("argless new Date()"); super(...a); }
  static now() { throw new Error("Date.now()"); }
};
Math.random = () => { throw new Error("Math.random()"); };
const source = fs.readFileSync(input.script, "utf8")
  .replace(/^export const meta = /m, "const meta = globalThis.__meta = ");
const replies = input.replies.slice();
const out = { calls: [], phases: [], logs: [], overrun: false };
async function agent(prompt, opts) {
  const seen = Object.assign({}, opts);
  if (seen.schema) seen.schema = seen.schema.required;
  out.calls.push({ prompt, opts: seen });
  if (!replies.length) { out.overrun = true; return null; }
  const reply = replies.shift();
  if (reply === "throw") throw new Error("the subagent died");
  return reply;
}
const run = new (async () => {}).constructor("agent", "phase", "log", "args", "budget", source);
const budget = { total: null, spent: () => 0, remaining: () => Infinity };
run(agent, (t) => out.phases.push(t), (m) => out.logs.push(m), input.args, budget).then(
  (result) => {
    Object.assign(out, { result, meta: globalThis.__meta, unused: replies.length });
    process.stdout.write(JSON.stringify(out));
  },
  (error) => { process.stderr.write(String((error && error.stack) || error)); process.exit(1); },
);
"""


def frontmatter(path: Path) -> dict[str, Any]:
    """Top-level keys of a Markdown file's YAML frontmatter; block lists become lists."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    head, sep, _ = text[4:].partition("\n---\n")
    assert sep, f"{path}: frontmatter is not closed"
    data: dict[str, Any] = {}
    key = ""
    for line in head.splitlines():
        if line[:1] not in ("", " ", "-") and ":" in line:
            key, _, rest = line.partition(":")
            key = key.strip()
            data[key] = rest.strip() or []
        elif line.strip().startswith("- ") and isinstance(data.get(key), list):
            data[key].append(line.strip()[2:].strip())
    return data


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- contract


def test_the_plugin_ships_exactly_the_writer_and_qa_agents():
    assert {p.name for p in AGENTS.iterdir()} == {"cell-writer.md", "cell-qa.md"}
    for path in AGENTS.glob("*.md"):
        front = frontmatter(path)
        assert front["name"] == path.stem and front["description"]
        assert path.read_text(encoding="utf-8")[4:].partition("\n---\n")[2].strip(), path


def test_no_skill_or_agent_pins_a_model_effort_or_privileges():
    paths = sorted((PLUGIN / "skills").glob("*/SKILL.md")) + sorted(AGENTS.glob("*.md"))
    assert len(paths) >= 5
    for path in paths:
        assert FORBIDDEN_KEYS.isdisjoint(frontmatter(path)), path


def test_the_writer_may_only_inspect_add_edit_and_run():
    front = frontmatter(AGENTS / "cell-writer.md")
    tools = ["nh_inspect", "nh_add_cell", "nh_edit_cell", "nh_run"]
    assert sorted(front["tools"]) == sorted(TOOL_PREFIX + tool for tool in tools)
    assert front["skills"] == ["nh:notebook"]
    assert front["maxTurns"] == "40"


def test_qa_may_only_inspect_and_read():
    front = frontmatter(AGENTS / "cell-qa.md")
    assert sorted(front["tools"]) == sorted([TOOL_PREFIX + "nh_inspect", "Read"])
    assert front["skills"] == ["nh:notebook"]


def test_the_script_sets_no_model_or_effort():
    text = script_text()
    assert "model:" not in text and "effort:" not in text
    assert not re.search(r"\b(?:model|effort)\s*:", text)
    assert not re.search(r"['\"](?:model|effort)['\"]\s*:", text)


def test_the_script_uses_no_clock_or_randomness():
    text = script_text()
    assert "Date.now" not in text and "Math.random" not in text
    assert not re.search(r"\bnew\s+Date\b(?!\s*\(\s*[^\s)])", text)


def test_meta_is_a_pure_literal_named_qa_cell():
    match = re.match(r"export const meta = (\{.*?\n\})\n", script_text(), flags=re.S)
    assert match, "qa-cell.js must begin with `export const meta = {...}`"
    literal = match.group(1)
    assert "`" not in literal and "..." not in literal
    rest = re.sub(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"", "S", literal)
    rest = re.sub(r"\b[A-Za-z_]\w*\s*:", "", rest)
    assert set(rest) <= set("{}[],S \n"), rest
    assert re.search(r"^  name: 'qa-cell',$", literal, flags=re.M)
    assert re.findall(r"title: '(\w+)'", literal) == PHASES


def test_names_match_the_gateway_constants():
    turn_record = pytest.importorskip("nh_gateway._shared.turn_record")
    assert (turn_record.WRITER_AGENT, turn_record.QA_AGENT) == (WRITER, QA)
    assert turn_record.WORKFLOW_NAME == "qa-cell"


# ---------------------------------------------------------------------------- node harness


def nh_result(
    status: str = "ok",
    *,
    revisions: tuple[int, int] | None = (0, 2),
    exec_count: int = 2,
    lead: tuple[str, ...] = (),
    newline: str = "\n",
) -> str:
    """An nh write result as a writer sees it: machine line with revisions, 'next' section last."""
    machine = f"nh: cell={CELL_ID} exec={exec_count} turn=1/1 retries=0/2 waits=0/2 undos=0/3"
    if revisions is not None:
        machine += f" revisions={revisions[0]}/{revisions[1]}"
    lines = [
        *lead,
        f'Added "{TITLE}" [{exec_count}] at the bottom; ran {status} in 0.4s.',
        machine,
        "--- output ---",
        "[stdout] rows: 43 -> 37",
        "--- self-check ---",
        "df_clean: new DataFrame 37×6 (from df 43×6); no nulls",
        "--- next ---",
        f'"{TITLE}" [{exec_count}] ran ok. Return this whole result to the workflow as your',
        "final answer; don't reply to the user.",
    ]
    return newline.join(lines)


def writer(status: str = "ok", *, lead: tuple[str, ...] = (), **kwargs: Any) -> dict[str, Any]:
    exec_count = kwargs.get("exec_count", 2)
    return {
        "status": status,
        "wrote": True,
        "result": nh_result(status, lead=lead, **kwargs),
        "changes": f"Wrote {TITLE} (exec {exec_count}).",
        "cell_title": TITLE,
        "cell_id": CELL_ID,
        "exec_count": exec_count,
        "notebook": "notebooks/eda.ipynb",
        "lead_lines": list(lead),
    }


def refused(code: str = "E112", *, lead: tuple[str, ...] = ()) -> dict[str, Any]:
    text = "\n".join(
        [
            *lead,
            f"Not written: nh refused this change ({code}).",
            f"nh: {code}",
            "Next: Return this refusal to the workflow as your final answer.",
            "Writer: return this refusal to the workflow; don't retry or reply to the user.",
        ]
    )
    return {
        "status": "refused",
        "wrote": False,
        "result": text,
        "changes": f"nh refused the edit with {code}.",
        "lead_lines": list(lead),
    }


def finding(severity: str = "major", what: str = "Rows with price 0 are kept") -> dict[str, str]:
    return {
        "severity": severity,
        "what": what,
        "evidence": "df_clean['price'].min() == 0",
        "fix": "Also drop rows with price <= 0.",
    }


def qa(verdict: str, *findings: dict[str, str], lead: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": f"QA {verdict}",
        "findings": list(findings),
        "numbers_checked": ["df_clean has 37 rows", "no nulls in price"],
        "lead_lines": list(lead),
    }


def run_script(replies: list[Any], **args: Any) -> dict[str, Any]:
    """Run qa-cell.js with fake agents answering ``replies`` in order; ``args=`` if given."""
    payload = {"script": str(SCRIPT), "replies": replies, **args}
    proc = subprocess.run(
        ["node", "-e", HARNESS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert not out["overrun"], "the script spawned more agents than the test expected"
    assert out["unused"] == 0, "the script spawned fewer agents than the test expected"
    assert [phase["title"] for phase in out["meta"]["phases"]] == PHASES
    assert out["meta"]["name"] == "qa-cell"
    for call in out["calls"]:
        assert set(call["opts"]) <= AGENT_OPTIONS, call["opts"]
        assert call["opts"]["agentType"] in (WRITER, QA)
        assert call["opts"]["phase"] in PHASES
        assert "--- next ---" not in call["prompt"]
    assert set(out["phases"]) <= set(PHASES)
    report = json.dumps(out["result"])
    assert "--- next ---" not in report
    assert not re.search(r"(?:^|\\n)(?:Next|Writer): ", report), report
    return out


def agent_types(out: dict[str, Any]) -> list[str]:
    return [call["opts"]["agentType"] for call in out["calls"]]


@needs_node
@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_one_round_when_qa_does_not_ask_for_a_revision(verdict: str):
    out = run_script([writer(), qa(verdict, finding("minor"))], args="drop rows with missing price")
    report = out["result"]
    assert agent_types(out) == [WRITER, QA]
    assert out["phases"] == ["Write", "QA"]
    assert report["outcome"] == "checked" and report["status"] == "ok"
    assert report["revisions"] == 0
    assert report["cell"] == {"title": TITLE, "exec": 2, "notebook": "notebooks/eda.ipynb"}
    assert report["qa"]["final_version_checked"] is True
    assert report["qa"]["verdict"] == verdict
    assert report["qa"]["open_findings"] == [finding("minor")]
    assert report["qa"]["earlier_findings"] == []
    assert report["qa"]["numbers_checked"] == ["df_clean has 37 rows", "no nulls in price"]
    assert report["result"].startswith(f'Added "{TITLE}" [2]')
    assert "--- output ---" in report["result"] and "--- self-check ---" in report["result"]
    assert report["changes"] == [f"Wrote {TITLE} (exec 2)."]
    assert f"cell_id for nh_inspect: {CELL_ID}." in out["calls"][1]["prompt"]
    assert "[stdout] rows: 43 -> 37" in out["calls"][1]["prompt"]


@needs_node
@pytest.mark.parametrize("most", [0, 1, 2, 3])
def test_revisions_stop_at_the_max_on_the_writers_machine_line(most: int):
    replies: list[Any] = [writer(revisions=(0, most))]
    for n in range(1, most + 1):
        replies += [qa("revise", finding()), writer(revisions=(n, most), exec_count=2 + n)]
    replies.append(qa("revise", finding("blocker", "Still keeps price 0")))
    out = run_script(replies, args={"ask": "drop rows with missing price"})
    report = out["result"]
    assert agent_types(out) == [WRITER] + [QA, WRITER] * most + [QA]
    assert report["revisions"] == most
    assert report["outcome"] == "checked"
    assert report["cell"]["exec"] == 2 + most
    assert report["qa"]["final_version_checked"] is True
    assert report["qa"]["verdict"] == "revise"
    assert report["qa"]["open_findings"] == [finding("blocker", "Still keeps price 0")]
    assert [r["verdict"] for r in report["qa"]["rounds"]] == ["revise"] * (most + 1)
    assert len(report["changes"]) == most + 1
    assert any("No revisions were left" in note for note in report["notes"])
    if most:
        revise = out["calls"][2]["prompt"]
        assert revise.startswith(f'Revise (1): change "{TITLE}" [2] with nh_edit_cell')
        assert (
            "Rows with price 0 are kept" in revise and "Also drop rows with price <= 0." in revise
        )
        # The next QA round checks the fixes it asked for.
        assert "check each" in out["calls"][3]["prompt"]
        assert "Rows with price 0 are kept" in out["calls"][3]["prompt"]


@needs_node
def test_the_round_cap_bounds_a_loop_without_a_machine_line():
    replies: list[Any] = [writer(revisions=None)]
    for n in range(1, 5):
        replies += [qa("revise", finding()), writer(revisions=None, exec_count=2 + n)]
    replies.append(qa("revise", finding()))
    report = run_script(replies, args="drop rows")["result"]
    assert report["revisions"] == 4 and len(report["qa"]["rounds"]) == 5
    assert report["qa"]["final_version_checked"] is True
    assert any("limit of 5 QA rounds" in note for note in report["notes"])


@needs_node
@pytest.mark.parametrize("status", ["error", "running", "aborted"])
def test_a_final_revision_that_is_not_ok_leaves_the_last_version_unchecked(status: str):
    replies = [writer(), qa("revise", finding()), writer(status, revisions=(1, 2), exec_count=3)]
    report = run_script(replies, args="drop rows")["result"]
    assert report["outcome"] == "not_checked"
    assert report["status"] == status and report["revisions"] == 1
    assert report["qa"]["final_version_checked"] is False
    assert report["qa"]["verdict"] == "unchecked"
    assert report["qa"]["open_findings"] == [] and report["qa"]["numbers_checked"] == []
    assert report["qa"]["earlier_findings"] == [finding()]
    assert report["qa"]["summary"] == ""
    assert report["qa"]["rounds"] == [{"round": 1, "verdict": "revise", "summary": "QA revise"}]
    assert any("one undo restores" in note for note in report["notes"])


@needs_node
@pytest.mark.parametrize("reply", [None, "throw"])
def test_a_writer_that_returns_nothing(reply: Any):
    report = run_script([reply], args="drop rows")["result"]
    assert report["outcome"] == "writer_failed"
    assert report["status"] is None and report["cell"] is None and report["result"] == ""
    assert report["qa"]["final_version_checked"] is False
    assert report["qa"]["verdict"] == "unchecked" and report["qa"]["rounds"] == []
    assert any("may or may not have been written" in note for note in report["notes"])
    if reply == "throw":
        assert any("cell-writer stopped: the subagent died" in note for note in report["notes"])


@needs_node
@pytest.mark.parametrize("reply", [None, "throw"])
def test_qa_that_returns_nothing(reply: Any):
    report = run_script([writer(), reply], args="drop rows")["result"]
    assert report["outcome"] == "not_checked" and report["status"] == "ok"
    assert report["qa"]["final_version_checked"] is False
    assert report["qa"]["verdict"] == "unchecked"
    assert report["qa"]["rounds"] == [] and report["qa"]["earlier_findings"] == []
    assert any("QA round 1 returned nothing" in note for note in report["notes"])


@needs_node
@pytest.mark.parametrize("reply", [None, "throw"])
def test_a_revision_that_returns_nothing_may_have_changed_the_cell(reply: Any):
    report = run_script([writer(), qa("revise", finding()), reply], args="drop rows")["result"]
    assert report["outcome"] == "not_checked"
    assert report["revisions"] == 0 and report["status"] == "ok"
    assert report["qa"]["final_version_checked"] is False
    assert report["qa"]["verdict"] == "unchecked"
    assert report["qa"]["earlier_findings"] == [finding()]
    assert any("may have changed after QA checked it" in note for note in report["notes"])


@needs_node
def test_a_refused_revision_keeps_the_checked_version():
    replies = [writer(), qa("revise", finding()), refused("E112")]
    report = run_script(replies, args="drop rows")["result"]
    assert report["outcome"] == "checked" and report["revisions"] == 0
    assert report["qa"]["final_version_checked"] is True
    assert report["qa"]["verdict"] == "revise"
    assert report["qa"]["open_findings"] == [finding()]
    assert (
        "The revision changed nothing: Not written: nh refused this change (E112)."
        in (report["notes"])
    )


@needs_node
@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (refused("E107"), "refused"),
        ({**refused("E103"), "status": "no_write", "result": ""}, "not_written"),
    ],
)
def test_a_writer_that_changed_nothing_is_not_checked(answer: dict[str, Any], outcome: str):
    report = run_script([answer], args="drop rows")["result"]
    assert report["outcome"] == outcome
    assert report["qa"]["verdict"] == "unchecked" and report["qa"]["rounds"] == []
    if outcome == "refused":
        # The refusal stays as data; its instructions to the writer are dropped.
        assert report["result"] == "Not written: nh refused this change (E107).\nnh: E107"


@needs_node
def test_qa_that_could_not_check_asks_for_no_revision():
    report = run_script([writer(), qa("unchecked", finding())], args="drop rows")["result"]
    assert report["outcome"] == "not_checked"
    assert report["qa"]["final_version_checked"] is False
    assert report["qa"]["verdict"] == "unchecked"
    assert report["qa"]["summary"] == "QA unchecked"
    assert report["qa"]["open_findings"] == [finding()]


@needs_node
def test_revise_with_only_minor_findings_revises_nothing():
    report = run_script([writer(), qa("revise", finding("minor"))], args="drop rows")["result"]
    assert report["revisions"] == 0 and report["outcome"] == "checked"
    assert any("no blocker or major finding" in note for note in report["notes"])


@needs_node
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"args": None},
        {"args": ""},
        {"args": "   "},
        {"args": {}},
        {"args": {"ask": " "}},
        {"args": []},
    ],
)
def test_no_ask_spawns_no_agent(args: dict[str, Any]):
    out = run_script([], **args)
    assert out["calls"] == [] and out["phases"] == []
    report = out["result"]
    assert report["outcome"] == "no_ask" and report["status"] is None
    assert report["qa"]["final_version_checked"] is False
    assert any("/nh:qa-cell <what the cell should do>" in note for note in report["notes"])


@needs_node
def test_a_string_is_the_ask():
    out = run_script([writer(), qa("pass")], args="  drop rows with missing price  ")
    prompt = out["calls"][0]["prompt"]
    assert prompt.startswith("Write: the one cell for the user's current message.")
    assert "The ask, in the user's words: drop rows with missing price\n" in prompt
    assert "Add it with nh_add_cell." in prompt
    assert "Notebook: the default one nh_inspect reports." in prompt
    assert "What the main conversation already knows" not in prompt


@needs_node
@pytest.mark.parametrize("as_json", [False, True])
def test_an_object_carries_ask_context_cell_and_notebook(as_json: bool):
    args = {
        "ask": "only keep 2024 orders",
        "context": "df_clean has order_date as datetime64",
        "cell": '"Drop rows with missing price" [2]',
        "notebook": "notebooks/eda.ipynb",
    }
    out = run_script([writer(), qa("pass")], args=json.dumps(args) if as_json else args)
    write, check = out["calls"][0]["prompt"], out["calls"][1]["prompt"]
    assert "The ask, in the user's words: only keep 2024 orders" in write
    assert 'existing nh cell "Drop rows with missing price" [2]: use nh_edit_cell' in write
    assert "Notebook: notebooks/eda.ipynb." in write and "Notebook: notebooks/eda.ipynb." in check
    assert "df_clean has order_date as datetime64" in write
    assert "df_clean has order_date as datetime64" in check


@needs_node
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_report_and_prompts_never_carry_a_next_block(newline: str):
    replies = [
        writer(newline=newline),
        qa("revise", finding()),
        writer(revisions=(1, 2), exec_count=3, newline=newline),
        qa("pass"),
    ]
    out = run_script(replies, args="drop rows")
    report = out["result"]
    assert report["revisions"] == 1 and report["outcome"] == "checked"
    assert report["result"].endswith("df_clean: new DataFrame 37×6 (from df 43×6); no nulls")
    assert "revisions=1/2" in report["result"]


@needs_node
def test_lead_lines_come_from_every_agent_once():
    other = "Kernel ≠ notebook: df_old still holds the undone cell's result."
    replies = [
        writer(lead=(NEW_KERNEL,)),
        qa("revise", finding(), lead=(NEW_KERNEL, other)),
        refused("E107", lead=("NEW kernel: again.",)),
    ]
    report = run_script(replies, args="drop rows")["result"]
    assert report["lead_lines"] == [NEW_KERNEL, other, "NEW kernel: again."]
    assert (
        "The revision changed nothing: Not written: nh refused this change (E107)."
        in (report["notes"])
    )


# ---------------------------------------------------------------------------- gateway texts


def config_max_revisions() -> int:
    from nh_gateway import config

    return int(config.DEFAULTS["turn"]["max_revisions"])


def gateway_result(revisions: int, exec_count: int) -> str:
    """A writer's OK result built from the gateway's own machine line and writer next block."""
    from nh_gateway import config, render
    from nh_gateway.policy.turn import TurnState
    from nh_gateway.tools.common import machine_line

    state = TurnState(
        session_id="s",
        prompt_id="p1",
        opened_at=0.0,
        claims=[CELL_ID],
        revisions={CELL_ID: revisions},
    )
    label = f'"{TITLE}" [{exec_count}]'
    body = render.next_block("ok", retries_left=2, waits_left=2, cell=label, audience="writer")
    return "\n".join(
        [
            f"Changed {label}; ran ok in 0.4s.",
            machine_line(CELL_ID, exec_count, state, config.DEFAULTS, writer=True),
            "--- output ---",
            "[stdout] rows: 43 -> 37",
            "--- next ---",
            body,
        ]
    )


def test_the_fake_machine_line_is_the_gateways():
    most = config_max_revisions()
    for done in range(most + 1):
        fake = nh_result(revisions=(done, most)).splitlines()[1]
        assert fake == gateway_result(done, 2).splitlines()[1]


@needs_node
def test_revisions_stop_at_the_gateways_max_revisions():
    most = config_max_revisions()
    replies: list[Any] = [dict(writer(), result=gateway_result(0, 2))]
    for n in range(1, most + 1):
        answer = dict(writer(exec_count=2 + n), result=gateway_result(n, 2 + n))
        replies += [qa("revise", finding()), answer]
    replies.append(qa("revise", finding("blocker", "Still keeps price 0")))
    report = run_script(replies, args="drop rows")["result"]
    assert report["revisions"] == most and report["outcome"] == "checked"
    assert any("No revisions were left" in note for note in report["notes"])
    assert "Return this whole result" not in report["result"]


@needs_node
@pytest.mark.parametrize("writer_line", [False, True])
def test_a_gateway_refusal_reaches_the_report_without_its_instructions(writer_line: bool):
    from nh_gateway.policy.errors import RETURN_TO_WORKFLOW, WRITER_LINE, NhError

    if writer_line:  # a catalogue Next line, then the line the gate adds for the writer
        error = NhError("E113", cell=f'"{TITLE}" [2]', other='"Load sales data" [1]')
        refusal = f"{error}\n{WRITER_LINE}"
    else:
        refusal = str(NhError("E107", next_step=RETURN_TO_WORKFLOW))
    answer = dict(refused(), result=refusal)
    report = run_script([answer], args="drop rows")["result"]
    assert report["outcome"] == "refused"
    assert report["result"] == "\n".join(refusal.splitlines()[:2])
