"""Drive the gateway the way Claude Code does: every write call is stamped by the real hook script."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "nh"
NH_HOOK = PLUGIN / "hooks" / "nh-hook"
QA_SCRIPT = PLUGIN / "workflows" / "qa-cell.js"
TOOL_PREFIX = "mcp__plugin_nh_nh__"
WRITER_AGENT = "nh:cell-writer"


class Turns:
    """Runs UserPromptSubmit and PreToolUse hooks for a project, then calls the in-memory client."""

    def __init__(self, project: Path, data: Path, session_id: str = "sess-1") -> None:
        self.project = project
        self.session_id = session_id
        self.transcripts = project.parent / "transcripts"  # Claude Code's session folders
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(project)),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_DATA": str(data),
            "NH_PYTHON": "/usr/bin/python3",
        }
        self._uses = 0

    def _hook(self, *args: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        proc = subprocess.run(
            ["/bin/sh", str(NH_HOOK), *args],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=self.env,
            cwd=str(self.project),
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr.decode()
        out = proc.stdout.decode().strip()
        return json.loads(out) if out else None

    def prompt(self, prompt_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        return self._hook(
            "prompt-submit",
            payload={
                "session_id": session_id or self.session_id,
                "prompt_id": prompt_id,
                "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "permission_mode": "default",
                "prompt": "…",
            },
        )

    def notification(
        self,
        prompt_id: str,
        *,
        tool_use_id: str | None = "toolu_launch",
        task_id: str = "w2g9v11xz",
        status: str = "completed",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """A background task's completion, as Claude Code submits it (a new prompt_id)."""
        return self._hook(
            "prompt-submit",
            payload={
                "session_id": session_id or self.session_id,
                "prompt_id": prompt_id,
                "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "permission_mode": "default",
                "prompt": notification_prompt(task_id, tool_use_id, status),
            },
        )

    def stamp(
        self,
        tool: str,
        args: dict[str, Any],
        prompt_id: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        agent_type: str | None = None,
        permission_mode: str = "default",
    ) -> dict[str, Any] | None:
        self._uses += 1
        payload = {
            "session_id": session_id or self.session_id,
            "prompt_id": prompt_id,
            "cwd": str(self.project),
            "hook_event_name": "PreToolUse",
            "permission_mode": permission_mode,
            "tool_name": TOOL_PREFIX + tool,
            "tool_input": args,
            "tool_use_id": f"toolu_{self._uses:04d}",
        }
        if agent_id:
            payload["agent_id"] = agent_id
            payload["agent_type"] = agent_type or "general-purpose"
        return self._hook("pre-tool", "nh", payload=payload)

    def transcript_dir(self, run_id: str, session_id: str | None = None) -> Path:
        """A workflow run's folder: its agents' transcripts and ``agent-<id>.meta.json`` files."""
        session = session_id or self.session_id
        return self.transcripts / session / "subagents" / "workflows" / run_id

    def workflow_launched(
        self,
        prompt_id: str,
        run_id: str = "wf_run-1",
        name: str = "nh:qa-cell",
        launched_by: str = "name",
        tool_use_id: str = "toolu_launch",
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> Path:
        """PostToolUse of a Workflow call Claude Code started in the background (spike S6).

        ``launched_by``: "name" passes nh's own script text, as Claude Code does for a launch
        by name; "script" another script; "scriptPath" a path and no script text. Creates and
        returns the run's transcript folder.
        """
        session = session_id or self.session_id
        folder = self.transcript_dir(run_id, session)
        folder.mkdir(parents=True, exist_ok=True)
        tool_input: dict[str, Any] = {"name": name, "args": "drop rows with missing price"}
        if launched_by == "name":
            tool_input["script"] = QA_SCRIPT.read_text()
        elif launched_by == "script":
            tool_input["script"] = "export const meta = { name: 'qa-cell' }\nreturn { ok: 1 }\n"
        else:
            tool_input = {"scriptPath": str(folder / "qa-cell.js"), "args": tool_input["args"]}
        payload = {
            "session_id": session,
            "prompt_id": prompt_id,
            "cwd": str(self.project),
            "hook_event_name": "PostToolUse",
            "permission_mode": "default",
            "tool_name": "Workflow",
            "tool_input": tool_input,
            "tool_response": {
                "status": "async_launched",
                "taskId": task_id or f"task-{run_id}",
                "taskType": "local_workflow",
                "workflowName": name.rsplit(":", 1)[-1],
                "runId": run_id,
                "summary": "Write this message's one notebook cell, then live-check it",
                "transcriptDir": str(folder),
                "scriptPath": str(folder / "script.js"),
            },
            "tool_use_id": tool_use_id,
        }
        if agent_id:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        self._hook("post-tool", "workflow", payload=payload)
        return folder

    def task_stopped(
        self,
        prompt_id: str,
        task_id: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """PostToolUse of a TaskStop that stopped a background task (it sends no notification)."""
        payload = {
            "session_id": session_id or self.session_id,
            "prompt_id": prompt_id,
            "cwd": str(self.project),
            "hook_event_name": "PostToolUse",
            "permission_mode": "default",
            "tool_name": "TaskStop",
            "tool_input": {"task_id": task_id},
            "tool_response": {
                "message": f"Successfully stopped task: {task_id} (Write and check the cell)",
                "task_id": task_id,
                "task_type": "local_workflow",
                "command": "Write and check the cell",
            },
            "tool_use_id": f"toolu_stop_{task_id}",
        }
        if agent_id:
            payload["agent_id"] = agent_id
            payload["agent_type"] = "general-purpose"
        self._hook("post-tool", "task-stop", payload=payload)

    def writer_meta(
        self,
        agent_id: str,
        run_id: str,
        *,
        agent_type: str = WRITER_AGENT,
        session_id: str | None = None,
    ) -> Path:
        """The ``agent-<id>.meta.json`` Claude Code writes before a workflow agent's first call."""
        folder = self.transcript_dir(run_id, session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"agent-{agent_id}.meta.json"
        meta = {
            "agentType": agent_type,
            "description": agent_type.rsplit(":", 1)[-1],
            "workflowPhase": "Write",
            "spawnDepth": 1,
            "requestShape": "foreground",
            "requestNonInteractive": True,
        }
        path.write_text(json.dumps(meta))
        return path

    def writer_stamp(
        self,
        agent_id: str,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        prompt_id: str,
        *,
        agent_type: str = WRITER_AGENT,
        session_id: str | None = None,
        permission_mode: str = "default",
    ) -> dict[str, Any] | None:
        """A workflow agent's nh call: its meta file in the run's folder, then the stamp hook."""
        self.writer_meta(agent_id, run_id, agent_type=agent_type, session_id=session_id)
        return self.stamp(
            tool,
            args,
            prompt_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_type=agent_type,
            permission_mode=permission_mode,
        )

    async def call(
        self, client: Any, tool: str, args: dict[str, Any], prompt_id: str, **stamp_kw: Any
    ) -> Any:
        if tool != "nh_inspect":
            self.stamp(tool, args, prompt_id, **stamp_kw)
        return await client.call_tool(tool, args, raise_on_error=False)

    async def writer_call(
        self,
        client: Any,
        agent_id: str,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        prompt_id: str,
        **stamp_kw: Any,
    ) -> Any:
        """``call`` as the workflow agent ``agent_id`` of run ``run_id`` (nh:cell-writer)."""
        if tool != "nh_inspect":
            self.writer_stamp(agent_id, run_id, tool, args, prompt_id, **stamp_kw)
        return await client.call_tool(tool, args, raise_on_error=False)


def notification_prompt(
    task_id: str = "w2g9v11xz",
    tool_use_id: str | None = "toolu_launch",
    status: str = "completed",
    result: str = '{"outcome":"ok","status":"ok"}',
) -> str:
    """One ``<task-notification>`` block shaped like Claude Code 2.1.282's (spike S6)."""
    lines = [
        "<task-notification>",
        f"<task-id>{task_id}</task-id>",
        f"<tool-use-id>{tool_use_id}</tool-use-id>" if tool_use_id else "",
        f"<output-file>/tmp/tasks/{task_id}.output</output-file>",
        f"<status>{status}</status>",
        '<summary>Dynamic workflow "Write and check the cell" completed</summary>',
        f"<result>{result}</result>",
        "<diagnostics>To re-run: Workflow({scriptPath: '/tmp/qa-cell.js'})</diagnostics>",
        "<usage><agent_count>2</agent_count><tool_uses>5</tool_uses></usage>",
        "</task-notification>",
    ]
    return "\n".join(line for line in lines if line)


def text(result: Any) -> str:
    return "\n".join(getattr(part, "text", "") for part in result.content)
