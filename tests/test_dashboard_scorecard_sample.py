"""What the calibration scorecard scores, and how it counts it.

The 2026-09-17 review found the six lamps reading the wrong sample in two
ways and three readings computed the wrong way:

* a 45-day calendar window over an append-only table, so the stable pool
  (charter 134) had served three days while 95% of what the lamps scored was
  the contest it replaced -- the sample now starts at P0's serving anchor;
* one row per candidate view, cycle and re-evaluation, a median 59 per
  station-day, labelled as station-days -- each station x event day x lead
  now counts once;
* the width ratio averaged standard deviations (Jensen: reads narrow);
* the PIT was the mid-PIT, which humps under a calibrated forecast;
* the stability interval resampled rows as if independent.

Fixtures use the production DDL for both tables the collector reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import center_width_store, ssot  # noqa: E402

TODAY = date.today()
ANCHOR_DAY = TODAY - timedelta(days=20)
ANCHOR = f"{ANCHOR_DAY.isoformat()}T00:00:00Z"
KEYS = [str(value) for value in range(10, 31)]
CENTERS = {key: float(key) for key in KEYS}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _table_ddl(name: str) -> str:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS " + name + r" \(.*?\n\);", ssot.DDL, re.S
    )
    assert match, name
    return match.group(0)


@pytest.fixture()
def conn(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute(_table_ddl("calibration_station_daily_high_truth"))
    db.execute(_table_ddl("calibration_cross_native_ensemble_oos_prediction"))
    monkeypatch.setattr(C, "_collect_fusion_layer", lambda _conn: {
        "health": "green", "verdict": "fusion", "detail": "fusion",
        "pairs": [], "summary": {},
    })
    monkeypatch.setattr(
        center_width_store, "served_arithmetic_anchor",
        lambda _conn, *, stage, known_floor=None: ANCHOR,
    )
    return db


def _truth(db, station: str, day: date, value: float) -> None:
    ident = f"{station}|{day}"
    db.execute(
        "INSERT INTO calibration_station_daily_high_truth(truth_id,station_id,"
        "local_date,settlement_timezone,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,timestamps_complete,qc_status,"
        "source_role,source_payload_sha256,observation_ids_json,"
        "authority_scope,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident, station, day.isoformat(), "UTC", value, "AWC", "TMAX", 0, 1,
         "ACCEPTED", "official", _sha(ident), "[]", "FORMAL_NATIVE_ELIGIBLE",
         _sha("truth" + ident)),
    )


def _prediction(db, station: str, day: date, probabilities: dict[str, float],
                *, decided: str, copy: int = 0, lead: str = "h00_12") -> None:
    ident = f"{station}|{day}|{lead}|{decided}|{copy}"
    db.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction("
        "prediction_id,cross_ensemble_artifact_id,selection_evidence_sha256,"
        "station_id,event_date,settlement_timezone,season_bin,lead_bucket,"
        "run_cycle,settlement_definition,trained_through_event_date,"
        "decision_at_utc,ensemble_system_ids_json,independence_family_ids_json,"
        "system_family_ids_json,system_weights_json,family_weight_caps_json,"
        "system_member_set_shas_json,probabilities_json,bucket_centers_c_json,"
        "prediction_oos,authority_scope,content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident, f"artifact-{ident}", _sha(ident), station, day.isoformat(),
         "UTC", "PEAK", lead, "00Z", "TMAX",
         (day - timedelta(days=5)).isoformat(), decided, "[]", "[]", "{}",
         "{}", "{}", "{}", json.dumps(probabilities), json.dumps(CENTERS), 1,
         "PRODUCTION_NO_LIVE_ONLY", _sha("prediction" + ident)),
    )


def _point(center: int) -> dict[str, float]:
    return {str(center): 1.0}


def _decided(day: date) -> str:
    return f"{(day - timedelta(days=1)).isoformat()}T06:00:00Z"


def _layer(scorecard, key):
    return next(layer for layer in scorecard["layers"] if layer["key"] == key)


def test_rows_decided_before_the_serving_anchor_are_not_scored(conn):
    for offset in range(1, 9):
        day = TODAY - timedelta(days=offset)
        _truth(conn, "AAAA", day, 20.0)
        _prediction(conn, "AAAA", day, _point(20), decided=_decided(day))
    stale_day = ANCHOR_DAY - timedelta(days=3)
    _truth(conn, "AAAA", stale_day, 20.0)
    _prediction(conn, "AAAA", stale_day, _point(25), decided=_decided(stale_day))

    card = C.collect_calibration_scorecard(conn)
    assert card["window"]["anchor"] == ANCHOR
    assert card["station_day_count"] == 8
    assert card["rows"][0]["bias_rungs"] == pytest.approx(0.0)


def test_without_an_anchor_the_calendar_window_is_the_whole_sample(
    conn, monkeypatch
):
    monkeypatch.setattr(
        center_width_store, "served_arithmetic_anchor",
        lambda _conn, *, stage, known_floor=None: None,
    )
    stale_day = ANCHOR_DAY - timedelta(days=3)
    _truth(conn, "AAAA", stale_day, 20.0)
    _prediction(conn, "AAAA", stale_day, _point(25), decided=_decided(stale_day))

    card = C.collect_calibration_scorecard(conn)
    assert card["window"]["anchor"] is None
    assert card["window"]["anchor_state"] == "missing"
    assert card["station_day_count"] == 1


def test_a_station_day_scored_many_times_counts_once(conn):
    day = TODAY - timedelta(days=2)
    _truth(conn, "AAAA", day, 20.0)
    _truth(conn, "BBBB", day, 20.0)
    for copy in range(9):
        _prediction(conn, "AAAA", day, _point(22), decided=_decided(day),
                    copy=copy)
    _prediction(conn, "BBBB", day, _point(20), decided=_decided(day))

    card = C.collect_calibration_scorecard(conn)
    assert card["row_count"] == 10
    assert card["station_day_count"] == 2
    assert card["station_day_lead_count"] == 2
    # One vote each: (+2 + 0) / 2, not (9 x 2 + 0) / 10.
    assert card["rows"][0]["bias_rungs"] == pytest.approx(1.0)
    assert card["rows"][0]["station_days"] == 2


def test_fewer_than_eight_event_days_accumulate_and_fusion_keeps_its_colour(
    conn,
):
    for offset in range(1, 8):
        day = TODAY - timedelta(days=offset)
        _truth(conn, "AAAA", day, 20.0)
        _prediction(conn, "AAAA", day, _point(20), decided=_decided(day))

    card = C.collect_calibration_scorecard(conn)
    assert card["accumulating"] is True
    assert card["event_day_count"] == 7
    for layer in card["layers"]:
        if layer["key"] == "fusion":
            assert layer["health"] == "green"
        else:
            assert layer["health"] == "muted"
            assert "攒证据 7/8" in layer["verdict"]

    day = TODAY - timedelta(days=8)
    _truth(conn, "AAAA", day, 20.0)
    _prediction(conn, "AAAA", day, _point(20), decided=_decided(day))
    card = C.collect_calibration_scorecard(conn)
    assert card["accumulating"] is False
    assert _layer(card, "point")["health"] == "green"


def test_width_ratio_is_the_root_of_the_mean_variance(conn):
    # Day one: all mass on the truth, variance 0, error 0.
    # Day two: half on 18 and half on 22, variance 4, error 0 on truth 20;
    # day three: point forecast two rungs off, variance 0, error 2.
    days = [TODAY - timedelta(days=offset) for offset in (1, 2, 3)]
    forecasts = [_point(20), {"18": 0.5, "22": 0.5}, _point(22)]
    for day, forecast in zip(days, forecasts):
        _truth(conn, "AAAA", day, 20.0)
        _prediction(conn, "AAAA", day, forecast, decided=_decided(day))

    card = C.collect_calibration_scorecard(conn)
    root_mean_variance = (4.0 / 3.0) ** 0.5
    rmse = (4.0 / 3.0) ** 0.5
    assert card["rows"][0]["spread_skill"] == pytest.approx(
        root_mean_variance / rmse
    )
    # Averaging standard deviations would have read (2/3) / rmse = 0.58.
    assert card["rows"][0]["spread_skill"] == pytest.approx(1.0)


def test_randomised_pit_is_exactly_uniform_under_a_calibrated_forecast():
    forecast = {"17": 0.05, "18": 0.15, "19": 0.40, "20": 0.25, "21": 0.15}
    expected = [0.0] * C.PIT_BIN_COUNT
    for key, weight in forecast.items():
        scores = C._prediction_scores(forecast, CENTERS, float(key))
        for index, value in enumerate(scores["pit_bins"]):
            expected[index] += weight * value
    assert expected == pytest.approx([0.1] * C.PIT_BIN_COUNT, abs=1e-12)
    # The fixture is sharp enough to tell the two apart: the mid-PIT of this
    # forecast lands 40% of its mass in one bin.
    cumulative, mid = 0.0, [0.0] * C.PIT_BIN_COUNT
    for key in sorted(forecast, key=int):
        mid[min(9, int((cumulative + forecast[key] / 2) * 10))] += forecast[key]
        cumulative += forecast[key]
    assert max(abs(value - 0.1) for value in mid) > 0.2


def test_the_probability_sentence_is_worded_on_the_lamp_lines():
    good, warn = C.SCORECARD_THRESHOLDS["pit_deviation"]
    between = (good + warn) / 2
    assert C._grade(between, "pit_deviation") == "amber"
    assert "匀称" not in C._probability_verdict(0.5, between)
    assert "匀称" in C._probability_verdict(0.5, good / 2)


def test_the_calibration_sentence_names_its_points_once():
    sentence = C._calibration_verdict(
        {"ece": 0.007, "resolution": 0.01, "reliability": 0.001},
        0.077, 0.077,
    )
    assert "说少了 8 个百分点" in sentence
    assert "% 个百分点" not in sentence


def test_stability_resamples_whole_event_days():
    assert C._cluster_bootstrap_half_width(
        {f"d{index}": (1.0, 1.0) for index in range(7)}
    ) is None
    # Eight days of 100 rows each, the days alternating between errors of 0
    # and 2: rows agree within a day, so the uncertainty is the day's.
    clusters = {
        f"d{index}": (100.0 * (2.0 if index % 2 else 0.0), 100.0)
        for index in range(8)
    }
    assert C._cluster_bootstrap_half_width(clusters) > 0.3


def test_the_reliability_bins_count_weight_not_rows():
    decomposition = C._reliability_decomposition(
        [(0.6, 1.0, 0.25)] * 4 + [(0.6, 0.0, 1.0)]
    )
    top = decomposition["bins"][0]
    assert top["n"] == pytest.approx(2.0)
    assert top["happened"] == pytest.approx(0.5)
