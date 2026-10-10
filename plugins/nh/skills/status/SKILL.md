---
name: status
description: >-
  Check whether Notebook Harness (nh) works in this project: Claude Code
  version, uv or conda, the nh runtime and MCP server, the project's
  JupyterLab and collaboration versions, the kernel, and the turn hooks. Use
  when nh tools are missing or failing, or the user asks whether nh is set up.
allowed-tools:
  - Bash(nhctl doctor *)
  - mcp__plugin_nh_nh__nh_inspect
---

# /nh:status: is nh working here?

Read-only: change nothing, start nothing, install nothing.

1. Run `nhctl doctor --json`. If the shell can't find `nhctl`, say nh's `bin/`
   isn't on PATH (the plugin is disabled or needs a restart) and continue.
2. Call `nh_inspect(view="status")`. If the tool is deferred, load it with
   ToolSearch `select:mcp__plugin_nh_nh__nh_inspect`. If it is unavailable or
   errors, report that first: the fix is /mcp, pick plugin:nh:nh, Reconnect
   (the first start after installing can take about a minute while nh builds
   its runtime).
3. Print one line per check, `OK` or `FAIL` (the Preset row prints the
   level instead), and after each FAIL its fix (from the doctor's
   `problems[]` or the status view):

| Check | OK when |
|---|---|
| Claude Code | version 2.1.282 or newer |
| uv / conda | uv (0.10+) or conda found |
| nh runtime | its Python environment is ready |
| MCP server | `nh_inspect` answered |
| Project | a `.nh/` folder and `harness.toml` exist here (else: run /nh:init) |
| Preset | always: print the level, `junior` or `senior` (the doctor's `project.preset`), instead of OK; if `problems[]` has D171, or D131 saying harness.toml can't be parsed (nh then reads junior), print its message and fix |
| Project env | JupyterLab 4.6+ and jupyter-collaboration 5+ in the project env |
| JupyterLab | running from the project env, reachable (else: `nhctl lab start`) |
| Kernel | attached, Python, running from the project env |
| Hooks | a turn record from this message exists (else: hooks aren't running) |
| Deny rule | optional: `.claude/settings.json` blocks raw `.ipynb` edits |

4. End with the fix commands for the user to run, in order. Don't run them.
