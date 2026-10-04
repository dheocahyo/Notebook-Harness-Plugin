# Draft (not filed): a kernel websocket's first request sometimes waits, unrun, until another client connects

Target: ipython/ipykernel first (only the ipykernel version changes the outcome, below), with a
cross-link to jupyter-server/jupyter_server, whose connect pattern seems to be needed. The
numbers below come from runs whose output is kept (scratch c12e-kc/fix: e5/out, kc/), except
the rows and the trace marked "earlier", from loops whose output wasn't kept.

## Summary

Open `/api/kernels/<id>/channels` and send one `execute_request` (or `kernel_info_request`) on
`shell` as soon as the websocket handshake completes. In about 1-3% of connections nothing
comes back: no reply, no `status: busy` on iopub. A second request on the same connection gets
nothing either, for seconds. The request is not lost, though: it runs, and both requests are
answered, 4-13 ms after another client opens a connection to the same kernel. Waiting 20 ms
after the handshake before the first send avoids it (0 of 300); with ipykernel 6.31.0 it never
happens (0 of 600).

## Versions

- jupyter_server 2.21.1, tornado 6.5.10, pyzmq 27.2.0, jupyter_client 8.10.0
- ipykernel 7.3.0 (affected); ipykernel 6.31.0 (not affected, everything else the same)
- Python 3.11.15, Linux 6.18 x86_64 (4 CPUs, idle: load 0.1-0.8)
- Clients: websocket-client 1.9.2 (the script below); also jupyter-kernel-client 1.0.2
- Also seen behind JupyterLab 4.6.4 with jupyter-collaboration 5.0.4

## Reproduction

`pip install jupyter_server==2.21.1 ipykernel==7.3.0 websocket-client requests`, then
`python repro.py 300` (immediate send) and `python repro.py 300 0.02` (20 ms wait):

```python
"""A kernel websocket whose first message is sent right after the handshake is sometimes dead.

usage: python repro.py [N] [DELAY_S]
Starts `jupyter server` on a free port with a token, starts one kernel, then N times: opens
/api/kernels/<id>/channels (default protocol), waits DELAY_S, sends one execute_request ("1",
silent) on shell, waits up to 3 s for its execute_reply, closes. Prints the dead connections
(no reply; a second request on the same connection is tried too).
"""

import datetime, json, os, secrets, socket, subprocess, sys, tempfile, time, uuid

import requests
import websocket  # websocket-client

n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
base = tempfile.mkdtemp()
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
token = secrets.token_hex(16)
env = {**os.environ, "JUPYTER_RUNTIME_DIR": base, "JUPYTER_CONFIG_DIR": base}
server = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "jupyter_server",
        "--no-browser",
        "--ip=127.0.0.1",
        f"--port={port}",
        f"--IdentityProvider.token={token}",
        f"--ServerApp.root_dir={base}",
    ],
    env=env,
    stdout=open(os.path.join(base, "server.log"), "w"),
    stderr=subprocess.STDOUT,
)
url, headers = f"http://127.0.0.1:{port}", {"Authorization": f"token {token}"}


def request(ws, session):
    msg_id = uuid.uuid4().hex
    ws.send(
        json.dumps(
            {
                "header": {
                    "msg_id": msg_id,
                    "username": "u",
                    "session": session,
                    "msg_type": "execute_request",
                    "version": "5.3",
                    "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                },
                "parent_header": {},
                "metadata": {},
                "channel": "shell",
                "buffers": [],
                "content": {
                    "code": "1",
                    "silent": True,
                    "store_history": False,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": False,
                },
            }
        )
    )
    until = time.monotonic() + 3
    ws.settimeout(0.3)
    while time.monotonic() < until:
        try:
            msg = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if msg.get("channel") == "shell" and msg["parent_header"].get("msg_id") == msg_id:
            return True
    return False


try:
    for _ in range(60):
        try:
            if requests.get(url + "/api", headers=headers, timeout=2).ok:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.5)
    kernel_id = requests.post(
        url + "/api/kernels", headers=headers, json={"name": "python3"}
    ).json()["id"]
    channels = f"ws://127.0.0.1:{port}/api/kernels/{kernel_id}/channels"
    first = websocket.create_connection(f"{channels}?session_id={uuid.uuid4().hex}", header=headers)
    request(first, "warm-up")
    first.close()  # the kernel is up and idle
    dead = []
    for i in range(n):
        session = uuid.uuid4().hex
        ws = websocket.create_connection(
            f"{channels}?session_id={session}", header=headers, timeout=5
        )
        if delay:
            time.sleep(delay)
        answered = request(ws, session)
        if not answered:
            second = "answered" if request(ws, session) else "unanswered too"
        ws.close()
        if not answered:
            dead.append((i, f"second request {second}"))
        time.sleep(0.6)
    print(json.dumps({"connections": n, "delay_s": delay, "dead": dead}))
finally:
    server.terminate()
    server.wait(15)
```

