---
description: /nh:plan with a goal gets a numbered plan of 5-12 one-cell steps in chat, in plain words, and changes nothing. The goal is the one batch-asks-once and batch-stops-on-check-this plan in their history files.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: No nh_add_cell, nh_edit_cell, nh_run or nh_undo call (Write, Edit and NotebookEdit are withheld); the /nh:plan skill is registered; a numbered plan of 5-12 steps toward the goal, one cell each, with no code, command or constant and no step that loads the data again, ending with where to start ("go" for step 1, or "run the next N" / "run steps a-b").
---

/nh:plan clean the sales data, then find which region brings in the most revenue and how that changes by month
