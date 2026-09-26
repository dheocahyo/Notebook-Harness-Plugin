"""Probe sources run in a plain exec namespace with a display shim, on Python 3.11 and 3.13, with and
without pandas. The probe code is exactly what the gateway sends (exec/probes.probe_code), executed
with the user namespace as globals, as the kernel does. It must bind nothing there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from functools import cache

import pytest

from nh_gateway.exec import probes

HARNESS = r"""
import builtins, json, sys, time, types
spec = json.load(sys.stdin)
captured = []
def display(*objs, raw=False, **kwargs):
    captured.extend(objs)
shell = types.SimpleNamespace(user_ns=None, user_ns_hidden={}, execution_count=7)
builtins.display = display
builtins.get_ipython = lambda: shell
ns = {"__name__": "__main__", "In": [""], "Out": {}, "exit": None, "quit": None, "get_ipython": builtins.get_ipython}
shell.user_ns = ns
shell.user_ns_hidden = dict(ns)
exec(spec["setup"], ns)
before = dict(ns)
start = time.monotonic()
exec(spec["code"], ns)
elapsed = time.monotonic() - start
changed = sorted(k for k in set(ns) | set(before) if k not in ns or k not in before or ns[k] is not before[k])
print(json.dumps({"captured": captured, "changed": changed, "elapsed": elapsed, "python": list(sys.version_info[:2])}))
"""

PLAIN_SETUP = """
import os
n = 42
s = "hello"
l = [1, 2, 3]
d = {"a": 1}
big = 2**80
_hidden = 1
def f():
    pass
class C:
    pass
obj = C()
class Weird:
    def __len__(self):
        raise RuntimeError("no len")
weird = Weird()
"""

PANDAS_SETUP = (
    PLAIN_SETUP
    + """
