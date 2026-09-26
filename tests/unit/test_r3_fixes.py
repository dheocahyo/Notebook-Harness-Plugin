"""Round-3 fixes: self-check labels and percentages, mask sources, column drops (W13-W15, W17),
loopback aliases, underscores in hostnames and the status reason (W20-W22)."""

from __future__ import annotations

import pytest

from nh_gateway import config
from nh_gateway._shared.paths import Layout
from nh_gateway.backend import discovery
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.policy.errors import NhError
from nh_gateway.render import selfcheck
from tests.unit import test_r2_secdocs as r2
from tests.unit.test_r2_secdocs import TOKEN, runtime_record
from tests.unit.test_render import frame, payload

# the fixtures, shared
home, jupyter, outgoing, project = r2.home, r2.jupyter, r2.outgoing, r2.project

COLUMNS = ["price", "units"]


# W13: the closing label says only what was compared
@pytest.mark.parametrize(
    ("value", "label"),
    [
        (frame(10, columns=COLUMNS, nulls={}), "same shape and nulls"),
        (frame(10, columns=COLUMNS), "same shape"),  # over max_cells: no null counts
        ({"kind": "ndarray", "shape": [3], "dtype": "int64"}, "same shape"),
        ({"kind": "container", "type": "list", "len": 3}, "same length"),
        ({"kind": "object", "type": "Model"}, "same type"),
    ],
)
def test_closing_label_names_what_was_compared(value, label):
    lines, _ = selfcheck(payload(x=value), payload(x=value), code="x")
    assert lines == [f"{label}: x"]


# W14: a few rows left is never "100% removed"
def test_percentage_never_rounds_up_to_all():
    before = payload(df=frame(1000, columns=COLUMNS, nulls={}))
    after = payload(
        df=frame(1000, columns=COLUMNS, nulls={}),
        is_bad={"kind": "Series", "len": 1000, "dtype": "bool", "nulls": 0},
        bad=frame(3, columns=COLUMNS, nulls={}),
    )
    code = 'is_bad = df["units"] < 0\nbad = df[is_bad]'
    _, check = selfcheck(before, after, code=code)
    assert check == ["bad kept 3 of the 1,000 rows in df (99% removed)."]
    _, lost = selfcheck(before, payload(df=frame(3, columns=COLUMNS, nulls={})), code="df = df[m]")
    assert lost == ["df lost 99% of its rows (1,000 → 3)."]


# W15: a mask made before its frame is filtered in the same cell comes from the frame as it was
def test_mask_source_is_the_frame_before_it_was_rebound():
    before = payload(df_orders=frame(1000, columns=COLUMNS, nulls={}))
    after = payload(
        df_orders=frame(1000, columns=COLUMNS, nulls={}),
        is_priced={"kind": "Series", "len": 1000, "dtype": "bool", "nulls": 0},
        df_work=frame(202, columns=COLUMNS, nulls={}),
    )
    code = (
        "df_work = df_orders.copy()\n"
        'is_priced = df_work["price"].notna()\n'
        "df_work = df_work[is_priced]"
    )
    lines, _ = selfcheck(before, after, code=code)
    mask_line = next(line for line in lines if line.startswith("is_priced:"))
    assert "df_work 202" not in mask_line


# W17: an explicit drop on one column still counts as rows lost
def test_column_level_drop_is_checked():
    before = payload(df=frame(1000, columns=COLUMNS, nulls={"price": 798}))
    after = payload(
        df=frame(1000, columns=COLUMNS, nulls={"price": 798}),
        valid_prices={"kind": "Series", "len": 202, "dtype": "float64", "nulls": 0},
    )
    _, check = selfcheck(before, after, code='valid_prices = df["price"].dropna()')
    assert check == ["valid_prices kept 202 of the 1,000 rows in df (79% removed)."]


# W22: jupyter_server writes the hostname for --ip 0.0.0.0, and it may have an underscore
def test_hostname_with_underscore_is_plain():
    assert discovery.clean_url("http://data_box:8888/") == "http://data_box:8888/"


# W20: [jupyter].url may name the lab by its other loopback spelling
def test_config_url_with_the_other_loopback_name_finds_the_lab(home, project, jupyter, outgoing):
    port, seen = jupyter
    runtime_record(home, f"http://localhost:{port}/", project)
    (project / "harness.toml").write_text(f'[jupyter]\nurl = "http://127.0.0.1:{port}/"\n')
    info = discovery.discover(project, config.load(project), dirs=[home])
    assert info.url == f"http://localhost:{port}/" and info.token == TOKEN
    assert all(entry["auth"] == f"token {TOKEN}" for entry in seen)


def test_a_server_that_needs_a_token_nh_lacks_says_so(home, project, jupyter, outgoing):
    port, _ = jupyter
    (project / "harness.toml").write_text(f'[jupyter]\nurl = "http://127.0.0.1:{port}/"\n')
    with pytest.raises(NhError) as refused:
        discovery.discover(project, config.load(project), dirs=[home])
    assert "needs a token nh doesn't have" in str(refused.value)
    assert "rejected nh's token" not in str(refused.value)


# W21: the status view shows why nh can't reach JupyterLab
async def test_status_view_keeps_the_reason(tmp_path):
    root = tmp_path / "proj"
    (root / ".nh" / "state").mkdir(parents=True)
    backend = RtcBackend(Layout(root), ConfigCache(root))

    async def no_server():
        raise NhError("E130", detail="The Jupyter server at http://127.0.0.1:9/ doesn't answer.")

    backend._server = no_server  # type: ignore[method-assign]
    lines = await backend.describe(None)
    assert lines["server"] == "nh can't find this project's JupyterLab."
    assert lines["reason"] == "The Jupyter server at http://127.0.0.1:9/ doesn't answer."
