#!/usr/bin/env python3
"""Spike V15's PreToolUse hook: asks (permissionDecision "ask") for a package install in Bash,
any WebFetch, and Claude's Write/Edit/MultiEdit of the project's harness.toml, each with its own
reason text, as C8's hooks will (plan D6). Everything else gets no decision.

Logs one JSON line per call to <project>/.spike/v15.jsonl: the time, the tool and which ask fired
(no command text, no prompt text). Python 3.9+ stdlib; always exits 0.
"""

import contextlib
import json
import os
import re
import sys
import time

REASONS = {
    "install": "[V15] nh asks: this command installs packages into an environment. Allow it?",
    "web": "[V15] nh asks: this fetches a page from outside the project. Allow it?",
    "harness": "[V15] nh asks: this changes harness.toml, nh's guardrail settings. Allow it?",
}
INSTALL = re.compile(
    r"\b(?:pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+pip\s+install|uv\s+add"
    r"|conda\s+install|mamba\s+install)\b"
)


def kind_of(payload, project):
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}
    if tool == "Bash" and INSTALL.search(str(args.get("command") or "")):
        return "install"
    if tool == "WebFetch":
        return "web"
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(args.get("file_path") or "")
        full = os.path.realpath(os.path.join(project, path))
        if full == os.path.realpath(os.path.join(project, "harness.toml")):
            return "harness"
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    kind = kind_of(payload, project)
    try:
        os.makedirs(os.path.join(project, ".spike"), exist_ok=True)
        with open(os.path.join(project, ".spike", "v15.jsonl"), "a", encoding="utf-8") as fh:
            record = {
                "t": round(time.time(), 3),
                "tool": payload.get("tool_name"),
                "ask": kind,
                "mode": payload.get("permission_mode"),
            }
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass
    if kind is None:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": REASONS[kind],
        }
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
