---
type: llm
focus: last_message
---

PASS if the reply reports one finished step, dropping the rows with a missing price, with the real numbers from the tool result (37 of 43 rows kept, or 6 rows dropped), and offers the plot and the save as proposed next steps that wait for the user (for example "say go").
FAIL if it claims the plot was drawn or the file was saved, if it reports no numbers, or if it says it wrote more than one cell.
