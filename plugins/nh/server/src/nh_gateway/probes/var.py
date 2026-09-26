# nh probe "var": one variable in detail. Runs after vars.py (loaded as a library, which defines
# _summarize) inside the same throwaway globals, so nothing is bound in the user namespace.
# Stdlib only; Python 3.10+. `name` must be an identifier: nothing is evaluated.
import keyword
import sys

_long = _SafeRepr()  # noqa: F821 -- from vars.py: never calls a user-defined __repr__
_long.maxstring = 400
_long.maxother = 400
_long.maxlist = 20
_long.maxtuple = 20
_long.maxdict = 20
_long.maxset = 20


def _head(value, rows):
    pd = sys.modules.get("pandas")
    pl = sys.modules.get("polars")
    np = sys.modules.get("numpy")
    if pd is not None and isinstance(value, pd.DataFrame):
        return value.head(rows).to_string(max_cols=20, max_colwidth=40)
    if pd is not None and isinstance(value, pd.Series):
        return value.head(rows).to_string(max_rows=rows)
    if pl is not None and isinstance(value, pl.DataFrame):
        with pl.Config(tbl_rows=rows, tbl_cols=20):
            return repr(value.head(rows))
    if np is not None and isinstance(value, np.ndarray):
        shown = value[:rows] if value.ndim else value
        return np.array2string(shown, threshold=400, edgeitems=5)
    return None


def _var_payload(args):
    name = args.get("name")
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        return {"error": f"ValueError: {name!r} is not a variable name"}
    rows = max(0, min(_number(args, "rows", 5), 20))  # noqa: F821 -- from vars.py
    namespace = get_ipython().user_ns  # noqa: F821 -- an IPython builtin while a cell runs
    if name not in namespace:
        return {"error": f"NameError: name {name!r} is not defined"}
    value = namespace[name]
    summary = _summarize(value, _number(args, "max_cells", 20_000_000))  # noqa: F821 -- from vars.py
    head = _head(value, rows)
    return {
        "name": name,
        "summary": summary,
        "head": head or "",
        "text": "" if head is not None else _long.repr(value),
    }


try:
    _payload = _var_payload(_A)  # noqa: F821 -- _A holds the probe arguments
except Exception as _exc:  # a probe always answers, even when it fails
    _payload = {"error": f"{type(_exc).__name__}: {_exc}"}

display({"application/vnd.nh.probe+json": _payload}, raw=True)  # noqa: F821
