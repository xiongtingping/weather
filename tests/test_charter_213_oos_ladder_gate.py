"""Charter 213: the OOS gate's holdout on the station's market ladder.

Serving dresses, blends, shapes and prices P0 on the day's market ladder;
the gate used to certify the same arithmetic on a 1C integer grid.  These
pin the ladder path to the serving functions, the one ladder rule, and the
band lane's reading of what the gate archives.
"""
from __future__ import annotations

import json
import math
import sqlite3

import pytest

from bayes_basket_target import band_reliability_store as B
from bayes_basket_target import native_ensemble_oos as O
from bayes_basket_target import native_ensemble_p0 as P
from bayes_basket_target.station_day_ladder import settled_rung, station_day_ladder

# A Fahrenheit ladder: open ends, 2F rungs on half-degree F edges.
F_ROWS = [
    ("59°F or below", 0, None, 15.277777777777779),
    ("60-61°F", 1, 15.277777777777779, 16.38888888888889),
    ("62-63°F", 2, 16.38888888888889, 17.5),
    ("64-65°F", 3, 17.5, 18.61111111111111),
    ("66°F or higher", 4, 18.61111111111111, None),
]


def _ladder(rows=F_ROWS):
    intervals = {bucket: (lower, upper) for bucket, _o, lower, upper in rows}
    ordered = [row[0] for row in rows]
    centers, edges = O.served_bucket_axis(intervals, ordered)
    return {"history_id": "h", "rows": list(rows), "ordered": ordered,
            "intervals": intervals, "centers_c": centers,
            "open_edge_bounds_c": edges}


def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "ladder.db")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history (history_id TEXT,"
        " station_id TEXT, event_date TEXT, state TEXT, stage TEXT,"
        " decision_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, ordinal INTEGER, lower_c REAL,"
        " upper_c REAL, probability REAL)"
    )
    return conn


def _decision(conn, history_id, at, rows, stage="P0", state="COMMITTED"):
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?,?,?,?)",
        (history_id, "KLGA", "2026-09-20", state, stage, at),
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_bucket_probability VALUES (?,?,?,?,?,?)",
        [(history_id, b, o, lo, hi, 0.2) for b, o, lo, hi in rows],
    )


def test_the_ladder_is_the_newest_committed_decision_known_at_the_as_of(tmp_path):
    conn = _store(tmp_path)
    shifted = [(b + "x", o, lo, hi) for b, o, lo, hi in F_ROWS]
    _decision(conn, "old", "2026-09-19T12:00:00Z", F_ROWS)
    _decision(conn, "new", "2026-09-20T12:00:00Z", shifted, stage="P1")
    _decision(conn, "draft", "2026-09-20T13:00:00Z", F_ROWS, state="PREPARED")
    assert station_day_ladder(conn, station_id="KLGA", event_date="2026-09-20")[0] == "new"
    held = station_day_ladder(conn, station_id="KLGA", event_date="2026-09-20",
                              as_of_utc="2026-09-20T00:00:00Z")
    assert held[0] == "old" and held[2][1][0] == "60-61°F"
    assert station_day_ladder(conn, station_id="KLGA", event_date="2026-09-21") is None


def test_the_settled_rung_is_lower_edge_inclusive_and_open_ends_catch_the_tails():
    assert settled_rung(F_ROWS, 15.277777777777779) == "60-61°F"
    assert settled_rung(F_ROWS, 2.0) == "59°F or below"
    assert settled_rung(F_ROWS, 40.0) == "66°F or higher"
    # 20.6C = 69.08F settles as 69F, which is not a rung here but the open end.
    assert settled_rung(F_ROWS, 20.6) == "66°F or higher"


def test_ladder_scores_every_rung_and_read_the_settled_one():
    ladder = _ladder()
    probability = dict(zip(ladder["ordered"], [0.1, 0.2, 0.4, 0.2, 0.1]))
    metrics = O._metrics(probability, 17.0, ladder)       # 62.6F -> 62-63F
    assert metrics["brier"] == pytest.approx(0.01 + 0.04 + 0.36 + 0.04 + 0.01)
    assert metrics["nll"] == pytest.approx(-math.log(0.4))
    # CRPS on the served axis, in degrees: rung gaps weight the step CDF.
    centers = ladder["centers_c"]
    expected = (0.1 ** 2) * (centers[1] - centers[0]) + (0.3 ** 2) * (centers[2] - centers[1]) \
        + (0.3 ** 2) * (centers[3] - centers[2]) + (0.1 ** 2) * (centers[4] - centers[3])
    assert metrics["crps"] == pytest.approx(expected)
    # The integer path is untouched when no ladder is given.
    assert O._metrics({"17": 1.0}, 17.0)["nll"] == pytest.approx(0.0)


def test_a_key_off_the_ladder_is_refused_not_scored():
    with pytest.raises(O.ContractError):
        O._metrics({"17": 1.0}, 17.0, _ladder())


