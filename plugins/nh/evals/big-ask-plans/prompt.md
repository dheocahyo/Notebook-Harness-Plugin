---
description: An end-to-end ask gets a 5-12 step plan of one-cell steps and no code.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: No nh write calls; a numbered plan of 5-12 one-cell steps without code, ending with a question about where to start.
---

Build a model that predicts price from the other columns in data/sales.csv, end to end, and tell me how accurate it is.
