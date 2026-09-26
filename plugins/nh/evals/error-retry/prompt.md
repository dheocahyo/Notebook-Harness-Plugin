---
description: A failing cell is fixed in place with nh_edit_cell, at most twice, and the reply says what failed and what changed.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_add_cell that fails on the impossible date 2024-02-30, then one or two nh_edit_cell retries on the same cell; the reply explains the failure and the fix.
---

Parse order_date as dates and count the orders per month.
