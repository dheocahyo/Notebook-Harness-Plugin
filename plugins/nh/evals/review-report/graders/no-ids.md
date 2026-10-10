---
type: regex
target: last_message
pattern: 'nh-[0-9a-f]{10}|\.nh/tmp/'
match: not_contains
weight: 1
---
