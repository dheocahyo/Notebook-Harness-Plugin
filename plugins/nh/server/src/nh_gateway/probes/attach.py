# nh probe "attach": which Python runs this kernel. Runs inside the user's kernel via exec() with
# throwaway globals, so nothing is bound in the user namespace. Stdlib only; Python 3.10+.
# "pid" and "started" identify the kernel process: a restart keeps Jupyter's kernel id but starts
# a new process. "started" is when nh first saw this process (kept on the sys module, not in the
# user namespace), so it survives pid reuse.
import os
import sys
import time

try:
    _shell = get_ipython()  # noqa: F821 -- an IPython builtin while a cell runs
except NameError:
    _shell = None

try:
    _payload = {
        "python": [sys.version_info[0], sys.version_info[1]],
        "prefix": sys.prefix,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "exec_count": getattr(_shell, "execution_count", None),  # the count the next cell will get
        "pid": os.getpid(),
        "started": sys.__dict__.setdefault("_nh_started", time.time()),
    }
except Exception as _exc:  # a probe always answers, even when it fails
    _payload = {"error": f"{type(_exc).__name__}: {_exc}"}

display({"application/vnd.nh.probe+json": _payload}, raw=True)  # noqa: F821
