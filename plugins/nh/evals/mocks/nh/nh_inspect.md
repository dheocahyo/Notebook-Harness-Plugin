---
type: fixed
---
notebook notebooks/eda.ipynb: 3 cells
  0 title            md          # Sales anaylsis
  1 nh-3b8f2a61c0-n  note        ### Load raw data and check schema
  2 nh-3b8f2a61c0    code [1]    ok         agent    Load raw data and check schema  → (43, 6)
--- variables ---
DATA_PATH: str = '../data/sales.csv'
df: pandas DataFrame 43x6, nulls price 6; columns: order_id, order_date, region, product, units, price
schema: pandas DataFrame 6x4; columns: dtype, non_null, null_pct, n_unique
installed: pandas 2.2.3, numpy 2.1.3, matplotlib (not imported)
