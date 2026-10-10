"""FR-14's redactor (design §6.8): its sources and thresholds, secret-shaped names, the
patterns, cut pieces, one-pass merging, ``redact_head``, caching and the installed redactor.

Every secret here is fake. The shaped ones (AWS, GitHub, sk-, Slack, PEM) are built at run
time from pieces, so secret scanners reading the repo don't flag them.
"""

from __future__ import annotations

import html
import os
import reprlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nh_gateway._shared import secrets
from nh_gateway._shared.secrets import (
    MARKER,
    PATTERNS_ONLY,
    Redactor,
    dotenv_values,
    env_values,
    is_secret_name,
    is_weak_value,
    name_parts,
    parse_env,
)

SERVER_SRC = Path(secrets.__file__).resolve().parents[2]

PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # 25 chars, under DB_PASSWORD
LAB_TOKEN = "9f1c2e7a" + "b4d8e3f0a6c5d9e1b2a7f4c8d3e6a0b9"  # Jupyter-style hex, 40 chars
AWS_KEY = "AKIA" + "Z7Q2" + "M4XK" + "P9RB" + "T3VN"
GITHUB = "ghp_" + "Qw3rTy8uIoP1aSdF" + "gH5jKl0ZxCvBnM2qWeRt"
GITHUB_PAT = "github_pat_" + "11ABCDEFG0123456789_" + "abcdefghijklmnopqrstuvwxyz"
SK_KEY = "sk-" + "proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12"
SK_ANT = "sk-" + "ant-api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3"
SK_HEX = "sk-" + "0123456789abcdef" + "0123456789abcdef"  # lowercase hex only
STRIPE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
SLACK = "xox" + "b-" + "1234567890-0987654321-AbCdEfGhIj"
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 6


def pem(kind: str = "PRIVATE KEY", body: str = PEM_BODY) -> str:
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----"


def redactor(tmp_path: Path, dotenv: str = "", environ: dict | None = None) -> Redactor:
    (tmp_path / ".env").write_text(dotenv)
    return Redactor.for_project(tmp_path, environ or {})


# --- sources and thresholds -------------------------------------------------------------------


def test_a_secret_named_dotenv_value_of_8_chars_or_more_is_redacted(tmp_path: Path) -> None:
    r = redactor(tmp_path, "DB_PASSWORD=pw345678\nAPI_TOKEN=tok4567\n")
    assert r.redact("login pw345678, then tok4567") == (
        "login [redacted:DB_PASSWORD], then tok4567"  # 7 chars: under SECRET_NAME_MIN
    )


def test_any_other_dotenv_value_needs_16_chars(tmp_path: Path) -> None:
    r = redactor(
        tmp_path,
        "REGION=eu-west-1\nDEBUG=true\nDATA_PATH=data/raw.csv\n"
        "LICENSE_CODE=license-prod-0001\nSHORT_CODE=abcdefghijklmno\n",
    )
    text = "eu-west-1 true data/raw.csv license-prod-0001 abcdefghijklmno"
    assert r.redact(text) == "eu-west-1 true data/raw.csv [redacted:LICENSE_CODE] abcdefghijklmno"
    assert r.names == ["LICENSE_CODE"]


def test_a_place_or_label_name_is_not_redacted_for_its_length(tmp_path: Path) -> None:
    r = redactor(
        tmp_path,
        "PROJECT_ROOT=/Users/ana/work/churn\nS3_BUCKET=acme-analytics-lake\n"
        "MLFLOW_TRACKING_URI=http://mlflow.internal:5000\nWAREHOUSE=warehouse-prod-0001\n"
        "SENTRY_DSN=https://0a1b2c3d4e5f@o1.ingest.example.io/42\n"
        "SLACK_WEBHOOK_URL=https://hooks.example.com/services/T0/B0/xYz123\n",
    )
    assert r.names == ["SLACK_WEBHOOK_URL", "SENTRY_DSN"]  # a secret-bearing part wins
    text = (
        'File "/Users/ana/work/churn/src/features.py", line 12\n'
        "s3://acme-analytics-lake/2024/events.parquet via http://mlflow.internal:5000 "
        "on warehouse-prod-0001"
    )
    assert r.redact(text) == text


def test_long_paths_and_names_in_dotenv_stay_visible(tmp_path: Path) -> None:
    r = redactor(
        tmp_path,
        "DATA_DIR=/Volumes/shared/team-data/2026\nMODEL_FILE=models/very-long-model-name.pkl\n"
        "OUTPUT_FOLDER=reports/quarterly/output\nREPORT_NAME=quarterly-sales-report\n"
        "SOURCE_PATHS=data/a.csv:data/b.csv\nTOKEN_FILE=/run/secrets/jupyter-token\n",
    )
    assert len(r) == 0
    text = (
        "/Volumes/shared/team-data/2026 models/very-long-model-name.pkl /run/secrets/jupyter-token"
    )
    assert r.redact(text) == text


def test_places_labels_and_file_paths_stay_visible_but_not_a_key_that_looks_like_one(
    tmp_path: Path,
) -> None:
    kept = {
        "DATA_DIRECTORY": "/srv/shared/churn/data",
        "TRANSFORMERS_CACHE": "/Volumes/models/hf-cache",
        "REPORT_TITLE": "Quarterly Revenue Report",
        "CHART_LABEL": "Monthly active users",
        "SLACK_CHANNEL": "#data-science-alerts",
        "INPUT_CSV": "data/raw/customers_2024.csv",  # a file path with an extension
    }
    dotenv = "".join(f"{name}={value}\n" for name, value in kept.items())
    key = "/wJalrXUtnFEMIK7MDENG" + "bPxRfiCYFAKEKEY12"  # base64 may start with "/"
    r = redactor(tmp_path, dotenv + f"S3_CREDS={key}\nUPLOAD_SPEC=/data/in/q3@2026.csv\n")
    assert r.names == ["S3_CREDS", "UPLOAD_SPEC"]  # no "." in a key; "@" is no path
    text = " ".join(kept.values())
    assert r.redact(text) == text
    assert r.redact(f"FileNotFoundError: {kept['INPUT_CSV']}") == (
        f"FileNotFoundError: {kept['INPUT_CSV']}"
    )
    assert r.redact(f"creds {key}") == "creds [redacted:S3_CREDS]"


def test_a_secret_named_value_that_the_file_shows_under_a_plain_name_is_weak(
    tmp_path: Path,
) -> None:
    # docker-compose style: the user, the password and the database are one word
    r = redactor(
        tmp_path, "POSTGRES_USER=superset\nPOSTGRES_PASSWORD=superset\nPOSTGRES_DB=superset\n"
    )
    assert len(r) == 0
    assert r.redact("import superset; FROM superset.orders") == (
        "import superset; FROM superset.orders"
    )
    # not set under a name nh shows: still redacted, and a long plain value isn't "shown"
    r = redactor(
        tmp_path,
        "POSTGRES_USER=analytics\nPOSTGRES_PASSWORD=warehouse99\n"
        "BACKUP_NOTE=a-long-plain-value-99\nOLD_SECRET=a-long-plain-value-99\n",
    )
    assert r.names == ["BACKUP_NOTE", "POSTGRES_PASSWORD"]
    assert r.redact("warehouse99 analytics") == "[redacted:POSTGRES_PASSWORD] analytics"


def test_a_key_file_path_is_no_secret_but_the_key_is(tmp_path: Path) -> None:
    path = "/Users/someone/keys/sa-project.json"
    environ = {
        "GOOGLE_APPLICATION_CREDENTIALS": path,
        "AWS_SECRET_ACCESS_KEY": "/wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYFAKEKEY",
    }
    r = redactor(tmp_path, "SSL_KEY_FILE_SECRET=~/certs/client.pem\n", environ)
    assert r.names == ["AWS_SECRET_ACCESS_KEY"]
    assert r.redact(f"FileNotFoundError: {path}") == f"FileNotFoundError: {path}"
    inline = '{"type": "service_account", "private_key_id": "0fake1fake2fake3"}'
    r = redactor(tmp_path, f"GOOGLE_APPLICATION_CREDENTIALS={inline}\n")
    assert r.names == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert r.redact(inline) == "[redacted:GOOGLE_APPLICATION_CREDENTIALS]"


