"""Network hosts: what a URL connects to, and the project's approved hosts (design §6.4).

``.nh/state/approved_hosts.json`` is a JSON list of host keys: host names (lowercase IDNA
ASCII, no scheme, port, userinfo or trailing dot), and an object store's bucket as
``s3://<bucket>``, ``gs://<bucket>`` or ``az://<container>``. ``nhctl scaffold`` writes it (the
data URL's host) and the user may edit it; the gateway reads it on every ``nh_add_cell`` and
``nh_edit_cell`` call and passes the set to lint, which skips an L012 site whose every host is in
it.

Stdlib only and Python 3.9 compatible: nhctl may run on the system Python, and lint imports it
in CI's no-project job.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .paths import atomic_write_json

# Schemes whose URL reaches another machine (a database URL, ``file://`` and the rest don't).
NETWORK_SCHEMES = frozenset(
    {"http", "https", "ftp", "ftps", "sftp", "scp", "ssh", "git", "rsync", "ws", "wss"}
    | {"s3", "s3a", "s3n", "gs", "gcs", "az", "abfs", "abfss", "adl", "wasb", "wasbs"}
    | {"hdfs", "webhdfs", "hf", "smb"}
)
# Object stores whose URL names a bucket, not a host: its key keeps the store, so the same
# name on another store (or a host of that name) is another key.
BUCKET_STORES = {"s3": "s3", "s3a": "s3", "s3n": "s3", "gs": "gs", "gcs": "gs", "az": "az"}
# Schemes that always reach one service, whatever their URL's first part says.
SERVICE_HOSTS = {"hf": "huggingface.co"}
MAX_FILE_BYTES = 1 << 20  # a larger approved_hosts.json reads as corrupt: it is a short list
LIST_PATH = ".nh/state/approved_hosts.json"  # as problems() names it

_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)(.*)\Z", re.S)
_TEMPLATE = re.compile(r"[{}%$<>\s\\]")  # a host built at run time: "https://{host}/x"
_ASCII_HOST = re.compile(r"[a-z0-9_\-]+(?:\.[a-z0-9_\-]+)*")
_BARE_HOST = re.compile(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+\.?(?::\d{1,5})?|localhost")
# IDNA 2003 (Python's codec) maps these to other names than IDNA 2008/UTS 46, which requests and
# httpx send: "faß.de" is fass.de to the codec but xn--fa-hia.de on the wire.
_DEVIATIONS = re.compile("[ßς‌‍]")


def normalize(host: str) -> str | None:
    """``host`` as the approved list holds it: lowercase, without userinfo, port, IPv6 brackets
    or a trailing dot, IDNA-encoded (``Bücher.Example.`` -> ``xn--bcher-kva.example``). None
    when it isn't a host name nh can read."""
    if not isinstance(host, str):
        return None
    host = host.strip().rpartition("@")[2]
    if host.startswith("["):  # [::1]:8888
        inner, close, rest = host[1:].partition("]")
        if not close or (rest and not re.fullmatch(r":\d*", rest)):
            return None
        return _ipv6(inner)
    if host.count(":") > 1:  # a bare IPv6 address, as the list may hold it
        return _ipv6(host)
    name, colon, port = host.partition(":")
    if colon and not port.isdigit() and port != "":
        return None
    if name.endswith("."):
        name = name[:-1]
    if not name or _TEMPLATE.search(name):
        return None
    if not name.isascii():
        if _DEVIATIONS.search(name.lower()):
            return None  # IDNA 2003 and 2008 disagree on it: a host nh can't read
        try:
            name = name.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    name = name.lower()
    return name if _ASCII_HOST.fullmatch(name) else None


def _ipv6(text: str) -> str | None:
    try:
        return str(ipaddress.IPv6Address(text.split("%", 1)[0]))
    except ValueError:
        return None


def is_loopback(host: str) -> bool:
    """``localhost``, ``*.localhost``, 127.0.0.0/8, ``::1``, ``0.0.0.0`` and ``::``: this
    machine, so no network (a local JupyterLab, API or database)."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def host_key(scheme: str, host: str, userinfo: bool = False) -> str | None:
    """What a ``scheme://[userinfo@]host`` URL reaches, as the approved list names it: the
    normalised host, a bucket as ``<store>://<bucket>``, ``huggingface.co`` for ``hf://``. None
    when the host can't be read."""
    scheme = scheme.lower()
    if scheme in SERVICE_HOSTS:
        return SERVICE_HOSTS[scheme]
    name = normalize(host)
    store = BUCKET_STORES.get(scheme)
    if store == "az" and userinfo:  # az://container@account.dfs.core.windows.net: a real host
        store = None
    return f"{store}://{name}" if store and name else name


