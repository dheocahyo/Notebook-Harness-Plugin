"""Code that reaches the network (L012, design §6.4).

One walk over a cell in statement order: names carry the network URLs (their hosts), the literal
strings and the sessions they hold, and each call, magic and shell command that would reach
another machine is a site, with the hosts it reaches. The cells above the target are walked the
same way first (``bindings``), for their names only. It reads code only, never runs it, and keeps
hosts only, never a URL, so no userinfo, path, query or token reaches a finding. Stdlib plus
``_shared.hosts``, ``magics`` and ``secret_scan``'s shell reader, importable on Python 3.11 like
``secret_scan``.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from nh_gateway._shared import hosts as _hosts
from nh_gateway.lint.magics import Masked, lines_of, mask
from nh_gateway.lint.secret_scan import shell_commands, shell_lines


@dataclass(frozen=True)
class Site:
    """One place the cell reaches the network."""

    where: str  # the call's dotted name or the command, never its arguments
    hosts: tuple[str, ...]  # host keys, loopback left out, in source order
    unknown: bool  # it also reaches a host nh can't read


@dataclass(frozen=True)
class _Session:
    hosts: tuple[str, ...] = ()
    unreadable: bool = False
    local: bool = False


@dataclass(frozen=True)
class _Val:
    """What a value holds, to L012."""

    hosts: tuple[str, ...] = ()  # network URLs' host keys (loopback left out)
    unreadable: bool = False  # a network URL whose host nh can't read
    local: bool = False  # a loopback URL or host
    bare: tuple[str, ...] = ()  # a string that is only a host ("api.example.org")
    session: _Session | None = None
    ipython: bool = False  # get_ipython()
    text: str | None = None  # the whole string, when it is one literal

    @property
    def url(self) -> bool:
        return bool(self.hosts) or self.unreadable


@dataclass(frozen=True, eq=False)
class Seed:
    """What the cells above bind (``bindings``): names and import names, never sites. Two seeds
    are equal when they come from the same cells (``key``), so the per-cell cache hashes a short
    key, not every name."""

    names: tuple[tuple[str, _Val], ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()
    key: str = ""  # a digest of the cell sources it was walked from, in order

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Seed) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)


EMPTY = Seed()
_NOTHING = _Val()
_IPYTHON = _Val(ipython=True)

_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")
# Calls that reach the network themselves, whatever their URL's source.
_NET_FUNCS = frozenset(
    {f"requests.{verb}" for verb in _HTTP_VERBS}
    | {f"requests.api.{verb}" for verb in _HTTP_VERBS}
    | {f"httpx.{verb}" for verb in (*_HTTP_VERBS, "stream")}
    | {"urllib.request.urlopen", "urllib.request.urlretrieve", "urllib3.request"}
    | {"aiohttp.request", "socket.create_connection", "socket.getaddrinfo"}
    | {"socket.gethostbyname", "socket.gethostbyname_ex"}
)
# Clients and connections: their network methods reach the network (FTP and SMTP connect as
# soon as they are given a host).
_SESSIONS = frozenset(
    {"requests.Session", "requests.session", "requests.sessions.Session", "httpx.Client"}
    | {"httpx.AsyncClient", "aiohttp.ClientSession", "urllib3.PoolManager"}
    | {"urllib3.ProxyManager", "urllib3.HTTPConnectionPool", "urllib3.HTTPSConnectionPool"}
    | {"urllib.request.build_opener", "socket.socket", "http.client.HTTPConnection"}
    | {"http.client.HTTPSConnection", "ftplib.FTP", "ftplib.FTP_TLS", "smtplib.SMTP"}
    | {"smtplib.SMTP_SSL"}
)
_CONNECTS = frozenset({"ftplib.FTP", "ftplib.FTP_TLS", "smtplib.SMTP", "smtplib.SMTP_SSL"})
_SESSION_METHODS = frozenset(
    {*_HTTP_VERBS, "stream", "send", "open", "urlopen", "connect", "connect_ex", "sendto"}
    | {"ws_connect"}
)
# Calls that take a host by itself (a string or a (host, port) pair), not a URL.
_HOST_CALLS = frozenset(
    {"socket.create_connection", "socket.getaddrinfo", "socket.gethostbyname"}
    | {"socket.gethostbyname_ex", "http.client.HTTPConnection", "http.client.HTTPSConnection"}
    | {"urllib3.HTTPConnectionPool", "urllib3.HTTPSConnectionPool"}
    | _CONNECTS
)
_HOST_METHODS = frozenset({"connect", "connect_ex", "sendto"})
_HOST_KEYWORDS = frozenset({"host", "address"})  # a host call's host, given by keyword
# Request builders: quiet, and their URL counts where the request is sent.
_REQUESTS = frozenset({"urllib.request.Request", "requests.Request", "httpx.Request"})
_SHELL_CALLS = frozenset(
    {"os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.check_call"}
    | {"subprocess.check_output", "subprocess.Popen", "subprocess.getoutput"}
    | {"subprocess.getstatusoutput"}
)
# A URL in these is text, not a request: matched on the call's own name, or the last part of
# its dotted name (``rich.print``).
_QUIET_NAMES = frozenset(
    {"print", "pprint", "pp", "display", "repr", "ascii", "format", "len", "bool", "type"}
    | {"isinstance", "issubclass", "hash", "id", "callable", "help", "Markdown", "HTML"}
    | {"Latex", "Code", "IFrame", "display_markdown", "display_html", "warn"}
    # containers and frames that hold a URL as a value
    | {"DataFrame", "Series", "Index", "Categorical", "array", "asarray", "Counter"}
    | {"OrderedDict", "defaultdict", "deque", "HttpUrl", "AnyUrl", "AnyHttpUrl"}
)
_QUIET_QUALS = frozenset(
    {"os.getenv", "os.getenvb", "os.environ.get", "os.environ.setdefault", "json.dumps"}
    | {"json.dump", "numpy.where", "numpy.select", "yarl.URL"}  # warnings.warn: `warn`
)
_QUIET_MODULES = (
    "urllib.parse.",
    "re.",
    "logging.",
    "IPython.display.",
    "textwrap.",
    "os.path.",
    "posixpath.",
    "ntpath.",
    "pathlib.",
    "hashlib.",
    "shlex.",
)
# Methods that compare, relabel or label: a URL given to them is a value.
_QUIET_METHODS = frozenset(
    {"isin", "eq", "ne", "lt", "le", "gt", "ge", "fillna", "where", "mask", "assign"}
    | {"rename", "groupby", "contains", "drop", "query", "set_title", "set_xlabel"}
    | {"set_ylabel", "set_zlabel", "suptitle", "supxlabel", "supylabel", "xlabel", "ylabel"}
    | {"text", "annotate", "figtext", "legend", "set_label", "set_text", "set_caption"}
    | {"set_xticklabels", "set_yticklabels", "add_annotation", "update_layout"}
    | {"update_xaxes", "update_yaxes", "properties", "add_argument"}
)
# In any other call, a URL given as one of these keywords is a label, not a request.
_LABEL_KEYWORDS = frozenset(
    {"title", "label", "xlabel", "ylabel", "zlabel", "desc", "description", "caption"}
    | {"help", "name", "text", "legend"}
)
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception", "fatal", "log"}  # warn: quiet
)
_LOGGER = re.compile(r"_*(?:\w+_)?(?:log|logger|logging)", re.I)
_EXCEPTION = re.compile(r"\w*(?:Error|Exception|Warning)")
_WRITE_METHODS = frozenset(
    {"write", "writelines", "writerow", "writerows", "write_text", "write_bytes"}
)
# Calls that hand back what they are given (a URL stays a URL): no request of their own.
_PASS_NAMES = frozenset(
    {"str", "dict", "list", "tuple", "set", "frozenset", "sorted", "reversed", "enumerate"}
    | {"zip", "iter", "next", "min", "max", "filter", "tqdm"}
)
_PASS_QUALS = frozenset(
    {"copy.copy", "copy.deepcopy", "tqdm.tqdm", "tqdm.auto.tqdm", "tqdm.notebook.tqdm"}
    | {"shlex.quote"}  # shlex.split and shlex.join: rendered (_RENDERED), or a `.split`/`.join`
)
_PASS_METHODS = frozenset(
    {"strip", "rstrip", "lstrip", "removeprefix", "removesuffix", "replace", "lower"}
    | {"casefold", "encode", "decode", "copy", "values", "items", "geturl", "join"}
)
_RENDERED = frozenset({"str", "shlex.split", "shlex.join", "shlex.quote"})  # their text, rendered
_SPLITS = frozenset({"split", "rsplit", "partition", "rpartition"})  # item 0 keeps the start
_LOOKUPS = frozenset({"get", "pop", "setdefault"})  # (key, default): the default is handed back
_MUTATORS = frozenset(
    {"append", "extend", "insert", "add", "update", "appendleft", "extendleft", "setdefault"}
)
_STR_METHODS = frozenset(name for name in dir(str) if not name.startswith("_")) - _PASS_METHODS
# Names that stand for something else with no import (a module's own name, ``requests`` or
# ``socket``, stands for that module anyway).
_DEFAULT_NAMES = {
    "urlopen": "urllib.request.urlopen",
    "urlretrieve": "urllib.request.urlretrieve",
    "Request": "urllib.request.Request",
    "getenv": "os.getenv",
    "environ": "os.environ",
    "np": "numpy",
    "Path": "pathlib.Path",
} | {
    name: f"urllib.parse.{name}"
    for name in ("urljoin", "urlparse", "urlsplit", "urlunparse", "urlunsplit", "urlencode")
    + ("quote", "quote_plus", "unquote", "unquote_plus", "parse_qs", "parse_qsl", "urldefrag")
}

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_SHELL_CELL_MAGICS = frozenset({"bash", "sh", "system", "sx", "!"})
_PYTHON_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun", "debug", "python", "python3"})
_STATEMENT_MAGICS = frozenset({"timeit", "prun"})  # %time's statement is masked in as code
_MAGIC_VALUE_OPTIONS = frozenset({"-n", "-r", "-p", "-l", "-s", "-T", "-D"})
_SCRIPT_VALUE_OPTIONS = frozenset({"--out", "--err", "--proc"})
_SAYS = frozenset({"echo", "printf", "print", "true", ":"})
_PACKAGE_TOOLS = frozenset({"pip", "pip3", "conda", "mamba", "micromamba", "uv"})
_FETCHERS = frozenset({"curl", "wget", "kaggle", "gdown", "huggingface-cli", "hf", "gh"})
_LOGINS = frozenset({"ssh", "sftp"})
_HOST_TOOLS = frozenset(
    {"ftp", "telnet", "nc", "ncat", "netcat", "ping", "ping6", "dig", "nslookup", "host"}
    | {"traceroute", "whois"}
)
_SYSTEM_PACKAGES = frozenset({"apt", "apt-get", "brew", "npm", "yarn", "pnpm"})
_SYSTEM_VERBS = frozenset({"install", "i", "add", "ci", "update", "upgrade"})
_GIT_FETCHES = frozenset({"fetch", "pull", "push", "ls-remote"})
_SSH_VALUE_OPTIONS = frozenset(
    {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-P"}
    | {"-p", "-Q", "-R", "-S", "-W", "-w", "-B"}
)
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_ENV_VALUE_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
_REMOTE_WORD = re.compile(r"(?:[^@/\s:]+@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._\-]{2,}):(?!//)")
_BRACE_NAME = re.compile(r"\{([A-Za-z_]\w*)\}")
_DOLLAR_NAME = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
_LINE_MAGIC = re.compile(r"%(\w+)(.*)\Z", re.S)
_ASSIGNED = re.compile(r"\s*[A-Za-z_][\w\s,]*=\s*([!%].*)\Z", re.S)
_MASKED_ASSIGN = re.compile(r"\s*[A-Za-z_][\w\s,]*= None\s*\Z")
_SCHEME = re.compile(r"\s*([A-Za-z][A-Za-z0-9+.\-]*)://")
_PERCENT = re.compile(r"%(\([^)]*\))?[-#0 +]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?([a-zA-Z%])")
_FIELD = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_HOLE = "\0"  # an unknown part of a rendered string
_MAX_DEPTH = 4  # Python inside shell inside Python: as deep as shell_commands reads
_MAX_RENDER = 200  # parts of one rendered string


def scan(
    masked: Masked, lines: list[str], tree: ast.Module | None, seed: Seed = EMPTY
) -> list[Site]:
    """The places this cell reaches the network, in source order, with the names ``seed`` holds
    (the cells above, ``bindings``) bound first. ``tree`` is None for a ``%%`` cell or bad
    syntax."""
    scanner = _Scanner(masked, lines, seed)
    scanner.run(tree)
    return scanner.sites


def bindings(sources: Iterable[str]) -> Seed:
    """What the code cells ``sources`` (the cells above the target, in notebook order) bind, for
    ``scan``. A cell that doesn't parse binds nothing; a walk that raises gives the empty seed,
    and the cell itself is still scanned."""
    seed = EMPTY
    try:
        for source in sources:
            seed = _cell_seed(source, seed)
    except Exception:  # fail open: earlier cells only add names
        return EMPTY
    return seed


@functools.lru_cache(maxsize=1024)
def _cell_seed(source: str, seed: Seed) -> Seed:
    text = "\n".join(lines_of(source or ""))
    masked = mask(text)
    tree = _parse(masked.text) if masked.cell_magic is None else None
    if masked.cell_magic is None and tree is None:
        return seed
    scanner = _Scanner(masked, lines_of(text), seed)
    scanner.run(tree)
    key = hashlib.sha256(f"{seed.key}\0{source}".encode("utf-8", "surrogatepass")).hexdigest()
    return scanner.seed(key)


class _Scanner:
    def __init__(self, masked: Masked, lines: list[str], seed: Seed, depth: int = 0) -> None:
        self.masked = masked
        self.lines = lines
        self.masked_lines = masked.text.split("\n")
        self.names: dict[str, _Val] = dict(seed.names)
        self.aliases: dict[str, str] = dict(seed.aliases)
        self.sites: list[Site] = []
        self.magics = _magic_starts(masked, lines)
        self.done: set[int] = set()
        self.depth = depth

    def seed(self, key: str = "") -> Seed:
        return Seed(tuple(self.names.items()), tuple(self.aliases.items()), key)

    def run(self, tree: ast.Module | None) -> None:
        magic = self.masked.cell_magic
        if magic is not None:
            first = next((i for i, line in enumerate(self.lines) if line.strip()), None)
            if first is None:
                return
            program, body = _cell_program(self.lines[first]), self.lines[first + 1 :]
            if magic in _SHELL_CELL_MAGICS or (magic == "script" and program in _SHELLS):
                for text in shell_lines(body):
                    self.shell(text, python=False, prefix="")
                return
            python = magic == "script" and program.startswith("python")
            if magic not in _PYTHON_CELL_MAGICS and not python:
                return
            tree = _parse(self.masked.text)
        if tree is None:
            for number in sorted(self.magics):
                self.magic(number)
            return
        self.block(tree.body, definite=True)
        for number in sorted(set(self.magics) - self.done):  # none in practice: belt and braces
            self.magic(number)

    def python(self, code: str, label: str | None = None) -> None:
        """Python run elsewhere in the cell (``python -c``, ``%timeit``, ``run_cell_magic``),
        scanned with the names bound so far; its sites are this cell's, under ``label`` when
        given."""
        if self.depth >= _MAX_DEPTH:
            return
        text = "\n".join(lines_of(code))
        masked = mask(text)
        inner = _Scanner(masked, lines_of(text), self.seed(), self.depth + 1)
        inner.run(_parse(masked.text) if masked.cell_magic is None else None)
        for site in inner.sites:
            self.sites.append(Site(label, site.hosts, site.unknown) if label else site)

    # statements --------------------------------------------------------------------------------
    def block(self, body: Iterable[ast.stmt], *, definite: bool) -> None:
        for stmt in body:
            self.stmt(stmt, definite)

    def stmt(self, stmt: ast.stmt, definite: bool) -> None:
        if self._is_magic(stmt):
            self.done.add(stmt.lineno)
            self.magic(stmt.lineno)
            return
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._import(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.visit(stmt.decorator_list)
            args = stmt.args
            positional = [*args.posonlyargs, *args.args]
            tail = positional[len(positional) - len(args.defaults) :]
            defaults = list(zip(tail, args.defaults, strict=True))
            defaults += [
                (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d
            ]
            for arg, default in defaults:
                self.visit([default])
                self.bind(ast.Name(arg.arg, ast.Store()), self.eval(default), definite=False)
            self.block(stmt.body, definite=False)
        elif isinstance(stmt, ast.ClassDef):
            self.visit([*stmt.decorator_list, *stmt.bases, *stmt.keywords])
            self.block(stmt.body, definite=False)
        elif isinstance(stmt, (ast.If, ast.While)):
            self.visit([stmt.test])
            self.block(stmt.body, definite=False)
            self.block(stmt.orelse, definite=False)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.visit([stmt.iter])
            self.bind(stmt.target, self.eval(stmt.iter), definite=False)
            self.block(stmt.body, definite=False)
            self.block(stmt.orelse, definite=False)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self.visit([item.context_expr])
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, self.eval(item.context_expr), definite)
            self.block(stmt.body, definite=definite)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            self.block(stmt.body, definite=False)
            for handler in stmt.handlers:
                self.block(handler.body, definite=False)
            self.block(stmt.orelse, definite=False)
            self.block(stmt.finalbody, definite=False)
        elif isinstance(stmt, ast.Match):
            self.visit([stmt.subject])
            for case in stmt.cases:
                self.block(case.body, definite=False)
        elif isinstance(stmt, ast.Assign):
            self.visit([stmt.value])
            for target in stmt.targets:
                self.assign(target, stmt.value, definite)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self.visit([stmt.value])
                self.assign(stmt.target, stmt.value, definite)
        elif isinstance(stmt, ast.AugAssign):
            self.visit([stmt.value])
            self.bind(stmt.target, self.eval(stmt.value), definite=False)
        else:
            self.visit(list(ast.iter_child_nodes(stmt)))

    def _is_magic(self, stmt: ast.stmt) -> bool:
        if stmt.lineno not in self.magics or stmt.lineno in self.done:
            return False
        masked = self.masked_lines[stmt.lineno - 1].strip()
        return masked == "pass" or bool(_MASKED_ASSIGN.fullmatch(masked))

    def _import(self, stmt: ast.Import | ast.ImportFrom) -> None:
        for alias in stmt.names:
            if isinstance(stmt, ast.Import):
                if alias.asname:
                    self.aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    self.aliases[top] = top
            elif stmt.module and not stmt.level and alias.name != "*":
                self.aliases[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"

    def assign(self, target: ast.expr, value: ast.expr, definite: bool) -> None:
        """``a, b = URL1, URL2`` binds each to its own; any other form binds every name."""
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            starred = any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
            if len(target.elts) == len(value.elts) and not starred:
                for one, its in zip(target.elts, value.elts, strict=True):
                    self.assign(one, its, definite)
                return
        self.bind(target, self.eval(value), definite)

    def bind(self, target: ast.expr, value: _Val, definite: bool) -> None:
        if isinstance(target, ast.Name):
            old = self.names.get(target.id, _NOTHING)
            self.names[target.id] = value if definite else _union(old, value)
            self.aliases.pop(target.id, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self.bind(element, _strip_text(value), definite)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, value, definite)
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            name = target.value.id  # cfg["url"] = URL: the container now holds it
            self.names[name] = _strip_text(_union(self.names.get(name, _NOTHING), value))

    # expressions -------------------------------------------------------------------------------
    def visit(self, nodes: Iterable[ast.AST]) -> None:
        """Check every call in ``nodes``, inner ones first (as they run)."""
        for node in nodes:
            self._visit(node)

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            saved = dict(self.names)
            for comp in node.generators:
                self._visit(comp.iter)
                self.bind(comp.target, self.eval(comp.iter), definite=False)
                for test in comp.ifs:
                    self._visit(test)
            parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for part in parts:
                self._visit(part)
            self.names = saved
            return
        if isinstance(node, ast.Lambda):
            self._visit(node.body)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child)
        if isinstance(node, ast.NamedExpr):
            self.bind(node.target, self.eval(node.value), definite=False)
        if isinstance(node, ast.Call):
            self.call(node)

    def qual(self, node: ast.expr) -> str | None:
        """The dotted name a call's function stands for (``rq.get`` -> ``requests.get``)."""
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.aliases[node.id]
            if node.id in _DEFAULT_NAMES and node.id not in self.names:
                return _DEFAULT_NAMES[node.id]
            return node.id
        if isinstance(node, ast.Attribute):
            owner = self.qual(node.value)
            return f"{owner}.{node.attr}" if owner else None
        return None

    def eval(self, node: ast.AST | None) -> _Val:
        """What ``node``'s value holds: the URLs it is or starts with, a session, a host, the
        literal string it is."""
        if node is None:
            return _NOTHING
        if isinstance(node, ast.Constant):
            return _text_value(node.value) if isinstance(node.value, str) else _NOTHING
        if isinstance(node, ast.JoinedStr):
            return self.template(self.render(node) or [])
        if isinstance(node, ast.Name):
            return self.names.get(node.id, _NOTHING)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            parts = self.render(node)
            if parts is not None:
                return self.template(parts)
            if isinstance(node.op, ast.Add) and isinstance(
                node.right, (ast.List, ast.Tuple, ast.ListComp)
            ):
                return _union(self.eval(node.left), self.eval(node.right))
            return self.eval(node.left)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return _strip_text(_union(*(self.eval(e) for e in node.elts)))
        if isinstance(node, ast.Dict):
            values = (*node.keys, *node.values)
            return _strip_text(_union(*(self.eval(e) for e in values if e is not None)))
        if isinstance(node, ast.Subscript):
            inner = node.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in _SPLITS
            ):  # URL.split("?")[0] keeps the URL's start; any other item doesn't
                index = _constant(node.slice)
                return _strip_text(self.eval(inner.func.value)) if index == 0 else _NOTHING
            return _strip_text(self.eval(inner))
        if isinstance(node, (ast.Starred, ast.Await, ast.FormattedValue)):
            return self.eval(node.value)
        if isinstance(node, ast.NamedExpr):
            return self.eval(node.value)
        if isinstance(node, ast.IfExp):
            return _union(self.eval(node.body), self.eval(node.orelse))
        if isinstance(node, ast.BoolOp):
            return _union(*(self.eval(v) for v in node.values))
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return _strip_text(self._comprehension(node.generators, [node.elt]))
        if isinstance(node, ast.DictComp):
            return _strip_text(self._comprehension(node.generators, [node.key, node.value]))
        if isinstance(node, ast.Call):
            return self._call_value(node)
        return _NOTHING

    def _comprehension(self, generators: list[ast.comprehension], parts: list[ast.expr]) -> _Val:
        saved = dict(self.names)
        for comp in generators:
            self.bind(comp.target, self.eval(comp.iter), definite=False)
        value = _union(*(self.eval(part) for part in parts))
        self.names = saved
        return value

    # strings -----------------------------------------------------------------------------------
    def render(self, node: ast.AST, depth: int = 0) -> list[str | ast.expr] | None:
        """A string expression as its parts: literal text (a name bound to one literal string
        included) and the nodes whose text nh can't know, in order. None when ``node`` isn't a
        string expression nh reads."""
        if depth > 20:
            return None
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            text = self.names.get(node.id, _NOTHING).text
            return [text] if text is not None else [node]
        if isinstance(node, ast.JoinedStr):
            parts: list[str | ast.expr] = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    inner = None
                    if part.format_spec is None and part.conversion in (-1, ord("s")):
                        inner = self.render(part.value, depth + 1)
                    parts.extend(inner if inner is not None else [part.value])
            return parts[:_MAX_RENDER]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.render(node.left, depth + 1), self.render(node.right, depth + 1)
            if not any(isinstance(part, str) for part in (*(left or ()), *(right or ()))):
                return None  # no literal text on either side: a sum nh doesn't read as a string
            return [*(left or [node.left]), *(right or [node.right])][:_MAX_RENDER]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = self._literal(node.left, depth)
            if template is None:
                return None
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return self._percent(template, args, depth)
        if isinstance(node, ast.Call):
            return self._render_call(node, depth)
        return None

    def _render_call(self, node: ast.Call, depth: int) -> list[str | ast.expr] | None:
        func = node.func
        qual = self.qual(func)
        if qual in _RENDERED and len(node.args) == 1:  # before `.join`: shlex.join([…])
            argument = node.args[0]
            if qual == "shlex.join" and isinstance(argument, (ast.List, ast.Tuple)):
                words = [self._word(item) for item in argument.elts]
                return [" ".join(words)]
            return self.render(argument, depth + 1)
        if isinstance(func, ast.Attribute) and func.attr == "format":
            template = self._literal(func.value, depth)
            return None if template is None else self._format(template, node, depth)
        if isinstance(func, ast.Attribute) and func.attr == "join" and len(node.args) == 1:
            items = node.args[0]
            separator = self._literal(func.value, depth)
            if separator is None or not isinstance(items, (ast.List, ast.Tuple)):
                return None
            parts: list[str | ast.expr] = []
            for number, item in enumerate(items.elts):
                parts += [separator] if number else []
                parts += self.render(item, depth + 1) or [item]
            return parts[:_MAX_RENDER]
        return None

    def _literal(self, node: ast.AST, depth: int) -> str | None:
        parts = self.render(node, depth + 1)
        if parts is None or not all(isinstance(part, str) for part in parts):
            return None
        return "".join(part for part in parts if isinstance(part, str))

    def _percent(
        self, template: str, args: list[ast.expr], depth: int
    ) -> list[str | ast.expr] | None:
        """``"…%s…" % args``: each ``%s`` filled with its argument's parts."""
        parts: list[str | ast.expr] = []
        position, index = 0, 0
        for found in _PERCENT.finditer(template):
            parts.append(template[position : found.start()])
            position = found.end()
            if found.group(2) == "%":
                parts.append("%")
                continue
            if found.group(1) is not None or index >= len(args):
                parts.append(ast.Constant(None))  # a mapping key or a missing argument
                continue
            argument = args[index]
            index += 1
            value = _constant(argument)
            if found.group(2) in "sd" and isinstance(value, int) and not isinstance(value, bool):
                parts.append(str(value))
            elif found.group(2) == "s":
                parts.extend(self.render(argument, depth + 1) or [argument])
            else:
                parts.append(argument)
        parts.append(template[position:])
        return parts[:_MAX_RENDER]

    def _format(self, template: str, node: ast.Call, depth: int) -> list[str | ast.expr]:
        """``"…{}…".format(args)``: each plain field filled with its argument's parts."""
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        parts: list[str | ast.expr] = []
        position, auto = 0, 0
        for found in _FIELD.finditer(template):
            parts.append(template[position : found.start()])
            position = found.end()
            if found.group(1) is None:  # "{{" or "}}"
                parts.append(found.group(0)[0])
                continue
            field, _, spec = found.group(1).partition(":")
            field, _, conversion = field.partition("!")
            argument: ast.expr | None = None
            if field == "":
                argument = node.args[auto] if auto < len(node.args) else None
                auto += 1
            elif field.isdigit():
                argument = node.args[int(field)] if int(field) < len(node.args) else None
            elif field.isidentifier():
                argument = keywords.get(field)
            if argument is None or spec or conversion not in ("", "s"):
                parts.append(argument if argument is not None else ast.Constant(None))
                continue
            parts.extend(self.render(argument, depth + 1) or [argument])
        parts.append(template[position:])
        return parts[:_MAX_RENDER]

    def template(self, parts: list[str | ast.expr]) -> _Val:
        """What a rendered string reaches: a literal one's URL; one that starts with a node, that
        node's value (``f"{BASE}/items"``); one that starts with literal text, the URL whose scheme
        and host are literal (unknown parts only in its userinfo, port, path, query or
        fragment), or whose whole host is one name holding bare hosts."""
        parts = [part for part in parts if part != ""]
        if not parts:
            return _text_value("")
        if all(isinstance(part, str) for part in parts):
            return _text_value("".join(part for part in parts if isinstance(part, str)))
        first = parts[0]
        if not isinstance(first, str):
            return _strip_text(self.eval(first))
        nodes = [part for part in parts if not isinstance(part, str)]
        text = "".join(part if isinstance(part, str) else _HOLE for part in parts)
        scheme = _SCHEME.match(text)
        if not scheme or _HOLE in scheme.group(0):
            return _NOTHING
        rest = text[scheme.end() :]
        end = min((rest.find(mark) for mark in "/?#" if mark in rest), default=len(rest))
        netloc, tail = rest[:end], rest[end:]
        if any(ch.isspace() for ch in tail):
            return _NOTHING  # "https://x.org/a b{c}": prose
        userinfo, at, hostport = netloc.rpartition("@")
        host = _host_part(hostport)
        if _HOLE not in host:
            return _strip_text(_text_value(f"{scheme.group(1)}://{userinfo}{at}{host}/"))
        if host != _HOLE:
            return _NOTHING  # a host built at run time ("https://{sub}.example.org")
        before = text[: scheme.end() + len(userinfo) + len(at)].count(_HOLE)
        value = _bare_value(self.eval(nodes[before]))
        found = [_text_value(f"{scheme.group(1)}://{userinfo}{at}{name}/") for name in value.hosts]
        return _strip_text(_union(*found, _Val(local=True) if value.local else _NOTHING))

    def _command(self, node: ast.AST | None) -> str | None:
        """A shell command as text: rendered, its unknown parts as ``{name}`` (a name, which
        IPython-style lookups read) or ``?``. None when it isn't a string expression."""
        if node is None:
            return None
        parts = self.render(node)
        if parts is None:
            return None
        return "".join(_placeholder(part) for part in parts)

    def _word(self, node: ast.expr) -> str:
        """A command list's element as a shell word."""
        text = self._command(node)
        return text if text is not None else "?"

    def _call_value(self, node: ast.Call) -> _Val:
        func = node.func
        qual = self.qual(func)
        if qual in _SESSIONS:
            return _Val(session=self._session(node, qual))
        if qual == "get_ipython":
            return _IPYTHON
        if qual in _QUIET_QUALS:
            return _NOTHING  # os.environ.get("DATA_URL", "https://…"): an env read (default 12)
        if qual in _RENDERED:  # str(URL), shlex.split(command): their text, as rendered
            parts = self.render(node)
            if parts is not None:
                return self.template(parts)
        args = _values(node)
        if qual in _REQUESTS or qual in _PASS_QUALS:
            return _strip_text(_union(*map(self.eval, args)))
        if qual and qual.startswith("urllib.parse."):
            return _strip_text(_union(*map(self.eval, args)))
        if isinstance(func, ast.Name) and func.id in _PASS_NAMES:
            if func.id == "str" and len(node.args) == 1:
                return self.eval(node.args[0])
            return _strip_text(_union(*map(self.eval, args)))
        if not isinstance(func, ast.Attribute):
            return _NOTHING
        method, receiver = func.attr, self.eval(func.value)
        if receiver.session is not None and method in _SESSION_METHODS:
            return _NOTHING  # a response
        if method in ("format", "join"):
            parts = self.render(node)
            if parts is not None:
                return self.template(parts)
        if method == "format":
            literal = _constant(func.value)
            starts = isinstance(literal, str) and literal.lstrip().startswith("{")
            return _union(receiver, *map(self.eval, args)) if starts else _strip_text(receiver)
        if method == "join" and node.args:
            return _strip_text(self.eval(node.args[0]))
        if method in _LOOKUPS:
            return _union(receiver, *map(self.eval, node.args[1:]))
        if method in _PASS_METHODS:
            return _strip_text(receiver)
        return _NOTHING

    def _session(self, node: ast.Call, qual: str) -> _Session:
        value = self._host_args(node, bare=qual in _HOST_CALLS)
        return _Session(value.hosts, value.unreadable, value.local)

    def _host_args(self, node: ast.Call, *, bare: bool, last: bool = False) -> _Val:
        """The hosts a network call's arguments name: URLs anywhere in them, and, for a call
        that takes a host, a bare host string or a ``(host, port)`` pair (first argument, or the
        last for ``sendto(data, address)``, or the ``host=`` or ``address=`` keyword)."""
        found = [self.eval(arg) for arg in _arguments(node)]
        if bare:
            given = [k.value for k in node.keywords if k.arg in _HOST_KEYWORDS]
            for arg in [*(node.args[-1:] if last else node.args[:1]), *given]:
                if isinstance(arg, ast.Tuple) and arg.elts:
                    arg = arg.elts[0]
                found.append(_bare_value(self.eval(arg)))
        return _union(*found)

    # calls -------------------------------------------------------------------------------------
    def call(self, node: ast.Call) -> None:
        func = node.func
        qual = self.qual(func)
        where = _label(func)
        if qual in _NET_FUNCS:
            self._reach(where, self._host_args(node, bare=qual in _HOST_CALLS))
            return
        if qual in _CONNECTS:
            if node.args or any(k.arg in _HOST_KEYWORDS for k in node.keywords):
                self._reach(where, self._host_args(node, bare=True))
            return
        if qual in _SESSIONS or qual in _REQUESTS:
            return
        if qual in _SHELL_CALLS:
            self._shell_call(node, where)
            return
        if isinstance(func, ast.Attribute):
            receiver = self.eval(func.value)
            if receiver.ipython:
                self._ipython(node, func.attr, where)
                return
            if receiver.session is not None and func.attr in _SESSION_METHODS:
                bare, last = func.attr in _HOST_METHODS, func.attr == "sendto"
                found = self._host_args(node, bare=bare, last=last)
                session = receiver.session
                if not found.url and not found.local:
                    found = _Val(session.hosts, session.unreadable, session.local)
                self._reach(where, found)
                return
            if func.attr in _MUTATORS and isinstance(func.value, ast.Name):
                value = _union(*map(self.eval, _arguments(node)))
                name = func.value.id
                self.names[name] = _strip_text(_union(self.names.get(name, _NOTHING), value))
                if func.attr != "setdefault":
                    return
        if self._quiet(func, qual):
            return
        if self._passes(func, qual):
            if not (isinstance(func, ast.Attribute) and func.attr in _LOOKUPS and node.args):
                return
            args: list[ast.expr] = [node.args[0]]  # .get(URL): a request; .get(key, URL): no
        else:
            args = _values(node)
        value = _union(*map(self.eval, args))
        if value.url:
            self._reach(where, value)

    def _quiet(self, func: ast.expr, qual: str | None) -> bool:
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if qual in _QUIET_QUALS or (qual and qual.startswith(_QUIET_MODULES)):
            return True
        if name in _QUIET_NAMES or _EXCEPTION.fullmatch(name or ""):
            return True
        if qual and qual.rsplit(".", 1)[-1] in _QUIET_NAMES:  # from rich import print as rprint
            return True
        if not isinstance(func, ast.Attribute):
            return False
        if name in _STR_METHODS or name in _WRITE_METHODS or name in _QUIET_METHODS:
            return True
        owner = func.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
        if isinstance(owner, ast.Call):
            owner_name = _label(owner.func).rsplit(".", 1)[-1]
            if owner_name in ("getLogger", "get_logger"):
                return name in _LOG_METHODS
        return name in _LOG_METHODS and bool(_LOGGER.fullmatch(owner_name or ""))

    def _passes(self, func: ast.expr, qual: str | None) -> bool:
        if qual in _PASS_QUALS or (isinstance(func, ast.Name) and func.id in _PASS_NAMES):
            return True
        return isinstance(func, ast.Attribute) and (
            func.attr in _PASS_METHODS or func.attr in _LOOKUPS
        )

    def _reach(self, where: str, value: _Val) -> None:
        """A site, unless every URL or host it reaches is this machine."""
        if value.hosts or value.unreadable or not value.local:
            self.sites.append(
                Site(where, _unique(value.hosts), value.unreadable or not value.hosts)
            )

    def _shell_call(self, node: ast.Call, where: str) -> None:
        """``os.system("curl …")``, ``subprocess.run(["curl", URL])``: the command it runs, as
        nh renders it. A command nh can't render (a name it knows nothing of) counts as any
        other call."""
        command = node.args[0] if node.args else _keyword(node, "args") or _keyword(node, "cmd")
        if isinstance(command, (ast.List, ast.Tuple)):
            words = [self._word(element) for element in command.elts]
            self.shell(shlex.join(words), python=True, prefix="", where=where)
            return
        parts = self.render(command) if command is not None else None
        if parts is not None and any(isinstance(part, str) and part.strip() for part in parts):
            self.shell("".join(map(_placeholder, parts)), python=True, prefix="", where=where)
            return
        value = _union(*map(self.eval, _arguments(node)))  # ARGS = [..., URL]; run(ARGS)
        if value.url:
            self._reach(where, value)

    def _ipython(self, node: ast.Call, method: str, where: str) -> None:
        """``get_ipython().system(…)``, ``.getoutput(…)``, ``.run_line_magic(name, line)``,
        ``.run_cell_magic(name, line, body)``."""
        if method in ("system", "getoutput"):
            self._shell_call(node, where)
            return
        if method not in ("run_line_magic", "run_cell_magic") or len(node.args) < 2:
            return
        name = _constant(node.args[0])
        if not isinstance(name, str):
            return
        if method == "run_line_magic":
            line = self._command(node.args[1]) or ""
            self._line_magic(name, line, where)
            return
        line = _constant(node.args[1])
        body = self._command(node.args[2]) if len(node.args) > 2 else None
        if body is None:
            return
        program = _cell_program(f"%%{name} {line if isinstance(line, str) else ''}")
        if name in _SHELL_CELL_MAGICS or (name == "script" and program in _SHELLS):
            for text in shell_lines(body.split("\n")):
                self.shell(text, python=False, prefix="", where=where)
        elif name in _PYTHON_CELL_MAGICS or (name == "script" and program.startswith("python")):
            self.python(body, where)

    # magics and shell --------------------------------------------------------------------------
    def magic(self, number: int) -> None:
        """A ``!``/``%`` line (continuation lines joined), with the names bound so far."""
        text = self.magics[number].strip()
        assigned = _ASSIGNED.fullmatch(text)
        if assigned:
            text = assigned.group(1).strip()
        if text.startswith("!"):
            self.shell(text.lstrip("!"), python=True, prefix="!")
            return
        found = _LINE_MAGIC.fullmatch(text)
        if found:
            self._line_magic(found.group(1), found.group(2).strip(), "")

    def _line_magic(self, name: str, rest: str, where: str) -> None:
        if name in ("sx", "system"):
            self.shell(rest, python=True, prefix=f"%{name} ", where=where)
        elif name == "load":
            value = _union(*(self._word_value(w, w, python=True) for w in rest.split()))
            if value.url:
                self._reach(where or "%load", value)
        elif name in _STATEMENT_MAGICS or (name == "time" and where):
            self.python(_statement(rest), where or None)

    def shell(self, text: str, *, python: bool, prefix: str, where: str = "") -> None:
        for raws, words in shell_commands(text):
            self.command(raws, words, python=python, prefix=prefix, where=where)

    def command(
        self, raws: list[str], words: list[str], *, python: bool, prefix: str, where: str = ""
    ) -> None:
        """One shell command: a site when it downloads, clones, copies to or logs in to another
        machine, or is given a network URL."""
        raws, words = _without_env(raws, words)
        if not words:
            return
        command = words[0].rsplit("/", 1)[-1].lower()
        args, raw_args = words[1:], raws[1:]
        if command in _SAYS or command in _SHELLS:
            return  # what it says is text; `bash -c` was read inside
        label = where or prefix + command
        values = [self._word_value(r, w, python) for r, w in zip(raw_args, args, strict=False)]
        urls = _union(*values)
        if command.startswith("python") and args[:2] == ["-m", "pip"]:
            command, args, raw_args = "pip", args[2:], raw_args[2:]
        if command in _PACKAGE_TOOLS:  # installs are L009's; a download is a fetch
            verb = next((a for a in args if not a.startswith("-")), "")
            if command in ("pip", "pip3") and verb == "download":
                self._reach(where or f"{prefix}pip download", urls)
            return
        if command.startswith("python"):
            script = _python_script(args)
            if script is not None:
                self.python(script, where or f"{prefix}{command} -c")
            elif urls.url:
                self._reach(label, urls)
            return
        if command in _FETCHERS:
            if any(not arg.startswith("-") for arg in args):  # not `curl --version`
                self._reach(label, urls)
        elif command == "git":
            self._git(raw_args, args, python, where, prefix)
        elif command in ("scp", "rsync"):
            remote = _union(urls, *(self._remote(w) for w in args if not w.startswith("-")))
            if remote.url:
                self._reach(label, remote)
        elif command in _LOGINS:
            self._reach(label, _union(urls, self._login_host(args)))
        elif command in _HOST_TOOLS:
            words = [w.lstrip("@").rpartition("@")[2] for w in args if not w.startswith("-")]
            self._reach(label, _union(urls, *(_bare_value(_text_value(w)) for w in words)))
        elif command in _SYSTEM_PACKAGES:
            if next((a for a in args if not a.startswith("-")), "") in _SYSTEM_VERBS:
                self._reach(label, urls)
        elif urls.url:
            self._reach(label, urls)

    def _git(
        self, raws: list[str], words: list[str], python: bool, where: str, prefix: str
    ) -> None:
        index = 0
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in _GIT_VALUE_OPTIONS else 1
        if index >= len(words):
            return
        sub, rest, raw_rest = words[index], words[index + 1 :], raws[index + 1 :]
        label = where or f"{prefix}git {sub}"
        plain = [(r, w) for r, w in zip(raw_rest, rest, strict=False) if not w.startswith("-")]
        if sub == "submodule":
            action = plain[0][1] if plain else ""
            if action == "update":
                self._reach(label, self._repository(plain[1:], python))
            elif action == "add":
                self._clone(label, plain[1:], python)
            return
        if sub == "clone":
            self._clone(label, plain, python)
        elif sub in _GIT_FETCHES:
            remote = self._repository(plain, python)
            if not remote.url and plain and _local_path(plain[0][1]):
                return  # git fetch ../other: no network
            self._reach(label, remote)

    def _repository(self, plain: list[tuple[str, str]], python: bool) -> _Val:
        """The hosts of a git command's URL and ``[user@]host:path`` words."""
        return _union(
            *(self._word_value(r, w, python) for r, w in plain),
            *(self._remote(w) for _, w in plain),
        )

    def _clone(self, label: str, plain: list[tuple[str, str]], python: bool) -> None:
        """``git clone`` and ``git submodule add``: a site for a remote repository (a URL or
        ``[user@]host:path`` word, or one nh can't read: ``$REPO``), none for a path."""
        remote = self._repository(plain, python)
        if remote.url or remote.local:
            self._reach(label, remote)
        elif any(self._unresolved(r, w, python) for r, w in plain):
            self._reach(label, _Val(unreadable=True))

    def _word_value(self, raw: str, word: str, python: bool) -> _Val:
        """A shell word's URL: the word itself, or (``!`` lines, where IPython fills them from
        Python first) the URL-holding names in its ``{name}`` and ``$name``."""
        found = [_text_value(word)]
        if python:
            for pattern in (_BRACE_NAME, _DOLLAR_NAME):
                for match in pattern.finditer(raw):
                    found.append(self.names.get(match.group(1), _NOTHING))
        return _strip_text(_union(*found))

    def _unresolved(self, raw: str, word: str, python: bool) -> bool:
        """A word that holds a value nh can't read: a shell variable, a part of a Python command
        nh can't render (``?``), or (``!`` lines) a Python name that holds no literal string."""
        if word == "?":
            return True
        names = [m.group(1) for m in _DOLLAR_NAME.finditer(raw)]
        if not python:
            return bool(names)
        names += [m.group(1) for m in _BRACE_NAME.finditer(raw)]
        return any(self.names.get(name, _NOTHING).text is None for name in names)

    def _remote(self, word: str) -> _Val:
        """``[user@]host:path`` (scp, rsync, git), ``host::module`` (rsync)."""
        if "://" in word:
            return _NOTHING
        found = _REMOTE_WORD.match(word)
        if not found:
            return _NOTHING
        value = _bare_value(_text_value(found.group(1)))
        return value if value.url or value.local else _Val(unreadable=True)  # an ssh alias

    def _login_host(self, args: list[str]) -> _Val:
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in _SSH_VALUE_OPTIONS else 1
        if index >= len(args):
            return _NOTHING
        target = args[index].rpartition("@")[2]
        target = target.split(":", 1)[0] if not target.startswith("[") else target
        value = _bare_value(_text_value(target))
        return value if value.url or value.local else _Val(unreadable=True)