import numpy as np
import pandas as pd
df = pd.DataFrame({"price": [1.0, None, 3.0, None], "region": ["n", "s", None, "e"], "qty": [1, 2, 3, 4]})
ser = pd.Series([1, None, 3], name="x")
arr = np.zeros((4, 3))
count = np.int64(5)
"""
)


@cache
def interpreters() -> dict[str, list[str]]:
    """label -> argv for python; '-S' drops site-packages, i.e. no pandas installed."""
    found = {"3.13+pandas": [sys.executable], "3.13-pandas": [sys.executable, "-S"]}
    uv = shutil.which("uv")
    if uv:
        result = subprocess.run(
            [uv, "python", "find", "3.11"], capture_output=True, text=True, timeout=30
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path:
            has_pandas = (
                subprocess.run([path, "-c", "import pandas, numpy"], capture_output=True).returncode
                == 0
            )
            if has_pandas:
                found["3.11+pandas"] = [path]
            found["3.11-pandas"] = [path, "-S"]
    return found


def run(label: str, name: str, args: dict, setup: str = PLAIN_SETUP) -> dict:
    argv = interpreters().get(label)
    if argv is None:
        pytest.skip(f"no interpreter for {label}")
    spec = {"setup": setup, "code": probes.probe_code(name, args)}
    proc = subprocess.run(
        [*argv, "-c", HARNESS], input=json.dumps(spec), capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["changed"] == [], f"probe touched the user namespace: {out['changed']}"
    assert len(out["captured"]) == 1, "a probe answers with exactly one display()"
    out["payload"] = out["captured"][0][probes.PROBE_MIME]
    return out


ALL = ["3.11-pandas", "3.11+pandas", "3.13-pandas", "3.13+pandas"]
PANDAS = ["3.11+pandas", "3.13+pandas"]
VARS_ARGS = {"names": None, "max_vars": 40, "budget_s": 0.8, "max_cells": 20_000_000}


@pytest.mark.parametrize("label", ALL)
def test_attach(label):
    out = run(label, "attach", {})
    payload = out["payload"]
    assert payload["python"] == out["python"]
    assert payload["exec_count"] == 7
    assert payload["prefix"] and payload["executable"] and payload["cwd"]


@pytest.mark.parametrize("label", ALL)
def test_vars_plain_objects(label):
    payload = run(label, "vars", VARS_ARGS)["payload"]
    found = payload["vars"]
    assert found["n"] == {"kind": "scalar", "type": "int", "repr": "42"}
    assert found["s"] == {"kind": "scalar", "type": "str", "repr": "'hello'"}
    assert found["l"] == {"kind": "container", "type": "list", "len": 3}
    assert found["d"] == {"kind": "container", "type": "dict", "len": 1}
    assert found["obj"] == {"kind": "object", "type": "__main__.C"}
    assert found["weird"] == {"kind": "object", "type": "__main__.Weird"}
    assert found["big"]["kind"] == "scalar"
    for skipped in ("os", "f", "C", "_hidden", "In", "Out", "exit", "quit", "get_ipython"):
        assert skipped not in found
    assert payload["truncated"] is False
    json.dumps(payload)  # plain JSON only


@pytest.mark.parametrize("label", ["3.11-pandas", "3.13-pandas"])
def test_packages_without_pandas_installed(label):
    packages = run(label, "vars", VARS_ARGS)["payload"]["packages"]
    assert "pandas" not in packages  # not installed: omitted


def test_packages_installed_but_not_imported():
    packages = run("3.13+pandas", "vars", VARS_ARGS)["payload"]["packages"]
    assert packages["pandas"] is None


@pytest.mark.parametrize("label", PANDAS)
def test_vars_with_pandas(label):
    payload = run(label, "vars", VARS_ARGS, PANDAS_SETUP)["payload"]
    found = payload["vars"]
    df = found["df"]
    assert df["kind"] == "DataFrame" and df["lib"] == "pandas"
    assert df["shape"] == [4, 3]
    assert df["columns"] == ["price", "region", "qty"]
    assert df["dtypes"]["qty"] == "int64"
    assert df["nulls"] == {"price": 2, "region": 1}
    assert df["mem"] > 0
    assert isinstance(df["sums"], str) and isinstance(found["ser"].pop("sums"), str)
    assert found["ser"] == {
        "kind": "Series",
        "lib": "pandas",
        "len": 3,
        "dtype": "float64",
        "name": "x",
        "nulls": 1,
    }
    assert found["arr"] == {"kind": "ndarray", "shape": [4, 3], "dtype": "float64"}
    assert found["count"]["kind"] == "scalar"
    assert "pd" not in found and "np" not in found
    assert payload["packages"]["pandas"] and payload["packages"]["numpy"]


@pytest.mark.parametrize("label", PANDAS)
def test_vars_skips_null_counts_on_huge_frames(label):
    args = {**VARS_ARGS, "names": ["df"], "max_cells": 5}
    df = run(label, "vars", args, PANDAS_SETUP)["payload"]["vars"]["df"]
    assert "nulls" not in df


@pytest.mark.parametrize("label", ["3.11-pandas", "3.13+pandas"])
def test_vars_names_limit_and_budget(label):
    payload = run(label, "vars", {**VARS_ARGS, "names": ["n", "l", "missing", "f"]})["payload"]
    assert sorted(payload["vars"]) == ["l", "n"]
    payload = run(label, "vars", {**VARS_ARGS, "max_vars": 2})["payload"]
    assert len(payload["vars"]) == 2 and payload["truncated"] is True
    payload = run(label, "vars", {**VARS_ARGS, "budget_s": 0.0})["payload"]
    assert payload["truncated"] is True


@pytest.mark.parametrize("label", PANDAS)
def test_var_frame_head(label):
    payload = run(label, "var", {"name": "df", "rows": 2}, PANDAS_SETUP)["payload"]
    assert payload["name"] == "df"
    assert payload["summary"]["shape"] == [4, 3]
    assert "price" in payload["head"] and len(payload["head"].splitlines()) == 3
    assert payload["text"] == ""


@pytest.mark.parametrize("label", ALL)
def test_var_plain_and_errors(label):
    payload = run(label, "var", {"name": "l", "rows": 5})["payload"]
    assert payload["summary"] == {"kind": "container", "type": "list", "len": 3}
    assert payload["text"] == "[1, 2, 3]" and payload["head"] == ""
    assert run(label, "var", {"name": "nope"})["payload"] == {
        "error": "NameError: name 'nope' is not defined"
    }
    assert run(label, "var", {"name": "os.path"})["payload"]["error"].startswith("ValueError")
    assert run(label, "var", {"name": "import"})["payload"]["error"].startswith("ValueError")


def test_probe_code_is_one_expression_and_rejects_unknown_probes():
    code = probes.probe_code("vars", VARS_ARGS)
    assert "\n" not in code
    compile(code, "<probe>", "eval")
    with pytest.raises(ValueError):
        probes.probe_code("../etc", {})


def test_extract_payload():
    good = [{"output_type": "display_data", "data": {probes.PROBE_MIME: {"a": 1}}, "metadata": {}}]
    assert probes.extract_payload(good) == {"a": 1}
    error = [{"output_type": "error", "ename": "NameError", "evalue": "x", "traceback": []}]
    assert probes.extract_payload(error) == {"error": "NameError: x"}
    assert probes.extract_payload([])["error"].startswith("ProbeError")


# Finding 67: a look never runs user code. Each of these kills the kernel process if it is called.
TRAPS_SETUP = """
import os
class Lazy:
    def __len__(self):
        os._exit(17)
    def __repr__(self):
        os._exit(18)
    def __iter__(self):
        os._exit(19)