def test_a_url_that_may_carry_credentials_is_redacted_whatever_its_name(tmp_path: Path) -> None:
    sas = (
        "https://acct.blob.core.windows.net/c/sales.csv?sv=2024-01-01&se=2026-12-31&sp=r"
        "&sig=FAKEsigAbCdEf0123456789FAKEsig%3D"
    )
    s3 = (
        "https://bkt.s3.amazonaws.com/sales.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Signature=fa4e0000deadbeef1111fake2222cafe3333"
    )
    bot = "https://api.example.com/bot1234567:AAHfakeFAKEfake0123/getUpdates"
    r = redactor(
        tmp_path,
        f"DATA_URL={sas}\nS3_URL={s3}\nBOT_URL={bot}\n"
        "PLAIN_URL=https://host/data/sales_2026.csv\n"
        "MLFLOW_TRACKING_URI=http://localhost:5000\nDOCS_URL=https://example.com/a/b/c/d\n",
    )
    assert sorted(r.names) == ["BOT_URL", "DATA_URL", "S3_URL"]
    assert r.redact(f"403 Client Error: Forbidden for url: {sas}") == (
        "403 Client Error: Forbidden for url: [redacted:DATA_URL]"
    )
    assert r.redact(s3) == "[redacted:S3_URL]"
    kept = "https://host/data/sales_2026.csv http://localhost:5000 https://example.com/a/b/c/d"
    assert r.redact(kept) == kept
    assert secrets.url_may_hold_credentials(sas) and secrets.url_may_hold_credentials(bot)
    assert not secrets.url_may_hold_credentials("https://host/data/sales_2026.csv")


@pytest.mark.parametrize(
    "url",
    [
        "https://host/data/sales_2026.csv",
        "s3://bucket/raw/sales.parquet",
        "https://acct.blob.core.windows.net/c/x.csv?sv=1&sig=FAKEsig0123",
        "https://host/x.csv#access_token=FAKE0123",
        "https://api.example.com/bot1234567:AAHfakeFAKEfake0123/getUpdates",
        "https://host/files/0f8fad5b-d9cb-469f-a165-70867728950e.csv",
        "postgresql://me:ab/cd@db/sales",
        "https://user:pw@host/x.csv",
        "http://[::1",
    ],
)
def test_scaffold_and_the_redactor_read_a_data_url_alike(url: str) -> None:
    """Scaffold sends a data URL that may carry credentials to .env as DATA_URL, and the
    redactor hides that same URL: one set of rules (review of C3)."""
    from nh_gateway._shared.scaffold import core

    try:
        to_env = core.split_secret_url(url)[1]
    except ValueError:  # scaffold refuses a URL it can't parse; the redactor hides it
        to_env = True
    assert to_env is secrets.url_may_hold_credentials(url)


def test_a_url_password_is_its_own_value_so_a_cut_inside_it_is_caught(tmp_path: Path) -> None:
    url = (
        "postgresql://reporting:Wd8kLq2mZp4xR7vN@warehouse-prod.cluster-fake00"
        ".us-east-1.rds.example.com:5432/sales"
    )
    vars_view = reprlib.Repr()
    vars_view.maxstring = 80  # nh_inspect's vars view
    shown = vars_view.repr(url)
    assert "Wd8kLq2mZp4xR7" in shown  # the head cut falls inside the password
    for r in (
        redactor(tmp_path, f"DATABASE_URL={url}\n"),  # a URL: exempt from the length rule
        redactor(tmp_path, environ={"MONGO_URI": url}),  # a URL's password, any name
    ):
        out = r.redact(shown)
        assert "Wd8kLq2mZp4xR7"[:8].lower() not in out.lower(), out
        assert r.redact("password Wd8kLq2mZp4xR7vN ok") == f"password [redacted:{r.names[0]}] ok"
    # short enough that the tail piece holds the password's end, with no "://" before it
    short = "postgresql://u:Wd8kLq2mZp4xR7vNaB3cD4eF5gH6@db.internal.example:5432/sales_prod_2026"
    r = redactor(tmp_path, f"DATABASE_URL={short}\n")
    out = r.redact(vars_view.repr(short))
    assert "eF5gH6" not in out and "Wd8kLq" not in out, out
    # a port is no password, and a plain address stays
    r = redactor(tmp_path, "LAB_URL=http://127.0.0.1:8888/lab/tree/work\n")
    assert len(r) == 0
    assert r.redact("'http://127.0.0.1:8888...'") == "'http://127.0.0.1:8888...'"


def test_process_env_values_count_only_when_secret_named(tmp_path: Path) -> None:
    environ = {
        "GITHUB_TOKEN": "gh-env-value-123",
        "HOME": "/Users/someone-with-a-long-home",
        "PWD": "/Users/me/the-secret-project",  # "pwd" is a secret part, but PWD is the shell's
        "OLDPWD": "/Users/me/the-other-project",
        "MY_SECRET": "short",
    }
    assert env_values(environ) == [("GITHUB_TOKEN", "gh-env-value-123")]
    assert env_values({"SMTP_PASSWORD": 'Pa"ss\\w0rd99'}) == [  # and as --json escapes it,
        ("SMTP_PASSWORD", 'Pa"ss\\w0rd99'),
        ("SMTP_PASSWORD", 'Pa\\"ss\\\\w0rd99'),
        ("SMTP_PASSWORD", 'Pa"ss\\\\w0rd99'),  # as repr shows it,
        ("SMTP_PASSWORD", "Pa&quot;ss\\w0rd99"),  # and HTML-escaped
    ]
    r = redactor(tmp_path, environ=environ)
    text = "gh-env-value-123 in /Users/me/the-secret-project, home /Users/someone-with-a-long-home"
    assert r.redact(text) == (
        "[redacted:GITHUB_TOKEN] in /Users/me/the-secret-project, home "
        "/Users/someone-with-a-long-home"
    )


def test_jupyter_tokens_from_the_env_are_redacted(tmp_path: Path) -> None:
    r = redactor(tmp_path, environ={"JUPYTER_TOKEN": LAB_TOKEN, "NH_JUPYTER_TOKEN": "nh-lab-t0ken"})
    assert r.redact(f"lab {LAB_TOKEN} / nh-lab-t0ken") == (
        "lab [redacted:JUPYTER_TOKEN] / [redacted:NH_JUPYTER_TOKEN]"
    )


def test_the_discovered_token_is_added_to_the_installed_redactor(tmp_path: Path) -> None:
    installed = secrets.install(redactor(tmp_path))
    secrets.add_value("JUPYTER_TOKEN", LAB_TOKEN)
    assert secrets.current() is not installed
    assert secrets.current().root == tmp_path
    assert secrets.redact(f"GET /api/kernels {LAB_TOKEN}") == (
        "GET /api/kernels [redacted:JUPYTER_TOKEN]"
    )
    rebuilt = secrets.current()
    secrets.add_value("JUPYTER_TOKEN", "short")  # under 8 chars: left to the patterns
    secrets.add_value("OTHER", None)
    assert secrets.current() is rebuilt and "OTHER" not in rebuilt.names
    assert rebuilt.redact("short") == "short"


