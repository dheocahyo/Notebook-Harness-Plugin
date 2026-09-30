"""Hints (L014, L101–L123): advisory by default, errors in strict mode, off per rule."""

from __future__ import annotations

from typing import Any

import pytest

from nh_gateway.config import Config, load
from nh_gateway.lint import lint as lint_module
from nh_gateway.lint import secret_scan
from nh_gateway.lint.lint import LintReport, lint_cell
from tests.unit.test_lint_hard import EVERYDAY, scan_returns

NOTE = {
    "title": "Summarise prices by region",
    "notes": ["Groups the clean rows by region.", "Shows the median price per region."],
    "intent": "which regions are expensive",
}


def lint(code: str, cfg: Config | None = None, **overrides: Any) -> LintReport:
    args: dict[str, Any] = {
        **NOTE,
        "cfg": cfg or load(None),
        "require_note": True,
        "require_intent": True,
        "kernel_python": None,
        "names_above": None,
    }
    args.update(overrides)
    return lint_cell(code, **args)


def hints(code: str, **overrides: Any) -> dict[str, str]:
    report = lint(code, **overrides)
    assert report.errors == [], [e.message for e in report.errors]
    return {h.rule: h.message for h in report.hints}


def lines(*parts: str) -> str:
    return "\n".join(parts)