def network_url(text: str) -> tuple[bool, str | None]:
    """``(True, key)`` for a network URL (one of ``NETWORK_SCHEMES``, with a host): its host key
    (``host_key``). ``(True, None)`` for one whose host nh can't read, ``(False, None)`` for
    anything else: another scheme, a path, prose that holds a URL, a URL with no host or one
    built at run time (``https://{host}/x``). The userinfo may hold anything (``%40``, ``$``, a
    ``{}`` template): it is dropped."""
    if not isinstance(text, str):
        return False, None
    found = _URL.fullmatch(text.strip())
    if not found or found.group(1).lower() not in NETWORK_SCHEMES:
        return False, None
    netloc, rest = found.group(2), found.group(3)
    _, at, host = netloc.rpartition("@")
    if not host or _TEMPLATE.search(host) or any(ch.isspace() for ch in netloc):
        return False, None
    if any(ch.isspace() for ch in rest):
        return False, None  # "https://x.org/a b": prose, not a URL
    return True, host_key(found.group(1), host, bool(at))


def bare_host(text: str) -> str | None:
    """A string that is only a host (``"api.example.org"``, ``"example.org:443"``,
    ``"10.0.0.5"``, ``"localhost"``), normalised; None for anything else (a path, a word)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if _BARE_HOST.fullmatch(text):
        return normalize(text)
    if ":" in text:
        try:
            return str(ipaddress.IPv6Address(text.strip("[]")))
        except ValueError:
            return None
    return None


def normalize_entry(entry: Any) -> str | None:
    """A list entry as a host key: a host (``normalize``) or a bucket key (``s3://trips``,
    ``gs://…``, ``az://…``, nothing after the bucket but ``/``). None for anything else: a URL, a
    path, a wildcard, a number."""
    if not isinstance(entry, str):
        return None
    text = entry.strip()
    if "://" not in text:
        return normalize(text)
    found = _URL.fullmatch(text)
    if not found or found.group(3) not in ("", "/") or "@" in found.group(2):
        return None
    store = BUCKET_STORES.get(found.group(1).lower())
    name = normalize(found.group(2)) if store else None
    return f"{store}://{name}" if name else None


def _load(path: Path) -> tuple[list[Any] | None, bool]:
    """The list's entries and whether the file is a problem: (None, False) for a missing file,
    (None, True) for one nh can't use (unreadable, over ``MAX_FILE_BYTES``, not a JSON list)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, False
    except Exception:  # a folder, no permission: nothing approved, and worth a line
        return None, True
    try:
        if len(raw) > MAX_FILE_BYTES:
            return None, True
        entries = json.loads(raw.decode("utf-8-sig"))  # a BOM some editors add is fine
    except Exception:
        return None, True
    return (entries, False) if isinstance(entries, list) else (None, True)


def read_approved(path: Path) -> frozenset[str]:
    """The approved host keys in ``path``. A missing, unreadable, oversized or corrupt file (not
    JSON, not a list) is an empty set, an entry that isn't a host key is skipped, and nothing
    here raises."""
    try:
        entries, _ = _load(path)
        return frozenset(filter(None, map(normalize_entry, entries or [])))
    except Exception:  # the list never breaks a write: an unreadable one approves nothing
        return frozenset()


def problems(path: Path) -> list[str]:
    """Why the list at ``path`` approves less than it holds, for the config lines nh shows
    (design §6.4). An entry is named by its place, never quoted: a pasted URL may hold a token.
    A missing file is no problem, and nothing here raises."""
    try:
        entries, broken = _load(path)
        if broken:
            return [f"`{LIST_PATH}` is not a JSON list of host names: nh approves no host from it."]
        bad = [n for n, entry in enumerate(entries or [], 1) if normalize_entry(entry) is None]
    except Exception:
        return []
    if not bad:
        return []
    named = [str(n) for n in bad[:3]] + ([f"{len(bad) - 3} more"] if len(bad) > 3 else [])
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    one = len(bad) == 1
    return [
        f"`{LIST_PATH}` {'entry' if one else 'entries'} {listed} "
        f"{'is not a host name' if one else 'are not host names'} (write the host only, such as "
        f"`data.example.org`): {'it approves' if one else 'they approve'} nothing."
    ]


def approve(path: Path, keys: Iterable[str]) -> str:
    """Add ``keys`` (hosts or bucket keys) to the list at ``path``: ``created``, ``updated`` (the
    entries there are kept as they are, odd ones included), ``kept`` (all listed already) or
    ``skipped`` (the file can't be read or isn't a JSON list, or no key can be read). Written
    atomically."""
    wanted = [k for k in (normalize_entry(key) for key in keys) if k]
    if not wanted:
        return "skipped"
    entries, broken = _load(path)
    if broken:
        return "skipped"
    if entries is None:
        atomic_write_json(path, list(dict.fromkeys(wanted)))
        return "created"
    listed = {normalize_entry(e) for e in entries}
    missing = [k for k in dict.fromkeys(wanted) if k not in listed]
    if not missing:
        return "kept"
    atomic_write_json(path, entries + missing)
    return "updated"
