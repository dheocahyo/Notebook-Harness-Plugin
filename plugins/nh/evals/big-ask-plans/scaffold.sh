#!/usr/bin/env bash
# Seeds the workspace with the shared eval fixture (../_scaffold/base.sh).
set -euo pipefail
# shellcheck source=../_scaffold/base.sh
. "$(dirname "${BASH_SOURCE[0]}")/../_scaffold/base.sh"
