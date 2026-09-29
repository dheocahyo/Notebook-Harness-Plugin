---
description: A failing cell is fixed in place with nh_edit_cell, at most twice, and the reply says what failed and what changed. The prompt asks for a strict parse on purpose, so the first cell fails on the fixture's impossible date 2024-02-30. Which order the reply names, how the retry leaves it out and whether the counts match the output are not graded, since more graders would dilute the three that gate the case.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_add_cell whose strict parse fails on the impossible date 2024-02-30, then one or two nh_edit_cell retries on the same cell that leave order 1020 out; the reply explains the failure, the fix and the monthly counts.
---

Count the orders per month from order_date. Parse the whole column in one go with pd.to_datetime and format="%Y-%m-%d": no errors="coerce" and no try/except, so a bad date makes the cell fail loudly. If it does, leave that order out of the counts and tell me which one it was.
