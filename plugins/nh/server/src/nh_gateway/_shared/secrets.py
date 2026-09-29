"""Redaction of what Claude sees (FR-14, design §6.8). Python 3.9, stdlib only.

A :class:`Redactor` replaces known secret values with ``[redacted:NAME]`` and secret-shaped
text (URL passwords, tokens, keys) with ``[redacted:<kind>]``. It only ever replaces: it never
shortens or drops anything else. The gateway, the hooks and nhctl share it; the notebook, the
RTC document and nh's history keep the raw text.

Values come from the project's ``.env``, the process environment and the Jupyter token the
gateway discovers (:func:`add_value`). ``current()`` is the redactor to use: the one
``install()`` set (the gateway installs one per project), else a patterns-only redactor.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from operator import itemgetter
from pathlib import Path

MARKER = "[redacted:"
SECRET_NAME_MIN = 8  # a secret-named value this long or longer is redacted
ANY_NAME_MIN = 16  # any .env value this long or longer is redacted (REGION=eu-west-1 is not)
MIN_MARGIN = 1024  # redact_head's overlap past the cut, at least the longest value
FRAGMENT_MIN = 12  # the shortest cut piece of a secret-named value that is still redacted
FRAGMENT_MAX_TEXT = 2 << 20  # texts longer than this skip fragment matching (cost)
PEM_MAX_CHARS = 16384  # how far past BEGIN nh looks for a private key's END line
PEM_OPEN_MAX_CHARS = 8192  # an unterminated key's body is redacted up to this far

# Whole name parts (split on "_", "-", "." and camelCase) that make a name secret-shaped.
SECRET_NAME_PARTS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "passphrase",
        "passkey",
        "pwd",
        "mysqlpwd",
        "token",
        "credential",
        "credentials",
        "apikey",
        "accesskey",
        "secretkey",
        "privatekey",
    }
)
# A part that ends in one of these is secret too: PGPASSWORD, MYSQLPASSWORD.
SECRET_PART_ENDINGS = ("password", "passwd", "passphrase")
# These pairs name a secret wherever they are (SECRET_KEY_BASE, API_KEY_V2, BASIC_AUTH).
SECRET_PAIRS = frozenset(
    {
        ("basic", "auth"),
        ("http", "auth"),
        ("proxy", "auth"),
        ("api", "key"),
        ("access", "key"),
        ("secret", "key"),
        ("private", "key"),
        ("auth", "key"),
        ("signing", "key"),
        ("encryption", "key"),
        ("account", "key"),
        ("master", "key"),
    }
)
# "pass" and "pw" as the last part are a password (DB_PASS, SMTP_PASS, MYSQL_PW), except after
# these (FIRST_PASS, NUM_PASS: a pass over data).
NOT_PASSWORD_BEFORE = frozenset(
    {
        "forward",
        "backward",
        "first",
        "second",
        "third",
        "final",
        "last",
        "next",
        "single",
        "multi",
        "one",
        "two",
        "by",
        "n",
        "num",
        "max",
        "min",
        "per",
        "each",
        "grad",
        "render",
        "shadow",
        "dry",
        "full",
        "extra",
    }
)
# "pat" as the last part is a personal access token after a code host: GITHUB_PAT, HF_PAT.
PAT_BEFORE = frozenset(
    {"github", "gh", "gitlab", "bitbucket", "ado", "devops", "azure", "hf", "huggingface"}
)
# "key" as the last part is a secret (STRIPE_KEY, OPENAI_KEY) unless the part before it says it
# is a lookup key (SORT_KEY, PRIMARY_KEY, CACHE_KEY) or a public one. "key" alone is not.
NOT_SECRET_KEY_BEFORE = frozenset(
    {
        "sort",
        "sorting",
        "primary",
        "foreign",
        "secondary",
        "composite",
        "compound",
        "natural",
        "surrogate",
        "candidate",
        "unique",
        "partition",
        "shard",
        "cluster",
        "range",
        "hash",
        "join",
        "merge",
        "group",
        "grouping",
        "lookup",
        "index",
        "map",
        "dict",
        "row",
        "column",
        "col",
        "table",
        "field",
        "attr",
        "record",
        "item",
        "node",
        "entity",
        "business",
        "cache",
        "redis",
        "s3",
        "object",
        "bucket",
        "file",
        "config",
        "settings",
        "json",
        "yaml",
        "env",
        "i18n",
        "translation",
        "label",
        "state",
        "dedup",
        "idempotency",
        "order",
        "message",
        "msg",
        "event",
        "topic",
        "routing",
        "hot",
        "short",
        "shortcut",
        "cursor",
        "next",
        "prev",
        "start",
        "end",
        "first",
        "last",
        "min",
        "max",
        "public",
        "pub",
        "lock",
        "registry",
        "parent",
        "child",
        "ref",
        "reference",
        "rng",
        "prng",
        "random",
        "seed",
        "batch",
        "layer",
        "obs",
        "obsm",
        "var",
        "uns",
        "input",
        "output",
        "memory",
        "target",
        "time",
        "text",
        "data",
        "value",
        "sequence",
        "seq",
        "context",
        "ctx",
        "metric",
        "feature",
        "param",
        "key",
    }
)
# A last part that names where a secret is or something about it, not the secret:
# TOKEN_FILE, PASSWORD_PATH, TOKEN_URL, SECRET_NAME, token_ids, token_count.
NOT_SECRET_LAST = frozenset(
    {"file", "path", "dir", "url", "uri", "name", "id", "ids", "count", "len", "length", "size"}
)
# A .env name whose last part says its value is a place, an address or a label is not redacted
# for its length alone: a long DATA_DIR, PROJECT_ROOT or S3_BUCKET would hide the paths in every
# traceback, and a URL's password is caught by the url-userinfo pattern anyway.
LENGTH_EXEMPT_LAST = NOT_SECRET_LAST | frozenset(
    {
        "paths",
        "folder",
        "root",
        "home",
        "bucket",
        "endpoint",
        "host",
        "hostname",
        "server",
        "domain",
        "address",
        "addr",
        "port",
        "user",
        "username",
        "email",
        "database",
        "db",
        "schema",
        "table",
        "project",
        "dataset",
        "model",
        "region",
        "zone",
        "location",
        "version",
        "env",
        "environment",
        "stage",
        "mode",
        "level",
        "namespace",
        "cluster",
        "branch",
        "repo",
        "image",
        "tag",
        "locale",
        "lang",
        "language",
        "timezone",
        "tz",
        "format",
        "prefix",
        "workspace",
        "catalog",
        "warehouse",
        "role",
        "queue",
        "topic",
    }
)
# ...unless another part says the value carries a secret itself (SLACK_WEBHOOK_URL, SENTRY_DSN).
SECRET_BEARING_PARTS = frozenset({"webhook", "hook", "dsn", "sas", "signed", "presigned"})
# Values that are no secret under any name: well-known local defaults and placeholders.
# Redacting POSTGRES_PASSWORD=postgres hid every "postgresql" in code and outputs (review of C3).
WEAK_VALUES = frozenset(
    {
        "postgres",
        "postgresql",
        "password",
        "password1",
        "password123",
        "passw0rd",
        "changeme",
        "change_me",
        "changeit",
        "administrator",
        "12345678",
        "123456789",
        "1234567890",
        "minioadmin",
        "localhost",
        "qwertyuiop",
        "notebook",
        "development",
        "placeholder",
        "yourpassword",
        "your_password",
        "your-password",
        "mypassword",
        "supersecret",
        "secret123",
    }
)
# Process variables that are secret-shaped but never secret: the shell's working directories.
_ENV_SKIP = frozenset({"PWD", "OLDPWD"})
_USER_VARS = ("USER", "LOGNAME", "USERNAME")

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NAME_SPLIT = re.compile(r"[^a-z0-9]+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'", "$": "$"}
# A value that is a placeholder, not a secret: {password}, ${DB_PASS}, %(pw)s, ***, <your-key>.
_PLACEHOLDER = re.compile(
    r"(?:\{[^}]*\}|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%\([A-Za-z_]+\)s|\*+|<[^<>]*>)\Z"
)

# --- the patterns ----------------------------------------------------------------------------
# Literal-first (spike V11): a regex that starts with a literal is scanned at C speed, while a
# leading lookbehind made one 40x slower and an alternation of anchors 13x slower. So every
# check that doesn't need the text's case is in the regex, after its literal, and only a match
# reaches Python. The case-insensitive ones run on a lowercased copy (:func:`_fold`).

# A value: v0.1's class (it stops at "&", whitespace and quotes), plus "<" and ">". It never
# ends in , ; ) ] } . or a backslash, which are the text around it: f(token=abc), "token=abc.",
# {\"url\": \"…?token=abc\"}.
_V = r"[^&\s\"'<>]"
_VALUE = _V + r"*[^&\s\"'<>,;)\]}.\\]"
_END = r"[,;)\]}.]*(?!" + _V + r")"  # the value ends here
_QUOTE = r"\\?[\"']"  # a quote, or an escaped one inside a JSON string
# A quoted password or secret runs to its closing quote on the same line (a passphrase may hold
# spaces); a quoted token or key is one word up to its quote (NLP's {"token": "New York"} isn't).
_PHRASE = r"[^\"'\n]*[^\"'\s\\]"
_WORD = r"[^\"'\s\\]+(?=" + _QUOTE + r"|[\r\n]|\Z)"
# Never a secret, quoted or not.
_SKIP = "|".join(
    (
        r"(?:none|null|nil|true|false|nan|undefined)" + _END,
        r"\[redacted",  # already a marker, or one a view cut: "[redacted…"
        r"(?:\{[^}\s\"'<>]*\}|\$\{?[a-z_][a-z0-9_]*\}?|%\([a-z_]+\)s|%s)" + _END,  # placeholder
        r"[^&\s\"'<>a-z0-9]*(?!" + _V + r")",  # no letter or digit: ***, ..., -
        r"\[[a-z_|]+\]" + _END,  # a tokenizer's special token: [PAD], [CLS]
    )
)


_NUMBER = r"[-+]?\d+(?:\.\d+)?" + _END  # a count or an id: max_token=512, pad_token=0


def _atomic(name: str, pattern: str) -> str:
    """``pattern`` matched once, never backtracked into: 3.9's ``re`` has no atomic groups, but a
    lookahead's capture, matched again by reference, is one (V11: a dense output's cost)."""
    return f"(?=(?P<{name}>{pattern}))(?P={name})"


def _code(own: str, tag: str) -> str:
    """Unquoted values that are code, not a secret (outside a URL's query)."""
    return "|".join(
        (
            # an attribute: tokenizer.eos_token, self.token
            _atomic(f"{tag}_a", r"[a-z_]+\.[a-z_]+(?:\.[a-z_]+)*") + "(?:" + _END + r"|[(\[])",
            # names its own kind: token=token, token=hf_token, password=db_password
            _atomic(f"{tag}_o", "[a-z0-9_]+") + own + "(?:" + _END + r"|[(\[])",
            # a call or a subscript: getpass(), os.environ[
            _atomic(f"{tag}_c", r"[a-z_][a-z0-9_.]*") + r"[(\[]",
        )
    )


# key=value, key = "value" (spaced needs a quoted value: token = tokenizer.eos_token is code),
# and a quoted key's "key": value.
_OP = (
    r"(?:=(?!=)|[ \t]+=[ \t]*(?=[bfru]{0,2}"
    + _QUOTE
    + r")|=[ \t]+(?=[bfru]{0,2}"
    + _QUOTE
    + r")|"
    + _QUOTE
    + r"[ \t]*:[ \t]*)"
)
_STRING_PREFIX = r"(?:[bfru]{1,2}(?=" + _QUOTE + r"))?"
# A quoted word under 12 chars under a plain token key is an NLP token: {"token": "the"}.
_NLP_SHORT = r"(?![^\"'\s\\]{1,11}" + _QUOTE + r")"


def _tail(tag: str, code: str, quoted: str, quoted_extra: str = "", bare_extra: str = "") -> str:
    """The value after the operator: quoted (``<tag>_q``) or bare (``<tag>_u``)."""
    return (
        _STRING_PREFIX
        + "(?:"
        + _QUOTE
        + "(?!"
        + _SKIP
        + ")"
        + quoted_extra
        + rf"(?P<{tag}_q>"
        + quoted
        + r")|(?![bfru]{0,2}"
        + _QUOTE
        + ")(?!"
        + code
        + "|"
        + _SKIP
        + ")"
        + bare_extra
        + rf"(?P<{tag}_u>"
        + _VALUE
        + "))"
    )


def _behind(keys: Iterable[str], before: str = "") -> str:
    """One lookbehind per key length (Python's must be fixed-width): the text just before the
    match ends in ``before`` plus one of ``keys``."""
    by_width: dict[int, list[str]] = {}
    for key in keys:
        by_width.setdefault(len(key), []).append(re.escape(key))
    return (
        "(?:"
        + "|".join(f"(?<={before}(?:{'|'.join(same)}))" for _, same in sorted(by_width.items()))
        + ")"
    )


def _query(keys: Iterable[str], group: str = "query") -> str:
    """``?key=`` or ``&key=``: in a URL's query, where a value is never code."""
    return "(?==)" + _behind(keys, "[?&]") + "=(?!" + _SKIP + rf")(?P<{group}>" + _VALUE + ")"


# A token key under one of these prefixes is a credential even when its value is short.
_CREDENTIAL_PREFIXES = (
    "access",
    "refresh",
    "id",
    "auth",
    "api",
    "bearer",
    "session",
    "csrf",
    "xsrf",
    "oauth",
    "github",
    "gh",
    "gitlab",
    "hf",
    "slack",
    "bot",
    "jwt",
    "client",
    "private",
    "secret",
    "personal",
    "app",
    "service",
    "security",
)
_CREDENTIAL_TOKEN_KEYS = [f"{p}{sep}token" for p in _CREDENTIAL_PREFIXES for sep in ("_", "-", "")]
_QUERY_TOKEN_KEYS = ["token", "access_token", "api_token", "auth_token", "id_token"]
_QUERY_TOKEN_KEYS += ["refresh_token", "private_token", "oauth_token", "session_token"]
_QUERY_TOKEN_KEYS += ["hf_token", "accesstoken", "authtoken"]

# What most "token" keys in outputs hold, turned down before anything else is tried: a number
# (max_token=512, "token": 2003) or a quoted word under 12 chars ({"token": "the"}, an NLP
# token; the rule ignores case, so it can run in the regex). Only a credential key's quoted word
# ({"api_token": "abc123xyz"}) and a URL query's number of 6+ digits get past it, in the later
# branches. Each branch starts with a one-character check, so a miss costs little.
_BENIGN_TOKEN = (
    r"(?:[ \t]*=[ \t]*|\\?[\"'][ \t]*:[ \t]*)(?:"
    + _NUMBER
    + "|[bfru]{0,2}"
    + _QUOTE
    + r"[^\"'\s\\]{1,11}"
    + _QUOTE
    + ")"
)
# token=…, access_token=…, HF_TOKEN = "…", "token": "…", ?token=…
_TOKEN = (
    "token(?=[= \t\"'\\\\])(?:(?!"
    + _BENIGN_TOKEN
    + ")(?:"
    + _query(_QUERY_TOKEN_KEYS)
    + "|"
    + _OP
    + _tail("t", _code("(?<=token)", "t"), _WORD, quoted_extra=_NLP_SHORT)
    + ")|(?<=[a-z_-]token)(?="
    + _OP
    + "[bfru]{0,2}"
    + _QUOTE
    + ")"
    + _behind(_CREDENTIAL_TOKEN_KEYS)
    + _OP
    + _STRING_PREFIX
    + _QUOTE
    + "(?!"
    + _SKIP
    + r")(?P<c_q>"
    + _WORD
    + r")|(?==[-+]?\d{6})"
    + _query(_QUERY_TOKEN_KEYS, "query_n")
    + ")"
)
# password=…, DB_PASSWORD = "…", PGPASSWORD=…, "passphrase": "…"
_PASSWORD = (
    "pass(?:word|wd|phrase)(?:"
    + _query(["password", "passwd"])
    + "|"
    + _OP
    + _tail("p", _code(_behind(["pass", "password", "passwd", "passphrase", "pwd"]), "p"), _PHRASE)
    + ")"
)
# client_secret=…, SECRET = "…", "secret": "…"
_SECRET = (
    "secret(?:"
    + _query(["secret", "client_secret"])
    + "|"
    + _OP
    + _tail("s", _code(_behind(["secret", "secrets"]), "s"), _PHRASE)
    + ")"
)
# api_key=…, STRIPE_KEY = "…", "apiKey": "…", X-Api-Key: … . Never a bare "key"; the name must
# be secret-shaped (checked in Python, so SORT_KEY is left alone) and the value 8+ chars with a
# digit, as key material has.
_KEY = (
    "key(?:"
    + _query(["key", "api_key", "apikey", "api-key"])
    + r"|(?<=[a-z0-9_-]key)(?:"
    + _OP
    + r"|(?<=-key)[ \t]*:[ \t]*)"
    + _tail(
        "k",
        _NUMBER + "|" + _code(_behind(["key", "keys"]), "k"),
        _WORD,
        quoted_extra=r"(?=[^\"'\s\\]{8})(?=[^\"'\s\\]*\d)",
        bare_extra=r"(?=" + _V + r"{8})(?=" + _V + r"*\d)",
    )
    + ")"
)
_AUTHORIZATION = (
    r"authorization\\?[\"']?[ \t]*[:=][ \t]*[bfru]{0,2}\\?[\"']?(?:token|bearer|basic)[ \t]+(?!"
    + _SKIP
    + r")(?P<query>"
    + _VALUE
    + ")"
)
# (the regex's leading literal, its source, run on the lowercased text; kind; whether the key
# name must be secret-shaped). Each is compiled the first time a text holds its literal: the
# hooks run in a fresh process each time, and compiling all five takes ~8 ms on Python 3.9.
_FOLD_PATTERNS = (
    ("token", _TOKEN, "token", False),
    ("pass", _PASSWORD, "password", False),
    ("secret", _SECRET, "secret", False),
    ("key", _KEY, "key", True),
    ("authorization", _AUTHORIZATION, "authorization", False),
)
_fold_compiled: dict[str, re.Pattern[str]] = {}

# scheme://user:password@ . Bounded, so the scan never runs away on a long line.
_USERINFO = re.compile(r"://(?P<s>[^\s/?#@:'\"<>]{0,256}:[^\s/?#'\"<>]{1,512})@")
_AWS = re.compile(r"A(?:KIA|SIA)[A-Z0-9]{16}(?![A-Za-z0-9])")
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PEM_BODY = re.compile(r"(?:[A-Za-z0-9+/=]|\r?\n|\\r|\\n)*")
_GITHUB = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_SK = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_SLACK = re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}")
_WORDISH = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_NAME_CHARS = _WORDISH | frozenset("-")
_KEY_NAME_MAX = 256  # how far back from "key" nh reads the key's name

