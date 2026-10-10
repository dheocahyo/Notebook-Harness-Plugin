---
type: llm
focus: last_message
weight: 5
---

PASS if the reply walks through the "Load raw data and check schema" cell in chat as a numbered list of steps that quote or name its parts (the pandas import, DATA_PATH, read_csv into df, the schema table, the shape print), and uses the real numbers from its output (43 rows and 6 columns, and price with 37 non-null values, 6 missing or 14%).
FAIL if it says it added, changed, re-ran or undid any cell, puts the explanation in the notebook, invents numbers the output below doesn't show, or gives no numbered walkthrough.

The cell's whole output, as nh_inspect showed it, so any other number in the reply can be checked here (a count worked out from it, such as 6 missing prices, is real too):

[stdout] (43, 6)
[out]
              dtype  non_null  null_pct  n_unique
order_id      int64        43       0.0        43
order_date   object        43       0.0        25
region       object        43       0.0         4
product      object        43       0.0         3
units         int64        43       0.0        12
price       float64        37      14.0         9
