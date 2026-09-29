---
type: llm
focus: last_message
---

PASS if the reply walks through the "Load raw data and check schema" cell in chat as a numbered list of steps that quote or name its parts (the pandas import, DATA_PATH, read_csv into df, the schema table, the shape print), uses the real numbers from its output (43 rows and 6 columns, and price with 37 non-null values, 6 missing or 14%), and ends by offering a next step without taking it.
FAIL if it says it added, changed, re-ran or undid any cell, puts the explanation in the notebook, invents numbers the output doesn't show, or gives no numbered walkthrough.
