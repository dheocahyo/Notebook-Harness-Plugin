#!/usr/bin/env bash
# The project /nh:init builds for a data URL, made by the real `nhctl scaffold`: harness.toml
# (approve_before_run off, as evals need), notebooks/01_eda.ipynb (its title cell only), and
# .nh/state/approved_hosts.json holding data.example.org, the URL's host. Nothing is downloaded,
# and uv isn't needed: scaffold only writes files.
set -euo pipefail
here=$(dirname "${BASH_SOURCE[0]}")
sh "$here/../../bin/nhctl" scaffold \
    --name trips \
    --goal "How do 2023 trip durations vary by month?" \
    --data "https://data.example.org/trips-2023.csv" \
    --problem-type eda \
    --env-manager uv \
    --json >/dev/null
