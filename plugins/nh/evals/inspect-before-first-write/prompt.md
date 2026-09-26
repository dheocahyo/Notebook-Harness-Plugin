---
description: Claude looks at the live notebook with nh_inspect before its first write of the session.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: nh_inspect is called before the single nh_add_cell call.
---

Drop the rows with missing price from df.
