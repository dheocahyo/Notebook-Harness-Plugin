---
type: llm
focus: last_message
weight: 3
---

PASS if the reply is short plain prose, one or two short paragraphs (a first sentence saying where the cell landed, and a last sentence proposing the next cell, may stand on their own), that says what the cell does and what to check in its output, gives real numbers from the output below, and ends with one proposed next cell. Sentences that name the cell's steps, or say why it uses an option (errors="coerce" keeps the rows whose date is impossible), are a summary, not a walkthrough or a definition.
FAIL if it has headings or part labels (such as "**What it does**", "Why:" or "Numbers:"), a bulleted or numbered list, or a table; runs to more than two paragraphs besides those two sentences; says what a common pandas method (to_datetime, dropna, groupby, agg, sort_values) does in general, such as "groupby splits the rows into groups"; explains the code line by line, each line or call with its own explanation; presents a number the output below doesn't show; or says it wrote or will write a second cell.

The cell's whole output and nh's self-check of its variables, as nh returned them, so any number in the reply can be checked here (a number worked out from them, such as the 6 orders without a price dropped from 43 or the total revenue of 3100.74, is real too, and so is a fact from data/sales.csv itself, such as order 1020's impossible date 2024-02-30):

--- output ---
[display]
        revenue  orders
region                 
East     968.42      10
South    921.70       9
North    811.77      11
West     398.85       7
[display]
         revenue  orders
product                 
Gadget   1435.80      11
Widget    987.81      12
Gizmo     677.13      14
--- self-check ---
by_product: new DataFrame 3×2 (from df_clean 37×7); no nulls
by_region: new DataFrame 4×2 (from df_clean 37×7); no nulls
df_clean: new DataFrame 37×7 (from df 43×6); nulls: order_date 1
same shape and nulls: df
