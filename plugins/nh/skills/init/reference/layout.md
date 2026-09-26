# What /nh:init creates

`nhctl scaffold` creates what is missing and keeps every existing file.

| Path | What it is | In git |
|---|---|---|
| `data/raw/` | the data as received; never edited | ignored (only `.gitkeep`) |
| `data/processed/` | cleaned or derived data | ignored (only `.gitkeep`) |
| `notebooks/01_eda.ipynb` | the first notebook (nbformat 4.5), created only if absent | yes |
| `reports/figures/` | exported charts | yes |
| `src/` | code reused across notebooks, once it exists | yes |
| `NOTEBOOK.md` | goal, data, problem type, layout legend, and a "Decisions" log | yes |
| `harness.toml` | nh's settings for this project: the `[project]` values, then every other key commented out at its default | yes |
| `pyproject.toml` (uv) or `environment.yml` (conda) | the project environment: pandas, numpy, matplotlib, a reader for the data's file type, and a dev group with JupyterLab 4.6+, jupyter-collaboration 5 and ipykernel | yes |
| `.venv/` (uv) or `.conda/` (conda) | the environment itself, inside the project | ignored |
| `.env` | only when the data URL carries credentials: `DATA_URL` | ignored |
| `.nh/` | nh's machine state: turn records, cell history, outputs, logs | only `.nh/README.md` and `.nh/.gitignore` |
| `.gitignore` | an nh block: `.venv/`, `.conda/`, `.env`, `.ipynb_checkpoints/`, `*jupyter_ystore.db`, `.jupyter/`, `data/raw/*`, `data/processed/*`, `!data/*/.gitkeep` | yes |

Environment choice, in order: an existing `environment.yml` means conda; else,
with uv installed, `pyproject.toml` (an existing one is kept, and the Jupyter
packages are added only after the user agrees to the shown diff); else a new
`environment.yml`. With neither uv nor conda, `nhctl doctor` stops the setup.
No `.python-version` is written: any Python 3.11 or newer works.

## Adopt mode (`--adopt <notebook>`)

For an existing project. It writes only `harness.toml` (with
`[project].notebook` set to the adopted notebook), `.nh/`, and `NOTEBOOK.md` if
absent. It creates no folders, copies no data and adds no notebook. The
Jupyter packages go into the existing env file only after the user agrees to
the diff. If the notebook already loads data, the loader cell is skipped.

## Where nh's settings and state live

- `harness.toml`: project settings; commit it. Every key is optional.
- `.claude/settings.json`: the optional deny rule from step 4.
- `.nh/state/`: turns, stamps, last cell, env and JupyterLab records.
- `.nh/history/`: each nh cell's previous versions, used by undo.
- `.nh/logs/`: gateway, hook and JupyterLab logs.
