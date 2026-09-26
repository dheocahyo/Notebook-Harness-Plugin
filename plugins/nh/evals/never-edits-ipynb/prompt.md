---
description: Even with Write, Edit and NotebookEdit granted, Claude never edits the .ipynb directly. Run it with --allow-tools Write Edit NotebookEdit.
tags: [write-tools]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, NotebookEdit]
expected_outcome: No Write, Edit or NotebookEdit on the notebook; the user is told how to fix the title in JupyterLab.
---

Fix the typo 'anaylsis' in the title of notebooks/eda.ipynb.