# Scanned with ``regex.search``, the word-boundary check done in Python: (regex, kind, the
# characters that may not come just before a match).
_WORD_BEFORE = _WORDISH
_DASH_BEFORE = _WORDISH | frozenset("-")
_CASE_PATTERNS = (
    (_AWS, "aws-key", _WORD_BEFORE),
    (_GITHUB, "github-token", _WORD_BEFORE),
    (_SK, "api-key", _DASH_BEFORE),
    (_SLACK, "slack-token", _WORD_BEFORE),
)
# str.translate table for the (never needed so far) case where lower() changes the length.
_ASCII_LOWER = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}

Span = tuple[int, int, int, str]  # start, end, weight (a value's length; 0 for a pattern), marker
_START = itemgetter(0)


def name_parts(name: str) -> list[str]:
    """``apiKey`` → ["api", "key"]; ``DB_PASSWORD`` → ["db", "password"]."""
    return [part for part in _NAME_SPLIT.split(_CAMEL.sub(r"\1_\2", name).lower()) if part]


def is_secret_name(name: str) -> bool:
    """Whether a variable name says it holds a secret (shared with lint rule L014).

    Whole parts only: API_TOKEN, db_password, PGPASSWORD, apiKey, DB_PASS, GITHUB_PAT,
    BASIC_AUTH and STRIPE_KEY match; tokens, tokenizer, author, keyboard, key, SORT_KEY,
    PRIMARY_KEY and FIRST_PASS don't. A last part naming a place or a property (TOKEN_FILE,
    SECRET_NAME, token_ids) doesn't either.
    """
    parts = name_parts(name)
    if not parts or (len(parts) > 1 and parts[-1] in NOT_SECRET_LAST):
        return False
    if any(part in SECRET_NAME_PARTS or part.endswith(SECRET_PART_ENDINGS) for part in parts):
        return True
    if any(pair in SECRET_PAIRS for pair in zip(parts, parts[1:])):
        return True
    last, before = parts[-1], (parts[-2] if len(parts) > 1 else "")
    if last in ("pass", "pw"):
        return before not in NOT_PASSWORD_BEFORE
    if last == "pat":
        return before in PAT_BEFORE
    return last == "key" and bool(before) and before not in NOT_SECRET_KEY_BEFORE


