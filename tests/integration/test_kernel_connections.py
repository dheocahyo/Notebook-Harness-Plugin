"""Dead kernel connections (design §6.13) on the session's real JupyterLab.

With ipykernel 7.3.0 behind jupyter_server 2.21.1, a kernel websocket whose first message is
sent right after the handshake is sometimes dead (1-3% of connections here): the kernel doesn't
run its requests until another connection to it opens, and nh's probe on it waits out its
timeout. a7 met it as a ~5 s pre-run stall on the probe client nh opens after each run. This loop
does what nh does then, many times: connect nh's client, run the attach probe at once, close the
client in a thread. With ``open_client``'s check every probe answers; without it (the code
before the fix) a run of this loop had 5 dead of 200. A stuck connection answers once the next
one opens, and the check used to take that answer: such a connection sometimes missed the probe
(2 of the 122 met in 26 runs were traced doing so), so only a connection's own answer in time
passes the check now.
"""

from __future__ import annotations

import logging
import time

import pytest

from nh_gateway.backend import kernel
from nh_gateway.exec import probes

pytestmark = pytest.mark.integration

LOOPS = 200  # at a 2% rate the unchecked loop passes 1 time in 57; at 3%, 1 in 440
KEPT = "no new connection answered"  # open_client's INFO line when none answered in its window


@pytest.mark.parametrize("settle", ["settled", "unsettled"])
def test_every_new_kernel_connection_answers_its_first_probe(
    lab, caplog, monkeypatch, settle: str
) -> None:
    """``unsettled`` sends the first check right after nh's GET (~3 ms): 1-5% of those
    connections are dead, so the check itself must catch them; the connections replacing them
    settle ``RETRY_SETTLE_S`` as nh's do (design §6.13)."""
    if settle == "unsettled":
        monkeypatch.setattr(kernel, "CONNECT_SETTLE_S", 0.0)
    caplog.set_level(logging.INFO, logger="nh_gateway.kernel")
    created = lab.api("POST", "/api/kernels", json={"name": "python3"})
    created.raise_for_status()
    kernel_id = created.json()["id"]
    server = kernel.ServerInfo(url=lab.url, token=lab.token, root_dir=lab.root)
    dead: list[tuple[int, str]] = []
    try:
        # The server reads the kernel's state from its messages, which start with a client.
        warm = kernel.open_client(server, kernel_id)
        assert "python" in probes.run_probe(warm._manager.client, "attach", {}, 30.0)
        kernel.close_client(warm)
        deadline = time.monotonic() + 30
        while lab.api("GET", f"/api/kernels/{kernel_id}").json()["execution_state"] != "idle":
            assert time.monotonic() < deadline, "the kernel never became idle"
            time.sleep(0.1)
        began = time.monotonic()
        for i in range(LOOPS):
            kc = kernel.open_client(server, kernel_id)
            answer = probes.run_probe(kc._manager.client, "attach", {}, 3.0)
            if "python" not in answer:
                dead.append((i, str(answer.get("error"))))
            kernel.close_client_later(kc)
        took = time.monotonic() - began
    finally:
        lab.api("DELETE", f"/api/kernels/{kernel_id}")
    replaced = [r for r in caplog.records if "connecting again" in r.getMessage()]
    kept = [r for r in caplog.records if KEPT in r.getMessage()]
    warned = sorted(
        {f"{r.name}: {r.getMessage()[:120]}" for r in caplog.records if r.levelno >= 30}
    )
    print(
        f"\n{settle}: {LOOPS} connections, each probed at once, in {took:.1f}s: "
        f"{len(dead)} probe(s) unanswered, {len(replaced)} connection(s) replaced by the check, "
        f"{len(kept)} kept when none of three answered in time; warnings: {warned or 'none'}"
    )
    assert not dead, f"{len(dead)} of {LOOPS} new connections never answered: {dead}"
