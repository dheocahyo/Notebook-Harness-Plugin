"""Code that writes outside the project (L013, design §6.4).

L012's walk (``network._Scanner``) with four things changed: a value is a path (its literal
text, unknown parts kept as holes), a call is a write sink, a function body keeps its folder
moves and its parameters to itself (a call to the function writes what its body writes with the
call's arguments), and a shell command writes its redirections' files and its destinations. The
cells above the target are walked the same way first (``bindings``), for their names and
functions only. ``outside`` resolves a path against the project root and the notebook's folder
as text: nothing here touches the disk. Stdlib plus ``network``, ``magics`` and
``secret_scan``'s shell reader, importable on Python 3.11 like them.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import itertools
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from nh_gateway.lint import network
from nh_gateway.lint.magics import Masked, lines_of, mask
from nh_gateway.lint.secret_scan import shell_stages

HOLE = network._HOLE  # an unknown part of a path
# The notebook's folder, where the kernel starts (``Path.cwd()``): a path that starts with it is
# anchored there, so ``.parent`` climbs out of it; ``outside`` reads it as ``.``.
_HERE = "\x01"
# A function's parameter in a path its body writes: a hole that a call fills with its argument
# (``\0\x02<function>.<index>\x03``). Outside the body it is a plain unknown part.
_PARAM = HOLE + "\x02"
_MARKER = re.compile("\x00\x02(\\d+)\\.(\\d+)\x03")
# Where a join put a parameter after a folder: an absolute argument starts the path over there.
_RESTART = "\x04"
_RESTARTS_AT_ABSOLUTE = re.compile(f"{_RESTART}(?=[/{_HERE}])")
_RESTART_LEFT = re.compile(f"{_RESTART}(?!{re.escape(_PARAM)})")
_NUMBERS = itertools.count(1)  # function numbers, unique in the process


@dataclass(frozen=True)
class Write:
    """One place the cell writes a file or folder, or removes one."""

    where: str  # the call's dotted name or the command, never its arguments
    path: str  # as the cell writes it: literal text, holes for unknown parts
    removes: bool = False


@dataclass(frozen=True)
class _Function:
    """A function the cell defines, as L013 reads it: its parameters and the writes of its body
    whose paths hold them (templates with one ``_MARKER`` per parameter)."""

    number: int
    positional: tuple[str, ...]  # the parameters a call may give by position, in order
    names: tuple[str, ...]  # every parameter, by its marker's index
    defaults: tuple[str | None, ...]  # each parameter's default as a path; None: none or unread
    writes: tuple[tuple[str, str, bool], ...]  # (template, where in the body, removes)


@dataclass(frozen=True, eq=False)
class _Seed(network.Seed):
    """``network.Seed`` plus the functions the cells above define (equal by ``key`` as it is)."""

    functions: tuple[tuple[str, _Function], ...] = ()


@dataclass
class _Frame:
    """A function body being read: where it starts, its defaults, the writes that need a call."""

    number: int
    cwd: str | None
    defaults: tuple[str | None, ...]
    writes: dict[tuple[str, bool], str] = field(default_factory=dict)  # (template, removes): where


# Paths a write may reach without a question (decided, design §6.4): device files, and the
# system temp folders every program shares.
EXEMPT = ("/dev", "/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")

_UNSET = "\0unset"  # a ``cwd`` argument not given: the scanner's own
_WRITEFILE = frozenset({"writefile", "file"})
# Frame and series writers (pandas, xarray, geopandas): the path is the first argument.
_FRAME_WRITERS = frozenset(
    {"to_csv", "to_parquet", "to_excel", "to_pickle", "to_json", "to_feather", "to_hdf"}
    | {"to_stata", "to_html", "to_latex", "to_markdown", "to_xml", "to_orc", "to_netcdf"}
    | {"to_zarr", "to_file", "ExcelWriter"}
)
_FRAME_KEYWORDS = (
    "path",
    "path_or_buf",
    "path_or_buffer",
    "buf",
    "excel_writer",
    "fname",
    "filename",
    "store",
    "file",
)
_WRITE_PREFIXES = ("write_", "sink_")  # polars' writers and lazy sinks, plotly's write_image
_NOT_FILE_WRITES = frozenset({"write_database", "write_clipboard"})  # a path's own: below
_WRITE_KEYWORDS = ("file", "path", "target", "workbook", "fname", "filename")
_SAVES = frozenset(
    {"savefig", "save", "save_model", "save_weights", "save_pretrained", "save_to_disk"}
    | {"to_disk", "tofile"}
)
_SAVE_KEYWORDS = (
    "fname",
    "fp",
    "filepath",
    "filename",
    "file",
    "path",
    "save_directory",
    "dataset_path",
)
# A Spark frame's writer (``sdf.write``, then its settings): these methods write their path.
_SPARK_WRITES = frozenset({"csv", "parquet", "json", "orc", "text"})
_SPARK_STEPS = frozenset(
    {"mode", "option", "options", "format", "partitionBy", "bucketBy", "sortBy"}
)
# Functions by their dotted name: each path's (position, keywords, removes); position -1: only
# by keyword.
_Arg = tuple[int, tuple[str, ...], bool]
_DESTINATION: tuple[_Arg, ...] = ((1, ("dst",), False),)
_MOVE: tuple[_Arg, ...] = ((1, ("dst",), False), (0, ("src",), True))
_REMOVE: tuple[_Arg, ...] = ((0, ("path",), True),)
_LOG_FILE: tuple[_Arg, ...] = ((0, ("filename",), False),)
_IMAGE: tuple[_Arg, ...] = ((0, ("uri",), False),)
_FUNCTIONS: dict[str, tuple[_Arg, ...]] = {
    "numpy.savez": ((0, ("file",), False),),
    "numpy.savez_compressed": ((0, ("file",), False),),
    "numpy.savetxt": ((0, ("fname",), False),),
    "matplotlib.pyplot.imsave": ((0, ("fname",), False),),
    "matplotlib.image.imsave": ((0, ("fname",), False),),
    "cv2.imwrite": ((0, ("filename",), False),),
    "imageio.imwrite": _IMAGE,
    "imageio.v2.imwrite": _IMAGE,
    "imageio.v3.imwrite": _IMAGE,
    "skimage.io.imsave": ((0, ("fname",), False),),
    "scipy.io.savemat": ((0, ("file_name",), False),),
    "scipy.io.wavfile.write": ((0, ("filename",), False),),
    "soundfile.write": ((0, ("file",), False),),
    "logging.FileHandler": _LOG_FILE,
    "logging.handlers.RotatingFileHandler": _LOG_FILE,
    "logging.handlers.TimedRotatingFileHandler": _LOG_FILE,
    "logging.handlers.WatchedFileHandler": _LOG_FILE,
    "logging.basicConfig": ((-1, ("filename",), False),),
    "joblib.dump": ((1, ("filename",), False),),
    "pickle.dump": ((1, ("file",), False),),
    "torch.save": ((1, ("f",), False),),
    "pandas.to_pickle": ((1, ("filepath_or_buffer",), False),),
    "tensorflow.saved_model.save": ((1, ("export_dir",), False),),
    "torch.onnx.export": ((2, ("f",), False),),
    "pyarrow.parquet.write_table": ((1, ("where",), False),),
    "pyarrow.feather.write_feather": ((1, ("dest",), False),),
    "pyarrow.csv.write_csv": ((1, ("output_file",), False),),
    "urllib.request.urlretrieve": ((1, ("filename",), False),),
    "shutil.copy": _DESTINATION,
    "shutil.copy2": _DESTINATION,
    "shutil.copyfile": _DESTINATION,
    "shutil.copytree": _DESTINATION,
    "os.symlink": _DESTINATION,
    "os.link": _DESTINATION,
    "shutil.move": _MOVE,
    "os.rename": _MOVE,
    "os.replace": _MOVE,
    "os.renames": ((1, ("new",), False), (0, ("old",), True)),
    "os.mkdir": ((0, ("path",), False),),
    "os.makedirs": ((0, ("name",), False),),
    "shutil.make_archive": ((0, ("base_name",), False),),
    "os.remove": _REMOVE,
    "os.unlink": _REMOVE,
    "os.rmdir": _REMOVE,
    "os.removedirs": ((0, ("name",), True),),
    "shutil.rmtree": _REMOVE,
}
# Calls that open a file with a mode (the second argument, or ``mode``): a write when it says so.
_OPENS = frozenset(
    {"open", "io.open", "builtins.open", "codecs.open", "gzip.open", "bz2.open", "lzma.open"}
    | {"tarfile.open", "zipfile.ZipFile"}
)
_OPEN_KEYWORDS = ("file", "filename", "name")
_WRITE_MODES = frozenset("wax+")
_ARCHIVE_OPENS = frozenset({"tarfile.open"})  # a mode's part after `:` or `|` is compression
_ARCHIVE_MODE = re.compile(r"[:|]")
# A path's own methods: it writes, removes, or moves itself to its one argument.
_PATH_WRITES = frozenset(
    {"write_text", "write_bytes", "touch", "mkdir", "symlink_to", "hardlink_to"}
)
_PATH_REMOVES = frozenset({"unlink", "rmdir"})
_PATH_MOVES = frozenset({"rename", "replace"})
_CHDIRS = frozenset({"os.chdir"})
_CONTEXT_CHDIRS = frozenset({"contextlib.chdir"})  # `with chdir(path):` moves for its body
# Module names that stand for a module with no import, to L013 (L012's `np` and `Path` too).
_MODULE_NAMES = {"pd": "pandas", "plt": "matplotlib.pyplot", "tf": "tensorflow"}
# Expressions whose value is a path.
_PATH_TYPES = frozenset(
    {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath"}
)
_JOINS = frozenset({"os.path.join", "posixpath.join"})
_SAME = frozenset(
    {"os.path.expanduser", "os.path.normpath", "posixpath.expanduser", "posixpath.normpath"}
    | {"os.fspath", "str"}
)
_SAME_METHODS = frozenset({"expanduser"})
# The same path, from the folder the cell is in when it is relative.
_ABSOLUTE = frozenset(
    {"os.path.abspath", "os.path.realpath", "posixpath.abspath", "posixpath.realpath"}
)
_ABSOLUTE_METHODS = frozenset({"resolve", "absolute"})
_DIRNAMES = frozenset({"os.path.dirname", "posixpath.dirname"})
_HOMES = frozenset({"pathlib.Path.home", "pathlib.PosixPath.home"})
_CWDS = frozenset({"pathlib.Path.cwd", "pathlib.PosixPath.cwd", "os.getcwd"})
_HOME_READS = frozenset({"os.getenv", "os.environ.get"})
# Shell commands: what each one writes (design §6.4's shell table).
_EACH_WRITES = frozenset({"tee", "mkdir", "touch"})
_EACH_REMOVES = frozenset({"rm", "rmdir", "unlink"})
_DESTINATIONS = frozenset({"cp", "mv", "ln", "rsync"})
_NBCONVERTS = frozenset({"jupyter-nbconvert"})  # and `jupyter nbconvert`
_HISTORY = re.compile(r"-\d+")  # `%cd -3`: a folder from IPython's history
# What IPython fills in a `!` line, in one pass: `${name}`, `{name}`, `$name`.
_IPYTHON_FILLS = re.compile(r"\$\{([A-Za-z_]\w*)\}|\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")


def scan(
    masked: Masked, lines: list[str], tree: ast.Module | None, seed: network.Seed = network.EMPTY
) -> list[Write]:
    """The places this cell writes, in source order, with the names ``seed`` holds (the cells
    above, ``bindings``) bound first. ``tree`` is None for a ``%%`` cell or bad syntax."""
    scanner = _Scanner(masked, lines, seed)
    scanner.run(tree)
    return scanner.writes


def bindings(sources: Iterable[str]) -> network.Seed:
    """What the code cells ``sources`` (the cells above the target, in notebook order) bind, for
    ``scan``: as ``network.bindings``, with paths for values, in a cache of its own."""
    seed = network.EMPTY
    try:
        for source in sources:
            seed = _cell_seed(source, seed)
    except Exception:  # fail open: earlier cells only add names
        return network.EMPTY
    return seed


@functools.lru_cache(maxsize=1024)
def _cell_seed(source: str, seed: network.Seed) -> network.Seed:
    text = "\n".join(lines_of(source or ""))
    masked = mask(text)
    tree = network._parse(masked.text) if masked.cell_magic is None else None
    if masked.cell_magic is None and tree is None:
        return seed
    scanner = _Scanner(masked, lines_of(text), seed)
    scanner.run(tree)
    key = hashlib.sha256(
        f"writes\0{seed.key}\0{source}".encode("utf-8", "surrogatepass")
    ).hexdigest()
    return scanner.seed(key)


def outside(path: str, root: str, notebook_dir: str = "") -> str | None:
    """The path to name when ``path`` (as the cell writes it, from the notebook's folder
    ``notebook_dir`` under the project ``root``) lands outside the project; None when it lands
    inside, in an exempt place, or where nh can't tell. Each unknown part stands for text within
    one folder name: a path counts when no path it can stand for is inside or exempt, and is
    named by the folder before its first unknown part."""
    if path.startswith(_HERE):
        path = "." + path[len(_HERE) :]
    known, hole, _ = path.partition(HOLE)
    if not known:
        return None  # it starts unknown: it may be absolute
    if known.startswith("~"):
        if not hole:
            return known
        cut = known.rfind("/")
        return known[:cut] + "/…" if cut >= 0 else known + "…"
    absolute = path if path.startswith("/") else posixpath.join(root, notebook_dir, path)
    resolved = _normalised(absolute)
    places = (_normalised(root), *EXEMPT)
    if HOLE not in resolved:
        return None if any(_under(resolved, place) for place in places) else resolved
    if any(_may_be_under(resolved, place) for place in places):
        return None
    known = resolved.partition(HOLE)[0]
    return known[: known.rfind("/")] + "/…"


class _Scanner(network._Scanner):
    """L012's walk, reading paths and writes instead of URLs and sites."""

    def __init__(
        self, masked: Masked, lines: list[str], seed: network.Seed, depth: int = 0
    ) -> None:
        super().__init__(masked, lines, seed, depth)
        self.writes: list[Write] = []
        self.cwd: str | None = None  # where the kernel is; None: the notebook's folder
        self.shell_cwd: str | None = None  # a shell cell's folder, from line to line
        self.functions: dict[str, _Function] = dict(getattr(seed, "functions", ()))
        self.frames: list[_Frame] = []  # the function bodies being read, innermost last
        self.lambdas: dict[int, _Function] = {}  # each lambda read, by its node's id

    def seed(self, key: str = "") -> network.Seed:
        return _Seed(
            tuple(self.names.items()),
            tuple(self.aliases.items()),
            key,
            tuple(self.functions.items()),
        )

    def run(self, tree: ast.Module | None) -> None:
        magic = self.masked.cell_magic
        if magic in _WRITEFILE:
            first = next((line for line in self.lines if line.strip()), "")
            self._writefile(first.strip()[2 + len(magic) :], f"%%{magic}")
            return
        super().run(tree)

    def python(self, code: str, label: str | None = None, cwd: str | None = _UNSET) -> None:
        """Python run elsewhere in the cell (``python -c``, ``%timeit``, ``run_cell_magic``),
        from the folder ``cwd`` (the kernel's by default); its writes are this cell's, under
        ``label`` when given."""
        if self.depth >= network._MAX_DEPTH:
            return
        text = "\n".join(lines_of(code))
        masked = mask(text)
        inner = _Scanner(masked, lines_of(text), self.seed(), self.depth + 1)
        inner.cwd = self.cwd if cwd == _UNSET else cwd
        inner.frames = self.frames  # in a function body (`%timeit` there), its writes need a call
        inner.run(network._parse(masked.text) if masked.cell_magic is None else None)
        for write in inner.writes:
            self.writes.append(dataclasses.replace(write, where=label) if label else write)

    def _reach(self, where: str, value: network._Val) -> None:
        """L012's sites are not this scan's."""

    def qual(self, node: ast.expr) -> str | None:
        if (
            isinstance(node, ast.Name)
            and node.id in _MODULE_NAMES
            and node.id not in self.aliases
            and node.id not in self.names
        ):
            return _MODULE_NAMES[node.id]
        return super().qual(node)

    # statements and functions ------------------------------------------------------------------
    def stmt(self, stmt: ast.stmt, definite: bool) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.visit(stmt.decorator_list)
            body = stmt.body
            self.functions[stmt.name] = self._function(
                stmt.args, lambda: self.block(body, definite=True)
            )
            return
        if isinstance(stmt, ast.ClassDef):  # its methods are no names in the cell
            functions = dict(self.functions)
            super().stmt(stmt, definite)
            self.functions = functions
            self.functions.pop(stmt.name, None)
            return
        if isinstance(stmt, (ast.With, ast.AsyncWith)) and any(
            self._context_chdir(item.context_expr) for item in stmt.items
        ):
            self._with_chdir(stmt, definite)
            return
        super().stmt(stmt, definite)
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Lambda):
            function = self.lambdas.get(id(stmt.value))
            for target in stmt.targets:
                if function is not None and isinstance(target, ast.Name):
                    self.functions[target.id] = function

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Lambda):
            body = node.body
            self.lambdas[id(node)] = self._function(node.args, lambda: self._visit(body))
            return
        super()._visit(node)

    def bind(self, target: ast.expr, value: network._Val, definite: bool) -> None:
        if isinstance(target, ast.Name):
            self.functions.pop(target.id, None)  # rebound: no longer that function
        super().bind(target, value, definite)

    def _function(self, args: ast.arguments, walk: Callable[[], None]) -> _Function:
        """Read a function body once, where it is defined: each parameter stands for what a call
        gives it, and the writes whose paths hold one wait for a call (``_call_function``). Its
        names, functions and folder moves stay in it. Then the writes a call with no argument
        would make are made here (each whose parameters all have defaults nh reads, filled
        with them), as before; the rest wait for a call."""
        positional = [*args.posonlyargs, *args.args]
        params = [*positional, *args.kwonlyargs]
        given: dict[int, ast.expr] = {}
        first = len(positional) - len(args.defaults)
        for offset, default in enumerate(args.defaults):
            given[first + offset] = default
        for offset, keyword_default in enumerate(args.kw_defaults):
            if keyword_default is not None:
                given[len(positional) + offset] = keyword_default
        self.visit(list(given.values()))
        defaults = tuple(
            self.path_text(given[index]) if index in given else None for index in range(len(params))
        )
        frame = _Frame(next(_NUMBERS), self.cwd, defaults)
        saved = (dict(self.names), dict(self.functions), self.cwd, self.shell_cwd)
        for index, param in enumerate(params):
            self.names[param.arg] = network._Val(text=f"{_PARAM}{frame.number}.{index}\x03")
        for rest in (args.vararg, args.kwarg):
            if rest is not None:
                self.names.pop(rest.arg, None)
        self.frames.append(frame)
        try:
            walk()
        finally:
            self.frames.pop()
            self.names, self.functions, self.cwd, self.shell_cwd = saved
        function = _Function(
            frame.number,
            tuple(param.arg for param in positional),
            tuple(param.arg for param in params),
            defaults,
            tuple((path, where, removes) for (path, removes), where in frame.writes.items()),
        )
        for path, where, removes in _filled(function, list(defaults), known=True):
            self._write(where, path, removes=removes)
        return function

    def _call_function(self, function: _Function, node: ast.Call, where: str) -> None:
        """A call to a function the cell defines: its body's writes, with the call's arguments
        (by position or keyword, else the defaults; one nh can't read is unknown)."""
        values = list(function.defaults)
        for index, arg in enumerate(node.args):
            if isinstance(arg, ast.Starred):
                for rest in range(index, len(function.positional)):
                    values[rest] = None
                break
            if index < len(function.positional):
                values[index] = self.path_text(arg)
        if any(keyword.arg is None for keyword in node.keywords):  # **options: any of the rest
            for rest in range(len(node.args), len(values)):
                values[rest] = None
        for keyword in node.keywords:
            if keyword.arg in function.names:
                values[function.names.index(keyword.arg)] = self.path_text(keyword.value)
        for path, _, removes in _filled(function, values):
            self._write(where, path, removes=removes)

    def _context_chdir(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Call) and self.qual(node.func) in _CONTEXT_CHDIRS

    def _with_chdir(self, stmt: ast.With | ast.AsyncWith, definite: bool) -> None:
        """``with contextlib.chdir(path):``: the body runs in that folder; the one before it
        holds after."""
        before = self.cwd
        for item in stmt.items:
            self.visit([item.context_expr])
            if self._context_chdir(item.context_expr):
                assert isinstance(item.context_expr, ast.Call)
                self.cwd = _moved(self.cwd, self._argument(item.context_expr, 0, ("path",)))
            if item.optional_vars is not None:
                self.bind(item.optional_vars, self.eval(item.context_expr), definite)
        self.block(stmt.body, definite=definite)
        self.cwd = before

    # values ------------------------------------------------------------------------------------
    def eval(self, node: ast.AST | None) -> network._Val:
        if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call, ast.Attribute, ast.Subscript)):
            text = self.path_text(node)
            if text is not None:
                return network._Val(text=text)
        return super().eval(node)

    def path_text(self, node: ast.AST | None, depth: int = 0) -> str | None:
        """The path ``node`` holds: its literal text, with a hole for each part nh can't read;
        None when it isn't a path nh reads."""
        if node is None or depth > 20:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self.names.get(node.id, network._NOTHING).text
        if isinstance(node, ast.NamedExpr):  # (out := "/x")
            return self.path_text(node.value, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # Path("/x") / "y"
            left = self.path_text(node.left, depth + 1)
            right = self.path_text(node.right, depth + 1)
            if left is None and right is None:
                return None  # a division
            return _join(_or_hole(left), _or_hole(right))
        if isinstance(node, (ast.JoinedStr, ast.BinOp)):
            return self._rendered(node, depth)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            inner = self.path_text(node.value, depth + 1)
            return None if inner is None else _parent(inner)
        if isinstance(node, ast.Subscript):  # os.environ["HOME"]
            home = self.qual(node.value) == "os.environ" and _constant(node.slice) == "HOME"
            return "~" if home else None
        if isinstance(node, ast.Call):
            return self._call_path(node, depth)
        return None

    def _rendered(self, node: ast.AST, depth: int) -> str | None:
        """An f-string, ``+``, ``%``, ``.format`` or ``"/".join([…])`` as L012 renders it."""
        parts = self.render(node)
        if parts is None:
            return None
        return "".join(
            part if isinstance(part, str) else _or_hole(self.path_text(part, depth + 1))
            for part in parts
        )

    def _call_path(self, node: ast.Call, depth: int) -> str | None:
        func, args = node.func, node.args
        if any(isinstance(arg, ast.Starred) for arg in args):
            return None
        qual = self.qual(func)

        def texts(nodes: list[ast.expr]) -> list[str]:
            return [_or_hole(self.path_text(arg, depth + 1)) for arg in nodes]

        if qual in _PATH_TYPES:
            return _join_all(texts(args)) if args else "."
        if qual in _JOINS:
            return _join_all(texts(args)) if args else None
        if qual in _SAME and len(args) == 1 and not node.keywords:
            return self.path_text(args[0], depth + 1)
        if qual in _ABSOLUTE and len(args) == 1 and not node.keywords:
            inner = self.path_text(args[0], depth + 1)
            return None if inner is None else self._absolute(inner)
        if qual in _DIRNAMES and len(args) == 1:
            inner = self.path_text(args[0], depth + 1)
            return None if inner is None else _dirname(inner)
        if qual in _HOMES:
            return "~"
        if qual in _CWDS:
            return self._here()
        if qual in _HOME_READS and args and _constant(args[0]) == "HOME":
            return "~"
        if isinstance(func, ast.Attribute):
            method = func.attr
            if method in _SAME_METHODS and not args:
                return self.path_text(func.value, depth + 1)
            if method in _ABSOLUTE_METHODS and not args:
                inner = self.path_text(func.value, depth + 1)
                return None if inner is None else self._absolute(inner)
            if method in ("joinpath", "with_name", "with_suffix"):
                base = self.path_text(func.value, depth + 1)
                if base is None:
                    return None
                if method == "joinpath":
                    return _join_all([base, *texts(args)])
                if len(args) == 1:
                    [part] = texts(args)
                    if method == "with_name":
                        return _join(_parent(base), part)
                    return posixpath.splitext(base)[0] + part
                return None
        return self._rendered(node, depth)

    def _here(self) -> str:
        """The folder the kernel is in at this point of the cell, as a path."""
        if self.cwd is None:
            return _HERE
        if self.cwd.startswith(("~", HOLE)):
            return self.cwd
        return _join(_HERE, self.cwd)  # an absolute folder starts over

    def _absolute(self, path: str) -> str:
        """``os.path.abspath``, ``.resolve()``: a relative path from the folder the cell is in;
        ``~`` stays as written (6.4's ``~`` row)."""
        if path.startswith(("/", "~", HOLE, _HERE)) or network._SCHEME.match(path):
            return path
        return _join(self._here(), path)

    # calls -------------------------------------------------------------------------------------
    def call(self, node: ast.Call) -> None:
        func = node.func
        qual = self.qual(func)
        where = network._label(func)
        if qual in network._SHELL_CALLS:
            self._shell_call(node, where)
            return
        if isinstance(func, ast.Attribute) and self.eval(func.value).ipython:
            self._ipython(node, func.attr, where)
            return
        if qual in _CHDIRS:
            self.cwd = _moved(self.cwd, self._argument(node, 0, ("path",)))
            return
        if isinstance(func, ast.Name) and func.id in self.functions:
            self._call_function(self.functions[func.id], node, where)
            return
        for path, removes in self._sinks(node, qual):
            self._write(where, path, removes=removes)

    def _sinks(self, node: ast.Call, qual: str | None) -> list[tuple[str | None, bool]]:
        """The paths a call writes or removes (design §6.4's sink table)."""
        func = node.func
        if qual in _FUNCTIONS:
            return [
                (self._argument(node, position, keywords), removes)
                for position, keywords, removes in _FUNCTIONS[qual]
            ]
        if qual in _OPENS:
            writes = self._writes_mode(node, 1, archive=qual in _ARCHIVE_OPENS)
            return [(self._argument(node, 0, _OPEN_KEYWORDS), False)] if writes else []
        if isinstance(func, ast.Attribute):
            method = func.attr
        elif qual and "." in qual:  # from matplotlib.pyplot import savefig
            method = qual.rsplit(".", 1)[-1]
        else:
            return []
        if isinstance(func, ast.Attribute):
            if method in _PATH_WRITES or method in _PATH_REMOVES:
                return [(self.path_text(func.value), method in _PATH_REMOVES)]
            if method == "open":
                return [(self.path_text(func.value), False)] if self._writes_mode(node, 0) else []
            if method in _PATH_MOVES and len(node.args) + len(node.keywords) == 1:
                source = self.path_text(func.value)
                if source is None:
                    return []  # df.rename(…), a series' name: no path
                return [(self._argument(node, 0, ("target",)), False), (source, True)]
            if method in _SPARK_WRITES and _spark_writer(func.value):
                return [(self._argument(node, 0, ("path",)), False)]
        if method in _FRAME_WRITERS:
            return [(self._argument(node, 0, _FRAME_KEYWORDS), False)]
        if method in _SAVES:
            return [(self._argument(node, 0, _SAVE_KEYWORDS), False)]
        if method.startswith(_WRITE_PREFIXES) and method not in _NOT_FILE_WRITES:
            return [(self._argument(node, 0, _WRITE_KEYWORDS), False)]
        return []

    def _argument(self, node: ast.Call, position: int, keywords: tuple[str, ...]) -> str | None:
        """The path given at ``position`` (-1: none) or as one of ``keywords``."""
        found: ast.expr | None = None
        if 0 <= position < len(node.args) and not any(
            isinstance(arg, ast.Starred) for arg in node.args[: position + 1]
        ):
            found = node.args[position]
        else:
            found = next((k.value for k in node.keywords if k.arg in keywords), None)
        return self.path_text(found)

    def _writes_mode(self, node: ast.Call, position: int, *, archive: bool = False) -> bool:
        """A mode nh reads, holding ``w``, ``a``, ``x`` or ``+`` (an archive's: before its
        ``:`` or ``|``); no mode reads. A function's parameter reads as its default."""
        mode = node.args[position] if len(node.args) > position else _keyword(node, "mode")
        text = self.path_text(mode) if mode is not None else None
        if text is None:
            return False
        text = _MARKER.sub(self._default, text)
        if archive:
            text = _ARCHIVE_MODE.split(text, maxsplit=1)[0]
        return bool(set(text.replace(HOLE, "")) & _WRITE_MODES)

    def _default(self, found: re.Match[str]) -> str:
        """A parameter of a body being read, as its default (nothing when it has none)."""
        number, index = int(found.group(1)), int(found.group(2))
        frame = next((frame for frame in self.frames if frame.number == number), None)
        return (frame.defaults[index] or "") if frame is not None else ""

    def _write(
        self, where: str, path: str | None, *, removes: bool = False, cwd: str | None = _UNSET
    ) -> None:
        """Record a write of ``path``, joined to the folder the cell is in (``cwd``). In a
        function body, a path that holds a parameter waits for a call (``_Frame.writes``)."""
        if not path:
            return
        base = self.cwd if cwd == _UNSET else cwd
        if _MARKER.search(path):
            if self.frames:
                frame = self.frames[-1]
                if base != frame.cwd and base is not None:
                    path = _join(base, path)  # the body moved: from there, at any call
                frame.writes.setdefault((path, removes), where)
                return
            path = _MARKER.sub(HOLE, path)
        path = path.replace(_RESTART, "")
        if path.startswith(HOLE):
            return  # a path that starts unknown may be anywhere
        local = _local_path(path)
        if local is None:
            return
        base = self.cwd if cwd == _UNSET else cwd
        if base is not None and not local.startswith("~"):
            local = _join(base, local)  # an absolute path starts over
            if local.startswith(HOLE):
                return
        self.writes.append(Write(where, local, removes))

    def _ipython(self, node: ast.Call, method: str, where: str) -> None:
        """``run_cell_magic("writefile", "<path>", body)``; the rest as L012 reads them, a shell
        body starting in the kernel's folder."""
        writefile = len(node.args) >= 2 and _constant(node.args[0]) in _WRITEFILE
        if method == "run_cell_magic" and writefile:
            line = self.path_text(node.args[1])
            if line is not None:
                self._writefile(line, where)
            return
        self.shell_cwd = self.cwd  # a shell body starts in the kernel's folder
        super()._ipython(node, method, where)

    def _writefile(self, line: str, where: str) -> None:
        """``%%writefile [-a] <path>``: IPython fills ``{name}`` and ``$name`` first."""
        words = _split(line)
        path = next((word for word in words if not word.startswith("-")), None)
        if path is not None:
            self._write(where, self._expand(path, python=True))

    # magics and shell --------------------------------------------------------------------------
    def _line_magic(self, name: str, rest: str, where: str) -> None:
        if name in ("cd", "pushd"):
            self.cwd = self._cd(self.cwd, _split(rest), python=True)
        elif name == "popd":
            self.cwd = HOLE
        else:
            super()._line_magic(name, rest, where)

    def shell(self, text: str, *, python: bool, prefix: str, where: str = "") -> None:
        """A shell line: a ``!`` line or a shell call starts in the kernel's folder; a shell
        cell's lines share one folder."""
        cwd = self.cwd if python else self.shell_cwd
        outer: list[str | None] = []  # the folders a glued `(` saved, for its `)`
        for raws, words, outputs in shell_stages(text):
            raws, words, outputs, opens, closes = _subshell(raws, words, outputs)
            outer += [cwd] * opens
            cwd = self._shell_command(raws, words, outputs, python, prefix, where, cwd)
            for _ in range(closes):
                cwd = outer.pop() if outer else cwd
        if not python:
            self.shell_cwd = cwd

    def _shell_command(
        self,
        raws: list[str],
        words: list[str],
        outputs: list[str],
        python: bool,
        prefix: str,
        where: str,
        cwd: str | None,
    ) -> str | None:
        """One shell command's writes, its own paths before its redirections' (as written,
        mostly); returns the folder the next command runs in."""
        raws, words = network._without_env(raws, words)
        command = words[0].rsplit("/", 1)[-1].lower() if words else ""
        label = where or prefix + command
        redirect = where or (f"{prefix}{command} >" if command else f"{prefix}>")
        args = words[1:]
        after = cwd
        if command in ("cd", "pushd"):
            after = self._cd(cwd, args, python)
        elif command == "popd":
            after = HOLE
        elif command.startswith("python"):
            script = network._python_script(args)
            if script is not None:
                self.python(script, where or f"{prefix}{command} -c", cwd=cwd)
        else:
            for path, removes in _shell_targets(command, args):
                self._write(label, self._expand(path, python), removes=removes, cwd=cwd)
        for output in outputs:  # set up in the folder the command starts in
            self._write(redirect, self._expand(output, python), cwd=cwd)
        return after

    def _cd(self, cwd: str | None, args: list[str], python: bool) -> str:
        """The folder after ``cd``/``%cd``/``pushd`` with ``args``: home with none, unknown for
        ``-``, a bookmark or a history entry."""
        if "-b" in args or any(_HISTORY.fullmatch(arg) for arg in args):
            return HOLE
        plain = [arg for arg in args if arg == "-" or not arg.startswith("-")]
        if not plain:
            return "~"
        if plain[0] == "-":
            return HOLE
        return _moved(cwd, self._expand(plain[0], python))

    def _expand(self, word: str, python: bool) -> str:
        """A shell word as a path. In ``!`` lines IPython first fills ``{name}`` and ``$name``
        from Python names (``${name}`` keeps its ``$``: ``$/data``, a relative path); the shell
        then reads ``$HOME`` and ``${HOME}`` as ``~``. Any other name is unknown."""

        def fill(found: re.Match[str]) -> str:
            braced, field, name = found.group(1, 2, 3) if python else (None, None, found.group(1))
            key = braced or field or name or ""
            text = self.names.get(key, network._NOTHING).text if python else None
            if text is not None:
                return "$" + text if braced else text
            if field is None and key == "HOME":
                return "~"  # IPython leaves the line to the shell
            return HOLE

        return (_IPYTHON_FILLS if python else network._DOLLAR_NAME).sub(fill, word)


# helpers -----------------------------------------------------------------------------------------
def _shell_targets(command: str, args: list[str]) -> list[tuple[str, bool]]:
    """The paths one shell command writes (False) or removes (True), as written."""
    if command in _EACH_WRITES or command in _EACH_REMOVES:
        _, plain = _options(args, "", ())
        return [(path, command in _EACH_REMOVES) for path in plain]
    if command in _DESTINATIONS:
        targets, plain = _options(args, "t", ("--target-directory",), skip=("S", ("--suffix",)))
        if targets:
            destination: str | None = targets[-1]
            sources = plain
        elif len(plain) >= 2:
            destination, sources = plain[-1], plain[:-1]
        else:
            return []
        if command == "rsync" and destination and _remote(destination):
            destination = None  # a remote: L012's
        found = [(destination, False)] if destination else []
        if command == "mv":
            found += [(source, True) for source in sources]
        return found
    if command == "curl":
        values, _ = _options(args, "o", ("--output", "--output-dir"))
    elif command == "wget":
        values, _ = _options(args, "OP", ("--output-document", "--directory-prefix"))
    elif command == "dd":
        values = [arg[3:] for arg in args if arg.startswith("of=")]
    elif command == "zip":  # the archive, then -O's
        values, plain = _options(args, "O", ("--output-file",), skip=("bnPtZ", ("--temp-path",)))
        values = plain[:1] + values
    elif command in _NBCONVERTS or (command == "jupyter" and args[:1] == ["nbconvert"]):
        values, _ = _options(args, "", ("--output-dir",))
    else:
        return []
    return [(value, False) for value in values if value and value != "-"]


def _options(
    args: list[str],
    short: str,
    long: tuple[str, ...],
    skip: tuple[str, tuple[str, ...]] = ("", ()),
) -> tuple[list[str], list[str]]:
    """The values of the options ``short`` (letters) and ``long`` (``--name value`` or
    ``--name=value``), and the plain arguments left; the values of ``skip``'s short letters and
    long options are left out of both. Short options may be combined or glued (``-sLo out.csv``,
    ``-Oout.csv``)."""
    values: list[str] = []
    plain: list[str] = []
    index, rest = 0, False
    while index < len(args):
        arg = args[index]
        index += 1
        if rest or arg == "-" or not arg.startswith("-"):
            plain.append(arg)
        elif arg == "--":
            rest = True
        elif arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if name in long or name in skip[1]:
                if not equals:
                    if index >= len(args):
                        continue
                    value = args[index]
                    index += 1
                if name in long:
                    values.append(value)
        else:
            for at, letter in enumerate(arg[1:], 1):
                if letter in short or (skip[0] and letter in skip[0]):
                    value = arg[at + 1 :]
                    if not value and index < len(args):
                        value = args[index]
                        index += 1
                    if letter in short:
                        values.append(value)
                    break
    return values, plain


def _remote(word: str) -> bool:
    return "://" in word or bool(network._REMOTE_WORD.match(word))


def _subshell(
    raws: list[str], words: list[str], outputs: list[str]
) -> tuple[list[str], list[str], list[str], int, int]:
    """A command's words and redirection files without a subshell's ``(`` glued to its first
    word and ``)`` glued to its last word or file (outside quotes, beyond the word's own pairs:
    ``$(date)`` keeps its own), and how many of each it had."""
    raws, words, outputs = list(raws), list(words), list(outputs)
    opens = closes = 0
    if raws and raws[0].startswith("("):
        opens = len(raws[0]) - len(raws[0].lstrip("("))
        raws[0], words[0] = raws[0][opens:], words[0][opens:]
        if not raws[0]:
            raws, words = raws[1:], words[1:]
    if raws:
        closes = _unmatched_closes(raws[-1])
        if closes:
            raws[-1], words[-1] = raws[-1][:-closes], words[-1][:-closes]
            if not raws[-1]:
                raws, words = raws[:-1], words[:-1]
    if not closes and outputs:  # `(… > out.csv)`: the file came last
        closes = _unmatched_closes(outputs[-1])
        if closes:
            outputs[-1] = outputs[-1][:-closes]
    return raws, words, outputs, opens, closes


def _unmatched_closes(raw: str) -> int:
    """How many of the ``)`` that end ``raw`` close nothing opened in it, quotes aside."""
    depth = lowest = 0
    quote = ""
    for char in raw:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            lowest = min(lowest, depth)
    trailing = len(raw) - len(raw.rstrip(")"))
    return min(-lowest, trailing)


def _spark_writer(node: ast.expr) -> bool:
    """``sdf.write``, also after its settings (``.mode("overwrite")``, ``.option(…)``)."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SPARK_STEPS
    ):
        node = node.func.value
    return isinstance(node, ast.Attribute) and node.attr == "write"


def _filled(
    function: _Function, values: list[str | None], *, known: bool = False
) -> list[tuple[str, str, bool]]:
    """``function``'s writes with its parameters filled with ``values`` (None: unknown; with
    ``known``, a write that needs an unknown one is left out); the parameters of a function
    around it stay for its own call."""

    def fill(found: re.Match[str]) -> str:
        if int(found.group(1)) != function.number:
            return found.group(0)
        value = values[int(found.group(2))]
        return HOLE if value is None else value

    found = []
    for template, where, removes in function.writes:
        needed = [
            int(marker.group(2))
            for marker in _MARKER.finditer(template)
            if int(marker.group(1)) == function.number
        ]
        if known and any(values[index] is None for index in needed):
            continue
        path = _MARKER.sub(fill, template)
        starts = [match.start() for match in _RESTARTS_AT_ABSOLUTE.finditer(path)]
        if starts:  # a join given an absolute path starts over there
            path = path[starts[-1] + 1 :]
        found.append((_RESTART_LEFT.sub("", path), where, removes))
    return found


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _local_path(path: str) -> str | None:
    """A path on this machine: ``file://`` URLs as their path; any other URL is none."""
    scheme = network._SCHEME.match(path)
    if scheme is None:
        return path
    if scheme.group(1).lower() != "file":
        return None
    rest = path[scheme.end() :]
    return rest[len("localhost") :] if rest.startswith("localhost/") else rest


