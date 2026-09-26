"""Round-2 regressions for the kernel group: the reminder's 'Kernel ≠ notebook' line is hedged
(the hook can't see the kernel) and lists the newest names first, like the gateway's lead."""

from __future__ import annotations

import json

from hookenv import Sandbox, needs_system_python

pytestmark = needs_system_python


def test_the_drift_reminder_is_hedged_and_lists_the_newest_names_first(sandbox: Sandbox) -> None:
    state = sandbox.nh / "state"
    state.mkdir(parents=True, exist_ok=True)
    names = {name: "still holds results" for name in ["a1", "a2", "a3", "a4", "newest"]}
    (state / "kernel_drift.json").write_text(
        json.dumps({"notebooks/01_eda.ipynb": {"kernel_id": "k1:1@1", "names": names}})
    )
    run = sandbox.run("UserPromptSubmit", sandbox.payload("UserPromptSubmit", prompt="go"))
    assert (
        "Kernel ≠ notebook: newest, a4, a3, a2 and 1 more still hold results of undone cells "
        "(as of nh's last check); suggest Kernel → Restart Kernel and Run Up to Selected Cell."
    ) in run.context