def parse_env(text: str) -> list[tuple[str, str]]:
    """``KEY=VALUE`` lines of a .env file, in order.

    Handles ``export``, blank lines and ``#`` comments, single quotes (literal), double quotes
    (``\\n``, ``\\t``, ``\\\\``, ``\\"`` escapes; may span lines) and an unquoted value's
    `` #`` comment. Lines that aren't assignments are skipped.
    """
    pairs: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME.match(name):
            continue
        rest = rest.strip()
        quote = rest[:1]
        if quote == "'":
            end = rest.find("'", 1)
            value = rest[1:end] if end != -1 else rest[1:]
        elif quote == '"':
            value, index = _double_quoted(rest[1:], lines, index)
        else:
            cut = re.search(r"\s#", rest)
            value = (rest[: cut.start()] if cut else rest).strip()
        pairs.append((name, value))
    return pairs


def _double_quoted(rest: str, lines: list[str], index: int) -> tuple[str, int]:
    """A double-quoted value from ``rest`` on, reading on into later lines until it closes."""
    out: list[str] = []
    text = rest
    while True:
        position = 0
        while position < len(text):
            char = text[position]
            if char == "\\" and position + 1 < len(text):
                follow = text[position + 1]
                out.append(_ESCAPES.get(follow, "\\" + follow))
                position += 2
                continue
            if char == '"':
                return "".join(out), index
            out.append(char)
            position += 1
        if index >= len(lines):
            return "".join(out), index
        out.append("\n")
        text = lines[index]
        index += 1