def _or_hole(text: str | None) -> str:
    return HOLE if text is None else text


def _join(base: str, path: str) -> str:
    """``os.path.join`` and ``Path /``: an absolute part starts over (a parameter, at a call
    that gives it one)."""
    if path.startswith(("/", _HERE)) or not base:
        return path
    if not path:
        return base
    if path.startswith(_PARAM):
        path = _RESTART + path
    return base + path if base.endswith("/") else f"{base}/{path}"


def _join_all(parts: list[str]) -> str:
    joined = parts[0]
    for part in parts[1:]:
        joined = _join(joined, part)
    return joined


def _parent(path: str) -> str:
    """``Path.parent``: the folder the path is in (``/`` is its own). Above the kernel's folder
    (``Path.cwd().parent``) and the home folder it climbs; a relative path's stays lexical, as
    pathlib's (``Path("..").parent`` is ``.``)."""
    for anchor in (_HERE, "~"):
        if path == anchor or path.startswith(anchor + "/"):
            rest = posixpath.normpath("." + path[len(anchor) :])
            if rest == "." or set(rest.split("/")) == {".."}:
                return f"{anchor}/{rest}/.." if rest != "." else f"{anchor}/.."
            folder = posixpath.dirname(rest)
            return f"{anchor}/{folder}" if folder else anchor
    stripped = path.rstrip("/")
    if not stripped:
        return path or "."
    folder = posixpath.dirname(stripped)
    if not folder and HOLE in stripped:
        return HOLE  # the folder of a path nh can't read
    return folder or "."