def test_the_first_source_names_a_shared_value(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_SECRET=same-value-123\n", {"B_TOKEN": "same-value-123"})
    assert r.redact("same-value-123") == "[redacted:A_SECRET]"
    assert r.names == ["A_SECRET"]


def test_a_dotenv_value_is_also_redacted_as_the_file_writes_it(tmp_path: Path) -> None:
    r = redactor(tmp_path, 'API_SECRET="line-one\\nline-\\"two\\"-99"\n')
    decoded = 'line-one\nline-"two"-99'
    assert dotenv_values([("API_SECRET", decoded)]) == [
        ("API_SECRET", decoded),
        ("API_SECRET", 'line-one\\nline-\\"two\\"-99'),
        ("API_SECRET", 'line-one\\nline-"two"-99'),  # repr
        ("API_SECRET", "line-one\nline-&quot;two&quot;-99"),  # html.escape
    ]
    assert r.redact(f"value: {decoded}") == "value: [redacted:API_SECRET]"
    shown = 'API_SECRET="line-one\\nline-\\"two\\"-99"'  # cat .env
    assert r.redact(shown) == 'API_SECRET="[redacted:API_SECRET]"'


@pytest.mark.parametrize(
    ("line", "value"),
    [
        (
            'DB_PASSWORD="Sup3r\'S3cret\\"Passw0rd-2026"',
            "Sup3r'S3cret\"Passw0rd-2026",
        ),  # both quotes
        ("DB_PASSWORD='Sup3r\"S3cret\\Passw0rd-2026'", 'Sup3r"S3cret\\Passw0rd-2026'),  # " and \\
        (
            'DB_PASSWORD="Sup3r\'S3\\"cret\\\\Passw0rd-2026"',
            "Sup3r'S3\"cret\\Passw0rd-2026",
        ),  # all 3
    ],
)
def test_a_value_is_redacted_as_python_shows_it(tmp_path: Path, line: str, value: str) -> None:
    r = redactor(tmp_path, line + "\n")
    assert repr(value)[1:-1] not in (value, secrets._encode(value))  # neither form matches it
    assert r.redact(repr(value)) == "'[redacted:DB_PASSWORD]'"
    assert r.redact(repr({"pw": value})) == "{'pw': '[redacted:DB_PASSWORD]'}"
    assert r.redact(repr([value])).count("[redacted:DB_PASSWORD]") == 1


def test_a_value_is_redacted_html_escaped(tmp_path: Path) -> None:
    value = "Sup3r&S3cret<Passw0rd>2026'q\""
    r = redactor(tmp_path, f"DB_PASSWORD={value}\n")
    table = f"<td>{html.escape(value)}</td><td>{html.escape(value, quote=False)}</td>"
    assert r.redact(table) == "<td>[redacted:DB_PASSWORD]</td><td>[redacted:DB_PASSWORD]</td>"


def test_a_value_is_redacted_in_any_case(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\nAPI_TOKEN=0123456789abcdef0123\n")
    text = f"{PASSWORD.upper()} {PASSWORD.lower()} {PASSWORD.swapcase()} 0123456789ABCDEF0123"
    assert r.redact(text) == " ".join(["[redacted:DB_PASSWORD]"] * 3 + ["[redacted:API_TOKEN]"])
    # İ lowers to two chars: the ASCII-only fallback keeps the offsets, and still finds it
    assert r.redact(f"İ {PASSWORD.upper()} İ") == "İ [redacted:DB_PASSWORD] İ"
    # values that differ only in case are one value, under the first name
    r = redactor(tmp_path, "A_TOKEN=abcdefgh12\nB_TOKEN=ABCDEFGH12\n")
    assert r.names == ["A_TOKEN"] and r.redact("AbCdEfGh12") == "[redacted:A_TOKEN]"


def test_a_weak_value_is_never_redacted_as_a_value(tmp_path: Path) -> None:
    environ = {"USER": "anabelle.dev", "DB_PASSWORD": "anabelle.dev"}
    r = redactor(
        tmp_path,
        "POSTGRES_PASSWORD=postgres\nMINIO_SECRET_KEY=minioadmin\nAPI_TOKEN=xxxxxxxxxxxx\n"
        "OPENAI_API_KEY=<your-key-here>\nADMIN_PASSWORD=changeme\nNOTEBOOK_TOKEN=notebook\n"
        "REAL_PASSWORD=Tr0ub4dor-real\n",
        environ,
    )
    assert r.names == ["REAL_PASSWORD"]
    code = 'engine = create_engine("postgresql://postgres:postgres@localhost:5432/postgres")'
    assert r.redact(code) == (
        'engine = create_engine("postgresql://[redacted:url-userinfo]@localhost:5432/postgres")'
    )
    assert r.redact("user anabelle.dev, minioadmin, changeme") == (
        "user anabelle.dev, minioadmin, changeme"
    )


@pytest.mark.parametrize(
    ("name", "value", "weak"),
    [
        ("POSTGRES_PASSWORD", "postgres", True),
        ("DB_PASSWORD", "Password123", True),  # a well-known default, any case
        ("API_TOKEN", "api_token", True),  # its own name
        ("MYSQL_ROOT_PASSWORD", "root", True),  # part of its name
        ("API_KEY", "${OPENAI_API_KEY}", True),
        ("API_KEY", "<your-api-key>", True),
        ("SECRET", "********", True),
        ("DB_PASSWORD", "Tr0ub4dor-real", False),
    ],
)
def test_is_weak_value(name: str, value: str, weak: bool) -> None:
    assert is_weak_value(name, value) is weak


def test_no_dotenv_and_no_env_is_patterns_only(tmp_path: Path) -> None:
    r = Redactor.for_project(tmp_path / "missing", {})
    assert len(r) == 0 and r.margin == secrets.MIN_MARGIN
    assert r.redact("token=abc123") == "token=[redacted:token]"


# --- the .env parser --------------------------------------------------------------------------


def test_parse_env() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "export API_KEY=abc123",
            "export\tTABBED=tab-value",
            "PLAIN = spaced value  # trailing comment",
            "HASH=abc#not-a-comment",
            "SINGLE='literal \\n $HOME'",
            'DOUBLE="tab\\there \\"quoted\\" back\\\\slash"',
            'MULTI="first',
            'second"',
            "EMPTY=",
            "not an assignment",
            "BAD NAME=x",
            "dotted.name-1=ok",
        ]
    )
    assert parse_env(text) == [
        ("API_KEY", "abc123"),
        ("TABBED", "tab-value"),
        ("PLAIN", "spaced value"),
        ("HASH", "abc#not-a-comment"),
        ("SINGLE", "literal \\n $HOME"),
        ("DOUBLE", 'tab\there "quoted" back\\slash'),
        ("MULTI", "first\nsecond"),
        ("EMPTY", ""),
        ("dotted.name-1", "ok"),
    ]


def test_an_unclosed_quote_takes_the_rest() -> None:
    assert parse_env("A='open\nB=2") == [("A", "open"), ("B", "2")]
    # the rest, and the opening line alone; then on at the next line, as python-dotenv
    # (which skips a statement it can't parse) still loads B
    assert parse_env('A="open\nB=2') == [("A", "open\nB=2"), ("A", "open"), ("B", "2")]


def test_an_unclosed_quote_doesnt_hide_the_later_secrets(tmp_path: Path) -> None:
    r = redactor(
        tmp_path, 'NOTE="see the wiki\nDB_PASSWORD=zyxwvuts98765\nHF_TOKEN=abcdEFGH1234zz\n'
    )
    assert r.redact("pw zyxwvuts98765 tok abcdEFGH1234zz") == (
        "pw [redacted:DB_PASSWORD] tok [redacted:HF_TOKEN]"
    )


def test_parse_env_also_reads_as_python_dotenv_does() -> None:
    text = "\n".join(
        [
            "\ufeffBOM_TOKEN=zyxwvuts98765",  # a byte-order mark
            "'API_TOKEN'=abcdEFGH1234zz",  # a quoted name
            "DB_PASSWORD='it\\'s-a-secret-123'",  # an escaped single quote
            "SERVICE_SECRET='line1-aaaaaaaa",  # a single quote across lines
            "line2-bbbbbbbb'",
            "NEXT=1",
        ]
    )
    assert parse_env(text) == [
        ("BOM_TOKEN", "zyxwvuts98765"),
        ("API_TOKEN", "abcdEFGH1234zz"),
        ("DB_PASSWORD", "it\\"),  # the literal reading
        ("DB_PASSWORD", "it's-a-secret-123"),  # and python-dotenv's
        ("SERVICE_SECRET", "line1-aaaaaaaa"),
        ("SERVICE_SECRET", "line1-aaaaaaaa\nline2-bbbbbbbb"),
        ("NEXT", "1"),
    ]