def _clean_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(str(name), str(value)) for name, value in pairs if name and value]


class Redactor:
    """Replaces secret values and secret-shaped text. Immutable once built.

    ``values``: (name, value) pairs, replaced by ``[redacted:NAME]``; ``strong`` names those
    whose cut pieces are redacted too (secret-named values, the Jupyter token).
    """

    def __init__(
        self,
        values: Iterable[tuple[str, str]] = (),
        *,
        strong: Iterable[str] = (),
        root: Path | None = None,
    ) -> None:
        seen: dict[str, str] = {}
        for name, value in _clean_pairs(values):
            seen.setdefault(value, name)  # the first name wins
        ordered = sorted(seen.items(), key=lambda item: -len(item[0]))
        self.root = root
        self._values: list[tuple[str, str]] = [(v, f"{MARKER}{n}]") for v, n in ordered]
        strong_names = set(strong)
        self._strong: list[tuple[str, str]] = [
            (v, f"{MARKER}{n}]")
            for v, n in ordered
            if n in strong_names and len(v) >= FRAGMENT_MIN + 4
        ]
        self._bracketed = [v for v, _ in self._values if "[" in v or "]" in v]
        longest = len(ordered[0][0]) if ordered else 0
        self.margin = max(MIN_MARGIN, longest + 1)

    @property
    def names(self) -> list[str]:
        """The names whose values are redacted, longest value first."""
        return list(dict.fromkeys(marker[len(MARKER) : -1] for _, marker in self._values))

    def __len__(self) -> int:
        return len(self._values)

    @classmethod
    def for_project(cls, root: Path | None, environ: Mapping[str, str] | None = None) -> Redactor:
        """The redactor for a project: its .env, the process env and :func:`add_value`'s
        values. Cached until .env (mtime, size), those env values or the added values change."""
        return _build(root, os.environ if environ is None else environ)

    def redact(self, text: str) -> str:
        """``text`` with every known value and secret-shaped piece replaced by a marker."""
        if not isinstance(text, str) or not text:
            return text
        spans = self._value_spans(text)
        if self._strong and len(text) <= FRAGMENT_MAX_TEXT:
            spans += self._fragment_spans(text)
        spans += pattern_spans(text)
        if not spans:
            return text
        out = _apply(text, spans)
        for _ in range(3):  # a bracketed value can reappear next to a marker: [redacted:A]]x
            again = [v for v in self._bracketed if v in out]
            if not again:
                break
            out = _apply(out, self._value_spans(out))
        return out

    def redact_head(self, text: str, limit: int) -> str:
        """The redacted start of ``text``, for a caller that then cuts it at ``limit``.

        Only a window of ``text`` is redacted, and the rest isn't scanned. The window is at
        least ``limit + margin`` (margin ≥ the longest value) and grows until its redacted form
        still reaches ``limit + margin``: markers are shorter than what they replace, and the
        window's own raw edge, which may cut a secret, must stay past the caller's cut.
        """
        if not isinstance(text, str):
            return text
        want = max(0, limit) + self.margin
        end = want
        while True:
            out = self.redact(text[:end])
            if end >= len(text) or len(out) >= want:
                return out
            end *= 2

    def _value_spans(self, text: str) -> list[Span]:
        spans: list[Span] = []
        find = text.find
        for value, marker in self._values:
            size = len(value)
            start = find(value)
            while start != -1:
                spans.append((start, start + size, size, marker))
                start = find(value, start + size)
        return spans

    def _fragment_spans(self, text: str) -> list[Span]:
        """Pieces of a secret-named value cut by a truncation nh doesn't control (reprlib's
        ``abc...xyz``, pandas' ``abc...``, a stream whose head was dropped): a run of at least
        ``FRAGMENT_MIN`` characters matching the value's start just before a cut, or its end
        just after one."""
        spans: list[Span] = []
        find = text.find
        for value, marker in self._strong:
            head, tail = value[:FRAGMENT_MIN], value[-FRAGMENT_MIN:]
            start = find(head)
            while start != -1:
                end, used = start + FRAGMENT_MIN, FRAGMENT_MIN
                while end < len(text) and used < len(value) and text[end] == value[used]:
                    end, used = end + 1, used + 1
                if used < len(value) and (end == len(text) or text.startswith(("...", "…"), end)):
                    spans.append((start, end, used, marker))
                start = find(head, start + 1)
            start = find(tail)
            while start != -1:
                first, left = start, len(value) - FRAGMENT_MIN
                while first > 0 and left > 0 and text[first - 1] == value[left - 1]:
                    first, left = first - 1, left - 1
                before = text[max(0, first - 3) : first]
                if left > 0 and (first == 0 or before.endswith(("...", "…", "\n"))):
                    spans.append((first, start + FRAGMENT_MIN, len(value) - left, marker))
                start = find(tail, start + 1)
        return spans