def test_the_holdout_dresses_a_system_with_the_serving_function():
    params = {
        "correction_form": "constant", "bias_c": 0.4, "spread_factor": 1.3,
        "reliability_weight": 1.0, "climate_samples_c": [15.0, 16.5, 18.0, 19.5],
        "train_residual_sigma_c": 1.1,
    }
    values = [16.0, 16.8, 17.2, 17.9, 18.4]
    ladder = _ladder()
    ours = O._apply_parameters(values, params, intervals=ladder["intervals"])
    served = P._calibrated_system_probability(
        values=values, intervals=ladder["intervals"], bias_parameters=params,
        spread_parameters=params, reliability_parameters=params,
    )
    assert ours == served
    assert list(ours) == ladder["ordered"] and sum(ours.values()) == pytest.approx(1.0)


def test_a_celsius_ladder_reads_like_the_integer_grid_away_from_its_ends():
    rows = [("14°C or below", 0, None, 14.5)] + [
        (f"{k}°C", k - 14, k - 0.5, k + 0.5) for k in range(15, 22)
    ] + [("22°C or higher", 8, 21.5, None)]
    ladder = _ladder(rows)
    params = {"correction_form": "constant", "bias_c": 0.0, "spread_factor": 1.0,
              "reliability_weight": 1.0, "climate_samples_c": [18.0]}
    values = [17.6, 18.0, 18.3, 18.9]
    on_ladder = O._apply_parameters(values, params, intervals=ladder["intervals"])
    on_grid = O._apply_parameters(values, params)
    for k in range(16, 21):
        assert on_ladder[f"{k}°C"] == pytest.approx(on_grid.get(str(k), 0.0), abs=1e-6)


def test_the_shape_stack_on_a_ladder_keeps_its_rungs_and_mass():
    ladder = _ladder()
    base = dict(zip(ladder["ordered"], [0.05, 0.25, 0.4, 0.25, 0.05]))
    shaped = O._holdout_shape_stack(
        base, grid=ladder["ordered"], cooling_shift_sigma=0.3,
        station_latitude=40.8, neighbour_center_c=17.0,
        center_width_layer=None, band_layer=None, ladder=ladder,
    )
    assert list(shaped) == ladder["ordered"]
    assert sum(shaped.values()) == pytest.approx(1.0)
    assert shaped != base   # the radiation shift moved mass


def test_the_band_lane_reads_ladder_rows_against_the_ladder(tmp_path):
    conn = _store(tmp_path)
    _decision(conn, "h1", "2026-09-20T12:00:00Z", F_ROWS)
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_oos_prediction ("
        " cross_ensemble_artifact_id TEXT, station_id TEXT, event_date TEXT,"
        " lead_bucket TEXT, trained_through_event_date TEXT,"
        " decision_at_utc TEXT, probabilities_json TEXT,"
        " bucket_centers_c_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_artifact ("
        " cross_ensemble_artifact_id TEXT, combination_method TEXT)"
    )
    ladder = _ladder()
    probs = dict(zip(ladder["ordered"], [0.1, 0.2, 0.4, 0.2, 0.1]))
    centres = dict(zip(ladder["ordered"], ladder["centers_c"]))
    conn.execute("INSERT INTO calibration_cross_native_ensemble_artifact VALUES ('a','m')")
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction VALUES"
        " ('a','KLGA','2026-09-20','h12_24','2026-09-19','2026-09-19T00:00:00Z',?,?)",
        (json.dumps(probs), json.dumps(centres)),
    )
    # An integer-degree row on another day keeps the 1C window rule.
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction VALUES"
        " ('a','KLGA','2026-09-19','h12_24','2026-09-18','2026-09-18T00:00:00Z',?,?)",
        (json.dumps({"16": 0.5, "17": 0.5}), json.dumps({"16": 16.0, "17": 17.0})),
    )
    out = B.gate_lane_by_day(conn, {("KLGA", "2026-09-20"): 16.4,
                                    ("KLGA", "2026-09-19"): 16.5})
    # 16.4C = 61.5F settles in 62-63F.  A 1C window around each rung's
    # centre misses it altogether (60-61F reaches 16.33C, 62-63F starts at
    # 16.44C): the 2F rung is 1.11C wide, and the ladder is what settles.
    assert out["2026-09-20"] == [[(0.1, 0), (0.2, 0), (0.4, 1), (0.2, 0), (0.1, 0)]]
    assert out["2026-09-19"] == [[(0.5, 0), (0.5, 1)]]


def test_the_default_grid_is_the_market_ladder():
    assert O.HOLDOUT_GRID == O.HOLDOUT_GRID_MARKET_LADDER
    assert O.HOLDOUT_GRIDS == ("integer_c", "market_ladder")


def test_charter_213_records_the_rule():
    from pathlib import Path
    charter = (Path(__file__).resolve().parents[1] / "docs" / "constitution"
               / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n213. ", 1)[1]
    for needle in ("market_ladder", "PM_OOS_HOLDOUT_GRID", "station_day_ladder",
                   "裁决人：用户", "改判"):
        assert needle in clause
