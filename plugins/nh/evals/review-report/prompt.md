---
description: After /nh:review ran (the history file holds that message, the review skill it loaded, Claude's first line, its nhctl fresh-run --review call and that call's real result on this project), "What did the review find?" gets the report from the recorded JSON, the report file named, and no nh tool call, notebook read, nh- id or copy path. An eval can't run nhctl (no Bash in a run, design §6.10), so the run is recorded, and tests/unit/test_skill_files.py reruns it and checks the record.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 8
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: No nh tool call and no Read of the .ipynb; the reply names both failing cells ("Count priced orders per region" [4] with a NameError, "Add a revenue column" [3] with a KeyError for price), df_clean read in [4] before "Drop rows with missing price" [2] defines it, the two cells that ran out of order, "Check every column's values" [6] (45 lines) as a src/ candidate and each cell's intent under Load and Clean, and names .nh/reviews/20261010T101500-eda.md, with no nh- id and no .nh/tmp/ path.
---

What did the review find?