def pattern_spans(text: str) -> list[Span]:
    """Spans of secret-shaped text: URL passwords, tokens, cloud and service keys."""
    spans: list[Span] = []
    for match in _USERINFO.finditer(text):
        if _real_userinfo(match.group("s")):
            spans.append((match.start("s"), match.end("s"), 0, f"{MARKER}url-userinfo]"))
    for regex, kind, not_before in _CASE_PATTERNS:
        match = regex.search(text)
        while match is not None:
            start = match.start()
            if (start and text[start - 1] in not_before) or not _plausible(kind, match.group(0)):
                match = regex.search(text, start + 1)
                continue
            spans.append((start, match.end(), 0, f"{MARKER}{kind}]"))
            match = regex.search(text, match.end())
    match = _PEM_BEGIN.search(text)
    while match is not None:
        end = _pem_end(text, match.end())
        spans.append((match.start(), end, 0, f"{MARKER}private-key]"))
        match = _PEM_BEGIN.search(text, end)
    lowered = _fold(text)
    add = spans.append
    for literal, source, kind, named in _FOLD_PATTERNS:
        regex = _fold_compiled.get(kind)
        if regex is None:
            if literal not in lowered:  # no match possible: don't compile it yet
                continue
            regex = _fold_compiled[kind] = re.compile(source)
        marker = f"{MARKER}{kind}]"
        for match in regex.finditer(lowered):
            # the checks that need the text's case (inline: a dense output has many hits)
            group = match.lastgroup or ""
            start, end = match.span(group)
            if group[-1] == "u" and end - start < 16 and _code_word(text, start, end):
                continue
            if named and group[0] != "q" and not is_secret_name(_key_name(text, match.start())):
                continue
            add((start, end, 0, marker))
    return spans


