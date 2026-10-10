#!/usr/bin/env bash
# Seeds the workspace with the shared eval fixture (../_scaffold/base.sh), then makes the
# project senior: [preset] level = "senior" in its harness.toml (design §6.9).
set -euo pipefail
# shellcheck source=../_scaffold/base.sh
. "$(dirname "${BASH_SOURCE[0]}")/../_scaffold/base.sh"
printf '\n[preset]\nlevel = "senior"\n' >> harness.toml
