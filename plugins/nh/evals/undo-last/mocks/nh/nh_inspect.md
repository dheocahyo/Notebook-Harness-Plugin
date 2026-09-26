---
type: fixed
---
notebook notebooks/eda.ipynb: 5 cells
  0 title            md          # Sales anaylsis
  1 nh-3b8f2a61c0-n  note        ### Load raw data and check schema
  2 nh-3b8f2a61c0    code [1]    ok         agent    Load raw data and check schema  → (43, 6)
  3 nh-7d41c9e2a5-n  note        ### Drop rows with missing price
  4 nh-7d41c9e2a5    code [2]    ok         agent    Drop rows with missing price  → rows: 43 -> 37
--- variables ---
DATA_PATH: str = '../data/sales.csv'
df: pandas DataFrame 43x6, nulls price 6; columns: order_id, order_date, region, product, units, price
schema: pandas DataFrame 6x4; columns: dtype, non_null, null_pct, n_unique
df_clean: pandas DataFrame 37x6; columns: order_id, order_date, region, product, units, price
installed: pandas 2.2.3, numpy 2.1.3, matplotlib (not imported)
