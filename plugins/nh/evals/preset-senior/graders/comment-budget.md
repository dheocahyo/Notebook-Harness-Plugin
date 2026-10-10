---
type: regex
target: mock_calls
pattern: '"code":\s*"(?:[^"\\]|\\.)*#(?:[^"\\]|\\.)*?\\n(?:[^"\\]|\\.)*?#'
match: not_contains
arm: both
weight: 2
---
