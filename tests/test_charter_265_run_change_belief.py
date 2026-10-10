"""Charter 265: the latest-run change as the model's resolution, and the
Proper lane back on charter 231's standard rule with s read off the
realisation layer's belief.

User rulings 2026-10-10 「加，并把这个研究结果写进宪章」「作为执行层的选桶、作为模型
的分辨率和条件性」「并不一定是ECMWF家族，去研究一下其他家族」「改」.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import realisation_layer as layer  # noqa: E402
from bayes_basket_target import realisation_store as rstore  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    MODEL,
    NOW,
    SETTINGS,
    _intents,
    _ladder,
    _run,
    _seed,
    store,
)


def _t(hours: float) -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# The change.


def test_the_latest_change_is_the_newest_run_in_hand_minus_the_one_before() -> None:
    runs = {
        "ECMWF_AIFS_ENS_GLOBAL": [(_t(0), "i00", 20.0), (_t(6), "i06", 20.4), (_t(12), "i12", 19.9)],
        "NOAA_GEFS_05": [(_t(1), "g00", 21.0)],
    }
    change = run_change.latest_change(runs, as_of=_t(7))
    assert change["ECMWF_AIFS_ENS_GLOBAL"] == pytest.approx({"delta_c": 0.4, "hours_since": 1.0})
    # one run in hand is no change; a run not yet in hand is not read
    assert "NOAA_GEFS_05" not in change
    assert run_change.latest_change(runs, as_of=_t(13))["ECMWF_AIFS_ENS_GLOBAL"]["delta_c"] == pytest.approx(-0.5)
    assert run_change.latest_change(runs, as_of=_t(0.5)) == {}


def test_the_columns_are_in_rungs_and_fade_with_the_hours() -> None:
    change = {"ECMWF_AIFS_ENS_GLOBAL": {"delta_c": 0.9, "hours_since": 6.0}}
    c = run_change.change_columns(change, rung_c=1.0)
    assert c == pytest.approx([0.9 * math.exp(-1.0), 0.0, 0.0])
    f = run_change.change_columns(change, rung_c=run_change.ladder_rung_c(["70-71°F", "72-73°F"]))
    assert f[0] == pytest.approx(0.9 * math.exp(-1.0) * 0.9)
    assert run_change.ladder_rung_c(["24°C", "25°C"]) == 1.0
    assert run_change.SYSTEMS == ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05")


def test_a_store_without_the_run_tables_reads_no_change() -> None:
    conn = sqlite3.connect(":memory:")
    assert run_change.change_at(conn, station_id="X", event_date="2026-10-01", as_of=_t(0)) == {}


# ---------------------------------------------------------------------------
# The layer.


def test_v1_terms_are_untouched_and_v2_adds_a_tilt_about_the_mode() -> None:
    p = [0.01, 0.05, 0.30, 0.40, 0.20, 0.04, 0.0]
    q = [0.02, 0.10, 0.25, 0.33, 0.20, 0.07, 0.03]
    v1 = layer.features(p, q)
    v2 = layer.features(p, q, [0.5, 0.0, -0.25])
    assert v1["terms"].shape == (7, 7) and v2["terms"].shape == (7, 10)
    assert np.array_equal(v1["terms"], v2["terms"][:, :7])
    reach = np.clip(np.arange(7) - v2["mode"], -2, 2)
    assert np.allclose(v2["terms"][:, 7], 0.5 * reach) and np.allclose(v2["terms"][:, 9], -0.25 * reach)
    # a positive weight moves mass to the side the change points to, the mode kept in place
    theta = list(layer.START)
    theta[7] = 1.0
    b = layer.belief(v2["terms"], theta)
    assert b[4:].sum() > np.asarray(v2["q"])[4:].sum() and b[:3].sum() < np.asarray(v2["q"])[:3].sum()
    assert len(layer.PARAM_NAMES) == 10 and layer.PARAM_NAMES[:7] == layer.PARAM_NAMES_V1


def test_the_locked_acceptance_still_reads_v1() -> None:
    # docs/research/realisation_acceptance_20261006/read.py calls _ladder_inputs(p, buckets)
    # and layer.belief(terms, frozen_theta) with seven weights.
    book = [{"bucket": b, "ordinal": i, "ask_available": True, "ask_levels": [{"price": a, "shares": 100}],
             "bid_levels": [{"price": a - 0.02, "shares": 100}]}
            for i, (b, a) in enumerate([("24°C", 0.21), ("25°C", 0.52), ("26°C", 0.31)])]
    inputs = rstore._ladder_inputs({"24°C": 0.2, "25°C": 0.5, "26°C": 0.3}, book)
    assert inputs["state"] == "ok" and inputs["features"]["terms"].shape == (3, 7)


def test_an_unsupported_change_column_is_held_at_zero() -> None:
    rng = np.random.default_rng(1)
    decisions = []
    for n in range(400):
        q = rng.dirichlet(np.ones(5) * 3)
        change = [0.8, 0.0, 0.0] if n < 20 else [0.0, 0.0, 0.0]   # 20 station-days carry AIFS
        f = layer.features(q, q, change)
        decisions.append({"terms": f["terms"], "y": int(rng.integers(0, 5)), "weight": 1.0})
    arrays = layer.stack(decisions)
    theta, cols = rstore.fit_supported(arrays)
    assert 7 not in cols and theta[7] == 0.0
    assert rstore.MIN_CHANGE_SUPPORT_STATION_DAYS == 150.0


def _artifact(**over):
    art = {"rule": layer.RULE, "theta": list(layer.START), "draws": [], "fit_id": "realisation_test",
           "gate": {"state": rstore.STATE_ADMITTED}, "code_sha256": rstore.code_sha256(),
           "fitted_at_utc": "2026-09-18T11:00:00Z"}
    art.update(over)
    return art


@pytest.mark.parametrize("over,state", [
    ({"theta": list(layer.START_V1), "rule": layer.RULE_V1}, "fit_without_change_terms"),
    ({"gate": {"state": rstore.STATE_REFUSED}}, "fit_refused"),
    ({"code_sha256": "other"}, "fit_not_current"),
    ({"fitted_at_utc": "2026-09-16T00:00:00Z"}, "fit_not_current"),
])
def test_the_lane_reads_b_only_under_an_admitted_current_v2_fit(over, state) -> None:
    conn = sqlite3.connect(":memory:")
    read = rstore.belief_vector(conn, station_id="X", event_date="2026-09-20", as_of_utc=NOW,
                                p_raw=MODEL, book_buckets=_ladder(), artifact=_artifact(**over))
    assert read["state"] == state


def test_b_under_an_admitted_fit_is_a_ladder_vector() -> None:
    conn = sqlite3.connect(":memory:")
    read = rstore.belief_vector(conn, station_id="X", event_date="2026-09-20", as_of_utc=NOW,
                                p_raw=MODEL, book_buckets=_ladder(), artifact=_artifact())
    assert read["state"] == "ok" and set(read["b"]) == set(MODEL)
    assert sum(read["b"].values()) == pytest.approx(1.0) and read["change"] == {}


# ---------------------------------------------------------------------------
# The Proper lane on b.

V4 = {**SETTINGS, "rule": lane.RULE_MAKER_BELIEF_B_V4, "belief": "realisation_b",
      "sides": ["YES", "NO"], "buckets": "all"}


def test_no_admitted_b_no_plan(store, monkeypatch) -> None:
    _seed(store)
    monkeypatch.setattr(rstore, "load_active", lambda directory=None, stage="P0": None)
    receipt = _run(store, [], **V4)
    assert receipt["rule"] == "proper_bet_lane_v4"
    assert _intents(store) == []
    assert receipt["waiting"] == 1
    assert "belief:no_fit" in json.dumps(receipt, ensure_ascii=False)


def test_the_standard_rule_bets_both_sides_on_b(store, monkeypatch) -> None:
    _seed(store)
    # The market (27/28/29C mids 0.49/0.31/0.19, normalised) against a b that
    # holds 27C, sinks 28C and lifts 29C: YES on 29C, NO on 28C.
    b = {"27°C": 0.49, "28°C": 0.20, "29°C": 0.31}
    calls = []

    def fake(conn, **kwargs):
        calls.append(kwargs)
        return {"state": "ok", "b": b, "fit_id": "realisation_test", "change": {"NOAA_GEFS_05": {"delta_c": 0.6, "hours_since": 1.0}},
                "change_rule": run_change.RULE}

    monkeypatch.setattr(rstore, "belief_vector", fake)
    _run(store, [], **V4)
    plan_json = store.execute("SELECT plan_json FROM proper_bet_lane_evaluation").fetchone()[0]
    plan = json.loads(plan_json)
    assert plan["belief"] == "realisation_b" and plan["belief_fit_id"] == "realisation_test"
    sides = {leg["bucket"]: leg["side"] for leg in plan["legs"]}
    assert sides.get("29°C") == "YES" and sides.get("28°C") == "NO"
    assert {leg["bucket"]: round(leg["p"], 6) for leg in plan["legs"]}["29°C"] == pytest.approx(0.31, abs=2e-3)
    assert calls and calls[0]["as_of_utc"] == NOW


def test_v4_ships_and_v3_stays_selectable() -> None:
    defaults = strategy_lanes.PROPER_BET_DEFAULT_SETTINGS
    assert defaults["rule"] == lane.RULE_MAKER_BELIEF_B_V4 == lane.DEFAULT_RULE
    assert defaults["belief"] == "realisation_b" == lane.belief_source(defaults)
    assert defaults["sides"] == ["YES", "NO"] and defaults["buckets"] == "all"
    assert lane.RULE_MAKER_MODE_YES_V3 in lane.RULE_VERSIONS and lane.belief_source({}) == "weather_p"
    shipped = strategy_lanes.load_strategy_lanes(ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    proper = shipped["lanes"]["proper_bet"]
    # The on/off switch is the operator's; the rule it runs when on is pinned.
    assert proper["rule"] == lane.RULE_MAKER_BELIEF_B_V4 and proper["belief"] == "realisation_b"
    assert proper["sides"] == ["YES", "NO"] and proper["buckets"] == "all"
    with pytest.raises(ValueError):
        lane.belief_source({"belief": "market_q"})