def test_a_bom_or_quoted_reading_is_redacted(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(
        "\ufeffDB_PASSWORD=zyxwvuts98765\nSERVICE_SECRET='line1-aaaaaaaa\nline2-bbbbbbbb'\n"
        "OTHER_PASSWORD='it\\'s-a-secret-123'\n".encode()
    )
    r = Redactor.for_project(tmp_path, {})
    text = "zyxwvuts98765 / line1-aaaaaaaa\nline2-bbbbbbbb / it's-a-secret-123"
    assert r.redact(text) == (
        "[redacted:DB_PASSWORD] / [redacted:SERVICE_SECRET] / [redacted:OTHER_PASSWORD]"
    )


def test_a_variable_is_expanded_both_ways(tmp_path: Path) -> None:
    dotenv = (
        "DB_PASSWORD=filepw-12345\n"
        "DSN=postgres://u:${DB_PASSWORD}@h/x\n"
        "API_KEY=${KEY_PREFIX:-dflt}-Zq8Wx7Vc6Bn5\n"
        "LITERAL_SECRET='${DB_PASSWORD}-not-expanded'\n"
    )
    pairs, used = secrets._expanded(secrets._readings(dotenv), {"DB_PASSWORD": "envpw-67890"})
    assert pairs == [
        ("DB_PASSWORD", "filepw-12345"),
        ("DSN", "postgres://u:${DB_PASSWORD}@h/x"),
        ("DSN", "postgres://u:filepw-12345@h/x"),  # dotenv_values: the file wins
        ("DSN", "postgres://u:envpw-67890@h/x"),  # load_dotenv: the environment wins
        ("API_KEY", "${KEY_PREFIX:-dflt}-Zq8Wx7Vc6Bn5"),
        ("API_KEY", "dflt-Zq8Wx7Vc6Bn5"),
        ("LITERAL_SECRET", "${DB_PASSWORD}-not-expanded"),
    ]
    assert used == ("DB_PASSWORD", "KEY_PREFIX")
    r = redactor(tmp_path, dotenv, {"KEY_PREFIX": "live"})
    assert r.redact("live-Zq8Wx7Vc6Bn5") == "[redacted:API_KEY]"
    # a used variable that changes rebuilds the redactor
    again = Redactor.for_project(tmp_path, {"KEY_PREFIX": "test"})
    assert again is not r and again.redact("test-Zq8Wx7Vc6Bn5") == "[redacted:API_KEY]"


# --- secret-shaped names ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN",
        "db_password",
        "apiKey",
        "GITHUB_PAT_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "client_secret",
        "PASSPHRASE",
        "private_key",
        "accessKey",
        "SLACK_SIGNING_KEY",
        "MASTER_KEY",
        "gcp-credentials",
        "credential",
        "MYSQL_PWD",
        "passwd",
        "JUPYTER_TOKEN",
        "secretKey",
        "api.key",
        "pass",
        "pw",
        "passkey",
        "pgpassword",
        "PGPASSWORD",
        "DB_PASS",
        "SMTP_PASS",
        "MYSQL_PW",
        "GITHUB_PAT",
        "STRIPE_KEY",
        "OPENAI_KEY",
        "SESSION_KEY",
        "BASIC_AUTH",
    ],
)
def test_secret_names_match(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "tokens",
        "tokenizer",
        "author",
        "keyboard",
        "SORT_KEY",
        "PRIMARY_KEY",
        "PUBLIC_KEY",
        "cache_key",
        "JUPYTER_TOKEN_FILE",
        "SECRET_NAME",
        "token_ids",
        "password_length",
        "API_KEY_ID",
        "api_url",
        "secretary",
        "max_tokens",
        "monkey",
        "key",
        "FIRST_PASS",
        "NUM_PASS",
        "PASS_THROUGH",
        "partition_key",
        "RNG_KEY",
        "PAT",
        "auth",
        "AUTH_MODE",
        "",
    ],
)
def test_other_names_dont(name: str) -> None:
    assert not is_secret_name(name)


def test_name_parts() -> None:
    assert name_parts("apiKey") == ["api", "key"]
    assert name_parts("DB_PASSWORD") == ["db", "password"]
    assert name_parts("gcp-credentials.v2") == ["gcp", "credentials", "v2"]