def _dirname(path: str) -> str:
    """``os.path.dirname``: as ``.parent`` from the kernel's and the home folder, else as
    ``posixpath`` (``dirname("a/")`` is ``a``)."""
    if path.startswith((_HERE, "~/")) or path == "~":
        return _parent(path)
    folder = posixpath.dirname(path)
    return HOLE if not folder and HOLE in path else folder


def _moved(cwd: str | None, target: str | None) -> str:
    """The folder after moving from ``cwd`` (None: the notebook's) to ``target``."""
    if not target or target.startswith(HOLE):
        return HOLE
    if cwd is None or target.startswith("~"):
        return target
    return _join(cwd, target)  # an absolute folder starts over


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _normalised(path: str) -> str:
    """``posixpath.normpath``, with a leading ``//`` (which it keeps) read as ``/``."""
    resolved = posixpath.normpath(path)
    return "/" + resolved.lstrip("/") if resolved.startswith("//") else resolved


def _may_be_under(pattern: str, folder: str) -> bool:
    """Whether a path ``pattern`` can stand for (each hole: text within one folder name) is
    ``folder`` or under it."""
    wanted = [name for name in folder.split("/") if name]
    parts = [part for part in pattern.split("/") if part]
    if len(parts) < len(wanted):
        return False
    return all(
        re.fullmatch("[^/]*".join(map(re.escape, part.split(HOLE))), name, re.S)
        for part, name in zip(parts, wanted, strict=False)
    )


def _constant(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == name), None)
