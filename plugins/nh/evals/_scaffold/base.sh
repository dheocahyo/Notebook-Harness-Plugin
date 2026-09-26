#!/usr/bin/env bash
# Eval fixture: a tiny nh project in the current (empty) workspace.
#   harness.toml (approve_before_run = false: evals can't answer approval prompts)
#   data/sales.csv (43 orders, 6 missing prices, one impossible date at row 19)
#   notebooks/eda.ipynb (a title with the typo "anaylsis", then nh's loader cell, run as [1])
#   .nh/ (marks the folder as an nh project)
# Each case's scaffold.sh sources this file. NH_EVAL_LAST_CELL=drop also adds the
# "Drop rows with missing price" cell nh wrote in the previous message, run as [2].
# The numbers in evals/mocks/nh/*.md are computed from this data; keep them in sync.
set -euo pipefail

mkdir -p data notebooks .nh/state

cat > harness.toml <<'TOML'
version = 1

[project]
name = "sales-eval"
goal = "Explore sales.csv: size, types, missing values"
problem_type = "eda"
data_source = "data/sales.csv"
notebook = "notebooks/eda.ipynb"
env_manager = "uv"

[approval]
approve_before_run = false
TOML

cat > data/sales.csv <<'CSV'
order_id,order_date,region,product,units,price
1001,2024-01-01,North,Widget,4,11.25
1002,2024-01-08,West,Gizmo,11,7.25
1003,2024-01-15,East,Gadget,6,26.40
1004,2024-01-22,South,Gadget,1,23.40
1005,2024-01-01,South,Widget,8,
1006,2024-01-08,North,Gizmo,3,6.89
1007,2024-01-15,West,Gizmo,10,7.61
1008,2024-01-22,East,Gadget,5,22.20
1009,2024-02-01,East,Widget,12,12.81
1010,2024-02-08,South,Widget,7,11.25
1011,2024-02-15,North,Gizmo,2,7.25
1012,2024-02-22,West,Gadget,9,
1013,2024-02-01,West,Gadget,4,23.40
1014,2024-02-08,East,Widget,11,13.44
1015,2024-02-15,South,Gizmo,6,6.89
1016,2024-03-22,North,Gizmo,1,7.61
1017,2024-03-01,North,Gadget,8,22.20
1018,2024-03-08,West,Widget,3,
1019,2024-03-15,East,Widget,10,11.25
1020,2024-02-30,South,Gizmo,5,7.25
1021,2024-03-01,South,Gadget,12,26.40
1022,2024-03-08,North,Gadget,7,23.40
1023,2024-04-15,West,Widget,2,13.44
1024,2024-04-22,East,Gizmo,9,
1025,2024-04-01,East,Gizmo,4,7.61
1026,2024-04-08,South,Gadget,11,22.20
1027,2024-04-15,North,Widget,6,12.81
1028,2024-04-22,West,Widget,1,11.25
1029,2024-04-01,West,Gizmo,8,7.25
1030,2024-05-08,East,Gadget,3,26.40
1031,2024-05-15,South,Gadget,10,
1032,2024-05-22,North,Widget,5,13.44
1033,2024-05-01,North,Gizmo,12,6.89
1034,2024-05-08,West,Gizmo,7,7.61
1035,2024-05-15,East,Gadget,2,22.20
1036,2024-05-22,South,Widget,9,12.81
1037,2024-06-01,South,Widget,4,11.25
1038,2024-06-08,North,Gizmo,11,7.25
1039,2024-06-15,West,Gadget,6,
1040,2024-06-22,East,Gadget,1,23.40
1041,2024-06-01,East,Widget,8,13.44
1042,2024-06-08,South,Gizmo,3,6.89
1043,2024-06-15,North,Gizmo,10,7.61
CSV