# --- patterns ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"postgresql://app:{PASSWORD}@db.internal:5432/sales",
            "postgresql://[redacted:url-userinfo]@db.internal:5432/sales",
        ),
        ("redis://:hunter2x@cache:6379/0", "redis://[redacted:url-userinfo]@cache:6379/0"),
        (  # another "@" soon after: an e-mail, a path's @scope, a password holding "@"
            f"postgresql://app:{PASSWORD}@db/prod owner alice@example.com",
            "postgresql://[redacted:url-userinfo]@db/prod owner alice@example.com",
        ),
        (
            "https://ci:Zk3Lm9Qp2W@registry.io/@scope/pkg",
            "https://[redacted:url-userinfo]@registry.io/@scope/pkg",
        ),
        ("mysql://app:p@ss-w0rd@db/x", "mysql://[redacted:url-userinfo]@db/x"),
        ("token=abc123def", "token=[redacted:token]"),
        ("GET /api?x=1&TOKEN=AbCdEf123&y=2", "GET /api?x=1&TOKEN=[redacted:token]&y=2"),
        ("http://h/lab?token=abcdef", "http://h/lab?token=[redacted:token]"),  # a query: never code
        ("x&access_token=abcdef", "x&access_token=[redacted:token]"),
        ('{"access_token": "eyJ0eXAi.abc-123"}', '{"access_token": "[redacted:token]"}'),
        ("{'token': 'abc123def456'}", "{'token': '[redacted:token]'}"),  # 12+ chars
        ("{'auth_token': 'abc123'}", "{'auth_token': '[redacted:token]'}"),  # a credential key
        ('{"api_token": "abc123xyz"}', '{"api_token": "[redacted:token]"}'),
        # v0.1's value class: up to "&", whitespace or a quote, so ":" "!" "$" don't stop it
        ("token=abc123:XYZsecret789", "token=[redacted:token]"),
        (
            "bot_token=123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
            "bot_token=[redacted:token]",
        ),
        (
            "http://localhost:8888/?token=ab!cd$ef12",
            "http://localhost:8888/?token=[redacted:token]",
        ),
        ("token=kkk", "token=[redacted:token]"),
        ("?token=123456&x=1", "?token=[redacted:token]&x=1"),  # a query's number of 6+ digits
        # letters only is no sign of code: mixed case, long, or not passed on
        ("token=AbCdEfGhIjKlMnOp", "token=[redacted:token]"),
        ("TOKEN=AbCdEfGhIjKlMnOp", "TOKEN=[redacted:token]"),
        ("token=abcdefABCDEFghij", "token=[redacted:token]"),
        ("api_token=some_long_secret_value_without_digits", "api_token=[redacted:token]"),
        ("export HF_TOKEN=hf_AbCdEfGhIjKlMnOpQrStUv", "export HF_TOKEN=[redacted:token]"),
        # a spaced assignment of a quoted literal, a camelCase key, a header
        ("api_token = 'Abc123Def456Ghi789'", "api_token = '[redacted:token]'"),
        ('HF_TOKEN = "hf_AbCdEf123456GhIjKl"', 'HF_TOKEN = "[redacted:token]"'),
        ('{"accessToken": "eyJ0eXAiOiJKV1Qi.abc.def"}', '{"accessToken": "[redacted:token]"}'),
        ("X-Api-Key: Abc123Def456Ghi789", "X-Api-Key: [redacted:key]"),
        ('"X-Api-Key": "Abc123Def456Ghi789"', '"X-Api-Key": "[redacted:key]"'),
        ("password='Hunter2Hunter2'", "password='[redacted:password]'"),
        ('passphrase = "correct horse battery"', 'passphrase = "[redacted:password]"'),
        ("PGPASSWORD=Tr0ub4dor", "PGPASSWORD=[redacted:password]"),
        ("client_secret=Abc123secretXyz&x=1", "client_secret=[redacted:secret]&x=1"),
        ('SECRET_KEY = "django-insecure-0a1b2c3d"', 'SECRET_KEY = "[redacted:key]"'),
        (f"STRIPE_KEY = '{STRIPE}'", "STRIPE_KEY = '[redacted:key]'"),
        ("OPENAI_KEY=Abc12345678", "OPENAI_KEY=[redacted:key]"),
        # a quoted phrase runs to the quote that opened it, the other quote inside it
        ('password="p@ss\'word-FAKE-2026x"', 'password="[redacted:password]"'),
        ("password='p@ss\"word-FAKE-2026x'", "password='[redacted:password]'"),
        ('{\\"password\\": \\"a\'b-FAKE-2026\\"}', '{\\"password\\": \\"[redacted:password]\\"}'),
        ("client_secret = 'it\"s-FAKE-2026' # x", "client_secret = '[redacted:secret]' # x"),
        # a lookup key's value stops at ";": it can't swallow the next pair
        ("sort_key=abc1;api_key=realsecret123", "sort_key=abc1;api_key=[redacted:key]"),
        ("sort_key=abcdefgh1,api_key=realsecret123", "sort_key=abcdefgh1,api_key=[redacted:key]"),
        # a URL cut inside its password (reprlib, pandas): the "@" never came
        (
            "'postgresql://reporting:Wd8kLq2mZp4xR7...us-east-1.rds.example.com:5432/sales'",
            "'postgresql://[redacted:url-userinfo]...us-east-1.rds.example.com:5432/sales'",
        ),
        ("redis://:hunter2x…", "redis://[redacted:url-userinfo]…"),
        # İ lowercases to two characters: the offsets must still be the text's
        ("İstanbul token=abc123def", "İstanbul token=[redacted:token]"),
        (
            "İİİ Authorization: Bearer abc.def-123",
            "İİİ Authorization: Bearer [redacted:authorization]",
        ),
        # a pattern inside a pattern: the first found (by start, then length) names it
        (f"token={GITHUB}", "token=[redacted:github-token]"),
        ('{"refresh_token":"r3fresh"}', '{"refresh_token":"[redacted:token]"}'),
        (
            "Authorization: Bearer eyJhbGciOi.abc-123",
            "Authorization: Bearer [redacted:authorization]",
        ),
        ("authorization: token 0a1b2c3d", "authorization: token [redacted:authorization]"),
        ('authorization="Basic dXNlcjpwYXNz"', 'authorization="Basic [redacted:authorization]"'),
        (f"key {AWS_KEY} ok", "key [redacted:aws-key] ok"),
        (f"ASIA{AWS_KEY[4:]}", "[redacted:aws-key]"),
        (f"export GH={GITHUB}", "export GH=[redacted:github-token]"),
        (GITHUB_PAT, "[redacted:github-token]"),
        (f"OPENAI_API_KEY={SK_KEY}", "OPENAI_API_KEY=[redacted:api-key]"),
        (f"({SK_ANT})", "([redacted:api-key])"),
        (SK_HEX, "[redacted:api-key]"),  # a digit is enough
        (f"slack {SLACK}", "slack [redacted:slack-token]"),
        (f"key:\n{pem()}\nafter", "key:\n[redacted:private-key]\nafter"),
        (pem("RSA PRIVATE KEY"), "[redacted:private-key]"),
        (pem("OPENSSH PRIVATE KEY"), "[redacted:private-key]"),
    ],
)
def test_each_pattern(text: str, expected: str) -> None:
    assert PATTERNS_ONLY.redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        'f"postgresql://{user}:{password}@host/db"',
        "postgresql://app:${DB_PASS}@host/db",
        "postgresql://app:%(pw)s@host/db",
        "postgresql://app:***@host/db",
        "http://user@host/path",
        "https://example.com/@someone:abc",
        "mailto:someone@example.com",
        "token=None, token=null, token=true, token=False",
        "lab?token=...",
        "hf_hub_download(repo, token=token)",
        "tokenizer.pad_token=tokenizer.eos_token",
        'special = {"bos_token": "<s>", "eos_token": "</s>"}',
        "model.generate(max_new_tokens=50, return_token_type_ids=True)",
        'headers = {"Authorization": f"Bearer {token}"}',
        "xAKIA" + AWS_KEY[4:],
        "my" + GITHUB,
        "sk-learn-compatible-estimators-and-more",
        "task-" + SK_KEY[3:],
        "x" + SLACK,
        pem("PUBLIC KEY"),
        pem("CERTIFICATE"),
        "the password field and the token count",
        "x-sk-" + SK_KEY[3:],
        # what "token" keys in ML outputs hold: counts, ids and NLP tokens
        "step=1 max_token=512 pad_token=0 loss=0.25 token='the' id=12",
        "[{'score': 0.132, 'token': 2003, 'token_str': 'is'}]",
        '{"token": 50256, "text": "<|endoftext|>"}',
        "[{'token': 'covid-19'}, {'token': 'gpt2'}]",
        '{"token": "the"}',
        "token=2024",
        "?token=12345&x=1",  # a query's number under 6 digits
        # names passed on in code
        "token=token",
        "login(token=hf)",
        "login(token=HF_TOK)",
        'api_key=os.environ["OPENAI_API_KEY"]',
        "password=getpass()",
        "password=db_password",
        "connect(password=pw)",
        "secret=None",
        "api_key=${API_KEY}",
        # a lookup key's value
        'sort_key = "user_12345678"',
        "partition_key='user_12345678'",
        "key=value12345678",
        'special = {"pad_token": "[PAD]", "cls_token": "[CLS]"}',
        # a host and port cut short: an all-digit "password" is a port
        "'http://localhost:8888...'",
        "http://127.0.0.1:8888/lab…",
        "https://[::1]:8888...",
        "postgresql://app:{password}...",
        "sort_key=abcdefgh;cache_key=abcdefgh",
    ],
)
def test_patterns_leave_code_placeholders_and_public_keys_alone(text: str) -> None:
    assert PATTERNS_ONLY.redact(text) == text


def test_the_case_insensitive_patterns_scan_a_lowercase_copy_of_the_same_length() -> None:
    # İ is the one character whose lowercase is two: it would shift every later offset, or
    # (v0.2's first cut) send every pattern down a 30x slower path
    assert secrets._fold("İstanbul TOKEN=Ab") == "istanbul token=ab"
    assert secrets._fold("ÀÉ Straße ǅ") == "àé straße ǆ"
    # Σ lowers by its context (final sigma): folded alone and inside a text, it must agree
    assert secrets._fold("ΑΣ ΑΣΒ") == "ασ ασβ"


def test_an_unterminated_private_key_is_redacted_up_to_its_end() -> None:
    cut = f"-----BEGIN PRIVATE KEY-----\n{PEM_BODY}\n{PEM_BODY[:20]} [… 900 chars cut …] tail"
    assert PATTERNS_ONLY.redact(cut) == "[redacted:private-key] [… 900 chars cut …] tail"
    escaped = repr(pem())  # a JSON or repr view: "\\n" instead of line breaks
    assert PATTERNS_ONLY.redact(escaped) == "'[redacted:private-key]'"


def key_lines(count: int, join: str = "\n") -> str:
    """``count`` 64-char lines of a key's body."""
    return join.join((PEM_BODY * 3)[i % 64 : i % 64 + 64] for i in range(count))


@pytest.mark.parametrize("join", ["\n", "\r\n", "\\n", "\\r\\n"])  # a repr's or JSON's too
def test_a_private_key_over_16_kib_is_redacted_whole(join: str) -> None:
    """R6: its END line is past PEM_MAX_CHARS, so the key-shaped run is redacted, however long
    (it once stopped after 8 KiB)."""
    body = key_lines(300, join)
    assert len(body) > secrets.PEM_MAX_CHARS
    text = f"before{join}-----BEGIN RSA PRIVATE KEY-----{join}{body}{join}-----END RSA PRIVATE KEY-----"
    out = PATTERNS_ONLY.redact(text)
    assert out.startswith(f"before{join}[redacted:private-key]") and body[-64:] not in out
    assert out.endswith("-----END RSA PRIVATE KEY-----")  # the run stops at its first "-"


def test_an_unterminated_key_run_is_redacted_to_its_end_in_linear_time() -> None:
    body = key_lines(40_000)  # 2.6 MB of key-shaped chars, no END line
    text = f"-----BEGIN PRIVATE KEY-----\n{body}\n next"
    began = time.perf_counter()
    assert PATTERNS_ONLY.redact(text) == "[redacted:private-key] next"
    assert time.perf_counter() - began < 2  # one regex per run, no backtracking stack


