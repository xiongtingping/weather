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
    # The board's reads seek on the station list (query-plan guard); the
    # registry holds every station these rows name.
    db.execute(
        "CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
        " FROM calibration_cross_native_ensemble_oos_prediction"
    )
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


def test_interval_coverage_equals_nominal_under_a_calibrated_forecast():
    # Whole-rung intervals would over-cover on this ladder; spread over the
    # truth rung's PIT span, the calibrated expectation is exactly 1 - alpha.
    forecast = {"17": 0.05, "18": 0.15, "19": 0.40, "20": 0.25, "21": 0.15}
    for level in C.COVERAGE_LEVELS:
        name = f"coverage_{round(level * 100)}"
        expected = sum(
            weight * C._prediction_scores(forecast, CENTERS, float(key))[name]
            for key, weight in forecast.items()
        )
        assert expected == pytest.approx(level, abs=1e-12)


def test_an_overconfident_forecast_under_covers():
    # All mass on 20 while the truth spreads 19/20/21: the 90% interval
    # catches only the middle third.
    forecast = _point(20)
    covered = sum(
        C._prediction_scores(forecast, CENTERS, float(key))["coverage_90"]
        for key in ("19", "20", "21")
    ) / 3
    assert covered == pytest.approx(0.9 / 3)


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


def test_the_reliability_bins_summed_as_read_equal_the_per_pair_decomposition() -> None:
    # 2026-10-20 (user 「看板研究渲染为什么这么大」): the scorecard sums each
    # group's bins as it reads instead of keeping 1.37M rows' pairs; the
    # decomposition must read the same off the sums.
    import random
    from collections import defaultdict

    rnd = random.Random(3)
    pairs = [(rnd.random() ** 3, float(rnd.random() < 0.3), rnd.choice([1.0, 0.5, 0.25]))
             for _ in range(2000)]
    buckets: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for promised, happened, weight in pairs:
        cell = buckets[C._reliability_bin(promised)]
        cell[0] += weight * promised
        cell[1] += weight * happened
        cell[2] += weight
    by_pairs = C._reliability_decomposition(pairs)
    by_bins = C._reliability_from_buckets(buckets)
    for key in ("ece", "reliability", "resolution", "uncertainty"):
        assert by_bins[key] == pytest.approx(by_pairs[key], rel=1e-12)
    assert [row["bin"] for row in by_bins["bins"]] == [row["bin"] for row in by_pairs["bins"]]


def _memo_store(rows_per_scope: int = 3):
    import math
    import random
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_station_registry(station_id TEXT PRIMARY KEY);"
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT,"
        " official_tmax_c REAL, qc_status TEXT, revision_no INTEGER);"
        "CREATE TABLE calibration_cross_native_ensemble_oos_prediction(station_id TEXT,"
        " event_date TEXT, lead_bucket TEXT, probabilities_json TEXT,"
        " bucket_centers_c_json TEXT, decision_at_utc TEXT);"
    )
    rnd = random.Random(5)
    for station in ("ZUUU", "KLGA"):
        conn.execute("INSERT INTO calibration_station_registry VALUES(?)", (station,))
        for back in range(1, 20):
            day = (TODAY - timedelta(days=back)).isoformat()
            truth = 20 + rnd.uniform(-4, 4)
            conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,'ACCEPTED',1)",
                         (station, day, truth))
            for lead in ("h00_12", "h24_48"):
                for _ in range(rows_per_scope):
                    mu, sd = truth + rnd.gauss(0, 1.2), rnd.uniform(0.7, 2.0)
                    weights = {key: math.exp(-0.5 * ((CENTERS[key] - mu) / sd) ** 2) for key in KEYS}
                    total = sum(weights.values())
                    decided = (TODAY - timedelta(days=back, hours=rnd.randint(0, 60))).isoformat()
                    conn.execute(
                        "INSERT INTO calibration_cross_native_ensemble_oos_prediction VALUES(?,?,?,?,?,?)",
                        (station, day, lead, json.dumps({k: v / total for k, v in weights.items()}),
                         json.dumps(CENTERS), f"{decided}T06:00:00Z"),
                    )
    return conn


def _without_ages(value):
    if isinstance(value, dict):
        return {k: _without_ages(v) for k, v in value.items() if "age_hours" not in k}
    if isinstance(value, list):
        return [_without_ages(v) for v in value]
    return value


