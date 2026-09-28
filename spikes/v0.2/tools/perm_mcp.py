#!/usr/bin/env python3
"""Tiny stdio MCP server exposing one tool, `approve`, for --permission-prompt-tool.
Logs every request (full arguments + timestamp) as JSON lines to $PERM_LOG and answers
with behavior $PERM_BEHAVIOR (allow|deny)."""

import json
import os
import sys
import time

LOG = os.environ.get("PERM_LOG", "/dev/null")
BEHAVIOR = os.environ.get("PERM_BEHAVIOR", "deny")


def log(kind, data):
    with open(LOG, "a") as fh:
        fh.write(
            json.dumps(
                {"ts": time.time(), "iso": time.strftime("%H:%M:%S"), "kind": kind, "data": data}
            )
            + "\n"
        )


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOL = {
    "name": "approve",
    "description": "Permission prompt handler (spike logger).",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": pv,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "permlog", "version": "0.0.1"},
                    },
                }
            )
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            log("request", {"params": params})
            if BEHAVIOR == "allow":
                result = {"behavior": "allow", "updatedInput": args.get("input", {})}
            else:
                result = {"behavior": "deny", "message": "PERMLOG: denied by spike prompt tool"}
            log("response", result)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
                }
            )
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )


if __name__ == "__main__":
    main()