# (rule, cell that should get the hint, similar cell that should not)
CASES = [
    ("L014", "print(api_key)", "print(bool(api_key))"),
    (
        "L101",
        "label_total = '" + "x" * 100 + "'\nlabel_total",
        "label_total = '" + "x" * 80 + "'\nlabel_total",
    ),
    (
        "L102",
        lines(*[f"value_{i} = {i}" for i in range(41)], "value_0"),
        lines(*[f"value_{i} = {i}" for i in range(39)], "", "", "value_0"),
    ),
    (
        "L103",
        lines(
            "for region in regions:",
            "    for row in rows:",
            "        if row.ok:",
            "            if row.price:",
            "                print(row)",
        ),
        lines(
            "for region in regions:",
            "    for row in rows:",
            "        if row.ok and row.price:",
            "            print(row)",
        ),
    ),
    (
        "L103",
        "matrix = [[cell for cell in row] for row in rows]\nmatrix",
        "flat = [cell for row in rows for cell in row]\nflat",
    ),
    (
        "L104",
        lines(
            "summary = (",
            "    sales.dropna()",
            "    .groupby('region')",
            "    .agg('median')",
            "    .reset_index()",
            "    .sort_values('price')",
            ")",
            "summary",
        ),
        lines(
            "summary = (",
            "    sales.dropna()",
            "    .groupby('region')",
            "    .agg('median')",
            "    .reset_index()",
            ")",
            "summary",
        ),
    ),
    (
        "L105",
        lines("# one", "# two", *[f"value_{i} = {i}" for i in range(10)], "value_0"),
        lines("# one", "# two", *[f"value_{i} = {i}" for i in range(15)], "value_0"),
    ),
    (
        "L106",
        "# sales = sales.dropna()\nmedians = sales.median()\nmedians",
        "# Median is robust to the outliers in price\nmedians = sales.median()\nmedians",
    ),
    (
        "L106",
        "# for row in rows:\n#     print(row)\nmedians = sales.median()\nmedians",
        "# Step (1) of the check\nmedians = sales.median()\nmedians",
    ),
    ("L107", "count_a = 1; count_b = 2\ncount_a", "count_a = 1\ncount_b = 2\nplt.plot(count_a);"),
    ("L107", "if ready: show_table()\nready", "if ready:\n    show_table()\nready"),
    ("L108", "from helpers import *\nclean_table", "from helpers import clean\nclean"),
    (
        "L109",
        lines("try:", "    price = float(raw)", "except:", "    price = None", "price"),
        lines("try:", "    price = float(raw)", "except ValueError:", "    price = None", "price"),
    ),
    (
        "L111",
        "sales['price'] = sales['price'] / 100\nsales.head()",
        "sales_eur = sales.assign(price=sales['price'] / 100)\nsales_eur.head()",
    ),
    (
        "L111",
        "sales.dropna(inplace=True)\nsales.shape",
        "sales_clean = sales.dropna()\nsales_clean.shape",
    ),
    (
        "L111",
        "total_rows += len(batch)\ntotal_rows",
        lines(
            "total_rows = 0", "for batch in batches:", "    total_rows += len(batch)", "total_rows"
        ),
    ),
    ("L111", "results.append(score)\nresults", "results = [score]\nresults"),
    (
        "L111",
        "sales = sales.dropna()\nsales.shape",
        "sales = load_sales()\nsales = sales.dropna()\nsales.shape",
    ),
    (
        "L112",
        "sales['total'] = sales.apply(lambda r: r.price * r.qty, axis=1)\nsales.head()",
        "sales['name'] = sales['name'].apply(lambda s: s.strip())\nsales.head()",
    ),
    (
        "L112",
        "band = np.where(p > 9, 'high', np.where(p > 3, 'mid', 'low'))\nband",
        "band = np.where(p > 9, 'high', 'low')\nband",
    ),
    ("L113", "df2 = sales.copy()\ndf2.head()", "sales_copy = sales.copy()\nsales_copy.head()"),
    ("L113", "n = len(sales)\nn", "for i in range(3):\n    print(i)"),
    (
        "L114",
        "def to_eur(price):\n    return price / 100\nsales['eur'] = to_eur(sales['price'])\nsales",
        lines(
            "def to_eur(price):",
            "    return price / 100",
            "low_eur = to_eur(low)",
            "high_eur = to_eur(high)",
            "low_eur, high_eur",
        ),
    ),
    ("L116", "sales_clean = sales.dropna()", "sales_clean = sales.dropna()\nsales_clean.shape"),
    (
        "L116",
        "sales_clean = sales.dropna()\nsales_clean.head();",
        "import pandas as pd\nPRICE_CAP = 5_000",
    ),
    ("L117", "print(a_count)\nprint(b_count)\nprint(c_count)", "print(a_count)\nprint(b_count)"),
    (
        "L117",
        lines("print(sales.shape)", "sales.plot()", "sales.describe()"),
        lines(
            "fig, ax = plt.subplots()", "sales.plot(ax=ax)", "ax.set_title('Price')", "plt.show()"
        ),
    ),
    (
        "L118",
        "import warnings\nwarnings.filterwarnings('ignore')\nsales.head()",
        "import warnings\nwarnings.filterwarnings('default')\nsales.head()",
    ),
    (
        "L118",
        lines("try:", "    price = float(raw)", "except ValueError:", "    pass", "raw"),
        lines("try:", "    price = float(raw)", "except ValueError:", "    price = None", "price"),
    ),
    ("L118", "%%capture\nsales.plot()", "%%time\nsales.plot()"),
    (
        "L118",
        "pd.options.mode.chained_assignment = None\nsales.head()",
        "pd.options.display.max_rows = 20\nsales.head()",
    ),
    (
        "L119",
        "print('The median price is much higher in the north region, which suggests the "
        "premium stores skew the average')",
        "print(f'median price: {median_price:,.0f}')",
    ),
    (
        "L119",
        "print(f'The dataset has {n_rows} rows and it looks like most of them come from the north "
        "region stores')",
        "print('rows:', n_rows)",
    ),
]


@pytest.mark.parametrize(
    ("rule", "positive", "negative"), CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)]
)
def test_hint_fires_on_positive_only(rule: str, positive: str, negative: str) -> None:
    assert rule in hints(positive)
    assert rule not in hints(negative)


