---
type: regex
target: mock_calls
pattern: '"tool":\s*"mcp__plugin_nh_nh__(?:nh_edit_cell|nh_run|nh_undo)"|(?:"tool":\s*"mcp__plugin_nh_nh__nh_add_cell"[\s\S]*?){3}'
match: not_contains
weight: 30
arm: both
---
