# .nh: Notebook Harness state

This folder marks the project as a Notebook Harness (nh) project. It holds
machine state: turn stamps, cell history for undo, logs, and the JupyterLab
collaboration store. Everything here except this README and `.gitignore` is
git-ignored.

Don't edit these files by hand. Project settings live in `../harness.toml`.
Delete this folder to turn nh off for the project; `/nh:init` recreates it.
