---
type: regex
target: mock_calls
pattern: '"tool":\s*"mcp__plugin_nh_nh__(?:nh_add_cell|nh_edit_cell|nh_run|nh_undo)"'
match: not_contains
weight: 30
arm: both
---