lazy = Lazy()
boxed = [Lazy(), 1, "x"]
class Sneaky(list):
    def __len__(self):
        os._exit(20)
    def __repr__(self):
        os._exit(21)
sneaky = Sneaky([1, 2])
class Loud(int):
    def __repr__(self):
        os._exit(22)
loud = Loud(5)
class Text(str):
    def __repr__(self):
        os._exit(23)
    def __getitem__(self, key):
        os._exit(24)
text = Text("hello")
class list:  # noqa: A001 -- a user class that shadows a builtin's name
    def __repr__(self):
        os._exit(25)
fake_list = list()
"""


@pytest.mark.parametrize("label", ["3.11-pandas", "3.13-pandas"])
def test_probes_never_call_user_len_or_repr(label):
    found = run(label, "vars", VARS_ARGS, TRAPS_SETUP)["payload"]["vars"]
    assert found["lazy"] == {"kind": "object", "type": "__main__.Lazy"}
    assert found["boxed"] == {"kind": "container", "type": "list", "len": 3}
    assert found["sneaky"] == {"kind": "container", "type": "__main__.Sneaky", "len": 2}
    assert found["loud"]["repr"] == "5"
    assert found["text"]["repr"] == "'hello'"
    for name in ("lazy", "boxed", "sneaky", "loud", "text", "fake_list"):
        payload = run(label, "var", {"name": name, "rows": 5}, TRAPS_SETUP)["payload"]
        assert "error" not in payload, payload
    assert run(label, "var", {"name": "lazy"}, TRAPS_SETUP)["payload"]["text"] == (
        "<__main__.Lazy object>"
    )
    assert run(label, "var", {"name": "boxed"}, TRAPS_SETUP)["payload"]["text"] == (
        "[<__main__.Lazy object>, 1, 'x']"
    )


def test_attach_identifies_the_kernel_process():
    payload = run("3.13-pandas", "attach", {})["payload"]
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert isinstance(payload["started"], float)


class _Channel:
    def __init__(self) -> None:
        import queue

        self.queue: queue.Queue[dict] = queue.Queue()

    def get_msg(self, timeout: float | None = None) -> dict:
        return self.queue.get(timeout=timeout)

    def get_msgs(self) -> list[dict]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out


class _ProbeClient:
    """A kernel client whose kernel starts (or only queues) the probe and then never answers."""

    def __init__(self, starts: bool) -> None:
        import threading

        self.iopub_channel = _Channel()
        self.shell_channel = _Channel()
        self.connection_ready = threading.Event()
        self.connection_ready.set()
        self.starts = starts

    def execute(self, code: str, **kwargs) -> str:
        if self.starts:
            self.iopub_channel.queue.put(
                {
                    "header": {"msg_type": "status"},
                    "parent_header": {"msg_id": "probe-1", "msg_type": "execute_request"},
                    "content": {"execution_state": "busy"},
                }
            )
        return "probe-1"

    def stop(self) -> None:
        """What the kernel does on SIGINT: our KeyboardInterrupt, then idle."""
        for kind, content in (
            ("error", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": []}),
            ("status", {"execution_state": "idle"}),
        ):
            self.iopub_channel.queue.put(
                {
                    "header": {"msg_type": kind},
                    "parent_header": {"msg_id": "probe-1", "msg_type": "execute_request"},
                    "content": content,
                }
            )


def test_a_probe_that_overruns_is_interrupted_only_when_it_is_what_runs():
    started = _ProbeClient(starts=True)
    stops: list[str] = []
    payload = probes.run_probe(
        started, "vars", VARS_ARGS, 0.3, interrupt=lambda: (stops.append("x"), started.stop())
    )
    assert stops == ["x"] and "nh stopped its look" in payload["error"]
    assert started.iopub_channel.queue.empty(), "its KeyboardInterrupt and idle were consumed"
    queued = _ProbeClient(starts=False)
    stops.clear()
    payload = probes.run_probe(queued, "vars", VARS_ARGS, 0.3, interrupt=lambda: stops.append("x"))
    assert stops == [] and payload["error"].startswith("TimeoutError")  # someone else's cell runs