def test_a_settled_day_is_scored_once_and_read_back_identically(tmp_path, monkeypatch) -> None:
    # 2026-10-20 (user 「为什么要全量读历史」 / 「改」): the 45-day window was
    # re-read and re-scored every three hours, three times over.
    conn = _memo_store()
    window = {"since": f"{(TODAY - timedelta(days=15)).isoformat()}T13:00:00Z",
              "until": "9999-12-31T00:00:00Z", "version": "t"}
    unpaged = C.collect_calibration_scorecard(conn, window_override=window)
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    first = C.collect_calibration_scorecard(conn, window_override=window)
    kept = json.loads((tmp_path / C.RENDER_CLOSED_SCORECARD_DAYS).read_text())["days"]
    assert kept, "the closed days are kept"
    reads: list[str] = []
    conn.set_trace_callback(reads.append)
    second = C.collect_calibration_scorecard(conn, window_override=window)
    conn.set_trace_callback(None)
    assert _without_ages(first) == _without_ages(unpaged)
    assert _without_ages(second) == _without_ages(unpaged)
    whole_reads = [sql for sql in reads if "probabilities_json" in sql]
    # Read again: only the days the anchor cuts.  Open days are held too
    # (user 2026-11-03 「把它改成只读按日增量的部分」) and, unchanged, not read.
    expected = conn.execute(
        "SELECT COUNT(*) FROM (SELECT event_date, MIN(decision_at_utc) AS first,"
        " MAX(decision_at_utc) AS last FROM calibration_cross_native_ensemble_oos_prediction"
        " GROUP BY station_id, event_date)"
        " WHERE last >= ? AND first < ?",
        (window["since"], window["since"]),
    ).fetchone()[0]
    assert 0 < len(whole_reads) == expected, (len(whole_reads), expected)


def test_a_late_row_on_a_kept_day_is_read_again(tmp_path, monkeypatch) -> None:
    conn = _memo_store()
    window = {"since": f"{(TODAY - timedelta(days=30)).isoformat()}T00:00:00Z",
              "until": "9999-12-31T00:00:00Z", "version": "t"}
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    C.collect_calibration_scorecard(conn, window_override=window)
    day = (TODAY - timedelta(days=12)).isoformat()
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction"
        " SELECT * FROM calibration_cross_native_ensemble_oos_prediction"
        " WHERE station_id='ZUUU' AND event_date=? AND lead_bucket='h00_12' LIMIT 1", (day,))
    kept_again = C.collect_calibration_scorecard(conn, window_override=window)
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    fresh = C.collect_calibration_scorecard(conn, window_override=window)
    assert _without_ages(kept_again) == _without_ages(fresh)


def test_a_grown_day_reads_only_its_new_rows(tmp_path, monkeypatch) -> None:
    # User 2026-11-03 「把它改成只读按日增量的部分」: a held day that grew is
    # read past its held rowid only, and the sums equal a whole read.
    conn = _memo_store()
    window = {"since": f"{(TODAY - timedelta(days=30)).isoformat()}T00:00:00Z",
              "until": "9999-12-31T00:00:00Z", "version": "t"}
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    C.collect_calibration_scorecard(conn, window_override=window)
    for back in (1, 12):
        conn.execute(
            "INSERT INTO calibration_cross_native_ensemble_oos_prediction"
            " SELECT * FROM calibration_cross_native_ensemble_oos_prediction"
            " WHERE station_id='ZUUU' AND event_date=? AND lead_bucket='h24_48' LIMIT 2",
            ((TODAY - timedelta(days=back)).isoformat(),))
    reads: list[str] = []
    conn.set_trace_callback(reads.append)
    grown = C.collect_calibration_scorecard(conn, window_override=window)
    conn.set_trace_callback(None)
    row_reads = [sql for sql in reads if "probabilities_json" in sql]
    assert len(row_reads) == 2 and all("rowid>0" not in sql for sql in row_reads), row_reads
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    assert _without_ages(grown) == _without_ages(C.collect_calibration_scorecard(conn, window_override=window))


def test_a_day_that_lost_rows_is_read_whole(tmp_path, monkeypatch) -> None:
    # Cold offload deletes superseded rows; the held sums still count them.
    conn = _memo_store()
    window = {"since": f"{(TODAY - timedelta(days=30)).isoformat()}T00:00:00Z",
              "until": "9999-12-31T00:00:00Z", "version": "t"}
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    C.collect_calibration_scorecard(conn, window_override=window)
    day = (TODAY - timedelta(days=7)).isoformat()
    conn.execute(
        "DELETE FROM calibration_cross_native_ensemble_oos_prediction WHERE rowid IN (SELECT rowid FROM"
        " calibration_cross_native_ensemble_oos_prediction WHERE station_id='KLGA' AND event_date=? LIMIT 2)", (day,))
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction"
        " SELECT * FROM calibration_cross_native_ensemble_oos_prediction"
        " WHERE station_id='KLGA' AND event_date=? LIMIT 1", (day,))
    reads: list[str] = []
    conn.set_trace_callback(reads.append)
    after = C.collect_calibration_scorecard(conn, window_override=window)
    conn.set_trace_callback(None)
    assert [sql for sql in reads if "probabilities_json" in sql and "rowid>0" in sql], reads
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    assert _without_ages(after) == _without_ages(C.collect_calibration_scorecard(conn, window_override=window))
