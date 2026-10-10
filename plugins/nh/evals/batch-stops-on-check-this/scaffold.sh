#!/usr/bin/env bash
# Seeds the workspace with the shared eval fixture (../_scaffold/base.sh), then nh's turn record
# for the history's "run the next 3": the record the prompt hook writes for that message, under
# the history file's session id (a resumed run keeps it), so the "yes" prompt opens the batch.
set -euo pipefail
# shellcheck source=../_scaffold/base.sh
. "$(dirname "${BASH_SOURCE[0]}")/../_scaffold/base.sh"

session="49169f03-2f20-5915-ae1e-e23ebf975ddc"
mkdir -p .nh/state/turns
cat > ".nh/state/turns/$session.json" <<JSON
{"v": 2, "session_id": "$session", "prompt_id": "hist-p2", "turn_id": "hist-p2", "aliases": [], "human": true, "ts": $(($(date +%s) - 60)), "alias_ts": null, "earlier": {}, "mode": "ask", "request": {"batch": true, "n": 3}, "answer": null, "prev_turn_id": "hist-p1", "prev_request": null}
JSON
