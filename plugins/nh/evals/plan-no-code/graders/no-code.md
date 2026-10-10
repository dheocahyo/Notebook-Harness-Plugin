---
type: regex
target: last_message
pattern: '`|\b(?:df|pd|np|plt|sns|sklearn)\.\w|\b[a-z]\w*\.[a-z]\w*_\w|\b[A-Za-z_]\w*\.[A-Za-z_]\w*\(|\b[A-Za-z_]\w*\[["\x27]|(?:^|\n)[ \t>*-]*(?:import|from [\w.]+ import) \w|\b(?:pip|uv pip|conda) install\b|\buv add\b|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+[ \t]*=[^=]'
match: not_contains
weight: 5
---
