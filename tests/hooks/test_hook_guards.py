"""PreToolUse file and shell guards, run through the exact hooks.json command lines on 3.9."""

from __future__ import annotations

import pytest
from hookenv import Sandbox, needs_system_python

pytestmark = needs_system_python


def test_edit_of_project_notebook_is_denied(sandbox: Sandbox) -> None:
    nb = sandbox.project / "notebooks" / "01_eda.ipynb"
    run = sandbox.tool("Edit", {"file_path": str(nb), "old_string": "a", "new_string": "b"})
    assert run.returncode == 0
    assert run.decision == "deny"
    assert "nh_add_cell" in run.reason and "nhctl notebook new" in run.reason
    assert run.output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": run.reason,
        }
    }


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("NotebookEdit", {"notebook_path": "{proj}/notebooks/a.ipynb", "new_source": "x = 1"}),
        ("Write", {"file_path": "{proj}/notebooks/UPPER.IPYNB", "content": "{}"}),
        ("Write", {"file_path": "notebooks/relative.ipynb", "content": "{}"}),
        ("MultiEdit", {"file_path": "{proj}/notebooks/a.ipynb", "edits": []}),
        ("Write", {"file_path": "{proj}/.ipynb_checkpoints/a-checkpoint.ipynb", "content": ""}),
    ],
)
def test_notebook_writes_are_denied(sandbox: Sandbox, tool: str, tool_input: dict) -> None:
    proj = str(sandbox.project)
    filled = {
        k: v.replace("{proj}", proj) if isinstance(v, str) else v for k, v in tool_input.items()
    }
    run = sandbox.tool(tool, filled)
    assert run.decision == "deny", run.stderr
    assert "notebook" in run.reason


def test_write_through_symlinked_project_path_is_denied(sandbox: Sandbox) -> None:
    link = sandbox.tmp / "link-to-proj"
    link.symlink_to(sandbox.project)
    run = sandbox.tool("Write", {"file_path": f"{link}/notebooks/a.ipynb", "content": "{}"})
    assert run.decision == "deny"


def test_notebook_symlinked_in_from_outside_is_denied(sandbox: Sandbox) -> None:
    outside = sandbox.tmp / "shared" / "eda.ipynb"
    outside.parent.mkdir()
    outside.write_text("{}")
    link = sandbox.project / "notebooks" / "eda.ipynb"
    link.symlink_to(outside)
    assert sandbox.tool("Write", {"file_path": str(link), "content": "{}"}).decision == "deny"
    assert sandbox.tool("Write", {"file_path": str(outside), "content": "{}"}).stdout == ""


def test_edit_through_a_symlink_whose_name_hides_the_notebook_is_denied(
    sandbox: Sandbox,
) -> None:
    """Review finding 46: the shim's text pre-filter used to skip Python for this path."""
    notebook = sandbox.project / "notebooks" / "01_eda.ipynb"
    notebook.write_text("{}")
    link = sandbox.project / "notebooks" / "current"
    link.symlink_to(notebook.name)
    tool_input = {"file_path": str(link), "old_string": '"cells"', "new_string": '"cells"'}
    run = sandbox.tool("Edit", tool_input)
    assert run.decision == "deny", run.stderr
    assert "nh_add_cell" in run.reason


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_write_through_a_symlinked_dir_into_nh_state_is_denied(sandbox: Sandbox, tool: str) -> None:
    """Review finding 46: a link named state -> .nh/state hid .nh/ from the pre-filter."""
    (sandbox.nh / "state").mkdir()
    link = sandbox.project / "state"
    link.symlink_to(".nh/state")
    target = str(link / "last_cell.json")
    run = sandbox.tool(tool, {"file_path": target, "content": "{}", "edits": []})
    assert run.decision == "deny", run.stderr
    assert "nh's own state" in run.reason


def test_notebook_edit_through_a_symlink_is_denied(sandbox: Sandbox) -> None:
    (sandbox.project / "notebooks" / "01_eda.ipynb").write_text("{}")
    link = sandbox.project / "latest"
    link.symlink_to("notebooks/01_eda.ipynb")
    run = sandbox.tool("NotebookEdit", {"notebook_path": str(link), "new_source": "x = 1"})
    assert run.decision == "deny"


def test_writes_under_nh_state_are_denied(sandbox: Sandbox) -> None:
    target = sandbox.nh / "state" / "last_cell.json"
    run = sandbox.tool("Write", {"file_path": str(target), "content": "{}"})
    assert run.decision == "deny"
    assert ".nh/" in run.reason and "harness.toml" in run.reason


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("Edit", "{proj}/src/clean.py"),
        ("Write", "{proj}/harness.toml"),
        ("Write", "{tmp}/elsewhere/other.ipynb"),
        ("Read", "{proj}/data/raw/sales.csv"),
        ("Read", "{tmp}/elsewhere/other.ipynb"),
        ("Read", "{proj}/.nh/state/last_cell.json"),
    ],
)
def test_other_paths_are_allowed_silently(sandbox: Sandbox, tool: str, path: str) -> None:
    target = path.replace("{proj}", str(sandbox.project)).replace("{tmp}", str(sandbox.tmp))
    run = sandbox.tool(
        tool, {"file_path": target, "content": "", "old_string": "", "new_string": ""}
    )
    assert run.returncode == 0
    assert run.stdout == "", run.stdout