# L014 -------------------------------------------------------------------------------------------
# Design §6.7's L014 word lists, spelled out: a word taken out of secret_scan.py fails a row.
TOKEN_QUALIFIERS = [
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
    "jwt",
    "bot",
    "hf",
    "github",
    "gh",
    "gitlab",
    "slack",
    "client",
    "private",
    "personal",
    "app",
    "service",
    "security",
]
TOKEN_WORDS = [
    "bos",
    "eos",
    "pad",
    "unk",
    "sep",
    "cls",
    "mask",
    "special",
    "start",
    "end",
    "stop",
    "next",
    "last",
    "first",
    "prev",
    "new",
    "current",
]
NOT_THE_SECRET_LAST = [
    "counts",
    "num",
    "freq",
    "freqs",
    "frequency",
    "usage",
    "limit",
    "limits",
    "budget",
    "lengths",
    "sizes",
    "prob",
    "probs",
    "logprob",
    "logprobs",
    "logit",
    "logits",
    "score",
    "scores",
    "embedding",
    "embeddings",
    "emb",
    "type",
    "types",
    "list",
    "df",
    "hash",
    "digest",
    "policy",
    "env",
    "var",
    "vars",
    "names",
    "strength",
    "field",
    "prompt",
    "pattern",
]
# (cell, the name L014 reports)
L014_SHOWN = [
    ('api_key = "sk-placeholder"\nprint(api_key)', "api_key"),
    ("print(db_password)", "db_password"),
    ('print(f"password: {db_password}")', "db_password"),
    ('print("token=%s" % hf_token)', "hf_token"),
    ('print("key: " + OPENAI_API_KEY)', "OPENAI_API_KEY"),
    ("config.OPENAI_API_KEY", "config.OPENAI_API_KEY"),
    ("settings.client_secret", "settings.client_secret"),
    ("display(credentials)", "credentials"),
    ("pprint(private_key)", "private_key"),
    ("print(api_key.strip())", "api_key"),
    ("print(str(access_token))", "access_token"),
    ("print(api_key[:4])", "api_key"),
    ("print(api_key_prefix)", "api_key_prefix"),
    ("print([github_token, 'x'])", "github_token"),
    ("print(api_key or 'none')", "api_key"),
    ("logger.info('token %s', session_token)", "session_token"),
    ("import sys\nsys.stdout.write(secret_key)", "secret_key"),
    ("raise ValueError(api_token)", "api_token"),
    ("!echo $db_password", "db_password"),
    ("!echo {api_key}", "api_key"),
    ("def show():\n    print(api_key)", "api_key"),
    ("print(has_rows and api_key)", "api_key"),  # `a and b` shows b
    ("print({api_key: 1})", "api_key"),  # a dict key that isn't a literal
    ("print(masked_key)", "masked_key"),  # a masked preview still shows part of a key
    ("print(redacted_key)", "redacted_key"),
    ("print(db_pwd)", "db_pwd"),
    ("print(secret_word)", "secret_word"),
    ("print(new_password)", "new_password"),  # a token word without `token` still fires
    # the same expression rules as L011
    ("print(api_key if ready else None)", "api_key"),
    ("print(-api_key)", "api_key"),
    ("print((shown := api_key))", "api_key"),
    ("print(*api_key)", "api_key"),
    ("print([api_key for _ in range(2)])", "api_key"),
    ("print({api_key for _ in range(2)})", "api_key"),
    ("print(format(api_key))", "api_key"),
    ("print(next(api_key))", "api_key"),
    ("print(dict(api_key))", "api_key"),
    ('print("".join(api_key))', "api_key"),
    ("print(api_key + suffix)", "api_key"),
    ("def show(v):\n    print(v)\nshow(api_key)", "api_key"),  # a function that shows it
    ("print(get_cfg().api_key)", "api_key"),  # an attribute of a call
    ("print(secret_token)", "secret_token"),  # a secret word of its own
    # `token` as a credential: a qualifier, another secret word, an env var's capitals
    *((f"print({word}_token)", f"{word}_token") for word in TOKEN_QUALIFIERS),
    ("print(accessToken)", "accessToken"),
    ("print(password_token)", "password_token"),
    ("print(TOKEN)", "TOKEN"),
    ("print(MY_TOKEN)", "MY_TOKEN"),
    ("print(cfg.HF_TOKEN)", "cfg.HF_TOKEN"),
]


