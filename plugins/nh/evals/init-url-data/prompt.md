---
description: After /nh:init set the project up for a data URL (scaffold approved its host), the user asks in their own words for the data to be loaded and its schema shown; Claude writes whatever loader it chooses, nh writes it with no question because its only host is approved, and Claude reports what the cell showed. The add mock is an agent playing the gateway from the case's own description (mocks/nh/fixtures/nh-server.md), so the result shows what the run's own code prints, and a cell that also reached another host would get nh's question; test_skill_files replays the description's templates and worked examples on the real gateway. The overview mock is the real gateway's, for the scaffolded notebook.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: One nh_add_cell call whose code reads https://data.example.org/trips-2023.csv itself, written and run with no question from nh; the reply reports real facts from the run (such as 24 rows and 7 columns, or the missing distance_km and start_station values) and asks no permission to reach the network.
---

I set this project up with /nh:init for my 2023 bike trips, which live at https://data.example.org/trips-2023.csv. nh's tools weren't connected then, so it stopped before writing the loader cell. They're connected now: load the data and show me its schema.