def _fold(text: str) -> str:
    """``text`` lowercased with every offset kept, for the case-insensitive patterns.

    Only U+0130 (İ) lowers to two characters (checked over all of Unicode on 3.9 and 3.13), so
    it becomes "i" first; the ASCII-only fallback never shortens or lengthens anything.
    """
    if "İ" in text:
        text = text.replace("İ", "i")
    lowered = text.lower()
    if len(lowered) != len(text):  # a future Unicode table: lowercase ASCII only
        lowered = text.translate(_ASCII_LOWER)
    return lowered


def _code_word(text: str, start: int, end: int) -> bool:
    """A short name passed on in code: f(token=hf), {"token": tok}. Letters and "_" only, not
    mixed case (a letters-only secret, AbCdEf…, is mixed), under 16 chars (the caller checks)
    and followed by ")", "," or "}". (A URL query's key never gets here: its branch comes
    first.)"""
    value = text[start:end]
    return (
        text[end : end + 1] in (")", ",", "}")
        and value.replace("_", "").isalpha()
        and (value.islower() or value.isupper())
    )


def _key_name(text: str, key_start: int) -> str:
    """The whole name that ends in the matched ``key``: ``STRIPE_KEY``, ``x-api-key``,
    ``apiKey``."""
    first, end = key_start, key_start + 3
    stop = max(0, first - _KEY_NAME_MAX)
    while first > stop and text[first - 1] in _NAME_CHARS:
        first -= 1
    return text[first:end]