@pytest.mark.parametrize(("code", "name"), L014_SHOWN)
def test_l014_shown_secret_names(code: str, name: str) -> None:
    report = lint(code)
    assert [h.rule for h in report.hints if h.rule in ("L011", "L014")] == ["L014"]
    [hint] = [h for h in report.hints if h.rule == "L014"]
    assert hint.key == "secret_name" and hint.severity == "hint"
    assert f"`{name}`, whose name says it holds a secret" in hint.message
    assert hint.fix == (
        f"Show whether it is set instead, e.g. `print(bool({name}))`, or leave it out of the output."
    )


L014_CLEAN = [
    "print(tokens[:10])",
    "tokens",
    "print(tokenizer)",
    "tokenizer.decode(ids)",
    "print(author)",
    "print(df.author.value_counts())",
    "print(max_tokens)",
    "print(f'{max_tokens=}')",
    "print(token_count)",
    "print(n_tokens)",
    "print(token_ids)",
    "SORT_KEY",
    "print(sort_key, primary_key, cache_key)",
    "print(tok.eos_token)",
    "print(tokenizer.pad_token, tokenizer.bos_token)",
    'print(df["token"].head())',
    'df["password"].isna().sum()',
    "print(len(api_key))",
    "print(api_key is None)",
    "print(bool(token))",
    "print(api_key.startswith('sk-'))",
    "print('api_key' in config)",
    "engine = connect(password=db_password)\nengine",
    "login(token=hf_token)",
    "client = OpenAI(api_key=api_key)\nclient.models",
    "print(get_token())",
    "api_key = load_key()",
    "api_key;",
    "x = !echo {api_key}",
    "!echo {len(api_key)}",
    "print(DATA_URL)",
    "print(keys)",
    "print(auth)",
    "print(has_api_key)",
    "has_key = 'OPENAI_API_KEY' in env_names\nhas_key",
    "print(api_key_set, isKeySet, token_found, secret_status)",
    "print(api_key and has_rows)",  # `a and b` shows only b
    "print({'api_key': 1})",
    "print(api_key.keys())",
    # a tokenizer's special tokens, and a token's place in a sequence, even in capitals or
    # beside a credential word
    *(f"print({word.upper()}_TOKEN)" for word in TOKEN_WORDS),
    *(f"print(access_{word}_token)" for word in TOKEN_WORDS),
    # `token` with no credential word: an NLP token
    "print(token)",
    "print(df.token.value_counts())",
    *(
        f"print({name})"
        for name in [
            "token_str",
            "token_text",
            "token_string",
            "token_idx",
            "token_index",
            "token_pos",
            "token_offset",
            "token_col",
            "token_column",
            "token_label",
            "token_labels",
            "token_map",
            "token_vocab",
            "token_stats",
            "token_info",
            "token_data",
            "token_dist",
            "token_matrix",
            "token_counter",
            "token_array",
            "token_tensor",
            "token_weights",
            "token_level",
            "token_span",
            "token_spans",
            "token_seq",
            "token_expiry",
            "expires_token",
            "token_expires_at",
            "pred_token",
            "decoded_token",
            "input_token",
            "output_token",
            "top_token",
            "word_token",
            "sampled_token",
            "generated_token",
            "target_token",
            "query_token",
            "gold_token",
        ]
    ),
    'token_classification = pipeline("token-classification")\ntoken_classification',
    "print(not api_key)",
    "print(api_key.__len__())",
    'print(", ".join())',
    'import os\napi_key = os.getenv("K")\nos.system("echo \'$api_key\'")',  # no Python fill
    "!echo {api_key} | wc -c",  # only a count is shown
    "!echo {api_key} > out.txt",
    # a fact about the secret, not the secret
    *(f"print({word}_api_key)" for word in ["is", "has", "have", "can", "should", "use"]),
    *(
        f"print(api_key_{word})"
        for word in ["set", "present", "exists", "found", "missing", "ok", "valid"]
    ),
    *(f"print(api_key_{word})" for word in ["loaded", "configured", "available", "defined"]),
    "print(api_key_status)",
    # a measure, a container or a label of the secret
    *(f"print(api_key_{word})" for word in NOT_THE_SECRET_LAST),
    *(f"print(API_KEY_{word.upper()})" for word in ["env", "var", "vars"]),
    "print(password_strength, password_field, password_prompt, password_pattern)",
    "print(secret_names)",
    "pwd = os.getcwd()\npwd",
    "print(token_ids, SECRET_NAME, token_file)",
]


