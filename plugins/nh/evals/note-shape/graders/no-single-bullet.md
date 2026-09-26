---
type: regex
target: mock_calls
pattern: '"notes":\s*\[\s*"(?:[^"\\]|\\.)*"\s*\]'
match: not_contains
arm: both
---
