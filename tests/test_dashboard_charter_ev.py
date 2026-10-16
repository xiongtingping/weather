"""The charter EV identity on the board: edge = the market's error minus the
model's error, as ``Var(e_q) - Cov(e_p, e_q)``.

The page shows three numbers and a slope, and a reader who cannot see the
algebra has to take them on trust.  These tests pin the algebra to the two
temperature examples the explanation on the page quotes, and pin the
identity itself: the strategy's realised profit -- summed directly, share by
share -- equals ``Var - Cov`` on the same rows, so the number the page calls
EV is the money the disagreement strategy would have made and not a
restatement of a formula.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

LADDER = ("31°C", "32°C", "33°C")


@pytest.fixture(autouse=True)
def _own_cache_dir(tmp_path, monkeypatch):
    """The collector keeps a decision memo beside the section pages; a test
    must not write it into the live board's directory."""

    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)


def _row(p, m, *, label="32°C", station="KMIA", date="2026-09-10", weight=1.0):
    return {
        "station": station, "date": date, "at": f"{date}T00:00:00Z",
        "label": label, "weight": weight,
        "p": dict(zip(LADDER, p)), "m": dict(zip(LADDER, m)),
    }


def test_market_wrong_model_right_is_positive_and_matches_the_hand_sum():
    """Example A on the page: market 0.40 on 31°C, model 0.20, truth 32°C."""

    reading = C.charter_ev_identity([_row((0.20, 0.70, 0.10), (0.40, 0.50, 0.10))])
    # e_q = (+.40, -.50, +.10); e_p = (+.20, -.30, +.10)
    assert reading["var_market"] == pytest.approx(0.16 + 0.25 + 0.01)
    assert reading["var_model"] == pytest.approx(0.04 + 0.09 + 0.01)
    assert reading["cov"] == pytest.approx(0.08 + 0.15 + 0.01)
    assert reading["ev"] == pytest.approx(0.42 - 0.24)
    assert reading["k"] == pytest.approx(0.24 / 0.42)
    # Sold 0.20 of 31°C at 0.40 (+0.08), bought 0.20 of 32°C at 0.50 (+0.10).
    assert reading["strategy_profit"] == pytest.approx(0.18)
    assert reading["ev"] == pytest.approx(reading["strategy_profit"])
    assert reading["days_ev_positive"] == 1


def test_model_amplifying_the_markets_error_is_negative():
    """Example B: market 0.40 on 31°C, model 0.55 the same way, truth 32°C."""

    reading = C.charter_ev_identity([_row((0.55, 0.35, 0.10), (0.40, 0.50, 0.10))])
    assert reading["ev"] < 0
    assert reading["k"] > 1
    assert reading["ev"] == pytest.approx(reading["strategy_profit"])
    # On the 31°C bucket alone the page's arithmetic: 0.16 - 0.22 = -0.06.
    e_q, e_p = 0.40, 0.55
    assert e_q * e_q - e_p * e_q == pytest.approx(-0.06)


def test_the_spread_cost_prices_every_share_the_strategy_trades():
    """Example A again, with a book: crossing costs |p - m| times the half
    spread on each bucket, and a row without a book prices no spread."""

    row = _row((0.20, 0.70, 0.10), (0.40, 0.50, 0.10))
    row["half_spread"] = {"31°C": 0.02, "32°C": 0.05, "33°C": 0.01}
    reading = C.charter_ev_identity([row])
    # 0.20 shares on 31°C at 0.02, 0.20 on 32°C at 0.05, none on 33°C.
    assert reading["spread_cost"] == pytest.approx(0.20 * 0.02 + 0.20 * 0.05)
    assert reading["ev"] == pytest.approx(0.18)
    bare = C.charter_ev_identity([_row((0.20, 0.70, 0.10), (0.40, 0.50, 0.10))])
    assert bare["spread_cost"] is None


def test_a_model_that_repeats_the_market_has_k_one_and_no_edge():
    vector = (0.40, 0.50, 0.10)
    reading = C.charter_ev_identity([_row(vector, vector)])
    assert reading["ev"] == pytest.approx(0.0)
    assert reading["k"] == pytest.approx(1.0)
    assert reading["var_market"] == pytest.approx(reading["cov"])


