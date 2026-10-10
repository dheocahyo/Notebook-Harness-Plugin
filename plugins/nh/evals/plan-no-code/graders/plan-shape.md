---
type: llm
focus: last_message
weight: 10
---

Check each claim against the reply:
1. It is a numbered list of 5 to 12 steps toward the user's goal: clean the sales data, then find which region brings in the most revenue and how that changes by month.
2. Each step is one action for one notebook cell and says what it shows (a table, a count, a chart).
3. No step shows code, a command or a constant: no code block, no method call such as df.dropna(), no install command, no setting such as TEST_SIZE = 0.2, no name in backticks.
4. No step loads or reads the data file: the notebook's cell [1], "Load raw data and check schema", already loads it.
5. It does not say it wrote, ran or changed a cell.
6. It ends by asking where to start: "go" for step 1, or a way to run several steps in one reply ("run the next N" or "run steps a-b").
PASS if all six claims hold. FAIL if any claim does not hold.