@pytest.mark.parametrize("code", L014_CLEAN)
def test_l014_must_not_match(code: str) -> None:
    assert "L014" not in hints(code)


@pytest.mark.parametrize(
    "code",
    [
        'import os\napi_key = os.environ["OPENAI_API_KEY"]\nprint(api_key)',
        'import os\nOPENAI_API_KEY = os.getenv("OPENAI_API_KEY")\nOPENAI_API_KEY',
        "!echo $OPENAI_API_KEY",
        'import os\ndb_password = os.getenv("PW")\n!echo {db_password}',
    ],
)
def test_l014_leaves_what_l011_reports(code: str) -> None:
    report = lint(code)
    assert [e.rule for e in report.errors] == ["L011"]
    assert "L014" not in {h.rule for h in report.hints}
    with_l011_hint = lint(code, cfg=_rules(secret_print="hint"))
    assert [h.rule for h in with_l011_hint.hints if h.rule in ("L011", "L014")] == ["L011"]
    without_l011 = lint(code, cfg=_rules(secret_print="off"))
    assert without_l011.errors == [] and "L014" in {h.rule for h in without_l011.hints}


def test_l014_still_reports_another_name_beside_l011() -> None:
    code = 'import os\nkey = os.getenv("K")\nprint(key, db_password)'
    report = lint(code)
    assert [e.rule for e in report.errors] == ["L011"]
    assert "`db_password`" in next(h.message for h in report.hints if h.rule == "L014")


def test_l014_messages() -> None:
    assert hints("api_key = load_key()\napi_key")["L014"] == (
        "The last line shows `api_key`, whose name says it holds a secret."
    )
    assert hints("cfg.api_key.strip()")["L014"] == (
        "The last line `cfg.api_key.strip()` shows `cfg.api_key`, whose name says it holds "
        "a secret."
    )
    assert hints("print(api_key, db_password)\nprint(hf_token)")["L014"] == (
        "`print(api_key, db_password)` shows `api_key`, whose name says it holds a secret "
        "(+2 more)."
    )


def test_l014_is_the_first_hint() -> None:
    order = [rule for rule, _, _ in lint_module._CHECKS]
    first_hint = next(r for r, key, _ in lint_module._CHECKS if load(None).rule(key) == "hint")
    assert first_hint == "L014" and order.index("L014") < order.index("L120")
    code = lines("df2 = sales.copy()", "print(api_key)", "t = '" + "x" * 100 + "'")
    assert [h.rule for h in lint(code).hints][0] == "L014"


@pytest.mark.parametrize(
    "code", ["!echo {api_key} | wc -c", "!echo {api_key} > out.txt", "!echo $(date)"]
)
def test_l014_shell_lines_record_no_name_they_do_not_show(code: str) -> None:
    assert scan_returns(code).shown == []


def test_l014_scan_never_raises() -> None:
    """The scan and the names read from what it shows, with no catch-all around them."""
    for code in [*(code for code, _ in L014_SHOWN), *L014_CLEAN]:
        for cell in (code, f"{EVERYDAY}\n{code}"):
            for shown in scan_returns(cell).shown:
                list(secret_scan.secret_names(shown.expr))


def test_l014_strict_mode_makes_it_an_error() -> None:
    cfg = load(None)
    cfg.data["lint"]["mode"] = "strict"
    report = lint("print(api_key)", cfg=cfg)
    assert [e.rule for e in report.errors] == ["L014"]
    assert report.errors[0].severity == "error" and not report.ok
    assert "L014" not in {
        h.rule for h in lint("print(api_key)", cfg=_rules(secret_name="off")).hints
    }
    assert [e.rule for e in lint("print(api_key)", cfg=_rules(secret_name="error")).errors] == [
        "L014"
    ]


def _rules(**rules: str) -> Config:
    cfg = load(None)
    cfg.data["lint"]["rules"].update(rules)
    return cfg


