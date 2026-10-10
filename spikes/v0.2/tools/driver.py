#!/usr/bin/env python3
"""Stream-json driver for headless Claude Code spikes.

usage: driver.py --cwd DIR --out FILE --msg1 TEXT [--msg2 TEXT --trigger first_tool_use|tool_use_n
       --n N --delay S] [--quiet S] -- <claude args...>

Writes message 1 at once. Message 2 (optional) is written ``--delay`` seconds after the
N-th tool_use block (of any tool) appears in an assistant message on stdout. Every stdout
line, every stdin write and notes are logged with time.time_ns() to --out (JSONL).
stdin is closed after a ``result`` event followed by --quiet seconds of silence once all
messages have been sent.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time


def now():
    return time.time_ns()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--msg1", required=True)
    ap.add_argument("--msg2")
    ap.add_argument("--n", type=int, default=1, help="send msg2 after the n-th tool_use")
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--tool", default=None, help="count only tool_use blocks of this tool")
    ap.add_argument("--quiet", type=float, default=8.0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd

    out = open(a.out, "a", buffering=1)  # noqa: SIM115 - daemon reader threads write until exit
    lock = threading.Lock()

    def log(kind, **kw):
        with lock:
            out.write(json.dumps({"t_ns": now(), "kind": kind, **kw}) + "\n")

    env = dict(os.environ)
    proc = subprocess.Popen(
        cmd,
        cwd=a.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    log("started", pid=proc.pid, cmd=cmd)
    print(f"claude pid {proc.pid}", file=sys.stderr, flush=True)

    def send(text):
        msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }
        data = (json.dumps(msg) + "\n").encode()
        log("stdin_write_begin", text=text)
        proc.stdin.write(data)
        proc.stdin.flush()
        log("stdin_write_done", text=text)

    state = {
        "tool_uses": 0,
        "sent2": a.msg2 is None,
        "last_out": time.time(),
        "results": 0,
        "last_result": None,
        "closed": False,
    }

    def stderr_reader():
        for line in proc.stderr:
            log("stderr", line=line.decode("utf-8", "replace").rstrip("\n"))

    threading.Thread(target=stderr_reader, daemon=True).start()

    def delayed_send2():
        time.sleep(a.delay)
        send(a.msg2)
        state["sent2"] = True

    def stdout_reader():
        for line in proc.stdout:
            text = line.decode("utf-8", "replace").rstrip("\n")
            state["last_out"] = time.time()
            try:
                d = json.loads(text)
            except ValueError:
                log("stdout_raw", line=text)
                continue
            log("stdout", data=d)
            if d.get("type") == "assistant":
                for block in d.get("message", {}).get("content", []) or []:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and (a.tool is None or block.get("name") == a.tool)
                    ):
                        state["tool_uses"] += 1
                        if (
                            not state["sent2"]
                            and state["tool_uses"] == a.n
                            and not state.get("scheduled")
                        ):
                            state["scheduled"] = True
                            log("note", msg=f"tool_use #{a.n} seen; msg2 in {a.delay}s")
                            threading.Thread(target=delayed_send2, daemon=True).start()
            if d.get("type") == "result":
                state["results"] += 1
                state["last_result"] = time.time()
                if not state["sent2"] and a.n == 0 and not state.get("scheduled"):
                    state["scheduled"] = True
                    log("note", msg=f"result seen; msg2 in {a.delay}s")
                    threading.Thread(target=delayed_send2, daemon=True).start()

    reader = threading.Thread(target=stdout_reader, daemon=True)
    reader.start()
    send(a.msg1)
    deadline = time.time() + a.timeout
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.2)
        if (
            not state["closed"]
            and state["sent2"]
            and state["last_result"] is not None
            and time.time() - state["last_out"] >= a.quiet
        ):
            log("note", msg="closing stdin", results=state["results"])
            proc.stdin.close()
            state["closed"] = True
    if proc.poll() is None:
        log("note", msg="timeout; terminating")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    reader.join(5)
    log("exit", code=proc.returncode, results=state["results"], tool_uses=state["tool_uses"])
    print(
        f"exit {proc.returncode} results {state['results']} tool_uses {state['tool_uses']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
