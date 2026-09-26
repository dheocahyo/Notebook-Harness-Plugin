"""Entry point: ``python -m nh_gateway`` (launched by libexec/nh-mcp over stdio)."""

import os

# Keep the Jupyter token and localhost traffic away from any configured HTTP proxy. This must
# happen before requests/websockets are imported, which read proxy settings at import time.
_local = "localhost,127.0.0.1,::1"
for _var in ("NO_PROXY", "no_proxy"):
    _current = os.environ.get(_var, "")
    os.environ[_var] = f"{_current},{_local}" if _current else _local

from nh_gateway.app import main  # noqa: E402

main()
