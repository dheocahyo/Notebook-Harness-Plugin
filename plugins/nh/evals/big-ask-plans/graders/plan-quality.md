---
type: llm
focus: last_message
---

PASS if the reply is a numbered plan of 5 to 12 steps, each small enough for one notebook cell with one visible result, contains no code, and asks the user which step to start with (or to say go).
FAIL if it writes code, claims any step is already done, or gives fewer than 5 or more than 12 steps.
