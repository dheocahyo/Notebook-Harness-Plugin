# nh probe "vars": a summary of the user's variables. Runs inside the user's kernel via exec() with
# throwaway globals, so nothing is bound in the user namespace. Stdlib only; Python 3.10+.
# pandas, polars and numpy are only reached through sys.modules, never imported.
# The "var" probe runs this file first with _A["_as_library"] set, to reuse _summarize().
# A look must never run user code: len() and repr() are called only on builtins and on types from
# a few well-known libraries. Anything else is reported by its type alone (a lazy frame's __len__
# may compute the whole thing).
import importlib.metadata
import importlib.util
import reprlib
import sys
import time
import types
import warnings
import zlib

_PACKAGES = {
    "pandas": "pandas",
    "numpy": "numpy",
    "polars": "polars",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "sklearn": "scikit-learn",
    "scipy": "scipy",
    "statsmodels": "statsmodels",
    "pyarrow": "pyarrow",
    "plotly": "plotly",
}
_HIDDEN = {"In", "Out", "exit", "quit", "get_ipython"}
_SCALARS = (bool, int, float, complex, str, bytes, type(None))
_CONTAINERS = (list, tuple, dict, set, frozenset)
_CALLABLES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, type)
# Libraries whose own types have cheap, side-effect-free reprs (checked by the type's module).
_TRUSTED = {
    "builtins",
    "datetime",
    "decimal",
    "fractions",
    "pathlib",
    "uuid",
    "numpy",
    "pandas",
    "polars",
    "pyarrow",
    "sklearn",
    "scipy",
    "statsmodels",
    "matplotlib",
}


def _module_of(value):
    return str(getattr(type(value), "__module__", "") or "")


def _trusted(value):
    return _module_of(value).split(".")[0] in _TRUSTED


class _SafeRepr(reprlib.Repr):
    """reprlib that never calls a user-defined __repr__ (containers recurse into it too)."""

    def repr1(self, x, level):
        if _module_of(x) != "builtins":  # a user class named "list" must not reach repr_list
            return self.repr_instance(x, level)
        return super().repr1(x, level)

    def repr_instance(self, x, level):
        for base in (bool, int, float, complex):  # a number (True, not 1): its value, via C
            if isinstance(x, base):
                return base.__repr__(x)[: self.maxother]
        for base in (str, bytes):  # a subclass of text: a C-level slice of it
            if isinstance(x, base):
                return self.repr1(base.__getitem__(x, slice(0, self.maxstring + 1)), level)
        if not _trusted(x):
            return f"<{_type_name(x)} object>"
        return super().repr_instance(x, level)


_short = _SafeRepr()
_short.maxstring = 80
_short.maxother = 80


def _type_name(value):
    kind = type(value)
    module = getattr(kind, "__module__", "")
    return kind.__qualname__ if module == "builtins" else f"{module}.{kind.__qualname__}"


def _length(value):
    """len() through the builtin base type's own C implementation, never a user's __len__."""
    for base in _CONTAINERS:
        if isinstance(value, base):
            return base.__len__(value)
    return None


def _top_nulls(counts):
    nonzero = [(str(name), int(n)) for name, n in counts if int(n) > 0]
    nonzero.sort(key=lambda item: -item[1])
    return dict(nonzero[:10])