def test_l101_respects_max_line_length() -> None:
    cfg = load(None)
    cfg.data["lint"]["max_line_length"] = 20
    assert "L101" in hints("label_total = 'abcdefghij'\nlabel_total", cfg=cfg)


def test_l101_quotes_the_line_and_counts_the_rest() -> None:
    message = hints(("wide_one = '" + "x" * 100 + "'\n") * 3 + "wide_one")["L101"]
    assert message.startswith("`wide_one = 'xxx")
    assert "(+2 more)" in message


def test_l103_elif_chain_is_flat() -> None:
    chain = lines(
        "if a:",
        "    band = 1",
        "elif b:",
        "    band = 2",
        "elif c:",
        "    band = 3",
        "elif d:",
        "    band = 4",
        "elif e:",
        "    band = 5",
        "band",
    )
    assert "L103" not in hints(chain)


def test_l104_counts_logical_links_across_lines() -> None:
    chain = "summary = sales.dropna().groupby('a').agg('sum').reset_index().head()\nsummary"
    assert "chains 5 method calls (max 4)" in hints(chain)["L104"]


def test_l105_zero_ratio_forbids_comments() -> None:
    cfg = load(None)
    cfg.data["lint"]["comment_ratio"] = 0
    code = "# Median resists outliers\nmedians = sales.median()\nmedians"
    assert "asks for none" in hints(code, cfg=cfg)["L105"]
    assert "L105" not in hints(code)


def test_l105_ignores_pragmas_and_counts_bare_strings() -> None:
    assert "L105" not in hints("import pandas as pd  # noqa: F401\n# type: ignore\npd")
    prose = lines('"""This cell loads the data."""', '"And cleans it."', "sales.head()")
    report = lint(prose)
    assert report.comment_lines == 2
    assert report.code_lines == 1
    assert "L105" in {h.rule for h in report.hints}


def test_l106_ignores_prose_with_code_punctuation() -> None:
    for comment in [
        "# note: df.x holds cents",
        "# Drop rows where price = 0",
        "# see https://x.io/a.b",
        "# (optional)",
        "# TODO(me): fix",
    ]:
        assert "L106" not in hints(f"{comment}\nmedians = sales.median()\nmedians"), comment


def test_l113_allows_ml_conventions_and_with_targets() -> None:
    code = lines(
        "X = sales[FEATURES]",
        "y = sales['price']",
        "with open(path) as f:",
        "    raw = f.read()",
        "[v for v in raw]",
    )
    assert "L113" not in hints(code)


def test_l114_decorated_definitions_are_fine() -> None:
    assert "L114" not in hints("@interact\ndef show(region):\n    return region\nshow")


@pytest.mark.parametrize(
    "code",
    [
        "import warnings\nwarnings.filterwarnings('ignore', category=FutureWarning)\nsales.head()",
        "import warnings\nwarnings.filterwarnings(\n    'ignore', message='X does not have valid feature names'\n)\nmodel.predict(X_test[:5])",
        "import warnings\nwarnings.filterwarnings('ignore', 'X does not have valid')\nsales.head()",
        "import warnings\nwarnings.simplefilter('ignore', FutureWarning)\nsales.head()",
        "import warnings\nwarnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)\nsales.head()",
    ],
)
def test_l118_one_specific_warning_is_fine(code: str) -> None:
    # Review finding 59: this is exactly what L118's own fix asks for.
    assert "L118" not in hints(code)


@pytest.mark.parametrize(
    "code",
    [
        "import warnings\nwarnings.filterwarnings('ignore', category=Warning)\nsales.head()",
        "import warnings\nwarnings.filterwarnings('ignore', message='')\nsales.head()",
        "import warnings\nwarnings.filterwarnings('ignore', module='sklearn')\nsales.head()",
        "import warnings\nwarnings.simplefilter('ignore')\nsales.head()",
        "np.seterr(all='ignore')\nsales.head()",
    ],
)
def test_l118_blanket_filters_still_hint(code: str) -> None:
    assert "L118" in hints(code)