## Results (connections whose request got no answer within 3 s)

| Setup | Stuck |
|---|---|
| `jupyter server`, ipykernel 7.3.0, send at once | 2/300, 3/200 |
| the same, first send 20 ms after the handshake | 0/300 |
| send at once, in a clean venv with only these packages | 5/300 |
| clean venv, ipykernel 6.31.0, send at once | 0/300, 0/300 |
| JupyterLab 4.6.4 (jupyter-collaboration 5.0.4), jupyter-kernel-client, a `kernel_info_request` ~3 ms after the handshake | 8/300 |
| the same, 20 ms after the handshake | 0/300 |
| jupyter_client's `BlockingKernelClient` on the kernel's connection file (no server), send at once (earlier) | 0/200 |

## The request waits for another client

With jupyter-kernel-client against JupyterLab, 500 connections each sending a
`kernel_info_request` right after the handshake: 10 got no answer in 0.5 s, nor in 2.5 s more.
For each, the script then opened a second connection to the kernel and sent nothing on it: the
first connection's request was answered 4-13 ms later (10 of 10), and a second request on it was
answered at once. Opening a connection makes the server connect new ZMQ sockets to the kernel
and nudge it (`kernel_info` on transient shell and control channels), so the stuck request seems
to wait for activity on the kernel's sockets from another client; a second request from the same
connection doesn't release it.

Earlier, with trace points added to `ZMQChannelsWebsocketConnection` (scratch monkeypatches,
nothing else changed), a stuck connection looked like a live one up to the kernel: `open()`
returns, the client's request reaches `handle_incoming_message` 0.2 ms later and goes to the
connection's shell `ZMQStream`, and that stream's send queue is empty 0.5 s later: the message
was handed to ZMQ. The connection's iopub keeps forwarding messages, but the kernel publishes no
`busy` for the request. The server's debug log shows its own nudge resolving, often on the
control channel's reply.

## Expected

A message sent right after the handshake runs: tornado hands it to `on_message` only after
`open()` (and so the nudge) has completed, so a client shouldn't have to wait.

## Workaround (what we ship)

Wait 20 ms after the handshake before the first message; when the kernel is idle, check a new
connection with a `kernel_info_request` (its busy or its reply within 0.5 s); if it doesn't
answer, open another connection (0.1 s before its first message: an immediate retry stuck 3 of
24 times, 0 of 22 after 20 ms), check it the same way, and use the first connection whose own
check answers in time (at most three), closing the unanswered ones. We used to take the stuck
connection once it answered (after the next one opened), but under load such a connection
sometimes missed the next request (2 traced cases among 122 such connections).

## Separately: Nagle on kernel websockets (jupyter_server)

`KernelWebsocketHandler` never calls tornado's `set_nodelay(True)`, so with the client's delayed
ACKs every frame the server writes while an earlier one is unacknowledged waits ~40 ms. 100 new
connections, a `kernel_info_request` 30 ms after the handshake: `busy` p50 4.0 ms; reply p50
45.1 ms, p95 48.1 ms (76 of 100 at 30 ms or more; in the rest the busy came second and waited);
with a server config calling `self.set_nodelay(True)` in `open()`: reply p50 4.1 ms, p95 6.0 ms.
nh's attach probe (one small `execute_request`) on a connected client, until its reply and output: p50 48.8 ms, 9.7 ms with
`set_nodelay`. Over ZMQ without the server: 2-5 ms. Interactive frontends pay this on every
request.

## Added later (2026-10-03, not in the numbers above)

A 20 ms wait does not cover a kernel just started: fresh kernels (POST `/api/kernels`), nh's
client connecting and probing at once, 120 each: 4 dead with no wait, 4 with 20 ms. The server
read every one as `starting` at connect. Waiting until it no longer reads `starting`, then
checking the connection, gave 0 of 200 (design §6.13).
