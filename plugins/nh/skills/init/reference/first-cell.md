# The first cell: load the data and show its schema

One cell: a path constant, a reader chosen by file type, the shape, and one
row per column with dtype, non-null count, null % and unique count. Plain
pandas, nothing else: cleaning and plots are later cells.

## Paths

The kernel runs in the notebook's folder, not the project root. Never guess a
`../` prefix: `nhctl scaffold --json` reports `data.path_from_notebook`, the
data path as the kernel sees it from the notebook it set up (the new
`notebooks/01_eda.ipynb`, or the adopted notebook, wherever it sits).

| Scaffold reported `data.mode` | In the cell |
|---|---|
| `copy`, `in-place` | `DATA_PATH = "<data.path_from_notebook>"`: for example `"../data/raw/sales.csv"` from `notebooks/`, `"sales.csv"` for a notebook at the project root, an absolute path for data outside the project |
| `url` without credentials | the URL, as `DATA_URL` |
| `url` with credentials (`data.secret_in_env`) | read `DATA_URL` from the project's `.env` (below); never print it |

Other project files follow the same rule: count the folders in the scaffold's
`notebook` path and add one `../` per folder (`notebooks/01_eda.ipynb` → `../.env`;
`analysis.ipynb` → `.env`).

## Reader by file type

| `data.reader` (file types) | Reader | If it fails |
|---|---|---|
| `csv` (`.csv`, `.tsv`, `.txt`, also `.gz`/`.zip`) | `pd.read_csv(DATA_PATH)`; `.tsv`: `sep="\t"` | wrong columns: `sep=None, engine="python"`; `UnicodeDecodeError`: `encoding="latin-1"` |
| `parquet` (`.parquet`, `.pq`, or a folder of them) | `pd.read_parquet(DATA_PATH)` | pyarrow missing: ask before installing |
| `feather` (`.feather`, `.arrow`) | `pd.read_feather(DATA_PATH)` | as parquet |
| `excel` (`.xlsx`, `.xlsm`) | `pd.read_excel(DATA_PATH, sheet_name=0)` | wrong sheet: list `pd.ExcelFile(DATA_PATH).sheet_names` in the retry |
| `json` (`.json`, `.jsonl`, `.ndjson`) | `pd.read_json(DATA_PATH)`; line-delimited: `lines=True` | nested records: `pd.json_normalize` in a later cell |
| `sql`, SQLite file (`.sqlite`, `.db`) | `sqlite3.connect(DATA_PATH)`, then `pd.read_sql(QUERY, connection)` with `LIMIT` | unknown table: query `sqlite_master` for table names |
| `sql`, database URL | `sqlalchemy.create_engine(DATA_URL)`, then `pd.read_sql` with `LIMIT` | credentials stay in `.env` |

A CSV over 500 MB: read `nrows=100_000` first and say so in the reply.

## The canonical cell

For `notebooks/01_eda.ipynb` with the data copied into `data/raw/`:

```python
import pandas as pd

DATA_PATH = "../data/raw/sales.csv"

df = pd.read_csv(DATA_PATH)

schema = pd.DataFrame(
    {
        "dtype": df.dtypes.astype(str),
        "non_null": df.notna().sum(),
        "null_pct": (df.isna().mean() * 100).round(1),
        "n_unique": df.nunique(),
    }
)
print(df.shape)
schema
```

Note bullets for it, for example:
- Reads data/raw/sales.csv with pandas read_csv, the standard reader for CSV files
- The schema table shows each column's type, how many values are present, the share missing and how many distinct values it has

## Credentials in `.env`

When the URL carries a token or password, nhctl stored it in `.env` as
`DATA_URL`. Read it there so it never lands in the notebook (`ENV_FILE` as in
"Paths": `"../.env"` from `notebooks/`):

```python
import os
from pathlib import Path

import pandas as pd

ENV_FILE = Path("../.env")

env_lines = ENV_FILE.read_text().splitlines()
env_values = dict(line.split("=", 1) for line in env_lines if "=" in line)
DATA_URL = os.environ.get("DATA_URL") or env_values["DATA_URL"].strip("'\"")

df = pd.read_csv(DATA_URL)
```

Then the same `schema` table as above. Never print or display `DATA_URL`.

## In the summary

Pick 2-3 facts from the schema table, with numbers: columns with many nulls,
id-like columns (`n_unique` equals the row count), numbers or dates stored as
text (`dtype` is `object` or `str`), constant columns (`n_unique` is 1).