@pytest.mark.parametrize(
    "code",
    [
        "from sklearn.metrics import RocCurveDisplay\n\nRocCurveDisplay.from_estimator(model, X_test, y_test)\nplt.show()",
        "from sklearn.metrics import ConfusionMatrixDisplay\n\nConfusionMatrixDisplay.from_estimator(model, X_test, y_test)\nplt.show()",
        "ConfusionMatrixDisplay.from_predictions(y_test, y_pred)",
        "from sklearn.tree import plot_tree\n\nplot_tree(model, max_depth=2)\nplt.show()",
        "shap.summary_plot(shap_values, X_test)",
        "draw_revenue_chart(sales)\nplt.show()",  # a helper nh can't see into
    ],
)
def test_l116_library_figures_and_a_bare_plt_show_count_as_output(code: str) -> None:
    assert "L116" not in hints(code)


def test_l116_bare_plt_show_is_one_figure_not_more() -> None:
    counts = lint_module._display_points(
        lint_module._Cell.build(
            "draw_revenue_chart(sales)\nplt.show()",
            load(None),
            title=None,
            intent=None,
            bullets=[],
            names_above=None,
        )
    )
    assert counts == {"print": 0, "figure": 1, "last line": 0, "magic": 0}
    assert "L117" not in hints("print(n_rows)\nplot_tree(model)\nplt.show()")


@pytest.mark.parametrize(
    "code",
    [
        "sales_sorted = sales.sort_values('order_date')\nsales_sorted.reset_index(drop=True, inplace=True)\nsales_sorted.head()",
        "sales = pd.read_csv(DATA_PATH)\nsales.dropna(inplace=True)\nsales.shape",
    ],
)
def test_l111_inplace_on_a_name_the_cell_makes_first_is_rerun_safe(code: str) -> None:
    # Review finding 59: a re-run recreates the name before the inplace call.
    assert "L111" not in hints(code)


@pytest.mark.parametrize(
    "code",
    [
        "sales.dropna(inplace=True)\nsales.shape",
        "sales['price'].fillna(0, inplace=True)\nsales.head()",
        "if refresh:\n    sales = load_sales()\nsales.dropna(inplace=True)\nsales.shape",
        "sales.dropna(inplace=True)\nsales = sales.reset_index()\nsales.shape",
        "sales_view = sales\nsales_view.drop(columns=['sku'], inplace=True)\nsales_view.head()",
        "for part in parts:\n    part.dropna(inplace=True)\nparts",
    ],
)
def test_l111_inplace_on_an_earlier_cells_name_still_hints(code: str) -> None:
    assert "L111" in hints(code)


def test_l116_magic_output_counts_as_visible() -> None:
    assert "L116" not in hints("!ls data")
    assert "L116" not in hints("%timeit sales.median()")
    assert "L116" in hints("%matplotlib inline\nsales_clean = sales.dropna()")


def test_l116_non_ascii_before_a_trailing_semicolon() -> None:
    assert "L116" in hints("café_prices = sales['café']\ncafé_prices;")
    assert "L116" not in hints("café_prices = sales['café']\ncafé_prices")


def test_l117_names_the_kinds_of_output() -> None:
    message = hints(lines("print(sales.shape)", "sales.plot()", "sales.describe()"))["L117"]
    assert (
        message
        == "The cell shows 3 outputs (1 print, 1 figure, last line), which looks like 3 steps."
    )


# L120 -------------------------------------------------------------------------------------------
def test_l120_kernel_only_name() -> None:
    found = hints("sales_clean.head()", names_above={"pd", "sales"})
    assert found["L120"].startswith("`sales_clean` is not defined by any cell above")


def test_l120_lists_several_names() -> None:
    assert "`a_1`, `b_2` are not defined" in hints("a_1 + b_2", names_above=set())["L120"]


@pytest.mark.parametrize(
    ("code", "above"),
    [
        ("sales_clean.head()", None),  # nothing known about the cells above
        ("sales_clean.head()", {"*"}),  # a cell above may define anything
        ("sales_clean.head()", {"sales_clean"}),
        ("sales_clean = [1, 2]\nsales_clean", set()),
        ("len(range(3)), display(In), _i3, Out[2], __builtins__", set()),
        ("from helpers import *\nclean(sales)", set()),
        ("%run helpers.py\nclean(sales)", set()),
    ],
)
def test_l120_quiet_cases(code: str, above: set[str] | None) -> None:
    assert "L120" not in hints(code, names_above=above)


