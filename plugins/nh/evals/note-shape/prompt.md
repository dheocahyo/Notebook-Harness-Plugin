---
description: The note nh_add_cell builds has a title of at most 8 words and 2-5 plain bullets.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_add_cell call whose title passes the mock's expect guard and whose notes hold 2-5 bullets.
---

Add a cell that computes the correlation matrix of the numeric columns of df and shows it as a heatmap with a clear, descriptive title.
