"""Names a cell defines and uses, and the later cells a change reaches (FR-6 stale marking, L120).

``defs`` over-approximates on purpose: besides bindings it counts names a statement may
mutate (``df["a"] = …``, ``model.fit(X)``, ``shuffle(items)``). Missing a stale cell is worse
than marking one too many.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache

from nh_gateway.lint.magics import Masked, lines_of, mask

EVERYTHING = "*"

# Cell magics whose body runs as Python in the user namespace.
NAMESPACE_CELL_MAGICS = frozenset({"time", "capture", "prun", "debug"})
_SCRIPT_OUTPUT = re.compile(r"--(?:out|err|proc)\s+([A-Za-z_]\w*)")
_OPEN_MAGIC = re.compile(r"^\s*%(?:run|store\s+-r)\b")
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
# A bare call statement may mutate the names it is given, except for these.
_READ_ONLY_CALL = re.compile(
    r"print|display|pprint|len|repr|type|help|dir|id|isinstance|issubclass|hash|sorted|sum|min"
    r"|max|abs|round"
)
_READ_ONLY_METHOD = re.compile(
    r"to_\w+|tolist|head|tail|sample|describe|info|show|plot|hist|boxplot|value_counts|n?unique"
    r"|count|sum|mean|median|mode|min|max|std|var|quantile|corr|cov|is(?:na|null)|not(?:na|null)"
    r"|any|all|copy|get|keys|values|items|n(?:largest|smallest)|memory_usage|savefig"
    r"|debug|warning|error|critical|exception|log|format"
)
_COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRY = (ast.Try, ast.TryStar)
_Scope = (
    ast.FunctionDef
    | ast.AsyncFunctionDef
    | ast.Lambda
    | ast.ClassDef
    | ast.ListComp
    | ast.SetComp
    | ast.GeneratorExp
    | ast.DictComp
)


@dataclass(frozen=True)
class Flow:
    defs: frozenset[str]  # bound or possibly mutated names (the contract's defs)
    uses: frozenset[str]  # names read before this cell binds them; {"*"} when unknown
    bound: frozenset[str]  # names the cell binds (assignment, import, def, ...), mutations excluded
    fresh: frozenset[str]  # names bound unconditionally at top level
    parsed: bool
    open: bool  # may bind names nh can't see: star import, %run, exec, unparsable code
    # /nh:review (design §6.10); ``uses`` above is what L120 and stale marking read.
    # Names read when the cell runs, before any binding of them earlier in the cell (a branch's
    # included): module level, class bodies, decorators, defaults, comprehensions.
    now: frozenset[str] = frozenset()
    # Free names of the cell's function, lambda and method bodies: read when called.
    later: frozenset[str] = frozenset()


_UNPARSED = Flow(
    frozenset(), frozenset({EVERYTHING}), frozenset(), frozenset(), False, True,
    frozenset({EVERYTHING}), frozenset(),
)  # fmt: skip


def defs_uses(source: str) -> tuple[set[str], set[str], bool]:
    flow = analyze(source)
    return set(flow.defs), set(flow.uses), flow.parsed


def downstream(cells: list[tuple[str, str]], start: int, tainted: set[str]) -> list[str]:
    """Ids of ``cells[start:]`` that read a tainted name; their defs become tainted in turn.

    A cell that rebinds a tainted name without reading it starts that name afresh.
    """
    tainted = set(tainted)
    hits: list[str] = []
    for cell_id, source in cells[max(start, 0) :]:
        if not tainted:
            break
        flow = analyze(source)
        if EVERYTHING in flow.uses or flow.uses & tainted:
            hits.append(cell_id)
            tainted |= flow.defs
        else:
            tainted -= flow.fresh
    return hits


def defined_names(sources: list[str]) -> set[str]:
    """Names bound by these cells (for L120); includes "*" when one may bind names nh can't see."""
    names: set[str] = set()
    for source in sources:
        flow = analyze(source)
        names |= flow.bound
        if flow.open:
            names.add(EVERYTHING)
    return names


