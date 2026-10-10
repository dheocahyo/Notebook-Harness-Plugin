---
description: After the plan /nh:plan gave (the history file holds that exchange), "run the next 3" gets one question in chat, whether to run steps 1-3 in one reply, and no write. The prompt hook sees this message itself and adds its ask part.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: No nh_add_cell, nh_edit_cell, nh_run or nh_undo call; one question, whether to run steps 1-3 (Count orders per region, Drop duplicate orders, Drop rows with missing price) in one reply.
---

run the next 3
