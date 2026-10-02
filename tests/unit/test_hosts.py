"""_shared/hosts.py: hosts as the approved list holds them, network URLs, and the list file
(design §6.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nh_gateway._shared import hosts
from nh_gateway._shared.paths import Layout


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("data.example.org", "data.example.org"),
        ("DATA.Example.ORG", "data.example.org"),
        ("data.example.org.", "data.example.org"),
        ("data.example.org:8443", "data.example.org"),
        ("analyst:pw@data.example.org:8443", "data.example.org"),
        ("  data.example.org  ", "data.example.org"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("BÜCHER.Example.", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("[2001:DB8::1]:8080", "2001:db8::1"),
        ("2001:db8:0:0::1", "2001:db8::1"),
        ("[::1]", "::1"),
        ("127.0.0.1:5000", "127.0.0.1"),
        ("my_bucket", "my_bucket"),
        ("localhost", "localhost"),
        ("", None),
        (".", None),
        ("a..b", None),
        ("data.example.org:http", None),
        ("{host}.example.org", None),
        ("data example.org", None),
        ("data%2Eexample.org", None),
        ("[::1", None),
        ("[not-ipv6]", None),
        ("\udcff.example", None),  # IDNA can't encode it
        ("faß.de", None),  # IDNA 2003 and 2008 disagree on the deviation characters
        ("FASS.ßeispiel", None),
        ("ςa.example", None),
        ("a\u200db.example", None),
        ("a\u200cb.example", None),
        ("xn--fa-hia.de", "xn--fa-hia.de"),  # its IDNA 2008 form is a plain name
    ],
)
def test_normalize(raw: str, expected: str | None) -> None:
    assert hosts.normalize(raw) == expected


def test_normalize_takes_only_strings() -> None:
    assert hosts.normalize(7) is None  # type: ignore[arg-type]
    assert hosts.normalize(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://data.example.org/trips.csv", (True, "data.example.org")),
        ("HTTP://Data.Example.ORG.:80", (True, "data.example.org")),
        ("https://u:p@data.example.org:8443/x?token=1#f", (True, "data.example.org")),
        ("  https://data.example.org/x  ", (True, "data.example.org")),
        ("s3://trips-bucket/key.parquet", (True, "s3://trips-bucket")),
        ("S3A://Trips-Bucket/key", (True, "s3://trips-bucket")),
        ("s3n://trips-bucket", (True, "s3://trips-bucket")),
        ("gs://trips-bucket/key", (True, "gs://trips-bucket")),
        ("gcs://trips-bucket/key", (True, "gs://trips-bucket")),
        ("az://trips/key.parquet", (True, "az://trips")),
        ("az://trips@acct.blob.core.windows.net/k", (True, "acct.blob.core.windows.net")),
        ("abfss://c@acct.dfs.core.windows.net/p", (True, "acct.dfs.core.windows.net")),
        ("ftp://ftp.example.org/pub", (True, "ftp.example.org")),
        ("ssh://git@github.com/org/repo.git", (True, "github.com")),
        ("hf://datasets/org/name", (True, "huggingface.co")),
        ("hf://models/org/name/model.safetensors", (True, "huggingface.co")),
        ("https://user:p%40ss@data.example.org/x.csv", (True, "data.example.org")),
        ("https://user:pa$$w0rd@data.example.org/x.csv", (True, "data.example.org")),
        ("https://{}:{}@data.example.org/x.csv", (True, "data.example.org")),
        ("https://a@b@data.example.org/x", (True, "data.example.org")),
        ("https://faß.de/data.json", (True, None)),
        ("https://xn--fa-hia.de/data.json", (True, "xn--fa-hia.de")),
        ("wss://stream.example.org/feed", (True, "stream.example.org")),
        ("http://localhost:8888/api", (True, "localhost")),
        ("https://[::1]:8888/api", (True, "::1")),
        ("https://bücher.example/x", (True, "xn--bcher-kva.example")),
        ("https://" + "\udcff" + ".example/x", (True, None)),
        ("postgresql://u:p@db.example.org/sales", (False, None)),
        ("sqlite:///trips.db", (False, None)),
        ("file:///tmp/trips.csv", (False, None)),
        ("mailto:a@example.org", (False, None)),
        ("../data/raw/trips.csv", (False, None)),
        ("data.example.org/trips.csv", (False, None)),
        ("see https://data.example.org", (False, None)),
        ("https://data.example.org/a b.csv", (False, None)),
        ("https://data.example.org /x", (False, None)),
        ("https://us er:pw@data.example.org/x", (False, None)),
        ("https://", (False, None)),
        ("https:///path", (False, None)),
        ("https://{host}/x", (False, None)),
        ("https://%s/x", (False, None)),
        ("https://$HOST/x", (False, None)),
        ("https://<host>/x", (False, None)),
    ],
)
def test_network_url(text: str, expected: tuple[bool, str | None]) -> None:
    assert hosts.network_url(text) == expected


def test_every_network_scheme_is_lowercase_and_no_database_one() -> None:
    from nh_gateway._shared import secrets

    for scheme in hosts.NETWORK_SCHEMES:
        assert scheme == scheme.lower() and not secrets.SQL_SCHEME.match(scheme), scheme
        store = hosts.BUCKET_STORES.get(scheme)
        key = hosts.SERVICE_HOSTS.get(scheme) or (
            f"{store}://h.example.org" if store else "h.example.org"
        )
        assert hosts.network_url(f"{scheme}://h.example.org/x") == (True, key), scheme
        assert hosts.normalize_entry(key) == key, scheme  # a site's key can be listed as it is
    assert "file" not in hosts.NETWORK_SCHEMES


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("data.example.org", "data.example.org"),
        ("DATA.example.org.", "data.example.org"),
        ("s3://trips-bucket", "s3://trips-bucket"),
        ("s3://trips-bucket/", "s3://trips-bucket"),
        ("S3A://Trips-Bucket", "s3://trips-bucket"),
        ("gcs://trips-bucket", "gs://trips-bucket"),
        ("az://trips", "az://trips"),
        (" gs://trips-bucket ", "gs://trips-bucket"),
        ("s3://trips-bucket/2023/", None),  # a path in a bucket: write the bucket only
        ("s3://user@trips-bucket", None),
        ("https://data.example.org", None),  # a URL: write the host only
        ("https://data.example.org/", None),
        ("hf://datasets", None),
        ("*.example.org", None),
        ("../data", None),
        ("faß.de", None),
        ("", None),
        (7, None),
        (None, None),
        (["data.example.org"], None),
    ],
)
def test_normalize_entry(entry: object, expected: str | None) -> None:
    assert hosts.normalize_entry(entry) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("api.example.org", "api.example.org"),
        ("API.example.org:443", "api.example.org"),
        ("10.0.0.5", "10.0.0.5"),
        ("localhost", "localhost"),
        ("::1", "::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("api", None),
        ("a/b", None),
        ("https://api.example.org", None),
        ("api example.org", None),
    ],
)
def test_bare_host(text: str, expected: str | None) -> None:
    assert hosts.bare_host(text) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("lab.localhost", True),
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("0.0.0.0", True),
        ("::", True),
        ("10.0.0.5", False),
        ("localhost.example.org", False),
        ("example.org", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert hosts.is_loopback(host) is expected


def test_the_file_lives_in_the_state_folder(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    assert layout.approved_hosts == tmp_path / ".nh" / "state" / "approved_hosts.json"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, frozenset()),
        (b"", frozenset()),
        (b"{", frozenset()),
        (b"\xff\xfe", frozenset()),
        (b'{"hosts": ["data.example.org"]}', frozenset()),
        (b'"data.example.org"', frozenset()),
        (b"null", frozenset()),
        (b'["data.example.org"]', frozenset({"data.example.org"})),
        (
            b'["DATA.example.org.", "api.example.org:443", 7, null, ["x"], "not a host!", ""]',
            frozenset({"data.example.org", "api.example.org"}),
        ),
        ('["bücher.example"]'.encode(), frozenset({"xn--bcher-kva.example"})),
        ('["faß.de", "fass.de"]'.encode(), frozenset({"fass.de"})),
        (
            b'["s3://trips-bucket", "gcs://x/", "s3://y/key"]',
            frozenset({"s3://trips-bucket", "gs://x"}),
        ),
        (b'\xef\xbb\xbf["data.example.org"]', frozenset({"data.example.org"})),  # a BOM
        ('["data.example.org"]'.encode("utf-16"), frozenset()),
        (b"[" + b'"x.example",' * 10 + b'"y.example"]', frozenset({"x.example", "y.example"})),
    ],
)
def test_read_approved(tmp_path: Path, content: bytes | None, expected: frozenset[str]) -> None:
    path = tmp_path / "approved_hosts.json"
    if content is not None:
        path.write_bytes(content)
    assert hosts.read_approved(path) == expected


def test_read_approved_never_raises(tmp_path: Path) -> None:
    folder = tmp_path / "approved_hosts.json"
    folder.mkdir()  # a folder where the file goes
    assert hosts.read_approved(folder) == frozenset()
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000)
    assert hosts.read_approved(deep) == frozenset()
    big = tmp_path / "big.json"
    big.write_text(json.dumps(["data.example.org"] + ["x" * 100] * 20_000))
    assert big.stat().st_size > hosts.MAX_FILE_BYTES
    assert hosts.read_approved(big) == frozenset()


def test_the_size_limit_is_on_the_file(tmp_path: Path) -> None:
    listed = b'["data.example.org"]'
    padded = tmp_path / "padded.json"
    padded.write_bytes(listed + b" " * (hosts.MAX_FILE_BYTES - len(listed)))
    assert hosts.read_approved(padded) == frozenset({"data.example.org"})  # 1 MiB exactly
    padded.write_bytes(listed + b" " * (hosts.MAX_FILE_BYTES - len(listed) + 1))
    assert hosts.read_approved(padded) == frozenset()
    assert hosts.problems(padded) == [BROKEN]


BROKEN = (
    "`.nh/state/approved_hosts.json` is not a JSON list of host names: nh approves no host from it."
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, []),
        (b"[]", []),
        (b'["data.example.org", "s3://trips-bucket/"]', []),
        (b'\xef\xbb\xbf["data.example.org"]', []),
        (b"{", [BROKEN]),
        (b'{"hosts": ["data.example.org"]}', [BROKEN]),
        (b"null", [BROKEN]),
        (b"\xff\xfe", [BROKEN]),
        (
            b'["data.example.org", "https://data.example.org/x.csv"]',
            [
                "`.nh/state/approved_hosts.json` entry 2 is not a host name (write the host only, "
                "such as `data.example.org`): it approves nothing."
            ],
        ),
        (
            b'[7, "data.example.org", "*.example.org", "../data", "a.example"]',
            [
                "`.nh/state/approved_hosts.json` entries 1, 3 and 4 are not host names (write "
                "the host only, such as `data.example.org`): they approve nothing."
            ],
        ),
        (
            b'["https://a.example/", 7, 8, 9, 10, "ok.example"]',
            [
                "`.nh/state/approved_hosts.json` entries 1, 2, 3 and 2 more are not host names "
                "(write the host only, such as `data.example.org`): they approve nothing."
            ],
        ),
    ],
)
def test_problems(tmp_path: Path, content: bytes | None, expected: list[str]) -> None:
    path = tmp_path / "approved_hosts.json"
    if content is not None:
        path.write_bytes(content)
    assert hosts.problems(path) == expected


def test_problems_never_quote_an_entry_and_never_raise(tmp_path: Path) -> None:
    path = tmp_path / "approved_hosts.json"
    path.write_text('["https://analyst:hunter2@data.example.org/x?token=abc"]', encoding="utf-8")
    [line] = hosts.problems(path)
    assert "hunter2" not in line and "token" not in line and "analyst" not in line
    folder = tmp_path / "folder.json"
    folder.mkdir()
    assert hosts.problems(folder) == [BROKEN]
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000)
    assert hosts.problems(deep) == [BROKEN]


def _listed(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_approve_creates_merges_and_keeps(tmp_path: Path) -> None:
    path = tmp_path / ".nh" / "state" / "approved_hosts.json"
    assert hosts.approve(path, ["Data.Example.org:443"]) == "created"
    assert _listed(path) == ["data.example.org"]
    assert hosts.approve(path, ["data.example.org."]) == "kept"
    assert hosts.approve(path, ["api.example.org", "data.example.org"]) == "updated"
    assert _listed(path) == ["data.example.org", "api.example.org"]
    path.write_text('["DATA.EXAMPLE.ORG", "mine.example"]', encoding="utf-8")
    assert hosts.approve(path, ["data.example.org"]) == "kept"
    assert hosts.approve(path, ["new.example"]) == "updated"
    assert _listed(path) == ["DATA.EXAMPLE.ORG", "mine.example", "new.example"]  # left as written
    assert [p.name for p in path.parent.iterdir()] == ["approved_hosts.json"]  # no temp file left


def test_approve_keeps_entries_it_cant_read(tmp_path: Path) -> None:
    path = tmp_path / "approved_hosts.json"
    path.write_text('["a.example", 7, "https://b.example/x"]', encoding="utf-8")
    assert hosts.approve(path, ["data.example.org"]) == "updated"
    assert _listed(path) == ["a.example", 7, "https://b.example/x", "data.example.org"]
    path.write_bytes(b'\xef\xbb\xbf["a.example"]')  # a BOM some editors add
    assert hosts.approve(path, ["a.example"]) == "kept"
    assert hosts.approve(path, ["s3://trips-bucket"]) == "updated"
    assert _listed(path) == ["a.example", "s3://trips-bucket"]


def test_approve_writes_bucket_keys(tmp_path: Path) -> None:
    path = tmp_path / "approved_hosts.json"
    assert hosts.approve(path, ["S3A://Trips-Bucket/"]) == "created"
    assert _listed(path) == ["s3://trips-bucket"]
    assert hosts.approve(path, ["s3://trips-bucket"]) == "kept"
    assert hosts.read_approved(path) == frozenset({"s3://trips-bucket"})


@pytest.mark.parametrize("content", ["{", '{"a": 1}', "null", '"a.example"'])
def test_approve_leaves_a_corrupt_file_alone(tmp_path: Path, content: str) -> None:
    path = tmp_path / "approved_hosts.json"
    path.write_text(content, encoding="utf-8")
    assert hosts.approve(path, ["data.example.org"]) == "skipped"
    assert path.read_text(encoding="utf-8") == content


def test_approve_skips_a_host_it_cant_read(tmp_path: Path) -> None:
    path = tmp_path / "approved_hosts.json"
    assert hosts.approve(path, ["{host}", ""]) == "skipped"
    assert not path.exists()