def test_station_day_weights_and_the_day_bootstrap_bracket_the_point():
    rows = []
    for i in range(12):
        day = f"2026-09-{i + 1:02d}"
        # Days differ -- the model's lean on 31°C walks from right to wrong
        # -- so the by-day resample has something to spread over.
        lean = 0.20 + 0.03 * i
        rows.append(_row((lean, 0.90 - lean, 0.10), (0.40, 0.50, 0.10),
                         date=day, weight=0.5))
        rows.append(_row((lean, 0.90 - lean, 0.10), (0.40, 0.50, 0.10),
                         date=day, weight=0.5))
        rows.append(_row((0.55, 0.35, 0.10), (0.40, 0.50, 0.10),
                         date=day, station="KDAL"))
    reading = C._charter_ev_reading(rows, draws=100, seed=7)
    assert reading["station_days"] == 24
    assert reading["days"] == 12
    assert reading["decisions"] == 36
    assert reading["ev_ci"] is not None and reading["k_ci"] is not None
    assert reading["ev_ci"][0] < reading["ev"] < reading["ev_ci"][1]
    assert reading["k_ci"][0] < reading["k"] < reading["k_ci"][1]
    assert "by_day" not in reading


def test_an_empty_population_reads_as_no_sample_not_as_zero_edge():
    reading = C._charter_ev_reading([], draws=10, seed=1)
    assert reading["decisions"] == 0
    assert reading["ev"] is None and reading["k"] is None


def test_the_collector_fails_closed_on_a_store_without_the_lane():
    conn = sqlite3.connect(":memory:")
    out = C.collect_charter_ev(conn)
    assert out["available"] is False
    assert all("error" in scope for scope in out["scopes"])


def test_the_section_is_cached_and_registered_on_the_page():
    assert "charter_ev" in C.SECTION_CACHE_TTL_SEC
    page = C.render_html({"generated_at_utc": "x"}, started_at=0.0)
    assert "function charterEv()" in page
    assert '[charterEv,"宪章 EV 恒等式"]' in page


def test_the_edge_summaries_lead_with_big_numbers_and_fold_the_tables() -> None:
    """2026-10-17 「边际板块……按同样版式极简化」: 模型对市场差距 opens on the gap,
    the centre's share and the shape, with the lead tiers drawn; 宪章 EV 恒等式
    on one EV per stage with its verdict; the former tables sit under 「明细」."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    src = C.HTML_TEMPLATE
    js = (src[src.index("// The edge area's three sections, the summary"):src.index("// The lambda tables, one per stage scope.")]
          + src[src.index("const EV = id =>"):src.index("// 纸面账本, one board")])
    row = {"station_days": 1268, "days": 28, "gap": 0.109, "gap_ci": [0.089, 0.131], "centre": 0.096,
           "shape": 0.013, "shape_ci": [0.009, 0.019], "centre_share": 0.88, "centre_share_ci": [0.85, 0.91]}
    ev = {"decisions": 10, "station_days": 1196, "days": 28, "ev": 0.0003, "ev_ci": [-0.0057, 0.0047],
          "k": 0.9996, "k_ci": [0.993, 1.008], "spread_cost": 0.0074, "days_ev_positive": 14}
    payload = {"charter_ev": {"available": True, "window": {"floor_day": "2026-09-15", "as_of_event_date": "2026-10-13"},
                              "centre_shape": {"available": True, "samples": {"stable_pool": row},
                                               "tiers": {"0-12": row, "12-24": dict(row, gap=0.1)}},
                              "scopes": [{"scope": "P0", "research": ev}]}}
    script = ("const D = " + json.dumps(payload, ensure_ascii=False) + ";\nconst h = s => String(s == null ? '' : s);\n"
              + js + "\nconsole.log(centreShapeGap() + '|||' + charterEv());\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    gap, evs = result.stdout.split("|||")
    top = gap[:gap.index("<summary>明细</summary>")]
    assert "+0.109" in top and "88%" in top and "+0.013" in top and "按距结算" in top and "<svg viewBox" in top
    assert "稳定池" not in top and "稳定池" in gap, "the sample table is folded"
    top = evs[:evs.index("<summary>明细</summary>")]
    assert "+0.0003" in top and "不够付点差" in top and "k 1.000" in top
    assert "EV &gt; 0 天数" not in top and "EV &gt; 0 天数" in evs, "the identity table is folded"