@lru_cache(maxsize=2048)
def analyze(source: str) -> Flow:
    masked = mask(source)
    try:
        tree = ast.parse(masked.text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        tree = None
    return flow_of(source, masked, tree)


def flow_of(source: str, masked: Masked, tree: ast.Module | None) -> Flow:
    """Dataflow of a cell that is already masked and parsed (``tree`` is None when unparsable)."""
    lines = lines_of(source or "")
    magic = [lines[n - 1] for n in sorted(masked.magic_lines) if n <= len(lines)]
    open_magic = any(_OPEN_MAGIC.match(line) for line in magic)
    if masked.cell_magic is not None:
        return _cell_magic_flow(masked.cell_magic, magic[0] if magic else "", tree, open_magic)
    if tree is None:
        return _UNPARSED
    try:
        walker = _Walker()
        walker.run(tree)
    except RecursionError:
        return _UNPARSED
    return Flow(
        frozenset(walker.defs),
        frozenset(walker.uses),
        frozenset(walker.bound),
        frozenset(walker.fresh),
        True,
        walker.open or open_magic,
        frozenset(walker.now),
        frozenset(walker.deferred | walker.later),
    )


def _cell_magic_flow(name: str, header: str, tree: ast.Module | None, open_magic: bool) -> Flow:
    extra = set(_SCRIPT_OUTPUT.findall(header))
    if name == "capture":  # `%%capture [--no-stderr ...] out`
        args = [arg for arg in header.split()[1:] if not arg.startswith("-")]
        if args and _IDENTIFIER.match(args[0]):
            extra.add(args[0])
    defs, bound, is_open = set(extra), set(extra), open_magic
    if name in NAMESPACE_CELL_MAGICS:
        if tree is None:
            is_open = True
        else:
            try:
                walker = _Walker()
                walker.run(tree)
                defs |= walker.defs
                bound |= walker.bound
                is_open = is_open or walker.open
            except RecursionError:
                is_open = True
    return Flow(
        frozenset(defs), frozenset({EVERYTHING}), frozenset(bound), frozenset(), False, is_open,
        frozenset({EVERYTHING}), frozenset(),
    )  # fmt: skip


def base_name(node: ast.AST) -> str | None:
    """``x`` for ``x``, ``x.a``, ``x[0].b``; None when the chain starts elsewhere (a call, ...)."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _Walker:
    """Module-level pass: statements in order; nested scopes are summarised by ``_free``."""

    def __init__(self) -> None:
        self.defs: set[str] = set()
        self.uses: set[str] = set()
        self.bound: set[str] = set()
        self.fresh: set[str] = set()
        self.deferred: set[str] = set()  # free names of function bodies: read when called
        # a class's methods' and a comprehension's lambdas' free names: in Flow.later only, so
        # uses (L120, stale marking) stays as it was
        self.later: set[str] = set()
        self.now: set[str] = set()  # read before any binding of them in the cell (Flow.now)
        self.open = False

    def run(self, tree: ast.Module) -> None:
        self.block(tree.body, self.fresh)
        self.uses |= self.deferred - self.bound

    # names -------------------------------------------------------------------------------
    def bind(self, name: str, scope: set[str]) -> None:
        self.defs.add(name)
        self.bound.add(name)
        scope.add(name)

    def read(self, name: str, scope: set[str], now: bool = True) -> None:
        if name not in scope:
            self.uses.add(name)
            if now and name not in self.bound:
                self.now.add(name)

    def mutate(self, node: ast.AST) -> None:
        name = base_name(node)
        if name:
            self.defs.add(name)

    def nested(self, node: _Scope) -> None:
        free, global_defs, _, _ = _free(node)
        self.deferred |= free
        self.defs |= global_defs
        self.bound |= global_defs

    # statements --------------------------------------------------------------------------
    def block(self, stmts: list[ast.stmt], scope: set[str]) -> None:
        for stmt in stmts:
            self.stmt(stmt, scope)

    def branch(self, stmts: list[ast.stmt], scope: set[str]) -> None:
        # Names bound in a branch may not exist afterwards, so later reads still count as uses.
        self.block(stmts, set(scope))

    def stmt(self, node: ast.stmt, scope: set[str]) -> None:
        if isinstance(node, _FUNCS):
            self.exprs(_outer_parts(node), scope | _type_params(node))
            self.nested(node)
            self.bind(node.name, scope)
        elif isinstance(node, ast.ClassDef):
            self.exprs(_outer_parts(node), scope | _type_params(node))
            free, global_defs, _, later = _free(node)  # a class body runs now, its methods later
            for name in free:
                self.read(name, scope, now=name not in later)
            self.later |= later
            self.defs |= global_defs
            self.bound |= global_defs
            self.bind(node.name, scope)
        elif isinstance(node, ast.Assign):
            self.expr(node.value, scope)
            for target in node.targets:
                self.target(target, scope)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                self.read(node.target.id, scope)
            else:
                self.expr(node.target, scope)
            self.expr(node.value, scope)
            self.target(node.target, scope)
        elif isinstance(node, ast.AnnAssign):
            self.expr(node.annotation, scope)
            if node.value is not None:
                self.expr(node.value, scope)
                self.target(node.target, scope)
            elif not isinstance(node.target, ast.Name):
                self.expr(node.target, scope)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.expr(node.iter, scope)
            inner = set(scope)
            self.target(node.target, inner)
            self.block(node.body, inner)
            self.branch(node.orelse, scope)
        elif isinstance(node, (ast.While, ast.If)):
            self.expr(node.test, scope)
            self.branch(node.body, scope)
            self.branch(node.orelse, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self.expr(item.context_expr, scope)
                if item.optional_vars is not None:
                    self.target(item.optional_vars, scope)
            self.block(node.body, scope)
        elif isinstance(node, _TRY):
            self.branch(node.body, scope)
            for handler in node.handlers:
                if handler.type is not None:
                    self.expr(handler.type, scope)
                inner = set(scope)
                if handler.name:
                    inner.add(handler.name)  # deleted when the handler ends: not a def
                self.block(handler.body, inner)
            self.branch(node.orelse, scope)
            self.block(node.finalbody, scope)
        elif isinstance(node, ast.Match):
            self.expr(node.subject, scope)
            for case in node.cases:
                inner = set(scope)
                self.pattern(case.pattern, inner)
                if case.guard is not None:
                    self.expr(case.guard, inner)
                self.block(case.body, inner)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    self.open = True
                else:
                    self.bind(alias.asname or alias.name.split(".")[0], scope)
        elif isinstance(node, ast.Delete):
            for target in _flatten(node.targets):
                if isinstance(target, ast.Name):
                    self.read(target.id, scope)
                else:
                    self.expr(target, scope)
                self.mutate(target)
        elif isinstance(node, ast.Expr):
            self.expr(node.value, scope)
            call = node.value.value if isinstance(node.value, ast.Await) else node.value
            if isinstance(call, ast.Call):
                self.call_statement(call)
        elif not isinstance(node, (ast.Global, ast.Nonlocal)):
            # return, raise, assert, pass, `type X = ...` (its value read now: conservative),
            # and statements future Pythons add
            self.expr(node, scope)

    def call_statement(self, call: ast.Call) -> None:
        """``x.m(...)`` may change ``x``; a bare ``f(a, b)`` may change ``a`` and ``b``."""
        func = call.func
        if isinstance(func, ast.Name):
            if _READ_ONLY_CALL.fullmatch(func.id):
                return
        elif isinstance(func, ast.Attribute):
            if _READ_ONLY_METHOD.fullmatch(func.attr):
                return
            self.mutate(func.value)
        else:
            return
        for arg in [*call.args, *(k.value for k in call.keywords)]:
            if isinstance(arg, ast.Starred):
                arg = arg.value
            if isinstance(arg, ast.Name):
                self.defs.add(arg.id)

    def target(self, node: ast.AST, scope: set[str]) -> None:
        if isinstance(node, ast.Name):
            self.bind(node.id, scope)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                self.target(element, scope)
        elif isinstance(node, ast.Starred):
            self.target(node.value, scope)
        else:  # x[...] = / x.attr =
            self.expr(node, scope)
            self.mutate(node)

    def pattern(self, pattern: ast.AST, scope: set[str]) -> None:
        for node in ast.walk(pattern):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                self.bind(node.name, scope)
            elif isinstance(node, ast.MatchMapping):
                self.exprs(node.keys, scope)
                if node.rest:
                    self.bind(node.rest, scope)
            elif isinstance(node, ast.MatchValue):
                self.expr(node.value, scope)
            elif isinstance(node, ast.MatchClass):
                self.expr(node.cls, scope)

    # expressions -------------------------------------------------------------------------
    def exprs(self, nodes: list, scope: set[str]) -> None:
        for node in nodes:
            if node is not None:
                self.expr(node, scope)

    def expr(self, root: ast.AST, scope: set[str]) -> None:
        # Iterative pre-order walk: long operator chains would overflow a recursive one.
        stack: list[ast.AST | str] = [root]
        while stack:
            node = stack.pop()
            if isinstance(node, str):  # deferred walrus binding
                self.bind(node, scope)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    self.read(node.id, scope)
                else:
                    self.bind(node.id, scope)
            elif isinstance(node, ast.NamedExpr):
                stack.append(node.target.id)
                stack.append(node.value)
            elif isinstance(node, ast.Lambda):
                self.exprs(_outer_parts(node), scope)
                self.nested(node)
            elif isinstance(node, _COMPS):
                self.exprs(_outer_parts(node), scope)
                free, global_defs, escaped, later = _free(node)
                for name in escaped:  # walrus inside a comprehension binds out here
                    self.bind(name, scope)
                for name in free:
                    self.read(name, scope, now=name not in later)
                self.later |= later
                self.defs |= global_defs
            else:
                if isinstance(node, ast.Call):
                    self.special_call(node)
                stack.extend(reversed(list(ast.iter_child_nodes(node))))

    def special_call(self, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Name) and func.id in ("exec", "globals"):
            self.open = True
        if isinstance(func, ast.Attribute) and any(
            k.arg == "inplace" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in call.keywords
        ):
            self.mutate(func.value)


def _flatten(targets: list[ast.expr]) -> list[ast.expr]:
    """``del a, (b, c)`` -> [a, b, c]."""
    out: list[ast.expr] = []
    stack = list(reversed(targets))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Tuple, ast.List)):
            stack.extend(reversed(node.elts))
        else:
            out.append(node)
    return out


def _signature_parts(args: ast.arguments) -> list[ast.AST]:
    """Defaults and annotations: evaluated where the function is defined."""
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    notes = [p.annotation for p in params if p is not None and p.annotation is not None]
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None), *notes]


def _outer_parts(node: _Scope) -> list[ast.AST]:
    """The parts of a nested scope evaluated where it is defined."""
    if isinstance(node, _FUNCS):
        returns = [node.returns] if node.returns else []
        return [*node.decorator_list, *_signature_parts(node.args), *returns]
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
    if isinstance(node, ast.Lambda):
        return _signature_parts(node.args)
    return [node.generators[0].iter]


def _type_params(node: _Scope) -> set[str]:
    """``T`` in ``def f[T](x: T)`` (3.12+): local to the definition and its annotations."""
    return {p.name for p in getattr(node, "type_params", ())}


def _param_names(args: ast.arguments) -> set[str]:
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {p.arg for p in params if p is not None}


def _free(scope: _Scope) -> tuple[set[str], set[str], set[str], set[str]]:
    """(free names, names assigned through ``global``, walrus names escaping a comprehension, the
    free names only a nested function or lambda body reads: read when it is called)."""
    local: set[str] = set()
    loads: set[str] = set()
    now: set[str] = set()  # loads when the scope itself runs
    declared: set[str] = set()
    global_defs: set[str] = set()
    escaped: set[str] = set()
    is_comp = isinstance(scope, _COMPS)
    body: list[ast.AST]
    if isinstance(scope, (*_FUNCS, ast.Lambda)):
        local |= _param_names(scope.args)
        body = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    elif isinstance(scope, ast.ClassDef):
        body = list(scope.body)
    else:
        body = [scope.key, scope.value] if isinstance(scope, ast.DictComp) else [scope.elt]
        for index, gen in enumerate(scope.generators):
            body += [gen.target, *gen.ifs]
            if index:
                body.append(gen.iter)
    local |= _type_params(scope)

    stack = body
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loads.add(node.id)
                now.add(node.id)
            else:
                local.add(node.id)
            continue
        if isinstance(node, (*_FUNCS, ast.ClassDef, ast.Lambda, *_COMPS)):
            if isinstance(node, (*_FUNCS, ast.ClassDef)):
                local.add(node.name)
            stack.extend(_outer_parts(node))
            inner_free, inner_globals, inner_escaped, inner_later = _free(node)
            loads |= inner_free
            if not isinstance(
                node, (*_FUNCS, ast.Lambda)
            ):  # a class body or comprehension runs now
                now |= inner_free - inner_later
            global_defs |= inner_globals
            (escaped if is_comp else local).update(inner_escaped)
            continue
        if isinstance(node, ast.NamedExpr) and is_comp:
            escaped.add(node.target.id)
            stack.append(node.value)
            continue
        if isinstance(node, ast.Global):
            declared.update(node.names)
            global_defs.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            declared.update(node.names)
        elif isinstance(node, ast.alias) and node.name != "*":
            local.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            local.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            local.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    local -= declared
    return loads - local, global_defs, escaped, (loads - now) - local
