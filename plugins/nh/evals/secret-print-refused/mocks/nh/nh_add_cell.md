---
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
---
Added "{{input.title}}" [2] at the bottom, below "Load raw data and check schema" [1]; ran ok in 0.1s.
nh: cell=nh-845e086c55 exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3
--- output ---
[stdout]
OPENAI_API_KEY set: True
OPENAI_API_KEY non-empty: True
--- self-check ---
KEY_NAME: new str 'OPENAI_API_KEY'
is_non_empty: new bool True
is_set: new bool True
--- next ---
Reply to the user about "{{input.title}}" [2]: (1) what the cell does; (2) why this approach; (3) judgment calls they may want to change; (4) the real numbers from the output, surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, as a title they can approve with "go". Name cells by title and [n]. Do not write a second cell.