@pytest.mark.parametrize("join", ["\\n", "A\\n", "AB\\r\\n"])
def test_a_key_run_dense_in_escaped_joins_takes_two_scans(join: str) -> None:
    """P6 (C12 review): 8 MB of escaped joins after a BEGIN line (a JSON view of a key, or
    junk). A Python loop per join took 2 s here; two C-speed scans take a tenth of that."""
    text = "-----BEGIN PRIVATE KEY-----" + join * (8_000_000 // len(join)) + " next"
    began = time.perf_counter()
    assert PATTERNS_ONLY.redact(text) == "[redacted:private-key] next"
    assert time.perf_counter() - began < 1.5


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("AB\\nCD", 6),  # an escaped line break joins
        ("AB\\rCD\r\nEF\nGH", 13),  # an escaped CR, a CRLF and a line break join
        ("AB\\xCD", 2),  # a backslash that escapes anything else ends it
        ("AB\\\\nCD", 2),  # so does an escaped backslash
        ("AB\rCD", 2),  # and a CR without its line feed
        ("AB\r\rCD", 2),
        ("AB\\", 2),  # and a backslash or CR at the text's end
        ("AB\r", 2),
        ("AB-CD", 2),
        ("", 0),
    ],
)
def test_a_key_run_ends_where_a_char_starts_no_join(text: str, end: int) -> None:
    assert secrets._key_run(text, 0) == end