# helpers -----------------------------------------------------------------------------------------
def _magic_starts(masked: Masked, lines: list[str]) -> dict[int, str]:
    """{line: raw text} of each line magic, continuation lines joined (``%%`` lines left out)."""
    starts: dict[int, str] = {}
    for number in sorted(masked.magic_lines):
        if number > len(lines):
            continue
        text, at = lines[number - 1], number
        while text.endswith("\\") and at + 1 in masked.magic_lines and at < len(lines):
            at += 1
            text = text[:-1] + " " + lines[at - 1]
        if not text.lstrip().startswith("%%"):
            starts[number] = text
    return starts


def _without_env(raws: list[str], words: list[str]) -> tuple[list[str], list[str]]:
    """A command's words without an ``env [-i] [-u N] [K=v]`` prefix (L012 and L013)."""
    raws, words = list(raws), list(words)
    while words and words[0].rsplit("/", 1)[-1] == "env":  # env [-i] [-u N] [K=v] cmd
        raws, words = raws[1:], words[1:]
        while words and (words[0].startswith("-") or "=" in words[0]):
            skip = 2 if words[0] in _ENV_VALUE_OPTIONS else 1
            raws, words = raws[skip:], words[skip:]
    return raws, words


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _cell_program(line: str) -> str:
    """The program a ``%%script`` line runs, its options (``--bg``, ``--out <name>``) skipped."""
    args = line.split()[1:]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _SCRIPT_VALUE_OPTIONS else 1
    return args[index].rsplit("/", 1)[-1] if index < len(args) else ""


