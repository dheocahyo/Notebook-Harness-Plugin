---
expect:
  cell_id: string
---
Re-ran "Load raw data and check schema" [2]; ran ok in 0.4s.
nh: cell={{input.cell_id}} exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3
--- output ---
[stdout] (43, 6)
[out]
              dtype  non_null  null_pct  n_unique
order_id      int64        43       0.0        43
order_date   object        43       0.0        25
region       object        43       0.0         4
product      object        43       0.0         3
units         int64        43       0.0        12
price       float64        37      14.0         9
--- self-check ---
same shape and nulls: df, schema; unchanged: DATA_PATH
--- next ---
Reply to the user about "Load raw data and check schema" [2]: (1) what the cell does; (2) why this approach; (3) judgment calls they may want to change; (4) the real numbers from the output, surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, as a title they can approve with "go". Name cells by title and [n]. Do not write a second cell.
