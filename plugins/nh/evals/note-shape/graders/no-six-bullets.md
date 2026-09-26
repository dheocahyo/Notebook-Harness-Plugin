---
type: regex
target: mock_calls
pattern: '"notes":\s*(?:\[\s*(?:"(?:[^"\\]|\\.)*"\s*,\s*){5,}"|"(?:(?:[^"\\]|\\[^n])+\\n){5,})'
match: not_contains
arm: both
---