def _checksum(sums):
    """A short fingerprint of a frame's or series' numeric values, or None. It lets the self-check
    say "values changed" when shape and nulls did not. ``sums`` computes the column sums; repr()
    keeps every digit (and NaN == NaN) and only sees builtin and numpy numbers."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            numbers = [x for x in sums() if _trusted(x)]
        return format(zlib.crc32(repr(numbers).encode()), "08x")
    except Exception:
        return None


def _summarize(value, max_cells):
    pd = sys.modules.get("pandas")
    pl = sys.modules.get("polars")
    np = sys.modules.get("numpy")
    if pd is not None and isinstance(value, pd.DataFrame):
        rows, cols = value.shape
        info = {
            "kind": "DataFrame",
            "lib": "pandas",
            "shape": [int(rows), int(cols)],
            "columns": [str(c) for c in list(value.columns[:30])],
            "dtypes": {str(c): str(t) for c, t in list(value.dtypes.items())[:30]},
            "mem": int(value.memory_usage(index=True, deep=False).sum()),
        }
        if int(rows) * int(cols) <= max_cells:
            info["nulls"] = _top_nulls(value.isna().sum().items())
            fingerprint = _checksum(lambda: value.sum(numeric_only=True).tolist())
            if fingerprint is not None:
                info["sums"] = fingerprint
        return info
    if pd is not None and isinstance(value, pd.Series):
        info = {
            "kind": "Series",
            "lib": "pandas",
            "len": int(len(value)),
            "dtype": str(value.dtype),
            "name": None if value.name is None else str(value.name),
        }
        if len(value) <= max_cells:
            info["nulls"] = int(value.isna().sum())
            if getattr(value.dtype, "kind", "O") in ("b", "i", "u", "f", "c"):
                fingerprint = _checksum(lambda: [value.sum()])
                if fingerprint is not None:
                    info["sums"] = fingerprint
        return info
    if pl is not None and isinstance(value, pl.DataFrame):
        rows, cols = value.shape
        info = {
            "kind": "DataFrame",
            "lib": "polars",
            "shape": [int(rows), int(cols)],
            "columns": [str(c) for c in value.columns[:30]],
            "dtypes": {str(c): str(t) for c, t in list(value.schema.items())[:30]},
            "mem": int(value.estimated_size()),
        }
        if int(rows) * int(cols) <= max_cells:
            info["nulls"] = _top_nulls(value.null_count().row(0, named=True).items())
        return info
    if pl is not None and isinstance(value, pl.LazyFrame):
        return {"kind": "LazyFrame", "lib": "polars"}
    if np is not None and isinstance(value, np.ndarray):
        return {
            "kind": "ndarray",
            "shape": [int(n) for n in value.shape],
            "dtype": str(value.dtype),
        }
    if isinstance(value, _SCALARS) or (np is not None and isinstance(value, np.generic)):
        return {"kind": "scalar", "type": _type_name(value), "repr": _short.repr(value)}
    if isinstance(value, _CONTAINERS):
        return {"kind": "container", "type": _type_name(value), "len": _length(value)}
    if isinstance(value, types.ModuleType):
        version = getattr(value, "__version__", None)
        return {
            "kind": "module",
            "type": value.__name__,
            "version": None if version is None else str(version),
        }
    if isinstance(value, type):
        return {"kind": "class", "type": f"{value.__module__}.{value.__qualname__}"}
    if isinstance(value, _CALLABLES):
        return {"kind": "function", "type": getattr(value, "__qualname__", _type_name(value))}
    return {"kind": "object", "type": _type_name(value)}  # no len(): it may run user code


def _is_data(name, value, hidden):
    if name.startswith("_") or name in _HIDDEN:
        return False
    if name in hidden and hidden[name] is value:
        return False
    return not isinstance(value, (types.ModuleType,) + _CALLABLES)


def _packages():
    found = {}
    for module, dist in _PACKAGES.items():
        if module in sys.modules:
            try:
                found[module] = importlib.metadata.version(dist)
            except Exception:
                version = getattr(sys.modules[module], "__version__", None)
                found[module] = None if version is None else str(version)
        else:
            try:
                installed = importlib.util.find_spec(module) is not None
            except Exception:
                installed = False
            if installed:
                found[module] = None
    return found


def _number(args, key, default):
    value = args.get(key)
    return default if value is None else type(default)(value)


def _vars_payload(args):
    started = time.monotonic()
    budget = _number(args, "budget_s", 0.8)
    max_vars = _number(args, "max_vars", 40)
    max_cells = _number(args, "max_cells", 20_000_000)
    shell = get_ipython()  # noqa: F821 -- an IPython builtin while a cell runs
    namespace = shell.user_ns
    hidden = getattr(shell, "user_ns_hidden", {}) or {}
    wanted = args.get("names")
    names = (
        list(namespace)
        if wanted is None
        else [n for n in wanted if isinstance(n, str) and n in namespace]
    )
    result = {}
    truncated = False
    for name in names:
        value = namespace.get(name)
        if not _is_data(name, value, hidden):
            continue
        if len(result) >= max_vars or time.monotonic() - started > budget:
            truncated = True
            break
        try:
            result[name] = _summarize(value, max_cells)
        except Exception as exc:
            result[name] = {
                "kind": "object",
                "type": _type_name(value),
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {"vars": result, "truncated": truncated, "packages": _packages()}


if not _A.get("_as_library"):  # noqa: F821 -- _A holds the probe arguments
    try:
        _payload = _vars_payload(_A)  # noqa: F821
    except Exception as _exc:  # a probe always answers, even when it fails
        _payload = {"error": f"{type(_exc).__name__}: {_exc}"}
    display({"application/vnd.nh.probe+json": _payload}, raw=True)  # noqa: F821