def test_read_of_project_notebook_points_to_nh_inspect(sandbox: Sandbox) -> None:
    nb = sandbox.project / "notebooks" / "01_eda.ipynb"
    run = sandbox.tool("Read", {"file_path": str(nb)})
    assert run.decision == "deny"
    assert 'nh_inspect(view="outline", notebook="notebooks/01_eda.ipynb")' in run.reason


@pytest.mark.parametrize(
    "command",
    [
        "jupyter nbconvert --to notebook --execute --inplace notebooks/01_eda.ipynb",
        "jupytext --sync notebooks/01_eda.py",
        "echo '{}' > notebooks/new.ipynb",
        "sed -i '' 's/foo/bar/' notebooks/01_eda.ipynb",
        "cp /tmp/x.ipynb notebooks/",
        "git status && git checkout -- notebooks/01_eda.ipynb",
        "python -c \"import nbformat; nbformat.write(nb, 'a.ipynb')\"",
        "rm -rf .nh/state",
        "cat x | tee notebooks/01_eda.IPYNB",
        "nhctl doctor --json; papermill in.ipynb out.ipynb",
        # Review finding 36: a bare .nh operand, which the shim's pre-filter let through.
        "rm -rf .nh",
        "mv .nh /tmp/old-nh",
        "cp -r backup .NH",
        # Review finding 45: nhctl exempts only its own words, not a whole segment.
        "nhctl doctor --json > notebooks/01_eda.ipynb",
        "nhctl doctor & cp /tmp/evil.json notebooks/01_eda.ipynb",
        "nhctl doctor & rm -rf .nh/state",
        "nhctl doctor & echo '{}' > .nh/state/last_cell.json",
        "nhctl doctor --json 2>&1 > notebooks/01_eda.ipynb",
        "nhctl lab start > .nh/logs/lab.txt",
        "nhctl notebook new $(cp /tmp/x.ipynb notebooks/01_eda.ipynb)",
        "nhctl notebook new `cp /tmp/x.ipynb notebooks/01_eda.ipynb` b",
        "`nhctl doctor` > notebooks/01_eda.ipynb",
        "(nhctl doctor) > notebooks/01_eda.ipynb",
        "(nhctl doctor; rm -rf .nh/state)",
        "sh /opt/nh/bin/nhctl doctor >> notebooks/01_eda.ipynb",
        # Redirections the redirect rule used to miss.
        "echo x >| notebooks/01_eda.ipynb",
        "echo x >& notebooks/01_eda.ipynb",
        "echo x &> notebooks/01_eda.ipynb",
        # Python writes keep working now that segments also split on a lone &.
        "python -c \"import json; json.dump(nb, open('a.ipynb', 'w'))\" &",
    ],
)
def test_shell_notebook_writes_are_denied(sandbox: Sandbox, command: str) -> None:
    run = sandbox.tool("Bash", {"command": command})
    assert run.decision == "deny", command
    assert "ask the user to run it themselves" in run.reason


@pytest.mark.parametrize(
    "command",
    [
        "ls notebooks/*.ipynb",
        "cat notebooks/01_eda.ipynb | head -c 200",
        "git status",
        "nhctl notebook new notebooks/02_model.ipynb",
        "cd sub && nhctl scaffold --adopt notebooks/01_eda.ipynb",
        "sh /opt/plugins/nh/bin/nhctl notebook new notebooks/03.ipynb",
        "uv run python -m pytest",
        # nhctl's own arguments may look like a rule; only they are exempt.
        "nhctl scaffold --adopt notebooks/touch-points.ipynb",
        'nhctl scaffold --adopt "$(pwd)/notebooks/touch-points.ipynb"',
        "nb=$(nhctl notebook new notebooks/cp-sales.ipynb --json)",
        "nhctl doctor --json 2>&1 | tail -5",
        "nhctl lab start --no-browser &",
        "ls -la .nh/state",
        "cat .nh/log.jsonl | tail -n 5",
    ],
)
def test_shell_reads_and_nhctl_are_allowed(sandbox: Sandbox, command: str) -> None:
    run = sandbox.tool("Bash", {"command": command})
    assert run.returncode == 0
    assert run.stdout == "", command


def test_powershell_notebook_write_is_denied(sandbox: Sandbox) -> None:
    run = sandbox.tool("PowerShell", {"command": "Set-Content notebooks/a.ipynb '{}'"})
    assert run.decision == "deny"
