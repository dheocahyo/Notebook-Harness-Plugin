---
description: A three-step ask gets exactly one nh cell (the first step); the reply reports its real numbers and proposes the rest.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_add_cell call that drops the rows with a missing price; the reply cites 37 of 43 rows and offers the plot and the save as next steps.
---

Drop the rows where price is missing, then plot the mean price per region, then save the cleaned data to data/processed/sales_clean.csv.