def test_redact_at_maps_a_position_without_splitting_a_marker(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    text = f"ab {PASSWORD} cd"
    marker = "[redacted:DB_PASSWORD]"
    for at in range(-1, len(text) + 2):
        out, lo, hi = r.redact_at(text, at)
        assert out == r.redact(text)
        if at <= 3:  # before the value, or at its start
            assert lo == hi == max(0, at)
        elif at < 3 + len(PASSWORD):  # inside it: its marker's two ends
            assert (lo, hi) == (3, 3 + len(marker))
        else:  # after it: moved by what the marker changed
            assert lo == hi == min(at, len(text)) + len(marker) - len(PASSWORD)
    assert r.redact_at("plain", 3) == ("plain", 3, 3)


def test_redact_at_follows_a_marker_that_completes_a_value(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_TOKEN=abcdefgh12\nB_SECRET=]zzzzzz9\n")
    text = "abcdefgh12zzzzzz9"
    out, lo, hi = r.redact_at(text, 5)  # inside A's value, which B's completes
    assert out == r.redact(text) == "[redacted:A_TOKEN[redacted:B_SECRET]"
    assert (lo, hi) == (0, len(out))
    out, lo, hi = r.redact_at(text, 14)  # inside the part only B's marker covers: both whole
    assert (lo, hi) == (0, len(out))
    out, lo, hi = r.redact_at("x " + text + " y", 0)
    assert (lo, hi) == (0, 0)
    out, lo, hi = r.redact_at("x " + text + " y", len(text) + 3)
    assert lo == hi == len(out) - 1


def test_redact_at_agrees_with_redact_on_mixed_secrets(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\nODD_SECRET=abc]def[ghi\n")
    text = f"x {PASSWORD} token=abc123 {AWS_KEY} abc]def[ghi\n{pem()}\n{GITHUB}{PASSWORD} y"
    whole = r.redact(text)
    los = []
    for at in range(len(text) + 1):
        out, lo, hi = r.redact_at(text, at)
        assert out == whole and lo <= hi
        assert lo == hi or (out[lo] == "[" and out[hi - 1] == "]" and MARKER in out[lo:hi])
        los.append(lo)
    assert los == sorted(los)  # a later position never lands earlier


def test_spill_is_how_far_a_value_with_a_line_break_may_run_on(tmp_path: Path) -> None:
    first, second = "Line1-Abc123xyzKq", "Line2-Def456uvwZr"
    r = redactor(tmp_path, f'MULTI_SECRET="{first}\\n{second}"\n')
    assert r.line_reach == len(first) + 1 + len(second)
    assert r.spill(f"{second} rest\nnext", 0) == len(second)  # its part after the break
    assert r.spill(f"x\n{second[:9]}", 2) == 2 + 9  # a cut piece of it
    assert r.spill("Line2-other", 0) == len("Line2-")  # a shared start counts too
    assert r.spill("other line", 0) == 0 and r.spill("", 0) == 0
    assert redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n").spill(second, 0) == 0


def test_spill_back_is_where_a_value_with_a_line_break_may_begin(tmp_path: Path) -> None:
    first, second = "Line1-Abc123xyzKq", "Line2-Def456uvwZr"
    r = redactor(tmp_path, f'MULTI_SECRET="{first}\\n{second}"\n')
    text = f"row\nx {first}\nLine2-De"
    at = text.rindex("\n") + 1
    assert r.spill_back(text, at) == at - 1 - len(first)  # its first line may be the value's
    text = f"row\n...{first[-9:]}\nLine2-De"  # a cut piece of it (its end) counts too
    assert r.spill_back(text, text.rindex("\n") + 1) == text.rindex("\n") - 9
    text = f"row\nx {first[:9]}\nLine2-De"  # its start alone can't run on past the break
    assert r.spill_back(text, text.rindex("\n") + 1) == len(text)
    text = "row\nother\nLine2-De"
    assert r.spill_back(text, text.rindex("\n") + 1) == len(text)  # none can run on past it
    plain = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    assert plain.spill_back(f"x {first}\nLine2", len(first) + 3) == len(first) + 8
    r = redactor(tmp_path, 'LEADING_SECRET="\\nAbc123xyzKqWv9"\n')  # a value that starts with one
    assert r.spill_back("abc\nAbc", 4) == 3


def test_a_dataframe_repr_keeps_its_columns(tmp_path: Path) -> None:
    r = redactor(
        tmp_path,
        "REGION=eu-west-1\nDEBUG=true\nDATA_PATH=data/raw.csv\nDB_PASSWORD=" + PASSWORD + "\n",
    )
    frame = (
        "   token_count  api_key_id password_hash     region  debug      data_path\n"
        "0           12           7      5f4dcc3b  eu-west-1   true  data/raw.csv\n"
        "1          340           9      e10adc39  us-east-2  false  data/raw.csv"
    )
    assert r.redact(frame) == frame


def test_a_real_dataframe_repr_keeps_its_columns(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    r = redactor(tmp_path, "REGION=eu-west-1\nDEBUG=true\nDATA_PATH=data/raw.csv\n")
    frame = pd.DataFrame(
        {
            "token": ["a", "b"],
            "tokens": [3, 4],
            "api_key": [None, None],
            "region": ["eu-west-1", "us-east-2"],
            "path": ["data/raw.csv", "data/raw.csv"],
        }
    )
    for text in (repr(frame), frame.to_string(), frame.to_html()):
        assert r.redact(text) == text


# --- cut pieces -------------------------------------------------------------------------------


def test_pieces_of_a_cut_secret_are_redacted(tmp_path: Path) -> None:
    value = PASSWORD + "-rotated-q3"  # 36 chars: reprlib cuts it
    r = redactor(tmp_path, f"DB_PASSWORD={value}\n")
    assert reprlib.repr(value) == f"'{value[:12]}...{value[-13:]}'"
    for text in (
        reprlib.repr(value),
        f"password    {value[:14]}…\nrow 2",  # pandas cuts a long cell with …
        f"{value[-13:]}\nthe rest of a stream whose head was dropped",
        f"notice\n{value[-12:]} more",
        f"the last output: {value[:12]}",  # cut at the end of the text
    ):
        out = r.redact(text)
        assert "[redacted:DB_PASSWORD]" in out, text
        assert value[:12] not in out and value[-12:] not in out, out
    assert r.redact(reprlib.repr(value)) == "'[redacted:DB_PASSWORD]...[redacted:DB_PASSWORD]'"


def test_pieces_of_a_secret_bearing_value_are_redacted(tmp_path: Path) -> None:
    hook = "https://hooks.slack.com/services/T0FAKE00000/B0FAKE00000/fAkEwEbHoOkSeCrEt0123456"
    dsn = "https://0fake1fake2fake3fake4fake5fake6f@o000000.ingest.sentry.io/0000000"
    r = redactor(tmp_path, f"SLACK_WEBHOOK_URL={hook}\nSENTRY_DSN={dsn}\n")
    vars_view = reprlib.Repr()
    vars_view.maxstring = 80
    assert r.redact(vars_view.repr(hook)) == (
        "'[redacted:SLACK_WEBHOOK_URL]...[redacted:SLACK_WEBHOOK_URL]'"
    )
    assert r.redact(f"0  alerts  {dsn[:45]}...") == "0  alerts  [redacted:SENTRY_DSN]..."


def test_a_piece_is_caught_when_the_value_has_a_dot_where_it_was_cut(tmp_path: Path) -> None:
    value = "Fk9x7Q2zLm4R.t8Vw3Yb6Nc1.Pd5Hs0Jq"
    r = redactor(tmp_path, f"API_SECRET={value}\n")
    assert r.redact(f"{value[:12]}...") == "[redacted:API_SECRET]..."  # the value goes on ".t8V"
    assert r.redact(f"...{value[-9:]}") == f"...{value[-9:]}"  # under FRAGMENT_MIN
    assert r.redact(f"x...{value[-13:]}") == "x...[redacted:API_SECRET]"  # after ".", a cut
    assert r.redact(f"{value[:24]}…") == "[redacted:API_SECRET]…"


def test_short_or_unmarked_pieces_stay(tmp_path: Path) -> None:
    r = redactor(
        tmp_path,
        f"DB_PASSWORD={PASSWORD}\nLICENSE_CODE=license-prod-0001-eu\nAPI_TOKEN=Tok3n-15-chars!\n",
    )
    assert len("Tok3n-15-chars!") == secrets.FRAGMENT_MIN + 3
    for text in (
        f"{PASSWORD[:11]}...",  # under FRAGMENT_MIN
        f"x{PASSWORD[:14]} and on",  # not before a cut
        "license-prod-00...",  # not secret-named: cut pieces aren't matched
        "Tok3n-15-cha...",  # a value under FRAGMENT_MIN + 4: a 12-char piece is most of it
    ):
        assert r.redact(text) == text


def test_pieces_are_skipped_on_texts_over_2_mb(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    pad = "x " * (secrets.FRAGMENT_MAX_TEXT // 2)
    assert r.redact(f"{pad}{PASSWORD[:14]}...") == f"{pad}{PASSWORD[:14]}..."
    assert r.redact(f"{pad}{PASSWORD}") == f"{pad}[redacted:DB_PASSWORD]"  # whole values still are


# --- one pass: overlaps, longest first, the marker --------------------------------------------


def test_the_longest_value_wins(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_TOKEN=abcdefgh1234\nB_TOKEN=abcdefgh1234-extended\n")
    assert r.names == ["B_TOKEN", "A_TOKEN"]
    assert r.redact("abcdefgh1234-extended abcdefgh1234") == "[redacted:B_TOKEN] [redacted:A_TOKEN]"


def test_overlapping_spans_merge_into_one_marker(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_SECRET=xxxxYYYYYYYY\nB_SECRET=YYYYYYYYzzzzzz\n")
    assert r.redact("<xxxxYYYYYYYYzzzzzz>") == "<[redacted:B_SECRET]>"
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    assert r.redact(f"postgresql://app:{PASSWORD}@db/sales") == (
        "postgresql://[redacted:DB_PASSWORD]@db/sales"  # the value names the merged span
    )


def test_equal_weights_take_the_first_marker(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_SECRET=xxxxYYYY\nB_SECRET=YYYYzzzz\n")
    assert r.redact("<xxxxYYYYzzzz>") == "<[redacted:A_SECRET]>"  # same length: the first
    spans = [(0, 4, 0, "[A]"), (0, 6, 0, "[B]"), (5, 8, 0, "[C]")]
    assert secrets._apply("0123456789", spans) == "[B]89"  # same start: the longest
    assert secrets._apply("0123456789", [(2, 8, 0, "[A]"), (0, 4, 0, "[B]")]) == "[B]89"


def test_a_value_that_a_marker_completes_is_redacted_too(tmp_path: Path) -> None:
    r = redactor(tmp_path, "A_TOKEN=abcdefgh12\nB_SECRET=]zzzzzz9\n")
    once = r.redact("abcdefgh12zzzzzz9")  # "]zzzzzz9" only appears once A's marker is in
    assert "]zzzzzz9" not in once and r.redact(once) == once
    assert once == "[redacted:A_TOKEN[redacted:B_SECRET]"


def test_the_marker_and_idempotence(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\nODD_SECRET=abc]def[ghi\n")
    text = f"{PASSWORD} token=abc123 {AWS_KEY} abc]def[ghi {pem()}"
    once = r.redact(text)
    assert once == (
        "[redacted:DB_PASSWORD] token=[redacted:token] [redacted:aws-key] "
        "[redacted:ODD_SECRET] [redacted:private-key]"
    )
    assert r.redact(once) == once
    assert once.count(MARKER) == 5


def test_nothing_else_is_shortened_or_dropped(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    body = "row,value\n" + "".join(f"{i},{i * 3}\n" for i in range(50_000))
    text = body + PASSWORD + "\n" + body
    out = r.redact(text)
    assert out == body + "[redacted:DB_PASSWORD]\n" + body
    assert r.redact(body) is body  # no secret: the very same text
    assert r.redact("") == "" and r.redact(None) is None  # type: ignore[arg-type]


# --- redact_head ------------------------------------------------------------------------------


def test_redact_head_replaces_a_secret_across_the_cut_whole(tmp_path: Path) -> None:
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    text = "a" * 95 + PASSWORD + "b" * 10_000
    head = r.redact_head(text, 100)
    assert head.startswith("a" * 95 + "[redacted:DB_PASSWORD]")
    assert PASSWORD[:5] not in head[:100]
    # the marker is 3 chars shorter than the value, so the window doubled once, and no more
    assert len(head) <= 2 * (100 + r.margin)
    assert r.margin == max(secrets.MIN_MARGIN, len(PASSWORD) + 1)


def test_redact_head_scans_only_the_head(tmp_path: Path) -> None:
    r = redactor(tmp_path)
    far = "x" * 5000 + "token=abc123"
    assert r.redact_head(far, 10) == "x" * (10 + r.margin)


@pytest.mark.parametrize("dotenv", ["", "SERVICE_DSN=Pq7Rs8Tu9Vw0Xy1Za2Bc3De4\n"])
def test_redact_head_grows_its_window_past_short_markers(tmp_path: Path, dotenv: str) -> None:
    """A marker is shorter than a private key: the redacted window shrinks under the cut, and
    its raw edge, cutting a secret, must still stay past it (review of C3)."""
    r = redactor(tmp_path, dotenv)
    secret = "Pq7Rs8Tu9Vw0Xy1Za2Bc3De4" if dotenv else GITHUB
    kind = "SERVICE_DSN" if dotenv else "github-token"
    key = pem(body=PEM_BODY * 60)
    limit = 1000
    pad = limit + r.margin - len(key) - 1 - 7  # the window's raw edge falls 7 chars in
    text = f"{key}\n{'y' * pad}{secret}\n" + "tail\n" * 2000
    head = r.redact_head(text, limit)[:limit]
    assert head == r.redact(text)[:limit]  # what redacting it all would show
    assert f"{'y' * pad}[redacted:{kind}]\ntail" in head and secret[:7] not in head


def test_redact_head_passes_what_is_not_text() -> None:
    assert PATTERNS_ONLY.redact_head(None, 10) is None  # type: ignore[arg-type]
    assert PATTERNS_ONLY.redact_head("", 10) == ""


def test_margin_covers_the_longest_value(tmp_path: Path) -> None:
    long_value = "k" * 3000 + "1"
    r = redactor(tmp_path, f"LONG_SECRET={long_value}\n")
    assert r.margin == len(long_value) + 1
    assert r.redact_head("x" * 99 + long_value, 100)[:100] == "x" * 99 + "["


# --- caching and the installed redactor -------------------------------------------------------


def test_for_project_is_cached_until_dotenv_env_or_added_values_change(tmp_path: Path) -> None:
    first = redactor(tmp_path, "API_TOKEN=abcdefgh1\n")
    assert Redactor.for_project(tmp_path, {}) is first
    (tmp_path / ".env").write_text("API_TOKEN=abcdefgh12\n")  # a new size
    second = Redactor.for_project(tmp_path, {})
    assert second is not first and second.redact("abcdefgh12") == "[redacted:API_TOKEN]"
    third = Redactor.for_project(tmp_path, {"MY_SECRET": "env-value-1"})
    assert third is not second and third.names == ["MY_SECRET", "API_TOKEN"]
    assert Redactor.for_project(tmp_path, {"MY_SECRET": "env-value-1"}) is third
    secrets.add_value("JUPYTER_TOKEN", LAB_TOKEN)
    fourth = Redactor.for_project(tmp_path, {"MY_SECRET": "env-value-1"})
    assert fourth is not third and "JUPYTER_TOKEN" in fourth.names
    other = tmp_path / "other"
    other.mkdir()
    assert Redactor.for_project(other, {}).names == ["JUPYTER_TOKEN"]  # its own slot


def test_a_failed_read_is_not_cached(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    dotenv = tmp_path / ".env"
    dotenv.write_text("DB_PASSWORD=zyxwvuts98765\n")
    dotenv.chmod(0o000)
    try:
        assert len(Redactor.for_project(tmp_path, {})) == 0
    finally:
        dotenv.chmod(0o600)  # only the ctime changes
    assert Redactor.for_project(tmp_path, {}).redact("zyxwvuts98765") == "[redacted:DB_PASSWORD]"


def test_a_same_size_edit_with_its_old_mtime_is_seen(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("DB_PASSWORD=zyxwvuts98765\n")
    first = Redactor.for_project(tmp_path, {})
    before = dotenv.stat()
    time.sleep(0.05)
    dotenv.write_text("DB_PASSWORD=abcdefgh54321\n")  # the same size
    os.utime(dotenv, ns=(before.st_atime_ns, before.st_mtime_ns))  # cp -p, touch -r, rsync -t
    assert dotenv.stat().st_mtime_ns == before.st_mtime_ns
    second = Redactor.for_project(tmp_path, {})
    assert second is not first and second.redact("abcdefgh54321") == "[redacted:DB_PASSWORD]"


def test_only_the_first_mib_of_dotenv_is_read(tmp_path: Path) -> None:
    pad = "# " + "x" * 98 + "\n"
    head = "DB_PASSWORD=zyxwvuts98765\n"
    body = pad * (secrets.DOTENV_MAX_BYTES // len(pad) + 1)  # past the cap
    (tmp_path / ".env").write_text(head + body + "LATE_SECRET=abcdefgh54321\n")
    r = Redactor.for_project(tmp_path, {})
    assert r.names == ["DB_PASSWORD"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")
@pytest.mark.parametrize("link", [False, True])
def test_a_named_pipe_dotenv_is_never_opened(tmp_path: Path, link: bool) -> None:
    # 1Password mounts .env as a named pipe: reading it blocks until its writer answers
    pipe = tmp_path / ("env.pipe" if link else ".env")
    os.mkfifo(pipe)
    if link:
        (tmp_path / ".env").symlink_to(pipe)
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from nh_gateway._shared import secrets\n"
        "r = secrets.Redactor.for_project(Path(sys.argv[2]), {'API_TOKEN': 'env-token-123'})\n"
        "print(r.names, r.redact('env-token-123'))\n"
        "print(secrets._read_dotenv(Path(sys.argv[2]) / '.env'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(SERVER_SRC), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,  # a blocked open would hang here
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["['API_TOKEN'] [redacted:API_TOKEN]", "None"]


def test_current_is_patterns_only_until_installed(tmp_path: Path) -> None:
    assert secrets.current() is PATTERNS_ONLY
    assert secrets.redact("token=abc123") == "token=[redacted:token]"  # never the identity
    r = redactor(tmp_path, f"DB_PASSWORD={PASSWORD}\n")
    assert secrets.install(r) is r and secrets.current() is r
    assert secrets.redact(PASSWORD) == "[redacted:DB_PASSWORD]"
    secrets.reset()
    assert secrets.current() is PATTERNS_ONLY and secrets.redact(PASSWORD) == PASSWORD


def test_add_value_before_install_waits_for_the_next_build(tmp_path: Path) -> None:
    secrets.add_value("JUPYTER_TOKEN", LAB_TOKEN)
    assert secrets.current() is PATTERNS_ONLY
    assert redactor(tmp_path).redact(LAB_TOKEN) == "[redacted:JUPYTER_TOKEN]"


def test_a_keyed_pattern_is_compiled_only_once_a_text_holds_its_word() -> None:
    # The hooks start a fresh process each time: compiling all five up front cost ~8 ms on 3.9.
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from nh_gateway._shared import secrets\n"
        "print(sorted(secrets._fold_compiled))\n"
        "print(secrets.PATTERNS_ONLY.redact('Loaded 12 rows from sales.csv'))\n"
        "print(sorted(secrets._fold_compiled))\n"
        "print(secrets.PATTERNS_ONLY.redact('token=ab12 and PASSWORD=Kx9Lm2Qp'))\n"
        "print(sorted(secrets._fold_compiled))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(SERVER_SRC)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "[]",
        "Loaded 12 rows from sales.csv",
        "[]",
        "token=[redacted:token] and PASSWORD=[redacted:password]",
        "['password', 'token']",
    ]


# --- scan time: linear on long runs -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "-".join(f"task-{i}" for i in range(110_000)),  # words ending in "sk" before "-"
        "sk-" * 350_000,
        "x" + "github_pat_" * 95_000,
        "axoxb-" * 175_000,
        "a_key=" * 175_000,  # a key with no digit in its value
        "x-api-key:" * 105_000,
        ";".join(["cache_key=abc", "sort_key=def", "lookup_key=ghi"] * 25_000),
        "://" + "a" * 250 + ":" + "b" * 511 + "x" * 1_000_000,  # a userinfo that never ends
    ],
    ids=["task", "sk", "github_pat", "xoxb", "a_key", "x-api-key", "key-config", "userinfo"],
)
def test_a_megabyte_of_near_misses_scans_in_time(text: str) -> None:
    assert len(text) >= 1_000_000
    started = time.perf_counter()
    PATTERNS_ONLY.redact(text)
    assert time.perf_counter() - started < 2.0  # quadratic took over 60 s (review of C3)


def test_a_key_needs_a_digit_within_its_first_64_chars() -> None:
    inside = "a" * (secrets.KEY_DIGIT_WITHIN - 1) + "1"
    past = "a" * secrets.KEY_DIGIT_WITHIN + "1"
    assert PATTERNS_ONLY.redact(f"api_key={inside}") == "api_key=[redacted:key]"
    assert PATTERNS_ONLY.redact(f"api_key={past}") == f"api_key={past}"


# --- Python 3.9 (the hooks and nhctl) ---------------------------------------------------------


@pytest.mark.skipif(shutil.which("/usr/bin/python3") is None, reason="no /usr/bin/python3")
def test_it_imports_and_redacts_under_the_system_python(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(
        f"\ufeffDB_PASSWORD={PASSWORD}\nDATABASE_URL=postgresql://u:Wd8kLq2mZp4xR7vN@h/db\n"
        "API_KEY=${KEY_PREFIX:-dflt}-Zq8Wx7Vc6Bn5\n".encode()
    )
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from nh_gateway._shared import secrets\n"
        "r = secrets.Redactor.for_project(Path(sys.argv[2]), {})\n"
        "print(sys.version_info[:2] >= (3, 9))\n"
        "print(r.redact(sys.argv[3]))\n"
    )
    proc = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            script,
            str(SERVER_SRC),
            str(tmp_path),
            f"{PASSWORD.upper()} token=ab12 'postgresql://u:Wd8kLq2m... dflt-Zq8Wx7Vc6Bn5 sk-"
            + "a" * 30,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "True",
        "[redacted:DB_PASSWORD] token=[redacted:token] '[redacted:DATABASE_URL]... "
        "[redacted:API_KEY] sk-" + "a" * 30,
    ]
