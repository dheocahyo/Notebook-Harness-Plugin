---
type: llm
focus: last_message
weight: 10
---

PASS if the reply asks the user one question: whether to run steps 1-3 of the plan (Count orders per region, Drop duplicate orders, Drop rows with missing price) in one reply. It may name or briefly describe those three steps, and it writes nothing.
FAIL if it says it wrote, ran or changed a cell, shows code, asks about other steps or another number of steps, asks more than one question, or asks nothing.