def _statement(rest: str) -> str:
    """``%timeit -n 10 stmt``: the statement, its options left out."""
    rest = rest.strip()
    while rest.startswith("-"):
        option, _, rest = rest.partition(" ")
        rest = rest.lstrip()
        if option in _MAGIC_VALUE_OPTIONS:
            rest = rest.partition(" ")[2].lstrip()
    return rest


def _python_script(args: list[str]) -> str | None:
    """``python -c '<code>'`` (``-uc`` too): the code; None for a script file or a module."""
    for index, arg in enumerate(args):
        if not arg.startswith("-") or arg.startswith("--"):
            return None
        if arg.endswith("c"):
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _local_path(word: str) -> bool:
    """A git repository given as a path, not a remote's name."""
    return word.startswith(("/", ".", "~", "file://"))


def _host_part(hostport: str) -> str:
    """``host:port`` or ``[v6]:port`` without its port."""
    if hostport.startswith("["):
        close = hostport.find("]")
        return hostport[: close + 1] if close >= 0 else hostport
    return hostport.partition(":")[0]


def _text_value(text: str) -> _Val:
    """A string's URL: its host key when it is a network URL, or this machine's; and the string
    itself."""
    is_url, host = _hosts.network_url(text)
    if is_url and host is None:
        return _Val(unreadable=True, text=text)
    if is_url and host is not None:
        if _hosts.is_loopback(host):
            return _Val(local=True, text=text)
        return _Val(hosts=(host,), text=text)
    bare = _hosts.bare_host(text)
    if bare is not None:
        return (
            _Val(local=True, text=text)
            if _hosts.is_loopback(bare)
            else _Val(bare=(bare,), text=text)
        )
    return _Val(text=text)