def test_l120_skipped_for_unparsable_cells() -> None:
    report = lint("sales_clean.head(", names_above=set())
    assert "L120" not in {h.rule for h in report.hints}


# L122–L123 (L121 is gone) ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "intent"),
    [
        ("Drop rows with missing price", "drop the rows with missing price"),  # the README's turn
        ("Summarise prices by region", "Summarise prices by region."),
        ("Summary table", "summary table"),
    ],
)
def test_an_intent_in_the_users_words_gets_no_hint(title: str, intent: str) -> None:
    # Review finding 58: the intent is the user's ask verbatim; nh must not push a paraphrase.
    report = lint("sales.head()", title=title, intent=intent)
    assert report.ok and report.hints == []
    assert all(rule != "L121" for rule, _, _ in lint_module._CHECKS)


def test_l122_intent_too_long() -> None:
    assert "L122" in hints("sales.head()", intent="x" * 201)
    assert "L122" not in hints("sales.head()", intent="x" * 200)


def test_l123_long_bullet() -> None:
    assert "L123" in hints("sales.head()", notes=["word " * 26, "short"])
    assert "L123" not in hints("sales.head()", notes=["word " * 25, "short"])
    report = lint("sales.head()", notes=["word " * 41, "short"])
    assert [e.rule for e in report.errors] == ["L004"]
    assert "L123" not in {h.rule for h in report.hints}


def test_note_hints_still_run_on_an_empty_cell() -> None:
    report = lint("", intent="x" * 201)
    assert [e.rule for e in report.errors] == ["L001"]
    assert [h.rule for h in report.hints] == ["L122"]


# configuration ----------------------------------------------------------------------------------
def test_hints_never_block() -> None:
    report = lint("df2 = sales.copy(); tmp = 1")
    assert report.ok
    assert {"L107", "L113", "L116"} <= {h.rule for h in report.hints}


def test_strict_mode_promotes_hints() -> None:
    cfg = load(None)
    cfg.data["lint"]["mode"] = "strict"
    report = lint("df2 = sales.copy()\ndf2", cfg=cfg)
    assert [e.rule for e in report.errors] == ["L113"]
    assert report.errors[0].severity == "error"
    assert report.hints == []


def test_rule_off_and_rule_error() -> None:
    cfg = load(None)
    cfg.data["lint"]["rules"].update(cryptic_name="off", long_line="error")
    report = lint("df2 = '" + "x" * 100 + "'\ndf2", cfg=cfg)
    assert [e.rule for e in report.errors] == ["L101"]
    assert "L113" not in {h.rule for h in report.hints}


def test_hints_come_most_useful_first() -> None:
    code = lines("sales['p'] = sales['p'] / 100", "t = '" + "x" * 100 + "'", "sales_clean.head()")
    order = [h.rule for h in lint(code, names_above={"sales"}).hints]
    assert order.index("L120") < order.index("L111") < order.index("L113") < order.index("L101")


def test_a_crashing_heuristic_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cell: Any) -> Any:
        raise AttributeError("unexpected node")

    monkeypatch.setattr(
        lint_module, "_CHECKS", [("L101", "long_line", boom), *lint_module._CHECKS[1:]]
    )
    report = lint("df2 = sales.copy()\ndf2")
    assert report.ok
    assert "L113" in {h.rule for h in report.hints}


def test_every_hint_has_its_config_key() -> None:
    keys = set(load(None).data["lint"]["rules"])
    for rule, key, _ in lint_module._CHECKS:
        assert key in keys, (rule, key)
    assert {r for r, _, _ in lint_module._CHECKS} >= {
        "L014",
        "L101",
        "L102",
        "L103",
        "L104",
        "L105",
        "L106",
        "L107",
        "L108",
        "L109",
        "L111",
        "L112",
        "L113",
        "L114",
        "L116",
        "L117",
        "L118",
        "L119",
        "L120",
        "L122",
        "L123",
    }