{
  cat <<'JSON'
{
 "cells": [
  {
   "cell_type": "markdown",
   "id": "title",
   "metadata": {},
   "source": ["# Sales anaylsis\n", "\n", "Goal: Explore sales.csv: size, types, missing values"]
  },
  {
   "cell_type": "markdown",
   "id": "nh-3b8f2a61c0-n",
   "metadata": {"nh": {"v": 1, "role": "note", "uid": "nh-3b8f2a61c0-n", "pair_uid": "nh-3b8f2a61c0", "turn_id": "eval-turn-0"}},
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
   "metadata": {"tags": ["nh-agent"], "nh": {"v": 1, "role": "code", "uid": "nh-3b8f2a61c0", "pair_uid": "nh-3b8f2a61c0-n", "intent": "Load data/sales.csv and check its columns", "rationale": ["Reads data/sales.csv with pandas read_csv, the standard reader for CSV files", "The schema table shows each column's type, non-null count, share missing and distinct values"], "created_by": "agent", "host": "claude-code", "turn_id": "eval-turn-0", "created": "2026-09-25", "source_sha": "ce1f0f725152b3c1", "edits": 0}},
   "outputs": [
    {"name": "stdout", "output_type": "stream", "text": ["(43, 6)\n"]},
    {
     "data": {"text/plain": [
      "              dtype  non_null  null_pct  n_unique\n",
      "order_id      int64        43       0.0        43\n",
      "order_date   object        43       0.0        25\n",
      "region       object        43       0.0         4\n",
      "product      object        43       0.0         3\n",
      "units         int64        43       0.0        12\n",
      "price       float64        37      14.0         9"
     ]},
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
  }
JSON
  if [ "${NH_EVAL_LAST_CELL:-}" = "drop" ]; then
    cat <<'JSON'
  ,{
   "cell_type": "markdown",
   "id": "nh-7d41c9e2a5-n",
   "metadata": {"nh": {"v": 1, "role": "note", "uid": "nh-7d41c9e2a5-n", "pair_uid": "nh-7d41c9e2a5", "turn_id": "eval-turn-1"}},
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
   "metadata": {"tags": ["nh-agent"], "nh": {"v": 1, "role": "code", "uid": "nh-7d41c9e2a5", "pair_uid": "nh-7d41c9e2a5-n", "intent": "drop the rows where price is missing", "rationale": ["Keeps only the orders that have a price, in a new frame df_clean", "Filling 6 of 43 prices would invent 14% of the data, so they are dropped instead"], "created_by": "agent", "host": "claude-code", "turn_id": "eval-turn-1", "created": "2026-09-25", "source_sha": "81712c8b3401c0a2", "edits": 0}},
   "outputs": [
    {"name": "stdout", "output_type": "stream", "text": ["rows: 43 -> 37\n"]},
    {
     "data": {"text/plain": [
      "   order_id  order_date region product  units  price\n",
      "0      1001  2024-01-01  North  Widget      4  11.25\n",
      "1      1002  2024-01-08   West   Gizmo     11   7.25\n",
      "2      1003  2024-01-15   East  Gadget      6  26.40\n",
      "3      1004  2024-01-22  South  Gadget      1  23.40\n",
      "5      1006  2024-01-08  North   Gizmo      3   6.89"
     ]},
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
  }
JSON
    cat > .nh/state/last_cell.json <<JSON
{"v": 1, "session_id": "eval", "notebook": "notebooks/eda.ipynb", "cell_id": "nh-7d41c9e2a5", "title": "Drop rows with missing price", "exec": 2, "status": "ok", "turn_id": "eval-turn-1", "retries_left": 2, "finished_at": $(date +%s)}
JSON
  fi
  cat <<'JSON'
 ],
 "metadata": {
  "kernelspec": {"display_name": "Python 3 (ipykernel)", "language": "python", "name": "python3"},
  "language_info": {"name": "python"},
  "nh": {"v": 1, "goal": "Explore sales.csv: size, types, missing values"}
 },
 "nbformat": 4,
 "nbformat_minor": 5
}
JSON
} > notebooks/eda.ipynb

printf '%s\n' '*' '!.gitignore' '!README.md' > .nh/.gitignore
printf '%s\n' "nh's machine state for this project (eval fixture)." > .nh/README.md
