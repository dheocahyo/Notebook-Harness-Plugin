#!/usr/bin/env python3
"""Merge a driver log and a hook log into one timeline and evaluate the D1 rule.

usage: analyze.py DRIVER_LOG HOOK_LOG [NH_LOG]

D1 (plan): a record exists and the call's prompt_id is not the record's turn_id, not an alias,
and not an earlier turn -> E102. Evaluated at each PreToolUse against the turn record as it
was at that moment (the logger's snapshot at hook start). "Earlier turn" is checked two ways:
  strict  = only fields the record has today: turn_id, aliases, earlier (keys and values)
  prev    = strict plus every turn_id any earlier record snapshot of the session held
            (what a prev_turn_id / turn-history field would give)
Also reports the current gate's E102 condition for a stamp written at PreToolUse time:
  record.ts > stamp.ts and canonical(prompt_id) != record.turn_id.
"""

import json
import sys


def load(path):
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def short(x):
    return (x or "None")[:8]


def main():
    drv = load(sys.argv[1])
    hooks = load(sys.argv[2])
    nhlog = load(sys.argv[3]) if len(sys.argv) > 3 else []
    events = []
    t0 = None
    for d in drv:
        if d["kind"] == "started":
            t0 = d["t_ns"]
        if d["kind"] == "stdin_write_done":
            events.append((d["t_ns"], "STDIN", f"user message written: {d['text']!r}"))
        elif d["kind"] == "stdout":
            x = d["data"]
            t = x.get("type")
            if t == "assistant":
                for b in x["message"].get("content", []):
                    if b.get("type") == "tool_use":
                        events.append(
                            (
                                d["t_ns"],
                                "OUT",
                                f"assistant tool_use {b['name']} {json.dumps(b.get('input'))[:60]} id={b['id'][-6:]}",
                            )
                        )
                    elif b.get("type") == "text":
                        events.append((d["t_ns"], "OUT", f"assistant text {b['text'][:60]!r}"))
            elif t == "user":
                c = x.get("message", {}).get("content")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_result":
                            events.append(
                                (
                                    d["t_ns"],
                                    "OUT",
                                    f"tool_result id={b.get('tool_use_id', '')[-6:]} {str(b.get('content'))[:40]!r}",
                                )
                            )
                        elif b.get("type") == "text":
                            events.append(
                                (d["t_ns"], "OUT", f"user echo text {b.get('text')[:60]!r}")
                            )
                else:
                    events.append((d["t_ns"], "OUT", f"user echo {str(c)[:60]!r}"))
            elif t == "result":
                events.append(
                    (
                        d["t_ns"],
                        "OUT",
                        f"RESULT {x.get('subtype')} {str(x.get('result'))[:50]!r} turns={x.get('num_turns')}",
                    )
                )
            elif t == "system" and x.get("subtype") not in (
                "thinking_tokens",
                "hook_started",
                "hook_response",
            ):
                events.append(
                    (
                        d["t_ns"],
                        "OUT",
                        f"system {x.get('subtype')} {json.dumps({k: v for k, v in x.items() if k not in ('type', 'subtype', 'uuid', 'session_id', 'tools', 'mcp_servers', 'slash_commands', 'skills', 'agents', 'plugins', 'model', 'cwd', 'apiKeySource', 'output_style', 'permissionMode', 'claude_code_version', 'memory_paths', 'fast_mode_state', 'analytics_disabled')})[:150]}",
                    )
                )
    session_records = []  # (t_ns, record) seen so far, for "prev"
    d1_hits = []
    gate_hits = []
    pretool_ids = []
    for h in hooks:
        if h["kind"] == "record_poll":
            events.append(
                (
                    h["t_ns"],
                    "POLL",
                    f"record names {short(h['prompt_id'])}: {h['found']} (after {h['polls']} polls)",
                )
            )
            continue
        p = h["payload"]
        ev = h["event"]
        pid = p.get("prompt_id")
        snap = h.get("turns_at_start") or {}
        sid = p.get("session_id")
        rec = None
        for entry in snap.values():
            data = entry.get("data") if isinstance(entry, dict) else None
            if isinstance(data, dict) and data.get("session_id") == sid:
                rec = data
        rec_s = (
            (
                f"record turn={short(rec.get('turn_id'))} aliases={[short(a) for a in rec.get('aliases') or []]} "
                f"earlier={ {short(k): short(v) for k, v in (rec.get('earlier') or {}).items()} } ts={rec.get('ts')}"
            )
            if rec
            else "no record"
        )
        if rec:
            session_records.append(rec)
        if ev == "UserPromptSubmit":
            events.append(
                (
                    h["t_ns"],
                    "HOOK",
                    f"UserPromptSubmit prompt={p.get('prompt')!r} prompt_id={short(pid)} | at hook start: {rec_s}",
                )
            )
        elif ev in ("PreToolUse", "PostToolUse"):
            ti = p.get("tool_input")
            extra = ""
            if ev == "PreToolUse":
                pretool_ids.append(pid)
                verdict = "-"
                if rec is not None:
                    known = {rec.get("turn_id")} | set(rec.get("aliases") or [])
                    earlier = rec.get("earlier") or {}
                    strict = known | set(earlier) | {v for v in earlier.values() if v}
                    prev = strict | {r.get("turn_id") for r in session_records if r.get("turn_id")}
                    d1_strict = pid not in strict
                    d1_prev = pid not in prev
                    # the current gate's E102 on a stamp written now (stamp.ts ~ hook time)
                    canon = rec.get("turn_id") if pid in known else earlier.get(pid, pid)
                    gate = (rec.get("ts") or 0) > h["t_ns"] / 1e9 and canon != rec.get("turn_id")
                    verdict = f"D1strict={'E102' if d1_strict else 'ok'} D1prev={'E102' if d1_prev else 'ok'} gate_now={'E102' if gate else 'ok'}"
                    if d1_strict or d1_prev:
                        d1_hits.append(
                            (
                                h["t_ns"],
                                p.get("tool_name"),
                                pid,
                                rec.get("turn_id"),
                                d1_strict,
                                d1_prev,
                            )
                        )
                    if gate:
                        gate_hits.append((h["t_ns"], p.get("tool_name"), pid))
                extra = f" | {rec_s} | {verdict}"
            events.append(
                (
                    h["t_ns"],
                    "HOOK",
                    f"{ev} {p.get('tool_name')} {json.dumps(ti)[:50]} id={str(p.get('tool_use_id'))[-6:]} prompt_id={short(pid)}{extra}",
                )
            )
    for n in nhlog:
        events.append(
            (
                int(n["ts"] * 1e9),
                "NHLOG",
                f"{n.get('event')} turn={short(n.get('turn_id'))} prompt={short(n.get('prompt_id'))} {json.dumps({k: v for k, v in n.items() if k not in ('v', 'ts', 'event', 'session_id', 'turn_id', 'prompt_id')})[:120]}",
            )
        )
    events.sort(key=lambda e: e[0])
    base = t0 or (events[0][0] if events else 0)
    for t, src, msg in events:
        print(f"{(t - base) / 1e9:8.3f}s {src:6} {msg}")
    print()
    print("PreToolUse prompt_ids in order:", [short(x) for x in pretool_ids])
    print("D1 hits:", d1_hits)
    print("current-gate E102 hits (stamp at PreToolUse time):", gate_hits)


if __name__ == "__main__":
    main()
