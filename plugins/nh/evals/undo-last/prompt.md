---
description: A review reply of "undo" calls nh_undo once and explains what still lives in the kernel.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_undo call and no new cell; the reply says df_clean is still in the kernel and how to rebuild it.
---

Dropping those rows was a mistake. Undo that cell.
