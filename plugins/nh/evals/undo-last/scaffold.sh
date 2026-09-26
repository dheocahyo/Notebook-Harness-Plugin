#!/usr/bin/env bash
# Seeds the workspace with the shared eval fixture (../_scaffold/base.sh).
set -euo pipefail
export NH_EVAL_LAST_CELL=drop  # the notebook also holds the cell to undo
# shellcheck source=../_scaffold/base.sh
. "$(dirname "${BASH_SOURCE[0]}")/../_scaffold/base.sh"