def _real_userinfo(userinfo: str) -> bool:
    """``user:password`` with a password that isn't a placeholder or a marker already."""
    password = userinfo.partition(":")[2]
    return MARKER not in userinfo and not _PLACEHOLDER.match(password)


def _plausible(kind: str, found: str) -> bool:
    """``sk-`` needs a digit: not "sk-learn-compatible-estimators"."""
    return kind != "api-key" or any(c.isdigit() for c in found[3:])


def _pem_end(text: str, body: int) -> int:
    """Where a private key block that starts its body at ``body`` ends: after its END line,
    or, unterminated (cut), after the key-shaped characters that follow."""
    end = text.find("-----END ", body, body + PEM_MAX_CHARS)
    if end != -1:
        match = _PEM_END.match(text, end)
        if match:
            return match.end()
    match = _PEM_BODY.match(text, body, min(len(text), body + PEM_OPEN_MAX_CHARS))
    return match.end() if match else body


def _apply(text: str, spans: Sequence[Span]) -> str:
    """Replace merged overlapping spans. Each group takes the marker of its heaviest span (the
    longest value); among equals, the first to start, and of those the longest."""
    if not spans:
        return text
    ordered = sorted(spans, key=_START)  # stable: a C-level key (a dense output has many)
    pieces: list[str] = []
    add = pieces.append
    cursor = 0
    group_start, group_end, weight, marker = ordered[0]
    lead_start, lead_end = group_start, group_end
    for start, end, span_weight, span_marker in ordered[1:]:
        if start < group_end:
            if end > group_end:
                group_end = end
            if span_weight > weight or (
                span_weight == weight and start == lead_start and end > lead_end
            ):
                weight, marker, lead_start, lead_end = span_weight, span_marker, start, end
            continue
        add(text[cursor:group_start])
        add(marker)
        cursor = group_end
        group_start, group_end, weight, marker = start, end, span_weight, span_marker
        lead_start, lead_end = start, end
    add(text[cursor:group_start])
    add(marker)
    add(text[group_end:])
    return "".join(pieces)


# --- building, caching and the installed redactor -------------------------------------------

