---
type: regex
target: last_message
pattern: '(^|\n)[ \t]*(?:[-*+][ \t]+)?(?:#{1,6}[ \t]*)?(?:\*\*)?(?:step[ \t]*13(?!\d)|13(?:\*\*|[ \t]*(?:[.):](?!\d)|[—–]|-[ \t])))'
flags: i
match: not_contains
---
