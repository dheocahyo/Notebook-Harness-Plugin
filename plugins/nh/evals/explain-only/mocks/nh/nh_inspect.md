---
type: fixed
---
"Load raw data and check schema" [1]  id=nh-3b8f2a61c0  sha=ce1f0f725152b3c1  author=agent
intent: Load data/sales.csv and check its columns
- Reads data/sales.csv with pandas read_csv, the standard reader for CSV files
- The schema table shows each column's type, non-null count, share missing and distinct values
--- source ---
import pandas as pd

DATA_PATH = "../data/sales.csv"

df = pd.read_csv(DATA_PATH)

schema = pd.DataFrame({
    "dtype": df.dtypes.astype(str),
    "non_null": df.notna().sum(),
    "null_pct": (df.isna().mean() * 100).round(1),
    "n_unique": df.nunique(),
})
print(df.shape)
schema
--- outputs ---
[stdout] (43, 6)
[out]
              dtype  non_null  null_pct  n_unique
order_id      int64        43       0.0        43
order_date   object        43       0.0        25
region       object        43       0.0         4
product      object        43       0.0         3
units         int64        43       0.0        12
price       float64        37      14.0         9