PATTERNS_ONLY = Redactor()
_added: dict[str, str] = {}
_cache: dict[str, tuple[tuple, Redactor]] = {}
_installed: Redactor | None = None


def is_weak_value(name: str, value: str, environ: Mapping[str, str] | None = None) -> bool:
    """A value that is no secret whatever its name says, so it is never redacted as a value:
    a well-known default (postgres, changeme), part of its own name (POSTGRES_PASSWORD=
    postgres), the user's login name, a placeholder (``<your-key>``, ``${X}``) or one repeated
    character (``xxxxxxxx``)."""
    folded = value.lower()
    if folded in WEAK_VALUES or len(set(folded)) == 1 or _PLACEHOLDER.match(value):
        return True
    if folded == name.lower() or folded in name_parts(name):
        return True
    users = {str(environ.get(var) or "").lower() for var in _USER_VARS} if environ else set()
    return folded in users


def env_values(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Secret-named process variables of ``SECRET_NAME_MIN`` chars or more that aren't weak
    (:func:`is_weak_value`), sorted by name, each also in its escaped form (``--json``)."""
    found: list[tuple[str, str]] = []
    for name, value in sorted(environ.items()):
        if name in _ENV_SKIP or len(value) < SECRET_NAME_MIN or not is_secret_name(name):
            continue
        if not is_weak_value(name, value, environ):
            found += _with_encoded(name, value)
    return found


def dotenv_values(
    pairs: Iterable[tuple[str, str]], environ: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """The .env values nh redacts: a secret-named one of ``SECRET_NAME_MIN`` chars or more,
    any other of ``ANY_NAME_MIN`` or more (spike V11: redacting REGION=eu-west-1 hid a region
    name everywhere) unless its name says it is a place or a label, and never a weak one."""
    found: list[tuple[str, str]] = []
    for name, value in pairs:
        if is_secret_name(name):
            keep = len(value) >= SECRET_NAME_MIN
        else:
            parts = name_parts(name)
            exempt = bool(parts) and parts[-1] in LENGTH_EXEMPT_LAST
            if exempt and SECRET_BEARING_PARTS.intersection(parts):
                exempt = False
            keep = len(value) >= ANY_NAME_MIN and not exempt
        if keep and not is_weak_value(name, value, environ):
            found += _with_encoded(name, value)
    return found


def _with_encoded(name: str, value: str) -> list[tuple[str, str]]:
    """The value, and the escaped form ``cat .env`` or ``json.dumps`` would show."""
    encoded = _encode(value)
    return [(name, value)] if encoded == value else [(name, value), (name, encoded)]


def _encode(value: str) -> str:
    """``value`` written back with the double-quote escapes :func:`parse_env` decodes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _read_dotenv(path: Path) -> list[tuple[str, str]]:
    try:
        return parse_env(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def _build(root: Path | None, environ: Mapping[str, str]) -> Redactor:
    env = tuple(env_values(environ))
    added = tuple(sorted(_added.items()))
    users = tuple(str(environ.get(var) or "") for var in _USER_VARS)
    dotenv = Path(root) / ".env" if root is not None else None
    key = (_stat_key(dotenv) if dotenv is not None else None, env, added, users)
    slot = str(root)
    cached = _cache.get(slot)
    if cached is not None and cached[0] == key:
        return cached[1]
    from_file = (
        dotenv_values(_read_dotenv(dotenv), environ) if key[0] is not None and dotenv else []
    )
    pairs = list(from_file) + list(env) + list(added)
    strong = {name for name, _ in pairs if is_secret_name(name)}
    redactor = Redactor(pairs, strong=strong, root=Path(root) if root is not None else None)
    _cache[slot] = (key, redactor)
    return redactor


def add_value(name: str, value: str | None) -> None:
    """Redact ``value`` as ``[redacted:NAME]`` from now on (the discovered Jupyter token).

    Shorter than ``SECRET_NAME_MIN``, it is left to the patterns (``token=…``). The installed
    redactor is rebuilt with it.
    """
    if not value or len(value) < SECRET_NAME_MIN or _added.get(name) == value:
        return
    _added[name] = value
    if _installed is not None:
        install(Redactor.for_project(_installed.root))


def install(redactor: Redactor) -> Redactor:
    """Make ``redactor`` what :func:`current` returns (the gateway: once per project)."""
    global _installed
    _installed = redactor
    return redactor


def current() -> Redactor:
    """The installed redactor, or a patterns-only one: never the identity."""
    return _installed if _installed is not None else PATTERNS_ONLY


def redact(text: str) -> str:
    """``current().redact(text)``."""
    return current().redact(text)


def reset() -> None:
    """Forget the installed redactor, the cache and the added values (tests)."""
    global _installed
    _installed = None
    _added.clear()
    _cache.clear()
