---
type: regex
target: mock_calls
pattern: '"title":\s*"[^"\S]*(?:[^"\s]+\s+){8,}[^"\s]'
match: not_contains
arm: both
---
