---
type: regex
target: last_message
pattern: '(^|\n)[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(?:step[ \t]*)?\d+(?:\*\*)?[.):][ \t]*(?:\*\*)?[ \t]*(?:re-?)?(?:load|read|import|open)\b'
flags: i
match: not_contains
---
