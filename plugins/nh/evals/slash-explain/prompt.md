---
description: /nh:explain on an existing cell gets a numbered walkthrough in chat and changes nothing in the notebook.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: No nh_add_cell, nh_edit_cell, nh_run or nh_undo call and no notebook edit; a numbered walkthrough in chat of "Load raw data and check schema" [1] with its real numbers (43 rows, 6 columns, 6 missing prices).
---

/nh:explain "Load raw data and check schema" [1]
