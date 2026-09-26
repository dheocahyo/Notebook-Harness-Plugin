# Sales sandbox

A small Notebook Harness project for trying the plugin from a checkout of this
repository. See "Development" in the repository README.

**Goal:** Which regions drive the drop in average price?

**Data:** `data/sales.csv` (40 orders; 6 have no price)

**Problem type:** eda

**Notebook:** [notebooks/01_eda.ipynb](notebooks/01_eda.ipynb)

## Layout

- `data/`: the data; never edited
- `notebooks/`: the analysis; nh adds one reviewed cell per message
- `harness.toml`: nh's settings (defaults shown commented out)
- `pyproject.toml`: the sandbox environment (`nhctl env sync` builds `.venv/`)
- `.nh/`: nh's machine state; only its README and `.gitignore` are committed

## Decisions

<!-- One line per decision: date, what was decided, why. -->
