"""Entry point for every nh hook: ``main.py <event> [variant]`` with the hook JSON on stdin.

Hooks are advisory (the gateway enforces every rule). Any failure is logged to
``.nh/logs/hooks.log`` and the hook exits 0 without output, so Claude Code carries on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.realpath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [HERE, os.path.join(ROOT, "server", "src")]

LOG_MAX_BYTES = 256 * 1024


def run(project: Path, event: str, variant: str) -> None:
    import common

    from nh_gateway._shared.paths import Layout

    payload = common.read_payload()
    layout = Layout(project)
    if event == "session-start":
        import session_start

        output = session_start.handle(layout, payload)
    elif event == "prompt-submit":
        import prompt_submit

        output = prompt_submit.handle(layout, payload)
    elif event == "pre-tool":
        import pre_tool

        output = pre_tool.handle(layout, payload, variant)
    elif event == "post-tool":
        import post_tool

        output = post_tool.handle(layout, payload, variant)
    else:
        output = None
    common.emit(output)


def log_failure(project: Path | None, event: str, variant: str) -> None:
    """Append the traceback to <project>/.nh/logs/hooks.log, starting over past 256 KB."""
    try:
        import re
        import time
        import traceback

        where = str(project or os.environ.get("NH_PROJECT_DIR") or "")
        if not where or not os.path.isdir(os.path.join(where, ".nh")):
            return
        logs = os.path.join(where, ".nh", "logs")
        os.makedirs(logs, exist_ok=True)
        path = os.path.join(logs, "hooks.log")
        try:
            mode = "w" if os.path.getsize(path) > LOG_MAX_BYTES else "a"
        except OSError:
            mode = "a"
        text = traceback.format_exc()
        try:  # the project's redactor (design §6.8)
            from nh_gateway._shared import secrets

            text = secrets.Redactor.for_project(Path(where)).redact(text)
        except BaseException:  # the redactor itself failed: at least the token pattern
            text = re.sub(r"(token=)[^&\s\"']+", r"\1[redacted:token]", text, flags=re.I)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, mode, encoding="utf-8", errors="replace") as handle:
            handle.write(f"{stamp} pid={os.getpid()} {event} {variant}\n{text}\n")
    except BaseException:
        pass


def main(argv: list[str]) -> None:
    event = argv[1] if len(argv) > 1 else ""
    variant = argv[2] if len(argv) > 2 else ""
    project: Path | None = None
    try:
        from nh_gateway._shared.paths import find_project

        project = find_project()
        if project is not None:
            run(project, event, variant)
    except BaseException:
        log_failure(project, event, variant)


if __name__ == "__main__":
    main(sys.argv)
