---
type: llm
focus: last_message
weight: 5
---

The user ran /nh:review, which ran a copy of their notebook in a fresh kernel; the review's report is below. They then asked "What did the review find?".
PASS if the reply reports all of these from the review: (1) the two failing cells, "Count priced orders per region" [4] with a NameError (df_clean is not defined) and "Add a revenue column" [3] with a KeyError for 'price'; (2) that "Count priced orders per region" [4] reads df_clean, which only the later cell "Drop rows with missing price" [2] defines; (3) the two cells that ran out of order, "Drop rows with missing price" [2] before "Count priced orders per region" [4] and "Add a revenue column" [3] before "Rename price to unit_price" [5]; (4) "Check every column's values" [6], 45 lines, as a candidate to move to src/; (5) under the headings Load and Clean, each of the six cells (by its title or [n]) with its intent, in the report's words or close to them; a cell's title alone is not its intent (for "Check every column's values" [6] the intent is to check each column for missing, repeated or impossible values). It may give the report's parts in its own words or order, explain why a cell fails, and suggest fixes.
FAIL if it misses any of (1)-(5), gives a finding to another cell than the report does, or states a number the review doesn't hold. The report's numbers are the date and time, 6 code cells, 4 ok, 2 failed, 6.2 s, the cells' [n] from 1 to 6, 40 and 45 lines; the review also gave its time as 6214 ms, and its deadline, 540 s, and its limit per cell, 180 s, count as held too.

The review's report:

# Review of notebooks/eda.ipynb

2026-10-10 10:15 · kernel python3 · 6 code cells: 4 ok, 2 failed · 6.2 s

## Failing cells

- "Count priced orders per region" [4]: NameError: name 'df_clean' is not defined
- "Add a revenue column" [3]: KeyError: 'price'

## Hidden state

Names read before any cell above defines them:
- "Count priced orders per region" [4] reads `df_clean`, defined later in "Drop rows with missing price" [2]

Cells that ran out of order:
- "Drop rows with missing price" [2] ran before "Count priced orders per region" [4] above it
- "Add a revenue column" [3] ran before "Rename price to unit_price" [5] above it

Cells that fail only in a fresh kernel:
- "Count priced orders per region" [4]: NameError here, though the notebook shows it ran without an error
- "Add a revenue column" [3]: KeyError here, though the notebook shows it ran without an error (a cell above it failed in the review)

## Cells over 40 lines (candidates for src/)

- "Check every column's values" [6]: 45 lines

## Intent summary

### Load

- "Load raw data and check schema" [1]: Load data/sales.csv and check its columns
- "Count priced orders per region" [4]: count the priced orders in each region

### Clean

- "Drop rows with missing price" [2]: drop the rows where price is missing
- "Rename price to unit_price" [5]: rename the price column to unit_price
- "Add a revenue column" [3]: add revenue as units times price
- "Check every column's values" [6]: check each column for missing, repeated or impossible values
