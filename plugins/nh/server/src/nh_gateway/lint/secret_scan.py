"""Code that would show a secret (design §6.7): env values reaching an output (L011), and shown
names that say they hold a secret (L014).

A taint walk over one cell: sources (``os.environ``, ``getenv``, ``dotenv_values``, ``.env``
reads, secret-showing magics) taint names; sinks (the last expression, ``print``/``display``,
logging, tracebacks, shell output) report what reaches them. It reads code only, never a value.
Stdlib plus ``_shared.secrets``.
"""

from __future__ import annotations

import ast
import builtins
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Literal

from nh_gateway._shared.secrets import is_secret_name, name_parts
from nh_gateway.lint.magics import Masked

Kind = Literal["value", "mapping", "items"]

_ENV_MAPPINGS = frozenset({"os.environ", "os.environb"})
_ENV_GETTERS = frozenset({"os.getenv", "os.getenvb"})
_DOTENV_VALUES = frozenset({"dotenv.dotenv_values", "dotenv.main.dotenv_values"})
_DOTENV_GET_KEY = frozenset({"dotenv.get_key", "dotenv.main.get_key"})
_DOTENV_LOAD = frozenset({"dotenv.load_dotenv", "dotenv.main.load_dotenv"})
_EXPANDVARS = frozenset({"os.path.expandvars", "posixpath.expandvars"})
_SHELL_RUNS = frozenset(
    {"os.system", "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.Popen"}
)
_SHELL_OUTPUTS = frozenset(
    {"subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput", "os.popen"}
)
# What get_ipython() and logging.getLogger() return, to the scan (module-like names).
_IPYTHON = "<ipython>"
_LOGGER_OBJECT = "<logger>"
_LOGGER_MAKERS = frozenset({"logging.getLogger", "structlog.get_logger", "structlog.getLogger"})
_LOGGERS = frozenset({"logging", _LOGGER_OBJECT, "loguru.logger"})
_OPENS = frozenset({"open", "io.open", "codecs.open"})
_PATHS = frozenset(
    {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath"}
    | {"os.path.join", "os.path.expanduser", "os.path.abspath", "os.path.realpath"}
    | {"os.path.normpath"}
)
# Path methods that return the same file: Path("~/.env").expanduser() is still `.env`.
_PATH_METHODS = frozenset({"expanduser", "resolve", "absolute", "as_posix"})
# What a `.env` path or open file is to the scan (a module-like name, never a taint itself).
_ENV_PATH = "<dotenv-path>"
_ENV_HANDLE = "<dotenv-file>"
# Attributes that hold a fact about what they belong to, or its names, never a value:
# `df.shape`, `env.index` (a Series of env values is indexed by their names), `result.returncode`.
_FACT_ATTRS = frozenset(
    {"shape", "size", "ndim", "dtype", "dtypes", "empty", "columns", "index", "name", "names"}
    | {"nbytes", "returncode", "args", "pid"}
)
# Names that mean the real thing when the cell doesn't import them (an earlier cell did).
_DEFAULT_NAMES = {
    "os": "os",
    "environ": "os.environ",
    "getenv": "os.getenv",
    "dotenv": "dotenv",
    "dotenv_values": "dotenv.dotenv_values",
    "load_dotenv": "dotenv.load_dotenv",
    "sys": "sys",
    "subprocess": "subprocess",
    "warnings": "warnings",
    "logging": "logging",
    "pathlib": "pathlib",
    "Path": "pathlib.Path",
    "open": "open",
    "get_ipython": "get_ipython",
}
_BUILTINS = frozenset(dir(builtins))
_PRINTS = frozenset({"print", "display", "pprint", "pp"})
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "fatal", "log"}
)
_LOGGER = re.compile(r"_*(?:\w+_)?(?:log|logger|logging)", re.I)
# Keyword arguments a sink shows: print(object=…) is pprint's, logging's msg=, warn's message=.
_SHOWN_KEYWORDS = frozenset({"sep", "end", "object", "msg", "message"})
# Calls that show a fact about their argument, never its text (also as map's function).
_CLEAN_CALLS = frozenset({"len", "bool", "hash", "id", "type", "callable"})
_STRINGIFY = frozenset({"str", "repr", "ascii", "bytes", "bytearray"})
_NUMBERS = frozenset({"int", "float", "complex", "abs", "round"})
_ITERATE = frozenset(
    {"list", "tuple", "set", "frozenset", "sorted", "reversed", "iter", "min", "max"}
)
_TEMPLATES = frozenset({"format_map", "substitute", "safe_substitute"})
# The text of whatever they are called on: os.environ.__repr__() is environ({…}).
_TEXT_DUNDERS = frozenset({"__repr__", "__str__", "__format__"})
# Calls that show what they wrap: serialisers, frames, IPython display objects.
_WRAPPERS = frozenset(
    {"dumps", "dump", "safe_dump", "pformat", "tabulate", "DataFrame", "Series", "from_dict"}
    | {"from_records", "Markdown", "HTML", "Latex", "Pretty", "JSON", "Code"}
)
# Types whose `d[k] = value` makes a dict of values under the cell's own keys.
_DICT_TYPES = frozenset({"dict", "defaultdict", "OrderedDict"})
# str methods that return a bool or a number, not the text.
_STR_FACTS = frozenset(
    {
        "startswith",
        "endswith",
        "isalnum",
        "isalpha",
        "isascii",
        "isdecimal",
        "isdigit",
        "isidentifier",
        "islower",
        "isnumeric",
        "isprintable",
        "isspace",
        "istitle",
        "isupper",
        "count",
        "find",
        "rfind",
        "index",
        "rindex",
        "__len__",
        "__contains__",
    }
)
# A tokenizer's special tokens (`eos_token`, `pad_token`) are text like "</s>", and a token's place
# in a sequence (`next_token`, `stop_token`) is model output: neither is a credential.
_TOKEN_WORDS = frozenset(
    {"bos", "eos", "pad", "unk", "sep", "cls", "mask", "special", "start", "end"}
    | {"stop", "next", "last", "first", "prev", "new", "current"}
)
# Words that make `token` a credential (`hf_token`, `accessToken`), as _shared.secrets' credential
# token keys; without one (or an env var's all-caps shape) `token` is an NLP token.
_TOKEN_QUALIFIERS = frozenset(
    {"access", "refresh", "id", "auth", "api", "bearer", "session", "csrf", "xsrf", "oauth"}
    | {"jwt", "bot", "hf", "github", "gh", "gitlab", "slack", "client", "private"}
    | {"personal", "app", "service", "security"}
)
# A name that holds a fact about a secret, not the secret: `has_api_key`, `api_key_set`.
_FACT_FIRST = frozenset({"is", "has", "have", "can", "should", "use"})
_FACT_LAST = frozenset(
    {
        "set",
        "present",
        "exists",
        "found",
        "missing",
        "ok",
        "valid",
        "loaded",
        "configured",
        "available",
        "defined",
        "status",
    }
)
# A last part that names a measure, a container or a label of the secret, not the secret itself
# (`token_counts`, `token_df`, `password_hash`, `API_KEY_ENV`), on top of is_secret_name's own.
_NOT_THE_SECRET_LAST = frozenset(
    {"counts", "num", "freq", "freqs", "frequency", "usage", "limit", "limits", "budget"}
    | {"lengths", "sizes", "prob", "probs", "logprob", "logprobs", "logit", "logits"}
    | {"score", "scores", "embedding", "embeddings", "emb", "type", "types", "list", "df"}
    | {"hash", "digest", "policy", "env", "var", "vars", "names", "strength", "field"}
    | {"prompt", "pattern"}
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*=\s*([!%].*)$", re.S)
_LINE_MAGIC = re.compile(r"^%(\w+)(.*)$", re.S)
# IPython's help: `key?`, `key??`, `?key`, `??key`.
_HELP = re.compile(r"\?{0,2}([^?]+?)\?{0,2}", re.S)
# The raw-text reading of a shell line, for a line whose quotes don't close: `;`, `&&`, `||` and
# newlines split commands; stdout to a file is `> f`, `>> f`, `1> f`, `&> f`, never `2> f`.
_SEGMENTS = re.compile(r"\n|;|&&|\|\|")
_REDIRECT = re.compile(r"(?:(?:^|(?<=\s))1|&|(?<![\d&>]))>>?\s*(?!&)\S")
_REDIRECTION = re.compile(r"(?:\d*|&)(?:>>?|<)(.+)?")
_DOLLAR = re.compile(r"\$(?:\{(!?)([A-Za-z_]\w*)([^}]*)\}|([A-Za-z_]\w*))")  # not ${#X}
_BRACE = re.compile(r"(?<![$\\{])\{([^{}]+)\}")
_SETTING = re.compile(r"[A-Za-z_]\w*=.*", re.S)  # a shell NAME=value word
_SHELL_PREFIX = frozenset({"sudo", "command", "builtin", "nohup", "time", "exec"})
_SHELL_KEYWORDS = frozenset({"do", "then", "else", "if", "while", "until", "!", "{", "("})
_SHELL_SETTERS = frozenset({"export", "local", "declare", "typeset", "readonly"})
# `env`'s options that take the next word: `env -u NAME`, `env -C dir`.
_ENV_ARG_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir"})
# `%timeit`/`%prun` options that take the next word (`%prun -r` and `-q` are flags).
_TIMED_ARG_OPTIONS = {
    "timeit": frozenset({"-n", "-r", "-p"}),
    "prun": frozenset({"-l", "-s", "-T", "-D"}),
}
_GREP = frozenset({"grep", "egrep", "fgrep", "rg"})
_FILE_READERS = frozenset(
    {"cat", "head", "tail", "less", "more", "bat", "batcat", "tac", "nl", "strings", "sort"}
    | {"uniq", "xxd", "od", "tee"}
    | _GREP
)
# Commands that print what they read, changed: on `.env` they show its values too.
_FILTERS = frozenset(
    {"sed", "awk", "gawk", "mawk", "cut", "tr", "paste", "column", "rev", "base64"}
)
_ENV_FILE_SAMPLES = frozenset({"example", "sample", "template", "dist", "defaults", "tpl"})
_PROC_ENVIRON = re.compile(r"/proc/[^/\s]+/environ")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_SHELL_CELL_MAGICS = frozenset({"bash", "sh", "system", "sx", "!"})
_FILE_MAGICS = frozenset({"pycat", "less", "more", "page", "cat"})
# stdout written here still reaches the output.
_SCREENS = frozenset({"/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"})
# `sed 's/=.*//'`: keep what is before the first `=` (the name).
_SED_NAMES = re.compile(r"s(.)=\.\*\1\1g?")
_AWK_VALUES = re.compile(r"\$(?:0|[2-9]|NF)|print\s*(?:[;}]|$)")
_MUTATORS = frozenset(
    {"update", "append", "extend", "insert", "add", "appendleft", "extendleft", "setdefault"}
)


@dataclass(frozen=True)
class Taint:
    kind: Kind
    var: str | None = None  # the env var's name, when the code spells it
    whole: bool = False  # every value (os.environ, a whole .env)
    dotenv: bool = False  # from .env rather than the process environment
    holder: str | None = None  # the cell's variable that holds it
    mapping: str | None = None  # the variable holding the .env mapping it came from
    live: bool = False  # the os.environ object itself, whose views' reprs show every value
    keys: bool = False  # os.environ.keys(): shown it is every value; set operations give names
    built: bool = False  # a dict the cell built: its keys are the cell's own, not env var names
    pair: bool = False  # items whose value is itself a (name, value) pair: enumerate(….items())
    # the function's parameters and free names it holds, while the function's body is summarised
    param: frozenset[str] | None = None


@dataclass(frozen=True)
class Hit:
    sink: str  # the code that would show it
    taint: Taint
    last: bool = False  # the sink is the cell's last expression


@dataclass(frozen=True)
class Shown:
    sink: str
    expr: ast.expr  # what the sink shows
    last: bool = False


@dataclass
class Scan:
    hits: list[Hit] = field(default_factory=list)  # L011, in the order found
    shown: list[Shown] = field(default_factory=list)  # every shown expression, for L014

    @property
    def reported(self) -> set[str]:
        """The names L011 reports (variables and env vars), which L014 then leaves alone."""
        return {name for hit in self.hits for name in (hit.taint.holder, hit.taint.var) if name}


@dataclass(frozen=True)
class _Name:
    """A module, function or object the scan knows by its qualified name (``os.getenv``)."""

    qual: str


@dataclass
class _Fn:
    """What a top-level function does with its parameters and free names (design §6.7)."""

    params: list[str]  # positional, in order
    tainted: frozenset[str]  # names tainted when it was defined: its first walk reported them
    shows: dict[str, str] = field(default_factory=dict)  # parameter or free name -> sink code
    returns: set[str] = field(default_factory=set)  # parameters and free names it returns
    result: Taint | None = None  # a tainted value it returns by itself


_Val = Taint | _Name | None
_WHOLE_ENV = Taint("value", whole=True)
_WHOLE_DOTENV = Taint("value", whole=True, dotenv=True)


def scan(
    masked: Masked,
    lines: list[str],
    tree: ast.Module | None,
    names_above: set[str] | None = None,
) -> Scan:
    """What in this cell would show a secret. ``tree`` is None for a ``%%`` cell or bad syntax;
    ``names_above`` holds the names earlier cells define (None when unknown)."""
    walker = _Walker(masked, lines, names_above)
    magic = masked.cell_magic
    if magic is not None:
        first = next(i for i, line in enumerate(lines) if line.strip())
        args = lines[first].split()[1:]
        program = args[0].rsplit("/", 1)[-1] if args else ""
        if magic in _SHELL_CELL_MAGICS or (magic == "script" and program in _SHELLS):
            walker.shell_cell(lines[first + 1 :])
            return walker.result
        python = magic == "script" and program.startswith("python")
        if magic not in _PYTHON_CELL_MAGICS and not python:
            return walker.result
        tree = _parse(masked.text)
    if tree is None:
        for number in sorted(walker.magics):
            walker.magic_line(number, None)
        return walker.result
    walker.last = tree.body[-1] if tree.body else None
    walker.block(tree.body, definite=True)
    return walker.result


def secret_names(expr: ast.expr) -> Iterator[str]:
    """Shown identifiers whose name says they hold a secret (L014): ``api_key``, ``cfg.token``."""
    for node in _shown_identifiers(expr):
        name = node.id if isinstance(node, ast.Name) else node.attr
        if not is_secret_name(name):
            continue
        parts = name_parts(name)
        if parts[0] in _FACT_FIRST or parts[-1] in _FACT_LAST:
            continue
        if parts[-1] in _NOT_THE_SECRET_LAST or parts == ["pwd"]:
            continue  # pwd alone is the working directory, as the shell names it
        if "token" in parts and not _credential_token(name, parts):
            continue
        yield _dotted(node) or name


def _credential_token(name: str, parts: list[str]) -> bool:
    """Whether a name with a `token` part holds a credential: another secret word
    (`password_token`), a qualifier (`hf_token`) or an env var's shape (`HF_TOKEN`); never a
    tokenizer's special token or a token's place (`EOS_TOKEN`, `next_token`)."""
    if _TOKEN_WORDS.intersection(parts):
        return False
    if is_secret_name("_".join(part for part in parts if part != "token")):
        return True
    return bool(_TOKEN_QUALIFIERS.intersection(parts)) or name.isupper()


# Kept in step with lint.PYTHON_CELL_MAGICS (cell magics whose body is Python source).
_PYTHON_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun", "debug", "python", "python3"})


class _Walker:
    def __init__(self, masked: Masked, lines: list[str], names_above: set[str] | None) -> None:
        self.text = masked.text
        self.masked_lines = masked.text.split("\n")
        self._source = [line.encode("utf-8", "surrogatepass") for line in _lines_as_ast(self.text)]
        self.lines = lines
        self.magics = self._magic_starts(masked)
        self.taints: dict[str, Taint] = {}
        self.names: dict[str, str] = dict(_DEFAULT_NAMES)
        self.last: ast.stmt | None = None
        self.assigned: set[str] = set()  # names the cell binds, which `$name` then means
        # Python names earlier cells define (a star import's "*" names none of them)
        self.above: set[str] = set(names_above or ())
        self.env_set: set[str] = set()  # env vars the cell set to a literal
        self.dicts: set[str] = set()  # names bound to a dict the cell builds (`d = {}`)
        self.functions: dict[str, _Fn] = {}  # "<fn:…>" qual -> its summary
        self.result = Scan()
        self._hits: set[object] = set()
        # Each expression's value in the statement being walked: a sink never evaluates its
        # arguments again, so nested calls cost linear time. Cleared at every statement.
        self._memo: dict[int, tuple[ast.AST, _Val]] = {}
        self._summary: _Fn | None = None  # the top-level function whose body is walked
        self._probing = False  # walking it with its parameters and free names as taints
        self._depth = 0  # function and class bodies the walk is in
        # `%timeit stmt`: (stmt, its tree), parsed once per line so a loop's second pass sees
        # the same nodes (hits are recorded once per node).
        self._snippets: dict[int, tuple[str, ast.Module] | None] = {}

    # recording -----------------------------------------------------------------------------------
    def hit(self, key: object, sink: str, taint: Taint | None, *, last: bool = False) -> None:
        """Record once per place: a loop body may be walked twice. ``key`` is a node or a tuple."""
        if taint is None:
            return
        if self._probing:  # a summary's walk: the first walk reported every real taint
            if taint.param is not None and self._summary is not None:
                for name in sorted(taint.param):
                    self._summary.shows.setdefault(name, sink)
            return
        if key not in self._hits:  # a node (by identity) or a tuple
            self._hits.add(key)
            self.result.hits.append(Hit(sink, taint, last))

    def show(
        self,
        sink: str,
        exprs: list[ast.expr],
        taints: list[_Val],
        key: object,
        *,
        last: bool = False,
    ) -> None:
        self.shows(sink, exprs, last=last)
        self.hit(key, sink, _first(taints), last=last)  # while probing: every parameter shown

    def shows(self, sink: str, exprs: list[ast.expr], *, last: bool = False) -> None:
        """Record what a sink shows, for L014."""
        self.result.shown += [Shown(sink, expr, last) for expr in exprs]

    def segment(self, node: ast.expr | ast.stmt) -> str:
        """``ast.get_source_segment``, without splitting the whole cell on every call."""
        lines = self._source[node.lineno - 1 : node.end_lineno]
        col, end_col = node.col_offset, node.end_col_offset
        if len(lines) == 1:
            text = lines[0][col:end_col]
        else:
            text = b"".join([lines[0][col:], *lines[1:-1], lines[-1][:end_col]])
        return text.decode("utf-8", "replace")

    # statements ----------------------------------------------------------------------------------
    def block(self, stmts: list[ast.stmt], *, definite: bool) -> None:
        for stmt in stmts:
            self.stmt(stmt, definite)

    def stmt(self, node: ast.stmt, definite: bool) -> None:
        self._memo.clear()
        if node.lineno in self.magics and self.magic_line(node.lineno, definite):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self.import_(node)
        elif isinstance(node, ast.Assign):
            value = self.eval(node.value)
            for target in node.targets:
                self.bind(target, value, node.value, definite)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.bind(node.target, self.eval(node.value), node.value, definite)
        elif isinstance(node, ast.AugAssign):
            value = self.eval(node.value)
            if isinstance(value, Taint):
                self.bind(node.target, _as_value(value), None, False)
        elif isinstance(node, ast.Expr):
            value = self.eval(node.value)
            self._env_update(node.value, definite)
            if node is self.last and not self._suppressed(node):
                self.show(self.segment(node.value), [node.value], [value], node, last=True)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            items = self.eval(node.iter)
            self.loop(lambda: self.bind_element(node.target, items), node.body)
            self.block(node.orelse, definite=False)
        elif isinstance(node, ast.While):
            self.loop(lambda: self.eval(node.test), node.body)
            self.block(node.orelse, definite=False)
        elif isinstance(node, ast.If):
            self.eval(node.test)
            self.block(node.body, definite=False)
            self.block(node.orelse, definite=False)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                value = self.eval(item.context_expr)  # `with open(".env") as f`: f is the file
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, value, None, definite)
            self.block(node.body, definite=definite)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            self.block(node.body, definite=False)
            for handler in node.handlers:
                self.eval(handler.type)
                # `except E as key`: the exception, in the handler only; when no exception is
                # raised, `key` still holds what it held before the try
                name = handler.name
                before = self.taints.pop(name, None) if name else None
                self.block(handler.body, definite=False)
                if name and before is not None:
                    self.taints.setdefault(name, before)
            self.block(node.orelse, definite=False)
            self.block(node.finalbody, definite=False)
        elif isinstance(node, ast.Match):
            subject = _as_value(self.eval(node.subject))
            for case in node.cases:
                for name in _captures(case.pattern):  # `case str() as k`: k holds the subject
                    self.bind(ast.Name(name, ast.Store()), subject, None, False)
                self.eval(case.guard)
                self.block(case.body, definite=False)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.definition(node)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and self._live(target.value):
                    self.set_env(_var(_constant(target.slice)), None)
                if not isinstance(target, ast.Name):
                    self.eval(target)
                elif definite:
                    self.taints.pop(target.id, None)
        elif isinstance(node, ast.Raise):
            self.raise_(node)
        elif isinstance(node, ast.Assert):
            self.eval(node.test)
            if node.msg is not None:
                self.show(self.segment(node), [node.msg], [self.eval(node.msg)], node)
        elif isinstance(node, ast.Return):
            self.return_(self.eval(node.value))
        # nothing else runs code: Global, Pass, Break, a `type` alias (evaluated lazily)

    def _env_update(self, call: ast.expr, definite: bool) -> None:
        """``os.environ.update(MODE="dev")`` sets literals, like ``os.environ["MODE"] = "dev"``;
        any other update may replace what the cell set."""
        if not isinstance(call, ast.Call):
            return
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "update"):
            return
        if not self._live(func.value):
            return
        pairs = [(k.arg, k.value) for k in call.keywords]
        for arg in call.args:
            if not isinstance(arg, ast.Dict):
                self.env_set.clear()  # os.environ.update(dotenv_values())
                return
            pairs += [
                (_constant(key), value) for key, value in zip(arg.keys, arg.values, strict=True)
            ]
        for key, value in pairs:
            self.set_env(_var(key), value if definite else None)  # **mapping: no key

    def set_env(self, var: str | None, value: ast.expr | None) -> None:
        """The cell sets env var ``var`` to ``value`` (None: deletes or pops it, or only may set
        it). A literal reads clean after it; anything else undoes that, for every var when nh
        can't read the name (``os.environ[name] = …``)."""
        if var is None:
            self.env_set.clear()
        elif _literal(value):
            self.env_set.add(var)
        else:
            self.env_set.discard(var)

    def loop(self, head: Callable[[], object], body: list[ast.stmt]) -> None:
        """Walk a loop body, and again when that changed the taint: what the end of one pass
        taints reaches the top of the next. Two passes at most, and one when nothing changed."""
        for _ in range(2):
            before = self._state()
            head()
            self.block(body, definite=False)
            if self._state() == before:
                return

    def _state(self) -> tuple[frozenset[tuple[str, Taint]], frozenset[tuple[str, str]]]:
        return frozenset(self.taints.items()), frozenset(self.names.items())

    def definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        for child in ast.iter_child_nodes(node):  # decorators, bases, defaults, annotations
            if isinstance(child, ast.expr):
                self.eval(child)
            elif isinstance(child, (ast.arguments, ast.keyword)):
                self.eval_any(child)
        self.taints.pop(node.name, None)
        self.names.pop(node.name, None)
        if isinstance(node, ast.ClassDef):
            self.class_body(node)
            return
        params = _arg_names(node.args)
        summary = None
        if self._depth == 0:  # a top-level function: its calls are followed
            positional = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
            summary = _Fn(positional, frozenset(self.taints))
        self.body(node.body, summary, clean=[*params, *_local_names(node)])
        if summary is None:
            return
        free = _free_names(node) - set(params) - set(self.names) - _BUILTINS
        self.body(node.body, summary, probes=[*params, *sorted(free)])
        qual = f"<fn:{id(node)}>"
        self.names[node.name] = qual
        self.functions[qual] = summary

    def body(
        self,
        stmts: list[ast.stmt],
        summary: _Fn | None,
        *,
        clean: Iterable[str] = (),
        probes: Iterable[str] | None = None,
    ) -> None:
        """Walk a function body: with ``clean`` names cleared (the walk that reports), or with
        each of ``probes`` standing for itself (the walk that fills ``summary``)."""
        saved = (
            dict(self.taints),
            dict(self.names),
            self.last,
            set(self.env_set),
            self._summary,
            self._probing,
        )
        if probes is not None:
            self.taints = {n: Taint("value", holder=n, param=frozenset({n})) for n in probes}
            for name in self.taints:
                self.names.pop(name, None)
            self._probing = True
        for name in clean:
            self.taints.pop(name, None)
            self.names.pop(name, None)
        self.last = None  # a body's last expression is not shown
        self._summary = summary or self._summary
        self._depth += 1
        try:
            self.block(stmts, definite=True)
        finally:
            self._depth -= 1
            self.taints, self.names, self.last = saved[0], saved[1], saved[2]
            self.env_set, self._summary, self._probing = saved[3], saved[4], saved[5]

    def class_body(self, node: ast.ClassDef) -> None:
        """A class body runs where it sits; a name it taints taints ``Class.name`` after it."""
        saved = dict(self.taints), dict(self.names), self.last
        self.last = None
        self._depth += 1
        try:
            self.block(node.body, definite=True)
        finally:
            self._depth -= 1
        tainted = {
            name: taint
            for name, taint in self.taints.items()
            if "." not in name and saved[0].get(name) != taint
        }
        self.taints, self.names, self.last = saved
        for name, taint in tainted.items():
            self.taints[f"{node.name}.{name}"] = replace(taint, holder=f"{node.name}.{name}")

    def return_(self, value: _Val) -> None:
        summary = self._summary
        if summary is None or self._depth != 1 or not isinstance(value, Taint):
            return  # only a top-level function's own `return` is its result
        if value.param is not None:
            summary.returns |= value.param
        elif not self._probing and summary.result is None:
            summary.result = value

    def raise_(self, node: ast.Raise) -> None:
        exc = node.exc
        taint = self.eval(exc)
        self.eval(node.cause)
        if isinstance(exc, ast.Call):
            shown = [*exc.args, *(k.value for k in exc.keywords)]
            self.show(self.segment(node), shown, [taint, *map(self.eval, shown)], node)
        elif exc is not None:
            self.show(self.segment(node), [exc], [taint], node)

    def import_(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:  # `from m import *` binds a "*" nothing reads
            if isinstance(node, ast.Import):
                bound = alias.asname or alias.name.split(".")[0]
                qual = alias.name if alias.asname else bound
            else:
                bound = alias.asname or alias.name
                qual = "" if node.level else f"{node.module}.{alias.name}"
            self.taints.pop(bound, None)
            if qual:
                self.names[bound] = qual
            else:
                self.names.pop(bound, None)

    def bind(self, target: ast.expr, value: _Val, source: ast.expr | None, definite: bool) -> None:
        if name := _dotted(target):  # a name or a dotted attribute
            self.assigned.add(name)
            if _builds_dict(source):  # `d = {}`, `self.d = {}`
                self.dicts.add(name)
            elif definite:
                self.dicts.discard(name)
            if isinstance(value, Taint):
                self.taints[name] = replace(value, holder=name)
                self.names.pop(name, None)
            elif isinstance(value, _Name) and isinstance(target, ast.Name):
                self.names[name] = value.qual
                self.taints.pop(name, None)
            elif definite:
                self.taints.pop(name, None)
                self.names.pop(name, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pairs = isinstance(source, (ast.Tuple, ast.List)) and len(source.elts) == len(
                target.elts
            )
            if pairs and not any(isinstance(e, ast.Starred) for e in target.elts):
                assert isinstance(source, (ast.Tuple, ast.List))
                for element, part in zip(target.elts, source.elts, strict=True):
                    self.bind(element, self.eval(part), part, definite)
            else:
                for element in target.elts:
                    self.bind(element, _element(value), None, definite)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, value, None, definite)
        elif isinstance(target, ast.Subscript):
            self.eval(target.slice)
            if self._live(target.value):  # os.environ["MODE"] = "dev": the env, not a dict
                self.set_env(_var(_constant(target.slice)), source if definite else None)
                return
            base = _dotted(target.value)
            if base and isinstance(value, Taint):
                self._store(base, value)
            else:
                self.eval(target.value)
        elif isinstance(target, ast.Attribute):
            self.eval(target.value)

    def _store(self, base: str, value: Taint) -> None:
        """``d[k] = value``: a dict of values under the cell's keys, else a container of values."""
        current = self.taints.get(base)
        if current is not None and current.kind == "mapping":
            return  # it holds env values under their names already
        kind: Kind = "mapping" if base in self.dicts else "value"
        built = kind == "mapping"
        self.taints[base] = replace(
            value, kind=kind, built=built, holder=base, live=False, keys=False
        )

    def bind_element(self, target: ast.expr, iterable: _Val) -> None:
        """Bind a ``for`` or comprehension target to one element of what it iterates: a
        mapping's element is a name (clean), an ``items`` pair a clean name and a value, an
        open ``.env`` file's a line."""
        if _is(iterable, _ENV_HANDLE):
            iterable = Taint("value", dotenv=True)
        if not isinstance(iterable, Taint) or iterable.kind == "mapping":
            self.bind(target, None, None, True)
        elif iterable.kind == "items" and _pair(target):
            assert isinstance(target, (ast.Tuple, ast.List))
            self.bind(target.elts[0], None, None, True)  # the name, or enumerate's index
            if iterable.pair:  # enumerate(os.environ.items()): (i, (name, value))
                self.bind_element(target.elts[1], replace(iterable, pair=False))
            else:
                self.bind(target.elts[1], _part(iterable), None, True)
        else:
            self.bind(target, _part(iterable), None, True)

    def _live(self, node: ast.expr) -> bool:
        """Whether ``node`` is the os.environ object itself (``os.environ``, ``env = os.environ``)."""
        value = self.eval(node)
        return isinstance(value, Taint) and value.live

    # expressions ---------------------------------------------------------------------------------
    def eval_any(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.eval(child)  # an `arg`, a `keyword`: no `_e_` method, so its children

    def eval(self, node: ast.AST | None) -> _Val:
        if node is None:
            return None
        seen = self._memo.get(id(node))
        if seen is not None and seen[0] is node:
            return seen[1]
        method = getattr(self, f"_e_{type(node).__name__}", None)
        if method is not None:
            value = method(node)
        else:
            self.eval_any(node)
            value = None
        self._memo[id(node)] = (node, value)
        return value

    def _e_Constant(self, node: ast.Constant) -> _Val:
        """A literal `.env` file name is a `.env` path: ``ENV_FILE = "../.env"``."""
        return _Name(_ENV_PATH) if _env_path(node) else None

    def _e_Name(self, node: ast.Name) -> _Val:
        if node.id in self.taints:
            return self.taints[node.id]
        return self._named(self.names.get(node.id))

    def _named(self, qual: str | None) -> _Val:
        if qual is None:
            return None
        if qual in _ENV_MAPPINGS:
            return Taint("mapping", whole=True, live=True)
        return _Name(qual)

    def _e_Attribute(self, node: ast.Attribute) -> _Val:
        dotted = _dotted(node)
        if dotted and dotted in self.taints:
            return self.taints[dotted]
        base = self.eval(node.value)
        if isinstance(base, Taint):
            if base.live:  # os.environ.get: its repr holds environ({…})
                return _WHOLE_ENV
            if base.kind == "mapping" or node.attr in _FACT_ATTRS:
                return None  # a dict's bound method; df.shape, env.index (the names)
            return replace(base, kind="value", keys=False, pair=False)  # df.loc, r.stdout, out.s
        return self._named(f"{base.qual}.{node.attr}") if isinstance(base, _Name) else None

    def _e_Subscript(self, node: ast.Subscript) -> _Val:
        base, key = self.eval(node.value), node.slice
        self.eval(key)
        if not isinstance(base, Taint):
            return None
        if base.kind == "mapping":
            return self._item(base, _constant(key))
        if _name_part(node, base):
            return None  # line.split("=")[0]: the name on a `.env` line
        return replace(base, kind="value", live=False, keys=False, pair=False)

    def _e_Starred(self, node: ast.Starred) -> _Val:
        value = self.eval(node.value)  # print(*values) shows them all; print(*os.environ) names
        return None if isinstance(value, Taint) and value.kind == "mapping" else _as_value(value)

    def _e_JoinedStr(self, node: ast.JoinedStr) -> _Val:
        return _first(map(self.eval, node.values))

    def _e_FormattedValue(self, node: ast.FormattedValue) -> _Val:
        self.eval(node.format_spec)
        return _as_value(self.eval(node.value))

    def _e_BinOp(self, node: ast.BinOp) -> _Val:
        left, right = self.eval(node.left), self.eval(node.right)
        views = [value for value in (left, right) if isinstance(value, Taint) and value.keys]
        if views and isinstance(node.op, (ast.Sub, ast.BitAnd, ast.BitXor, ast.BitOr)):
            return None  # required - os.environ.keys(): a set of names
        taint = _first([left, right])
        if isinstance(taint, Taint) and taint.kind == "mapping" and isinstance(node.op, ast.BitOr):
            return replace(taint, live=False)  # os.environ | {…} is a dict
        if isinstance(node.op, ast.Div) and _is(right, _ENV_PATH):
            return right  # ROOT / ".env"
        return _as_value(taint)

    def _e_BoolOp(self, node: ast.BoolOp) -> _Val:
        values = [_as_value(self.eval(v)) for v in node.values]
        # `a and b` returns `a` only when it is empty: what shows is the last operand.
        return values[-1] if isinstance(node.op, ast.And) else _first(values)

    def _e_IfExp(self, node: ast.IfExp) -> _Val:
        self.eval(node.test)
        return _first([_as_value(self.eval(node.body)), _as_value(self.eval(node.orelse))])

    def _e_UnaryOp(self, node: ast.UnaryOp) -> _Val:
        value = self.eval(node.operand)
        return None if isinstance(node.op, ast.Not) else _as_value(value)

    def _e_Compare(self, node: ast.Compare) -> _Val:
        self.eval(node.left)
        for comparator in node.comparators:
            self.eval(comparator)
        return None

    def _e_Tuple(self, node: ast.Tuple | ast.List | ast.Set) -> _Val:
        return _as_value(_first(map(self.eval, node.elts)))

    _e_List = _e_Tuple
    _e_Set = _e_Tuple

    def _e_Dict(self, node: ast.Dict) -> _Val:
        keys: list[_Val] = []
        values: list[_Val] = []
        spread: Taint | None = None
        for key, value in zip(node.keys, node.values, strict=True):
            keys.append(_as_value(self.eval(key)))  # None for `**`
            taint = self.eval(value)
            if key is None and isinstance(taint, Taint) and taint.kind == "mapping":
                spread = spread or replace(taint, holder=None, live=False, keys=False)  # {**env}
            else:
                values.append(_as_value(taint))
        found = _first(values)
        if (key_taint := _first(keys)) is not None:
            return key_taint  # a key that holds a value shows it
        if spread is not None:
            return spread
        if found is None:
            return None
        return replace(found, kind="mapping", built=True, holder=None, live=False, keys=False)

    def _e_NamedExpr(self, node: ast.NamedExpr) -> _Val:
        value = self.eval(node.value)
        self.bind(node.target, value, node.value, False)
        return value

    def _e_Lambda(self, node: ast.Lambda) -> _Val:
        self.eval_any(node.args)
        saved = dict(self.taints), dict(self.names)
        for arg in _arg_names(node.args):
            self.taints.pop(arg, None)
            self.names.pop(arg, None)
        self.eval(node.body)
        self.taints, self.names = saved
        return None

    def _comprehension(self, node: ast.expr, generators: list[ast.comprehension]) -> None:
        for generator in generators:
            self.bind_element(generator.target, self.eval(generator.iter))
            for condition in generator.ifs:
                self.eval(condition)

    def _e_ListComp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp) -> _Val:
        saved = dict(self.taints), dict(self.names)
        self._comprehension(node, node.generators)
        value = _as_value(self.eval(node.elt))
        self.taints, self.names = saved
        return value

    _e_SetComp = _e_ListComp
    _e_GeneratorExp = _e_ListComp

    def _e_DictComp(self, node: ast.DictComp) -> _Val:
        saved = dict(self.taints), dict(self.names)
        self._comprehension(node, node.generators)
        key, value = _as_value(self.eval(node.key)), _as_value(self.eval(node.value))
        self.taints, self.names = saved
        if key is not None:
            return key
        return None if value is None else replace(value, kind="mapping", holder=None)

    def _e_Call(self, node: ast.Call) -> _Val:
        func = node.func
        receiver: _Val = None
        if isinstance(func, ast.Attribute):
            receiver = self.eval(func.value)
            qualified = isinstance(receiver, _Name)
            callee = self._named(f"{receiver.qual}.{func.attr}") if qualified else None
        else:
            callee = self.eval(func)
        args = [self.eval(arg) for arg in node.args]
        keywords = [self.eval(k.value) for k in node.keywords]
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        qual = callee.qual if isinstance(callee, _Name) else None
        self._sink(node, name, qual, receiver)
        if isinstance(func, ast.Attribute) and name in _MUTATORS:
            self._mutate(func.value, name, _first([*args, *keywords]))
        if qual in self.functions:
            return self._called(node, self.functions[qual], args, keywords)
        if isinstance(func, ast.Attribute) and isinstance(receiver, Taint):
            return self._method(receiver, func.attr, node)
        return self._call_result(node, name, qual, args, keywords)

    def _called(self, node: ast.Call, fn: _Fn, args: list[_Val], keywords: list[_Val]) -> _Val:
        """A call to a top-level function: what reaches its sinks shows here, and what reaches
        its ``return`` is the result."""
        bound: dict[str, tuple[ast.expr, _Val]] = {}
        for index, (arg, taint) in enumerate(zip(node.args, args, strict=True)):
            if isinstance(arg, ast.Starred) or index >= len(fn.params):
                break
            bound[fn.params[index]] = arg, taint
        for keyword, taint in zip(node.keywords, keywords, strict=True):
            if keyword.arg:
                bound[keyword.arg] = keyword.value, taint

        def taint_of(name: str) -> _Val:
            if name in bound:
                return bound[name][1]
            return None if name in fn.tainted else self.taints.get(name)

        sink = self.segment(node)
        for name in fn.shows:
            if name in bound:
                self.shows(sink, [bound[name][0]])
            self.hit((id(node), name), sink, _first([taint_of(name)]))
        return _first([*(taint_of(name) for name in sorted(fn.returns)), fn.result])

    def _mutate(self, base: ast.expr, method: str, taint: Taint | None) -> None:
        """``d.update(os.environ)``, ``found.append(key)``: the container now holds it."""
        name = _dotted(base)
        if taint is None or name is None or self._live(base):
            return
        if method == "update" and taint.kind == "mapping":
            self.taints[name] = replace(taint, holder=name, live=False, keys=False)
        else:
            self._store(name, taint)

    def _item(self, mapping: Taint, key: object) -> Taint | None:
        """``os.environ["K"]``, ``config.get("K")``: one value of a mapping."""
        if mapping.built:  # {"key": os.getenv("K")}["key"]: what the cell put there
            return replace(mapping, kind="value", holder=None, built=False)
        var = _var(key)
        if mapping.live and var in self.env_set:
            return None  # the cell set it: os.environ["MODE"] = "dev"
        return Taint("value", var=var, dotenv=mapping.dotenv, mapping=mapping.holder)

    def _method(self, receiver: Taint, attr: str, node: ast.Call) -> Taint | None:
        if attr in _TEXT_DUNDERS:  # os.environ.__repr__(): the text of all it holds
            return replace(receiver, kind="value", live=False, keys=False, pair=False)
        if receiver.kind == "mapping":
            if attr in ("get", "pop", "setdefault", "__getitem__"):
                key = _constant(_arg(node, 0, "key"))
                taint = self._item(receiver, key)
                if attr == "pop" and receiver.live:
                    self.set_env(_var(key), None)
                return taint
            source = receiver.holder or receiver.mapping
            dict_ = replace(receiver, holder=None, mapping=source, live=False, keys=False)
            if attr == "copy":
                return dict_
            if attr == "values":
                return replace(dict_, kind="value", built=False)
            if attr == "items":
                return replace(dict_, kind="items", built=False)
            if attr == "keys" and receiver.live:  # KeysView(environ({…})): every value
                return replace(dict_, keys=True)
            return None  # a dict's keys(), update(), ...
        if receiver.kind == "items" or attr in _STR_FACTS:
            return None
        if attr == "items":  # a Series' (label, value) pairs
            return replace(receiver, kind="items", pair=False)
        return replace(receiver, kind="value")  # key.strip(), template.format(...), sep.join(...)

    def _call_result(
        self, node: ast.Call, name: str | None, qual: str | None, args: list[_Val], kw: list[_Val]
    ) -> _Val:
        arg0 = args[0] if args else None
        first = _first([*args, *kw])
        if qual in _ENV_GETTERS:
            var = _var(_constant(_arg(node, 0, "key")))
            return None if var in self.env_set else Taint("value", var=var)
        if qual in _DOTENV_VALUES:
            path = _constant(_arg(node, 0, "dotenv_path"))
            return None if _env_sample(path) else Taint("mapping", whole=True, dotenv=True)
        if qual in _DOTENV_GET_KEY:
            return Taint("value", var=_var(_constant(_arg(node, 1, "key_to_get"))), dotenv=True)
        if qual in _DOTENV_LOAD:
            self.env_set.clear()  # load_dotenv(override=True) may replace what the cell set
            return None
        if qual in _EXPANDVARS:
            text = _constant(_arg(node, 0, "path"))
            found = _DOLLAR.search(text) if isinstance(text, str) else None
            return Taint("value", var=found.group(2) or found.group(4)) if found else None
        if qual == "get_ipython":
            return _Name(_IPYTHON)
        if qual in _LOGGER_MAKERS:
            return _Name(_LOGGER_OBJECT)
        if qual in _SHELL_OUTPUTS or qual == f"{_IPYTHON}.getoutput":
            command = _command(_arg(node, 0, "args"))
            python = qual == f"{_IPYTHON}.getoutput"
            return _first(self.shell(command, python=python)) if command else None
        if qual in _SHELL_RUNS and _captured(node):  # its result holds what the command printed
            command = _command(_arg(node, 0, "args"))
            return _first(self.shell(command, python=False)) if command else None
        if qual and (qual in _OPENS or qual in _PATHS or qual.startswith("<dotenv-")):
            return _file(node, qual, args)
        if name == "joinpath" and any(_is(arg, _ENV_PATH) for arg in args):
            return _Name(_ENV_PATH)  # Path.home().joinpath(".env")
        if name in ("format", "join"):  # "{}".format(key), format(key, ""), ", ".join(values)
            return _as_value(first if name == "format" else _element(arg0))
        if name in _TEMPLATES and isinstance(first, Taint):  # "{K}".format_map(os.environ)
            return replace(first, kind="value", whole=False, holder=None, live=False, keys=False)
        if name in _STRINGIFY or name in _NUMBERS or name in _WRAPPERS:
            return _as_value(first if name in _WRAPPERS else arg0)
        if name == "enumerate":  # (index, element): like items, the index is clean
            element = _iterated(arg0)
            if element is None:
                return None
            return replace(element, kind="items", pair=element.kind == "items")
        if name in _ITERATE:
            return _iterated(arg0)
        if name in ("filter", "map", "zip"):
            return _lazy(node, name, args)
        if name == "next":
            return _as_value(_element(arg0))
        if name == "dict":
            if isinstance(first, Taint) and first.kind in ("mapping", "items"):
                return replace(first, kind="mapping", holder=None, live=False, keys=False)
            if isinstance(first, Taint) and first.dotenv and not first.var:  # lines of .env
                return replace(first, kind="mapping", whole=True, holder=None)
            return _as_value(first)
        return None

    def _sink(self, node: ast.Call, name: str | None, qual: str | None, receiver: _Val) -> None:
        func = node.func
        owner = func.value if isinstance(func, ast.Attribute) else None
        owner_qual = receiver.qual if isinstance(receiver, _Name) else None
        shown: list[ast.expr] | None = None
        if name in _PRINTS or (qual and qual.rsplit(".", 1)[-1] in _PRINTS):  # print as rprint
            target = _keyword(node, "file")
            to_screen = target is None or self._qual(target) in ("sys.stdout", "sys.stderr")
            shown = list(node.args) if to_screen else None
        elif name == "write" and (
            owner_qual in ("sys.stdout", "sys.stderr")
            or (owner_qual or "").rsplit(".", 1)[-1] == "tqdm"
            or getattr(owner, "id", None) == "tqdm"
        ):
            shown = list(node.args)
        elif self._logs(owner, name, qual):  # log(level, msg): the level isn't shown
            shown = list(node.args[1:] if name == "log" or qual == "logging.log" else node.args)
        elif name == "log" and _last_name(owner) == "console":  # rich's console.log(*objects)
            shown = list(node.args)
        elif qual == "warnings.warn":
            shown = list(node.args[:1])
        elif qual in _SHELL_RUNS or qual == f"{_IPYTHON}.system":
            if not _captured(node):
                command = _command(_arg(node, 0, "args"))
                sink = self.segment(node)
                python = qual == f"{_IPYTHON}.system"  # get_ipython().system fills $name
                shows = self.shell(command, sink, python=python) if command else []
                for index, taint in enumerate(shows):
                    self.hit((id(node), index), sink, taint)
            return
        if shown is None:
            return
        shown += [k.value for k in node.keywords if k.arg in _SHOWN_KEYWORDS]
        self.show(self.segment(node), shown, [self.eval(a) for a in shown], node)

    def _qual(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):  # a tainted name has no qual: bind drops it
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._qual(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _logs(self, owner: ast.expr | None, name: str | None, qual: str | None) -> bool:
        """A logging call: on a logger (``logging``, ``getLogger(…)``, a name bound to one, a
        receiver named like one), or a function imported from ``logging``."""
        if owner is None:  # from logging import warning
            module, _, function = (qual or "").rpartition(".")
            return module == "logging" and function in _LOG_METHODS
        if name not in _LOG_METHODS:
            return False
        if self._qual(owner) in _LOGGERS:
            return True
        if isinstance(owner, ast.Call):
            made = getattr(owner.func, "attr", getattr(owner.func, "id", None))
            return made in ("getLogger", "get_logger")
        return bool(_LOGGER.fullmatch(_last_name(owner)))

    def _suppressed(self, stmt: ast.stmt) -> bool:
        """A trailing ``;`` stops Jupyter from showing the last expression."""
        line = self.masked_lines[(stmt.end_lineno or stmt.lineno) - 1]
        after = line.encode("utf-8", "surrogatepass")[stmt.end_col_offset or 0 :]
        return after.decode("utf-8", "ignore").lstrip().startswith(";")

    # magics and shell ----------------------------------------------------------------------------
    def _magic_starts(self, masked: Masked) -> dict[int, str]:
        """{line: raw text} of each magic, continuation lines joined (a statement starts only on
        the first). A ``%%timeit`` or ``%%prun`` line reads as its line magic: the statement
        after its options runs too (timeit's setup); other ``%%`` lines are left out."""
        starts: dict[int, str] = {}
        for number in sorted(masked.magic_lines):
            text, at = self.lines[number - 1], number
            while text.endswith("\\") and at + 1 in masked.magic_lines:
                at += 1
                text = text[:-1] + " " + self.lines[at - 1]
            if text.lstrip().startswith(("%%timeit", "%%prun")):
                starts[number] = text.lstrip()[1:]
            elif not text.lstrip().startswith("%%"):
                starts[number] = text
        return starts

    def magic_line(self, number: int, definite: bool | None) -> bool:
        """Scan the magic on this line; False when it is Python after all (``%time stmt``)."""
        text = self.magics[number]
        stripped = text.strip()
        assigned = _ASSIGN.match(text)
        if assigned and self.masked_lines[number - 1].strip().endswith("= None"):
            taint = _first(self.magic(assigned.group(2).strip(), None, bool(definite)))
            for target in assigned.group(1).split(","):
                name = target.strip()
                self.bind(ast.Name(name, ast.Store()), taint, None, bool(definite))
            return True
        if not stripped.startswith(("!", "%")):  # the masker made it `pass`
            asked = _HELP.fullmatch(stripped)  # `/f x` and the like parse as no expression
            if asked:  # `key?`: IPython's help shows its "String form"
                self.hit(("help", number), stripped, self._help(asked.group(1)))
            return True
        if stripped.startswith("%time ") and self.masked_lines[number - 1].strip() != "pass":
            return False  # the masker kept `%time stmt` as Python: walked as code
        timed = _LINE_MAGIC.match(stripped)
        if timed and timed.group(1) in _TIMED_ARG_OPTIONS:
            self.timed(number, timed.group(1), timed.group(2), bool(definite))
            return True
        for index, taint in enumerate(self.magic(stripped, stripped, bool(definite))):
            self.hit(("magic", number, index), stripped, taint)
        return True

    def _help(self, text: str) -> Taint | None:
        """What IPython's help on ``text`` shows: the value of a tainted object."""
        return _as_value(self.eval(_parse_expr(text)))

    def timed(self, number: int, magic: str, rest: str, definite: bool) -> None:
        """``%timeit -n 1 stmt``, ``%prun -s cumulative stmt``: the statement runs (and prints)
        where it sits; its value is never the cell's shown last line."""
        if number not in self._snippets:
            statement = _timed_statement(rest, _TIMED_ARG_OPTIONS[magic])
            tree = _parse(statement) if statement else None
            self._snippets[number] = (statement, tree) if tree else None
        snippet = self._snippets[number]
        if snippet is None:
            return
        statement, tree = snippet
        # its nodes' positions are the statement's own: no line of the cell, and no magic
        saved = self._source, self.magics
        self._source = [line.encode("utf-8", "surrogatepass") for line in _lines_as_ast(statement)]
        self.magics = {}
        try:
            self.block(tree.body, definite=definite)
        finally:
            self._source, self.magics = saved

    def magic(self, text: str, sink: str | None, definite: bool) -> list[Taint]:
        """What a ``!``/``%`` line would show. ``sink`` is the line when it is shown, not
        assigned (``x = !cmd``)."""
        if text.startswith("!"):
            return self.shell(text.lstrip("!"), sink)
        found = _LINE_MAGIC.match(text)
        if not found:
            return []
        name, rest = found.group(1), found.group(2).strip()
        if name in ("env", "set_env"):
            return self.env_magic(name, rest, definite)
        if name in ("sx", "system"):
            return self.shell(rest, sink)
        if name in ("pinfo", "pinfo2"):
            taint = self._help(rest)
            return [taint] if taint is not None else []
        if name == "time":  # x = %time expr: the value of expr (a shown `%time` is Python)
            value = self.eval(_parse_expr(rest))
            return [value] if isinstance(value, Taint) else []
        if name == "whos":  # every variable with its value
            return [t for t in [_first(self.taints.values())] if t is not None]
        if name in _FILE_MAGICS and any(_env_file(word) for word in rest.split()):
            return [_WHOLE_DOTENV]
        return []

    def env_magic(self, name: str, rest: str, definite: bool) -> list[Taint]:
        """``%env`` shows every env var, ``%env NAME`` one; ``%env NAME=value``,
        ``%env NAME value`` and ``%set_env NAME value`` set one (a literal reads clean later)."""
        words = rest.split()
        if name == "env" and not words:
            return [Taint("mapping", whole=True)]
        if name == "env" and len(words) == 1 and "=" not in words[0]:
            var = _var(words[0])
            return [] if var in self.env_set else [Taint("value", var=var)]
        if not words:
            return []
        setting, _, value = words[0].partition("=")
        value = value if "=" in words[0] else " ".join(words[1:])
        var = _var(setting)
        if var and definite and "$" not in value and "{" not in value:
            self.env_set.add(var)
        elif var:
            self.env_set.discard(var)  # %env K=$key: IPython fills it from Python
        return []

    def shell_cell(self, body: list[str]) -> None:
        """A ``%%bash`` cell: its lines share shell locals; IPython fills no ``$name``. A line
        ending in a backslash, or with a quote still open, goes on to the next."""
        text, local = "", _Locals()
        for line_number, line in enumerate(body):
            if not text:
                text = line
            elif text.endswith("\\"):
                text = text[:-1] + " " + line
            else:  # a quote still open
                text = f"{text}\n{line}"
            more = line_number + 1 < len(body)
            if more and (text.endswith("\\") or _lex(text) is None):
                continue
            for index, taint in enumerate(self.shell(text, text.strip(), local=local)):
                self.hit(("shell", line_number, index), text.strip(), taint)
            text = ""

    def shell(
        self,
        text: str,
        sink: str | None = None,
        *,
        python: bool = True,
        local: _Locals | None = None,
    ) -> list[Taint]:
        """What a shell command line would print, pipeline by pipeline. ``sink`` is the code
        that shows the output (None when it is captured); ``python``: IPython fills ``$name``
        and ``{expr}`` from Python first (``!``, ``%sx``), not a shell cell or ``os.system``;
        ``local``: the shell variables set so far (one line's own when None)."""
        local = _Locals(python=python) if local is None else local
        lexed = _lex(text)
        found: list[Taint] = []
        for stages in lexed if lexed is not None else _lex_raw(text):
            shown = not stages[-1].to_file and not any(_quiet(s) for s in stages[1:])
            # a pipeline's stages run in subshells: they set no local
            own = local if len(stages) == 1 else local.copy()
            carry: Taint | None = None
            for stage in stages:
                taint = self._command(stage, sink if shown else None, own)
                carry = None if _quiet(stage) or stage.to_file else taint or carry
            if carry is not None:
                found.append(carry)
        return found

    def _command(self, stage: _Stage, sink: str | None, local: _Locals) -> Taint | None:
        raws = stage.words
        if all(_SETTING.fullmatch(raw) for raw in raws):  # X=1 Y=$Z (or no word at all)
            for raw in raws:
                name, value = raw.split("=", 1)
                local.names[name] = self._expanded(value, local)
            return None
        raws, words = _command_words(stage)
        if not words:
            return None
        command, args, raw_args = words[0].strip("(){};").rsplit("/", 1)[-1], words[1:], raws[1:]
        flags = [a for a in args if a.startswith("-") and not a.startswith("--")]
        plain = [a for a in args if not a.startswith("-")]
        if command in _SHELL_SETTERS or command == "read" or (command == "for" and plain):
            named = [raw for raw in raw_args if not _unquote(raw).startswith("-")]
            self._bind_locals(command, named, local)
        if command in _SHELLS:  # bash -c 'script'
            script = _shell_script(args)
            return None if script is None else _first(self.shell(script, sink, local=local.copy()))
        if command == "env":
            return _env(args)
        if command == "printenv":
            if plain and plain[0] in local.names:  # export X=1; printenv X
                return local.names[plain[0]]
            return Taint("value", var=_var(plain[0])) if plain else _WHOLE_ENV
        if command == "set":
            return None if args else _WHOLE_ENV
        if command == "export":
            return _WHOLE_ENV if all(a == "-p" for a in args) else None
        if command in ("declare", "typeset"):
            shows = any(set(f[1:]) & {"p", "x"} for f in flags)
            if shows and not any("=" in a for a in plain):
                return Taint("value", var=_var(plain[0])) if plain else _WHOLE_ENV
            return None
        if command in ("echo", "printf", "print"):
            return self._echoed(raw_args, sink, local)
        if command not in _FILE_READERS and command not in _FILTERS:
            return None
        # a quiet grep or a names-only filter shows nothing: shell() drops it (_quiet)
        return next(filter(None, map(_secret_file, [*plain, *stage.inputs])), None)

    def _bind_locals(self, command: str, plain: list[str], local: _Locals) -> None:
        """``export X=1``, ``read X``, ``for X in …``: shell variables the line sets."""
        if command == "for":
            names = [_unquote(word) for word in plain]
            words = plain[2:] if len(plain) > 1 and names[1] == "in" else []
            local.names[names[0]] = _first(self._expanded(word, local) for word in words)
            return
        for word in plain:
            name, eq, value = word.partition("=")
            if eq or command == "read":  # `export NAME` alone keeps its value (so may `local`)
                local.names[name] = self._expanded(value, local)

    def _expanded(self, value: str, local: _Locals) -> Taint | None:
        """The taint of a shell word's expansions: ``X=$OPENAI_API_KEY``, ``X=$(printenv K)``."""
        return _as_value(_first(taint for _, taint in self._filled([value], local)))

    def _filled(
        self, words: list[str], local: _Locals
    ) -> Iterator[tuple[ast.expr | None, Taint | None]]:
        """What the shell (and IPython, first) fills into ``words``: each shown name or
        expression with its taint."""
        for raw in words:
            for kind, name, rest, literal in _expansions(raw):
                if kind == "(":  # $(command): what it prints, in a subshell
                    yield None, _first(self.shell(name, None, local=local.copy()))
                elif rest.startswith((":+", "+")) or rest in ("*", "@"):
                    pass  # ${X:+set} is a fixed word, ${!X*} names
                elif kind == "!":
                    yield None, Taint("value")  # ${!name}: the env var that name names
                elif not literal or (local.python and name in self.taints):
                    yield ast.Name(name, ast.Load()), self._dollar(name, local)
        for match in _BRACE.finditer(" ".join(words)) if local.python else ():
            expr = _parse_expr(match.group(1))  # None: not Python, IPython leaves it
            yield expr, _as_value(self.eval(expr))

    def _dollar(self, name: str, local: _Locals) -> Taint | None:
        """What ``$NAME`` holds: IPython fills it from Python first, then the shell's own
        variables, else the environment (an upper-case name)."""
        if local.python and name in self.taints:
            return self.taints[name]
        if local.python and (
            name in self.assigned or (name in self.above and not is_secret_name(name))
        ):
            return None  # a Python name, not the env: !echo $DATA_PATH
        if name in local.names:
            return local.names[name]
        return Taint("value", var=name) if name == name.upper() else None

    def _echoed(self, words: list[str], sink: str | None, local: _Locals) -> Taint | None:
        """``echo $NAME``, ``echo {expr}``, ``echo $(cmd)``: the first value it would print.
        Every name and expression it prints is recorded for L014 when the output is shown."""
        found: list[_Val] = []
        shown: list[ast.expr] = []
        for expr, taint in self._filled(words, local):
            found.append(taint)
            if expr is not None:
                shown.append(expr)
        if sink is not None:
            self.shows(sink, shown)
        return _as_value(_first(found))


@dataclass
class _Locals:
    """The shell variables a ``%%bash`` cell (or one ``!`` line) sets, with their taint."""

    python: bool = False  # IPython fills `$name` and `{expr}` from Python first
    names: dict[str, Taint | None] = field(default_factory=dict)

    def copy(self) -> _Locals:
        return _Locals(self.python, dict(self.names))


@dataclass
class _Stage:
    """One command of a pipeline, as the shell reads it."""

    words: list[str] = field(default_factory=list)  # raw words, quotes kept, no redirections
    inputs: list[str] = field(default_factory=list)  # what `<` reads, unquoted
    to_file: bool = False  # its stdout goes to a file (`> f`, `>> f`, `&> f`, `1> f`)


# shell reading -----------------------------------------------------------------------------------
_LIST_OPS = ("&&", "||", ";;", ";", "&", "\n")
_PIPE_OPS = ("|&", "|")
_REDIRECTIONS = ("&>>", "&>", ">>", ">|", ">&", ">", "<<<", "<<-", "<<", "<&", "<>", "<")


def _lex(text: str) -> list[list[_Stage]] | None:
    """A shell line as the shell reads it: lists (`;`, `&&`, `||`, `&`, newlines) of pipelines
    (`|`) of commands, with quotes, backslashes, `$(…)`, backticks and `${…}` kept inside their
    words and redirections taken out. None when a quote or a substitution doesn't close."""
    pipelines: list[list[_Stage]] = []
    stages: list[_Stage] = []
    stage = _Stage()
    word: list[str] = []
    reading = False  # a word has started (it may be "" so far)
    target = ""  # what the next word is: "out" (stdout's file), "in", "dup" or "skip"
    fd: str | None = None
    i, n = 0, len(text)

    def end_word() -> None:
        nonlocal reading, target
        if reading:
            raw = "".join(word)
            if target == "out" or (target == "dup" and not raw.isdigit() and raw != "-"):
                stage.to_file = stage.to_file or _unquote(raw) not in _SCREENS
            elif target == "in":
                stage.inputs.append(_unquote(raw))
            elif not target:
                stage.words.append(raw)
            target = ""
        word.clear()
        reading = False

    def end_stage() -> None:
        nonlocal stage
        end_word()
        if stage.words or stage.inputs or stage.to_file:
            stages.append(stage)
        stage = _Stage()

    def end_pipeline() -> None:
        nonlocal stages
        end_stage()
        if stages:
            pipelines.append(stages)
        stages = []

    while i < n:
        char = text[i]
        if char == "\\":  # continuation lines were joined before: an escape is two characters
            word.append(text[i : i + 2])
            reading, i = True, i + 2
            continue
        if char in "'\"`" or text.startswith(("$(", "${"), i):
            end = _closing(text, i)
            if end < 0:
                return None
            word.append(text[i:end])
            reading, i = True, end
            continue
        if char == "#" and not reading:  # a comment, to the end of the line
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if char in " \t\r":
            end_word()
            i += 1
            continue
        if char in "<>" or text.startswith("&>", i):
            number = "".join(word)  # empty unless a word is being read
            if number.isdigit():  # `2>`: the descriptor is part of the operator
                word.clear()
                reading = False
                fd = number
            else:
                end_word()
                fd = None
            op = next(o for o in _REDIRECTIONS if text.startswith(o, i))
            i += len(op)
            if op.startswith("&>"):
                target = "out"
            elif op in (">>", ">|", ">"):
                target = "out" if fd in (None, "1") else "skip"
            elif op == ">&":  # `2>&file` is an error: bash runs nothing
                target = "dup"
            elif op in ("<", "<>"):  # `<>` opens it to read and write
                target = "in" if fd in (None, "0") else "skip"
            else:
                target = "skip"  # a here-document's word, `<&`, `<>`
            continue
        op = next((o for o in (*_LIST_OPS, *_PIPE_OPS) if text.startswith(o, i)), None)
        if op is not None:
            i += len(op)
            if op in _PIPE_OPS:
                end_stage()
            else:
                end_pipeline()
            continue
        word.append(char)
        reading, i = True, i + 1
    end_pipeline()
    return pipelines


def _lex_raw(text: str) -> list[list[_Stage]]:
    """A shell line read as raw text, for one whose quotes don't close: it may split inside a
    quote, so it reads more commands than the shell would, never fewer."""
    pipelines: list[list[_Stage]] = []
    for segment in _SEGMENTS.split(text):
        stages = [
            _Stage([w for w in part.split() if not _REDIRECTION.fullmatch(w)])
            for part in segment.split("|")
        ]
        stages[-1].to_file = bool(_REDIRECT.search(segment))
        pipelines.append(stages)
    return pipelines


def _closing(text: str, i: int) -> int:
    """The index just past the quote, backtick, ``$(…)`` or ``${…}`` that starts at ``i``; -1
    when it doesn't close."""
    if text.startswith(("$(", "${"), i):
        return _until(text, i + 2, ")" if text[i + 1] == "(" else "}")
    quote = text[i]
    if quote == "'":
        end = text.find("'", i + 1)
        return -1 if end < 0 else end + 1
    j = i + 1
    while j < len(text):
        char = text[j]
        if char == "\\":
            j += 2
        elif char == quote:
            return j + 1
        elif quote == '"' and (char == "`" or text.startswith(("$(", "${"), j)):
            j = _closing(text, j)
            if j < 0:
                return -1
        else:
            j += 1
    return -1


def _until(text: str, j: int, close: str) -> int:
    """Past the ``close`` bracket that matches an opened one, skipping quotes inside."""
    opener, depth = "(" if close == ")" else "{", 1
    while j < len(text):
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char in "'\"`" or text.startswith(("$(", "${"), j):
            j = _closing(text, j)
            if j < 0:
                return -1
            continue
        if char == opener:
            depth += 1
        elif char == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _unquote(raw: str) -> str:
    """A raw shell word as the command gets it: quotes and backslashes removed (expansions are
    left as written)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        char = raw[i]
        if char == "\\":
            out.append(raw[i + 1 : i + 2])
            i += 2
        elif char == "'":
            end = raw.find("'", i + 1)
            end = n if end < 0 else end
            out.append(raw[i + 1 : end])
            i = end + 1
        elif char == '"':
            i += 1
            while i < n and raw[i] != '"':
                if raw[i] == "\\" and raw[i + 1 : i + 2] in ('"', "\\", "$", "`"):
                    i += 1
                out.append(raw[i])
                i += 1
            i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _expansions(raw: str) -> Iterator[tuple[str, str, str, bool]]:
    """What the shell expands in a raw word, in order: ``("", NAME, rest, literal)`` for
    ``$NAME`` and ``${NAME…}`` (literal: in single quotes, where only IPython fills it),
    ``("!", …)`` for ``${!NAME}``, and ``("(", command, "", False)`` for ``$(command)`` and
    backticks; never ``${#NAME}``, a length."""
    i, n, double = 0, len(raw), False
    while i < n:
        char = raw[i]
        if char == "\\":
            i += 2
        elif char == "'" and not double:
            end = raw.find("'", i + 1)
            end = n if end < 0 else end
            for found in _DOLLAR.finditer(raw, i + 1, end):
                yield _dollar_event(found, literal=True)
            i = end + 1
        elif char == '"':
            double = not double
            i += 1
        elif raw.startswith("$(", i) or char == "`":
            end = _closing(raw, i)
            end = n + 1 if end < 0 else end
            yield "(", raw[i + 2 : end - 1] if char == "$" else raw[i + 1 : end - 1], "", False
            i = end
        elif found := _DOLLAR.match(raw, i):
            yield _dollar_event(found, literal=False)
            i = found.end()
        else:
            i += 1


def _dollar_event(found: re.Match[str], *, literal: bool) -> tuple[str, str, str, bool]:
    return found.group(1) or "", found.group(2) or found.group(4), found.group(3) or "", literal


def _command_words(stage: _Stage) -> tuple[list[str], list[str]]:
    """A command's raw and unquoted words, without ``sudo``/``NAME=value``/``do`` in front."""
    raws = list(stage.words)
    while raws and (
        _unquote(raws[0]) in _SHELL_PREFIX
        or _unquote(raws[0]) in _SHELL_KEYWORDS
        or _SETTING.fullmatch(raws[0])
    ):
        raws.pop(0)
    return raws, [_unquote(raw) for raw in raws]


def _shell_script(args: list[str]) -> str | None:
    """``bash -c 'script'``, ``sh -lc "script"``: the script the shell runs."""
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return None  # a script file runs
        if not arg.startswith("--") and "c" in arg[1:]:
            rest = [a for a in args[index + 1 :] if not a.startswith("-")]
            return rest[0] if rest else None
    return None


def shell_commands(text: str, _depth: int = 0) -> list[tuple[list[str], list[str]]]:
    """Each command a shell line runs, as (raw words, unquoted words) with ``sudo``,
    ``NAME=value`` and keywords taken off the front: its lists' and pipelines' commands, then
    those of its ``$(…)`` and backtick substitutions and of a ``bash -c '…'`` script. A line
    whose quotes don't close is read as raw text. L012 reads shell this way (design §6.4)."""
    lexed = _lex(text)
    found: list[tuple[list[str], list[str]]] = []
    for stages in lexed if lexed is not None else _lex_raw(text):
        for stage in stages:
            raws, words = _command_words(stage)
            if words:
                found.append((raws, words))
            if _depth >= 4:
                continue
            if words and words[0].rsplit("/", 1)[-1] in _SHELLS:
                script = _shell_script(words[1:])
                if script is not None:
                    found += shell_commands(script, _depth + 1)
            for raw in stage.words:
                for kind, inner, _, _ in _expansions(raw):
                    if kind == "(":
                        found += shell_commands(inner, _depth + 1)
    return found


def shell_lines(body: list[str]) -> list[str]:
    """A shell cell's lines as the shell reads them: a line that ends in a backslash, or with a
    quote still open, goes on to the next."""
    found: list[str] = []
    text = ""
    for number, line in enumerate(body):
        if not text:
            text = line
        elif text.endswith("\\"):
            text = text[:-1] + " " + line
        else:  # a quote still open
            text = f"{text}\n{line}"
        more = number + 1 < len(body)
        if more and (text.endswith("\\") or _lex(text) is None):
            continue
        found.append(text)
        text = ""
    return found


def _secret_file(word: str) -> Taint | None:
    """What reading this file shows: every value of `.env`, or of the environment."""
    if _env_file(word):
        return _WHOLE_DOTENV
    return _WHOLE_ENV if _PROC_ENVIRON.fullmatch(word) else None


def _names_only(command: str, words: list[str]) -> bool:
    """A filter that keeps only what is before each `=`: ``cut -d= -f1``, ``sed 's/=.*//'``,
    ``awk -F= '{print $1}'``, ``grep -o '^[^=]*'``."""
    args = words[1:]
    flags = [a[1:] for a in args if a.startswith("-") and not a.startswith("--")]
    pairs = set(zip(args, args[1:], strict=False))  # `-d =`, `-f 1`
    if command == "cut":
        split = "d=" in flags or ("-d", "=") in pairs
        return split and ("f1" in flags or ("-f", "1") in pairs)
    if command == "sed":
        return any(_SED_NAMES.fullmatch(a) for a in args)
    if command in ("awk", "gawk", "mawk"):
        split = "F=" in flags or ("-F", "=") in pairs
        program = next((a for a in args if "print" in a), "")
        return split and "$1" in program and not _AWK_VALUES.search(program)
    grep_names = any("o" in f for f in flags) and any(a.startswith("^[^=]") for a in args)
    return command in _GREP and grep_names


def _quiet(stage: _Stage) -> bool:
    """A pipe stage that shows only a count, a yes/no or the names: ``wc``, ``grep -q``,
    ``cut -d= -f1``."""
    _, words = _command_words(stage)
    if not words:
        return False
    command = words[0].rsplit("/", 1)[-1]
    flags = [w[1:] for w in words[1:] if w.startswith("-") and not w.startswith("--")]
    if command == "wc":
        return True
    if command in _GREP and any(set(f) & set("qclL") for f in flags):
        return True
    return _names_only(command, words)


# helpers -----------------------------------------------------------------------------------------
_NEWLINE = re.compile(r"\r\n|\r|\n")


def _lines_as_ast(text: str) -> list[str]:
    """The source lines as ``ast`` numbers them (``\r\n``, ``\r`` or ``\n``), ends kept."""
    lines, start = [], 0
    for found in _NEWLINE.finditer(text):
        lines.append(text[start : found.end()])
        start = found.end()
    return [*lines, text[start:]]


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _parse_expr(text: str) -> ast.expr | None:
    try:
        return ast.parse(text.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a plain chain of names, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _last_name(node: ast.expr | None) -> str:
    """``console`` for ``console`` or ``self.console``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _constant(node: ast.expr | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _literal(node: ast.expr | None) -> bool:
    """A literal value: ``"dev"``, ``8080``, or an f-string of literals only."""
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in node.values)
    return isinstance(node, ast.Constant)


def _builds_dict(node: ast.expr | None) -> bool:
    """``{}``, ``{"a": 1}``, ``{k: v for …}``, ``dict()``, ``defaultdict(list)``."""
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return True
    return isinstance(node, ast.Call) and _last_name(node.func) in _DICT_TYPES


def _var(key: object) -> str | None:
    return key if isinstance(key, str) and _IDENT.fullmatch(key) else None


def _arg(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(node.args) > index and not isinstance(node.args[index], ast.Starred):
        return node.args[index]
    return _keyword(node, keyword)


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == name), None)


def _arg_names(args: ast.arguments) -> list[str]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [a.arg for a in every if a is not None]


def _free_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names a function's body reads but never binds: globals it reads when it is called."""
    loaded: set[str] = set()
    bound: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            (loaded if isinstance(child.ctx, ast.Load) else bound).add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
    return loaded - bound


def _local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a function body assigns or deletes: its locals, which hide the cell's names in
    the whole body (read before they are bound they raise), but for those it declares ``global``
    or ``nonlocal``. Nested functions, classes and lambdas, and comprehension targets, are
    scopes of their own."""
    bound: set[str] = set()
    declared: set[str] = set()
    todo: list[ast.AST] = list(node.body)
    while todo:
        child = todo.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # its name is bound where the walk reaches it
        if isinstance(child, ast.comprehension):
            todo += [child.iter, *child.ifs]
            continue
        if isinstance(child, (ast.Global, ast.Nonlocal)):
            declared.update(child.names)
        elif isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Load):
            bound.add(child.id)
        todo += ast.iter_child_nodes(child)
    return bound - declared


def _captures(pattern: ast.pattern) -> list[str]:
    """The names a ``case`` pattern binds: ``case str() as k``, ``case {"a": v, **rest}``."""
    names: list[str] = []
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.append(node.rest)
    return names


def _command(node: ast.expr | None) -> str | None:
    """The literal command of ``os.system("…")`` or ``subprocess.run([…])``."""
    text = _constant(node)
    if isinstance(text, str):
        return text
    if isinstance(node, (ast.List, ast.Tuple)):
        words = [_constant(e) for e in node.elts]
        if all(isinstance(w, str) for w in words):  # [] runs nothing
            return " ".join(shlex.quote(str(w)) for w in words)
    return None


def _pair(target: ast.expr) -> bool:
    if not isinstance(target, (ast.Tuple, ast.List)) or len(target.elts) != 2:
        return False
    return not any(isinstance(e, ast.Starred) for e in target.elts)


def _first(values: Iterable[_Val]) -> Taint | None:
    """The first taint among ``values``, all of them consumed (each may hold a sink). While a
    function is summarised, it holds every parameter any of them holds."""
    found: Taint | None = None
    params: frozenset[str] = frozenset()
    for value in values:
        if isinstance(value, Taint):
            found = found or value
            params |= value.param or frozenset()
    if found is not None and params:
        found = replace(found, param=params)
    return found


def _as_value(value: _Val) -> Taint | None:
    """What shows when the taint is printed as text: always a value."""
    if not isinstance(value, Taint):
        return None
    return replace(value, kind="value", built=False)


def _part(value: Taint) -> Taint:
    """One element of something tainted: a value, and one of many (no longer every value)."""
    return replace(
        value, kind="value", whole=False, live=False, keys=False, built=False, pair=False
    )


def _element(value: _Val) -> Taint | None:
    """One element of iterating it: a mapping gives names, clean."""
    if not isinstance(value, Taint) or value.kind == "mapping":
        return None
    return _part(value)


def _iterated(value: _Val) -> Taint | None:
    """``list(x)``, ``sorted(x)``: a mapping's names are clean; items and values stay; an open
    ``.env`` file gives its lines."""
    if _is(value, _ENV_HANDLE):
        return _WHOLE_DOTENV
    if not isinstance(value, Taint) or value.kind == "mapping":
        return None
    return value


def _lazy(node: ast.Call, name: str, args: list[_Val]) -> Taint | None:
    """``filter(f, xs)``, ``map(f, xs)``, ``zip(xs, ys)``: what iterating the result gives."""
    if name == "zip":
        taints = [_iterated(arg) for arg in args]
        found = _first(taints)
        if found is not None and taints[0] is None and len(taints) == 2:
            return replace(found, kind="items")  # zip(names, values): the name is clean
        return found
    if name == "map" and args and isinstance(args[0], Taint):
        return Taint("value")  # map(os.environ.get, names)
    function = node.args[0] if node.args else None
    if name == "map" and getattr(function, "id", None) in _CLEAN_CALLS:
        return None  # map(len, values)
    return _first(_iterated(arg) for arg in args[1:])


def _is(value: _Val, qual: str) -> bool:
    return isinstance(value, _Name) and value.qual == qual


def _captured(node: ast.Call) -> bool:
    return _keyword(node, "capture_output") is not None or _keyword(node, "stdout") is not None


def _env_path(node: ast.expr | None) -> bool:
    """A literal `.env` file name, as the shell rule reads it: ``".env"``, ``"../.env"``."""
    text = _constant(node)
    return isinstance(text, str) and _env_file(text)


def _name_part(node: ast.Subscript, base: Taint) -> bool:
    """``line.split("=")[0]``, ``line.partition("=")[0]`` of a `.env` line: its name."""
    call = node.value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
        return False
    if call.func.attr not in ("split", "partition") or _constant(node.slice) != 0:
        return False
    line = base.dotenv and base.var is None and base.mapping is None
    return line and bool(call.args) and _constant(call.args[0]) == "="


def _file(node: ast.Call, qual: str, args: list[_Val]) -> _Val:
    """``open(".env")``, ``Path("../.env")``, ``ENV_FILE.read_text()``: a `.env` path, its open
    file, or what reading it gives."""
    names_env = any(_is(arg, _ENV_PATH) for arg in args)  # ".env", ENV_FILE, ROOT / ".env"
    if qual in _PATHS:
        return _Name(_ENV_PATH) if names_env else None
    if qual in _OPENS:
        return _Name(_ENV_HANDLE) if names_env else None
    owner, _, method = qual.rpartition(".")
    if owner == _ENV_PATH:
        if method == "open":
            return _Name(_ENV_HANDLE)
        if method in _PATH_METHODS:
            return _Name(_ENV_PATH)  # Path("~/.env").expanduser()
        return _WHOLE_DOTENV if method in ("read_text", "read_bytes") else None
    if method in ("read", "readlines"):  # an open `.env` file
        return _WHOLE_DOTENV
    return Taint("value", dotenv=True) if method == "readline" else None


def _env_sample(path: object) -> bool:
    """``.env.example`` and the like: a template, whose values are placeholders."""
    if not isinstance(path, str):
        return False
    base = path.rstrip("/").rsplit("/", 1)[-1]
    return ".env" in base and base.rsplit(".", 1)[-1] in _ENV_FILE_SAMPLES


def _timed_statement(rest: str, options: frozenset[str]) -> str:
    """The statement of ``%timeit -n 1 -r1 stmt`` or ``%prun -s cumulative stmt`` ("" when
    there is none)."""
    statement, takes_value = rest.strip(), False
    while statement and (statement.startswith("-") or takes_value):
        word, *after = statement.split(None, 1)
        takes_value = word in options  # `-n 1`; `-n1` holds its own value
        statement = after[0] if after else ""
    return statement


def _env(args: list[str]) -> Taint | None:
    """``env`` prints every env var unless a command follows (``env FOO=1 python x.py``) or
    ``-i`` empties the environment first; ``-u NAME`` and ``NAME=value`` don't stop it."""
    empty, index = False, 0
    while index < len(args):
        word = args[index]
        if word in ("-i", "--ignore-environment", "-"):
            empty = True
        elif word in _ENV_ARG_OPTIONS:
            index += 1  # its value
        elif not word.startswith("-") and not _SETTING.fullmatch(word):
            return None  # a command runs, with its own output
        index += 1
    return None if empty else _WHOLE_ENV


def _env_file(word: str) -> bool:
    base = word.rstrip("/").rsplit("/", 1)[-1]
    if base == ".envrc" or base.endswith(".env"):
        return True
    return base.startswith(".env.") and base.rsplit(".", 1)[-1] not in _ENV_FILE_SAMPLES


def _shown_identifiers(node: ast.expr | None) -> Iterator[ast.Name | ast.Attribute]:
    """The names whose value an expression shows, by the same rules as the taint walk (None, a
    dict's `**` key, shows none)."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        yield node
    elif isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if isinstance(func, ast.Attribute) and name not in _STR_FACTS:
            yield from _method_shows(func.value, name)
        if name in _STRINGIFY | _NUMBERS | _WRAPPERS | _ITERATE | {"next", "dict", "format"}:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                yield from _shown_identifiers(arg)
        elif name == "join" and node.args:
            yield from _shown_identifiers(node.args[0])
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            yield from _shown_identifiers(value)
    elif isinstance(node, ast.FormattedValue):
        yield from _shown_identifiers(node.value)
    elif isinstance(node, ast.BinOp):
        yield from _shown_identifiers(node.left)
        yield from _shown_identifiers(node.right)
    elif isinstance(node, ast.BoolOp):
        values = node.values[-1:] if isinstance(node.op, ast.And) else node.values
        for value in values:
            yield from _shown_identifiers(value)
    elif isinstance(node, ast.IfExp):
        yield from _shown_identifiers(node.body)
        yield from _shown_identifiers(node.orelse)
    elif isinstance(node, ast.UnaryOp) and not isinstance(node.op, ast.Not):
        yield from _shown_identifiers(node.operand)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for element in node.elts:
            yield from _shown_identifiers(element)
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            yield from _shown_identifiers(value)
            yield from _shown_identifiers(key)  # a literal key shows no name
    elif isinstance(node, (ast.Starred, ast.NamedExpr)):
        yield from _shown_identifiers(node.value)
    elif isinstance(node, ast.Subscript):
        yield from _shown_identifiers(node.value)  # never the key: df["token"] is a column
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        yield from _shown_identifiers(node.elt)


def _method_shows(receiver: ast.expr, method: str | None) -> Iterator[ast.Name | ast.Attribute]:
    """``api_key.strip()`` shows ``api_key``; ``config.get_token()`` shows no name."""
    if method == "keys" or not isinstance(receiver, (ast.Name, ast.Attribute)):
        yield from ()
        return
    yield receiver
