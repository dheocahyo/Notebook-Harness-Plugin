#!/usr/bin/env bash
# Seeds the workspace with the shared eval fixture (../_scaffold/base.sh), then replaces its
# notebook with the one the recorded /nh:review ran (design §6.10, C10b), and writes the report
# that run left in .nh/reviews/. history.jsonl holds that run's JSON; tests/unit/test_skill_files.py
# reruns the real `nhctl fresh-run --review --json` here and checks both against it.
#   # Load: the loader [1], and a cell [4] that reads df_clean, which only a later cell defines.
#   # Clean: that cell [2] (it ran before [4] above it), a rename of price [5], a revenue cell
#   [3] that ran before the rename (so it fails with KeyError 'price' in a fresh kernel), and a
#   45-line check of every column [6].
set -euo pipefail
# shellcheck source=../_scaffold/base.sh
. "$(dirname "${BASH_SOURCE[0]}")/../_scaffold/base.sh"

cat > notebooks/eda.ipynb <<'NOTEBOOK'
{
 "cells": [
  {
   "cell_type": "markdown",
   "id": "title",
   "metadata": {},
   "source": [
    "# Sales anaylsis\n",
    "\n",
    "Goal: Explore sales.csv: size, types, missing values"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "load",
   "metadata": {},
   "source": [
    "# Load"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-3b8f2a61c0-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-3b8f2a61c0-n",
     "pair_uid": "nh-3b8f2a61c0",
     "turn_id": "eval-turn-0"
    }
   },
   "source": [
    "### Load raw data and check schema\n",
    "\n",
    "- Reads data/sales.csv with pandas read_csv, the standard reader for CSV files\n",
    "- The schema table shows each column's type, non-null count, share missing and distinct values"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 1,
   "id": "nh-3b8f2a61c0",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-3b8f2a61c0",
     "pair_uid": "nh-3b8f2a61c0-n",
     "intent": "Load data/sales.csv and check its columns",
     "rationale": [
      "Reads data/sales.csv with pandas read_csv, the standard reader for CSV files",
      "The schema table shows each column's type, non-null count, share missing and distinct values"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-0",
     "created": "2026-09-25",
     "source_sha": "ce1f0f725152b3c1",
     "edits": 0
    }
   },
   "outputs": [
    {
     "name": "stdout",
     "output_type": "stream",
     "text": [
      "(43, 6)\n"
     ]
    },
    {
     "data": {
      "text/plain": [
       "              dtype  non_null  null_pct  n_unique\n",
       "order_id      int64        43       0.0        43\n",
       "order_date   object        43       0.0        25\n",
       "region       object        43       0.0         4\n",
       "product      object        43       0.0         3\n",
       "units         int64        43       0.0        12\n",
       "price       float64        37      14.0         9"
      ]
     },
     "execution_count": 1,
     "metadata": {},
     "output_type": "execute_result"
    }
   ],
   "source": [
    "import pandas as pd\n",
    "\n",
    "DATA_PATH = \"../data/sales.csv\"\n",
    "\n",
    "df = pd.read_csv(DATA_PATH)\n",
    "\n",
    "schema = pd.DataFrame({\n",
    "    \"dtype\": df.dtypes.astype(str),\n",
    "    \"non_null\": df.notna().sum(),\n",
    "    \"null_pct\": (df.isna().mean() * 100).round(1),\n",
    "    \"n_unique\": df.nunique(),\n",
    "})\n",
    "print(df.shape)\n",
    "schema"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-5c2e8a1f34-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-5c2e8a1f34-n",
     "pair_uid": "nh-5c2e8a1f34",
     "turn_id": "eval-turn-4"
    }
   },
   "source": [
    "### Count priced orders per region\n",
    "\n",
    "- Counts the orders that have a price, region by region\n",
    "- A region with few priced orders would make its revenue less reliable"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 4,
   "id": "nh-5c2e8a1f34",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-5c2e8a1f34",
     "pair_uid": "nh-5c2e8a1f34-n",
     "intent": "count the priced orders in each region",
     "rationale": [
      "Counts the orders that have a price, region by region",
      "A region with few priced orders would make its revenue less reliable"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-4",
     "created": "2026-09-25",
     "source_sha": "3e926d7a85bffdff",
     "edits": 0
    }
   },
   "outputs": [
    {
     "data": {
      "text/plain": [
       "region\n",
       "East     10\n",
       "North    11\n",
       "South     9\n",
       "West      7\n",
       "dtype: int64"
      ]
     },
     "execution_count": 4,
     "metadata": {},
     "output_type": "execute_result"
    }
   ],
   "source": [
    "orders_per_region = df_clean.groupby(\"region\").size()\n",
    "orders_per_region"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "clean",
   "metadata": {},
   "source": [
    "# Clean"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-7d41c9e2a5-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-7d41c9e2a5-n",
     "pair_uid": "nh-7d41c9e2a5",
     "turn_id": "eval-turn-1"
    }
   },
   "source": [
    "### Drop rows with missing price\n",
    "\n",
    "- Keeps only the orders that have a price, in a new frame df_clean\n",
    "- Filling 6 of 43 prices would invent 14% of the data, so they are dropped instead"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 2,
   "id": "nh-7d41c9e2a5",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-7d41c9e2a5",
     "pair_uid": "nh-7d41c9e2a5-n",
     "intent": "drop the rows where price is missing",
     "rationale": [
      "Keeps only the orders that have a price, in a new frame df_clean",
      "Filling 6 of 43 prices would invent 14% of the data, so they are dropped instead"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-1",
     "created": "2026-09-25",
     "source_sha": "81712c8b3401c0a2",
     "edits": 0
    }
   },
   "outputs": [
    {
     "name": "stdout",
     "output_type": "stream",
     "text": [
      "rows: 43 -> 37\n"
     ]
    },
    {
     "data": {
      "text/plain": [
       "   order_id  order_date region product  units  price\n",
       "0      1001  2024-01-01  North  Widget      4  11.25\n",
       "1      1002  2024-01-08   West   Gizmo     11   7.25\n",
       "2      1003  2024-01-15   East  Gadget      6  26.40\n",
       "3      1004  2024-01-22  South  Gadget      1  23.40\n",
       "5      1006  2024-01-08  North   Gizmo      3   6.89"
      ]
     },
     "execution_count": 2,
     "metadata": {},
     "output_type": "execute_result"
    }
   ],
   "source": [
    "df_clean = df.dropna(subset=[\"price\"])\n",
    "print(f\"rows: {len(df)} -> {len(df_clean)}\")\n",
    "df_clean.head()"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-9a4b6c8d2e-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-9a4b6c8d2e-n",
     "pair_uid": "nh-9a4b6c8d2e",
     "turn_id": "eval-turn-5"
    }
   },
   "source": [
    "### Rename price to unit_price\n",
    "\n",
    "- Renames the price column so it says the price is per unit\n",
    "- Keeps every row and every other column as it was"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 5,
   "id": "nh-9a4b6c8d2e",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-9a4b6c8d2e",
     "pair_uid": "nh-9a4b6c8d2e-n",
     "intent": "rename the price column to unit_price",
     "rationale": [
      "Renames the price column so it says the price is per unit",
      "Keeps every row and every other column as it was"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-5",
     "created": "2026-09-25",
     "source_sha": "91781644f0fe0c08",
     "edits": 0
    }
   },
   "outputs": [
    {
     "data": {
      "text/plain": [
       "['order_id', 'order_date', 'region', 'product', 'units', 'unit_price']"
      ]
     },
     "execution_count": 5,
     "metadata": {},
     "output_type": "execute_result"
    }
   ],
   "source": [
    "df_clean = df_clean.rename(columns={\"price\": \"unit_price\"})\n",
    "df_clean.columns.tolist()"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-2f6d8e0a1b-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-2f6d8e0a1b-n",
     "pair_uid": "nh-2f6d8e0a1b",
     "turn_id": "eval-turn-3"
    }
   },
   "source": [
    "### Add a revenue column\n",
    "\n",
    "- Revenue is units times price, for each order\n",
    "- The first rows show the three columns side by side to check the product"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 3,
   "id": "nh-2f6d8e0a1b",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-2f6d8e0a1b",
     "pair_uid": "nh-2f6d8e0a1b-n",
     "intent": "add revenue as units times price",
     "rationale": [
      "Revenue is units times price, for each order",
      "The first rows show the three columns side by side to check the product"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-3",
     "created": "2026-09-25",
     "source_sha": "a838394a0b566dcc",
     "edits": 0
    }
   },
   "outputs": [
    {
     "data": {
      "text/plain": [
       "   units  price  revenue\n",
       "0      4  11.25    45.00\n",
       "1     11   7.25    79.75\n",
       "2      6  26.40   158.40\n",
       "3      1  23.40    23.40\n",
       "5      3   6.89    20.67"
      ]
     },
     "execution_count": 3,
     "metadata": {},
     "output_type": "execute_result"
    }
   ],
   "source": [
    "df_clean[\"revenue\"] = df_clean[\"units\"] * df_clean[\"price\"]\n",
    "df_clean[[\"units\", \"price\", \"revenue\"]].head()"
   ]
  },
  {
   "cell_type": "markdown",
   "id": "nh-4e8c0a2b6d-n",
   "metadata": {
    "nh": {
     "v": 1,
     "role": "note",
     "uid": "nh-4e8c0a2b6d-n",
     "pair_uid": "nh-4e8c0a2b6d",
     "turn_id": "eval-turn-6"
    }
   },
   "source": [
    "### Check every column's values\n",
    "\n",
    "- Checks the columns, ids, dates, regions, products, units and prices against what they should hold\n",
    "- Prints one line per problem it finds, so an empty list means the data passed"
   ]
  },
  {
   "cell_type": "code",
   "execution_count": 6,
   "id": "nh-4e8c0a2b6d",
   "metadata": {
    "tags": [
     "nh-agent"
    ],
    "nh": {
     "v": 1,
     "role": "code",
     "uid": "nh-4e8c0a2b6d",
     "pair_uid": "nh-4e8c0a2b6d-n",
     "intent": "check each column for missing, repeated or impossible values",
     "rationale": [
      "Checks the columns, ids, dates, regions, products, units and prices against what they should hold",
      "Prints one line per problem it finds, so an empty list means the data passed"
     ],
     "created_by": "agent",
     "host": "claude-code",
     "turn_id": "eval-turn-6",
     "created": "2026-09-25",
     "source_sha": "e444a0566ba0af2c",
     "edits": 0
    }
   },
   "outputs": [
    {
     "name": "stdout",
     "output_type": "stream",
     "text": [
      "2 problems found\n",
      "- dates that don't parse: ['2024-02-30']\n",
      "- 6 rows without a price\n"
     ]
    }
   ],
   "source": [
    "EXPECTED_COLUMNS = [\"order_id\", \"order_date\", \"region\", \"product\", \"units\", \"price\"]\n",
    "VALID_REGIONS = {\"North\", \"South\", \"East\", \"West\"}\n",
    "VALID_PRODUCTS = {\"Widget\", \"Gizmo\", \"Gadget\"}\n",
    "MIN_UNITS = 1\n",
    "MAX_UNITS = 20\n",
    "MIN_PRICE = 0.01\n",
    "MAX_PRICE = 100.0\n",
    "\n",
    "problems = []\n",
    "\n",
    "if df.empty:\n",
    "    problems.append(\"no rows\")\n",
    "\n",
    "missing_columns = [c for c in EXPECTED_COLUMNS if c not in df.columns]\n",
    "if missing_columns:\n",
    "    problems.append(f\"missing columns: {missing_columns}\")\n",
    "\n",
    "extra_columns = [c for c in df.columns if c not in EXPECTED_COLUMNS]\n",
    "if extra_columns:\n",
    "    problems.append(f\"extra columns: {extra_columns}\")\n",
    "\n",
    "repeated_ids = df[\"order_id\"].duplicated().sum()\n",
    "if repeated_ids:\n",
    "    problems.append(f\"{repeated_ids} repeated order ids\")\n",
    "\n",
    "parsed_dates = pd.to_datetime(df[\"order_date\"], format=\"%Y-%m-%d\", errors=\"coerce\")\n",
    "bad_dates = df.loc[parsed_dates.isna(), \"order_date\"].tolist()\n",
    "if bad_dates:\n",
    "    problems.append(f\"dates that don't parse: {bad_dates}\")\n",
    "\n",
    "first_year, last_year = parsed_dates.min().year, parsed_dates.max().year\n",
    "if first_year != last_year:\n",
    "    problems.append(f\"orders span {first_year} to {last_year}\")\n",
    "\n",
    "unknown_regions = sorted(set(df[\"region\"]) - VALID_REGIONS)\n",
    "if unknown_regions:\n",
    "    problems.append(f\"unknown regions: {unknown_regions}\")\n",
    "\n",
    "unknown_products = sorted(set(df[\"product\"]) - VALID_PRODUCTS)\n",
    "if unknown_products:\n",
    "    problems.append(f\"unknown products: {unknown_products}\")\n",
    "\n",
    "odd_units = df[(df[\"units\"] < MIN_UNITS) | (df[\"units\"] > MAX_UNITS)]\n",
    "if len(odd_units):\n",
    "    problems.append(f\"{len(odd_units)} rows with units outside {MIN_UNITS}-{MAX_UNITS}\")\n",
    "\n",
    "prices = df[\"price\"].dropna()\n",
    "odd_prices = prices[(prices < MIN_PRICE) | (prices > MAX_PRICE)]\n",
    "if len(odd_prices):\n",
    "    problems.append(f\"{len(odd_prices)} prices outside {MIN_PRICE}-{MAX_PRICE}\")\n",
    "\n",
    "missing_prices = df[\"price\"].isna().sum()\n",
    "if missing_prices:\n",
    "    problems.append(f\"{missing_prices} rows without a price\")\n",
    "\n",
    "print(f\"{len(problems)} problems found\")\n",
    "for problem in problems:\n",
    "    print(\"-\", problem)"
   ]
  }
 ],
 "metadata": {
  "kernelspec": {
   "display_name": "Python 3 (ipykernel)",
   "language": "python",
   "name": "python3"
  },
  "language_info": {
   "name": "python"
  },
  "nh": {
   "v": 1,
   "goal": "Explore sales.csv: size, types, missing values"
  }
 },
 "nbformat": 4,
 "nbformat_minor": 5
}
NOTEBOOK

mkdir -p .nh/reviews
cat > .nh/reviews/20261010T101500-eda.md <<'REPORT'
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
REPORT