def _strip_text(value: _Val) -> _Val:
    """``value`` without its literal string: a container, a part or a call's result isn't it."""
    if value.text is None:
        return value
    if not (value.hosts or value.unreadable or value.local or value.bare or value.session):
        return _IPYTHON if value.ipython else _NOTHING
    return _Val(
        value.hosts, value.unreadable, value.local, value.bare, value.session, value.ipython
    )


def _bare_value(value: _Val) -> _Val:
    """A value read where a host goes: its bare host strings count as hosts."""
    if not value.bare:
        return value
    return _Val(_unique((*value.hosts, *value.bare)), value.unreadable, value.local)


def _union(*values: _Val) -> _Val:
    values = tuple(v for v in values if v is not _NOTHING)
    if not values:
        return _NOTHING
    if len(values) == 1:
        return values[0]
    session = next((v.session for v in values if v.session is not None), None)
    texts = {v.text for v in values}
    return _Val(
        _unique(h for v in values for h in v.hosts),
        any(v.unreadable for v in values),
        any(v.local for v in values),
        _unique(h for v in values for h in v.bare),
        session,
        any(v.ipython for v in values),
        texts.pop() if len(texts) == 1 else None,
    )


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _arguments(node: ast.Call) -> list[ast.expr]:
    return [*node.args, *(keyword.value for keyword in node.keywords)]


def _values(node: ast.Call) -> list[ast.expr]:
    """The arguments a call may hand back, label keywords left out (``tqdm(rows, desc=URL)``)."""
    return [*node.args, *(k.value for k in node.keywords if k.arg not in _LABEL_KEYWORDS)]


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == name), None)


def _constant(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _placeholder(part: str | ast.expr) -> str:
    if isinstance(part, str):
        return part
    return "{" + part.id + "}" if isinstance(part, ast.Name) else "?"


def _label(func: ast.AST) -> str:
    """A call's name as code, without its arguments: ``pd.read_csv``, ``requests.Session().get``,
    ``loaders[…]``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_label(func.value)}.{func.attr}"
    if isinstance(func, ast.Call):
        return f"{_label(func.func)}()"
    if isinstance(func, ast.Subscript):
        return f"{_label(func.value)}[…]"
    return "…"
