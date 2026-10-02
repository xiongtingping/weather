"""Charter 157: P1's clock is shifted by each station's own peak hour
(2026-09-19, 「测站点峰时」→「上线」).

Rung one and alpha read hours-left + offset, where the offset is the
station's historical peak hour minus the fleet's (RPLL -2.4h, LEMD +2.4h);
the continuation keeps the true hours left.  An artifact without the table
serves exactly what v13 served.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target import metar_four_layer as M4
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _artifact, _compile
from tests.test_charter_156_p1_smooth_clock import _curved, _features

ROOT = Path(__file__).resolve().parents[1]
BUCKETS = {"20": 0, "21": 1, "22": 2, "23": 3}
CENTRES = {"20": 20.0, "21": 21.0, "22": 22.0, "23": 23.0}


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_p1_charter157", ROOT / "scripts" / "fit_p1_observation_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _shifted(**offsets):
    art = _curved()
    art["phase"]["clock"] = M4.CLOCK_MIDPOINT_LINEAR
    art[OL.STATION_CLOCK_OFFSET_KEY] = offsets
    return art


def test_the_offset_is_read_per_station_and_zero_when_absent() -> None:
    art = _shifted(RPLL=-2.4, LEMD=2.4)
    assert OL.station_clock_offset(art, "RPLL") == pytest.approx(-2.4)
    assert OL.station_clock_offset(art, "lemd") == pytest.approx(2.4)
    assert OL.station_clock_offset(art, "ZUUU") == 0.0
    assert OL.station_clock_offset(art, None) == 0.0
    assert OL.station_clock_offset(_curved(), "RPLL") == 0.0
    assert OL.clock_hours(9.0, art, "RPLL") == pytest.approx(6.6)
    assert OL.clock_hours(1.0, art, "RPLL") == 0.0          # never below zero
    assert OL.clock_hours(9.0, art, "LEMD") == pytest.approx(11.4)


@pytest.mark.parametrize("table", [{"RPLL": float("nan")}, {"RPLL": 7.0}, {"RPLL": "x"}, ["RPLL"]])
def test_a_malformed_offset_is_refused(table) -> None:
    art = _curved()
    art[OL.STATION_CLOCK_OFFSET_KEY] = table
    with pytest.raises(ContractError):
        OL.station_clock_offset(art, "RPLL")


def test_rise_and_alpha_read_the_shifted_clock_and_the_continuation_does_not() -> None:
    art = _shifted(RPLL=-2.4)
    feats = _features(9.0)
    evidence, rise = OL.evidence_by_bucket(
        feats, art, bucket_ordinals=BUCKETS, bucket_centers_c=CENTRES,
        running_high_c=20.6, running_high_bucket="21", station_id="RPLL",
    )
    assert rise == pytest.approx(M4.phase_rise_probability({**feats, "remaining_hours": 6.6}, art))
    assert rise != pytest.approx(M4.phase_rise_probability(feats, art))
    # How far a rise runs is read on the true hours left.
    cont = M4._continuation_distribution(feats, art["continuation_full"], art["feature_fallbacks"])
    assert evidence["22"] / rise == pytest.approx(cont[1])
    _, receipt = OL.compile_observation_likelihood(
        feats, art, bucket_ordinals=BUCKETS, bucket_centers_c=CENTRES,
        running_high_c=20.6, running_high_bucket="21", station_id="RPLL",
    )
    assert receipt["station_clock_offset_h"] == pytest.approx(-2.4)
    assert receipt["clock_hours"] == pytest.approx(6.6)
    assert receipt["alpha"] == pytest.approx(OL.alpha_for(6.6, art))
    assert receipt["features"]["remaining_hours"] == pytest.approx(9.0)


def test_without_the_table_v13_is_served_unchanged() -> None:
    art = _curved()
    art["phase"]["clock"] = M4.CLOCK_MIDPOINT_LINEAR
    feats = _features(9.0)
    kw = dict(bucket_ordinals=BUCKETS, bucket_centers_c=CENTRES, running_high_c=20.6, running_high_bucket="21")
    with_station, r1 = OL.compile_observation_likelihood(feats, art, station_id="RPLL", **kw)
    without, r2 = OL.compile_observation_likelihood(feats, art, **kw)
    assert with_station == without
    assert r1["clock_hours"] == pytest.approx(9.0) and r1["station_clock_offset_h"] == 0.0


def test_the_p1_compile_hands_the_station_to_the_likelihood() -> None:
    root, _ = _compile(_artifact())
    art = _artifact()
    art[OL.STATION_CLOCK_OFFSET_KEY] = {root.station_id: 1.5}
    _, packets = _compile(art)
    receipt = json.loads(packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert receipt["state"] == "APPLIED"
    assert receipt["station_clock_offset_h"] == pytest.approx(1.5)
    assert receipt["clock_hours"] == pytest.approx(receipt["features"]["remaining_hours"] + 1.5, abs=1e-3)


def test_the_clock_intercept_is_the_one_serving_reads() -> None:
    art = _curved()
    phase = dict(art["phase"])
    assert M4.phase_clock_intercept(9.99, phase) == pytest.approx(0.0)      # bins: [6,10)
    phase["clock"] = M4.CLOCK_MIDPOINT_LINEAR
    assert M4.phase_clock_intercept(8.0, phase) == pytest.approx(0.0)       # midpoint of [6,10)
    assert 0.0 < M4.phase_clock_intercept(9.99, phase) < 1.5


def _archive(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE reported_trajectory_observations (station_id TEXT, source_lane TEXT,"
        " valid_at TEXT, raw_text TEXT, reported_temperature_c INTEGER)"
    )
    zone = ZoneInfo("UTC")
    for station, peak_hour in (("EARLY", 12), ("LATE", 18), ("MID", 15)):
        for day in range(1, 21):
            start = datetime(2026, 8, day, 0, tzinfo=zone)
            for hour in range(24):
                temp = 20 + (8 - abs(hour - peak_hour)) if abs(hour - peak_hour) < 8 else 20
                valid = (start + timedelta(hours=hour)).astimezone(timezone.utc)
                conn.execute(
                    "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?,?)",
                    (station, "awc", valid.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     f"METAR {station} {valid:%d%H%M}Z 00000KT CAVOK {temp:02d}/10 Q1013", temp),
                )
    conn.commit()
    conn.close()


def test_station_peak_offsets_read_the_first_hour_at_the_max_and_shrink(tmp_path) -> None:
    fitter = _fitter()
    archive = tmp_path / "archive.db"
    _archive(archive)
    zones = {"EARLY": "UTC", "LATE": "UTC", "MID": "UTC", "NONE": "UTC"}
    offsets, meta = fitter.station_peak_offsets(archive, zones=zones, before_date="2026-08-21", window_days=45)
    assert meta["state"] == "OK" and meta["fleet_peak_local_h"] == pytest.approx(15.0)
    # 20 days each, shrunk with K=10: 20/30 of the raw -3h / +3h.
    assert offsets["EARLY"] == pytest.approx(-2.0)
    assert offsets["LATE"] == pytest.approx(2.0)
    assert offsets["MID"] == pytest.approx(0.0)
    assert offsets["NONE"] == pytest.approx(0.0)           # no days: the fleet
    # Only days before the bound count: none of these are.
    empty, meta2 = fitter.station_peak_offsets(archive, zones=zones, before_date="2026-07-01", window_days=10)
    assert empty == {} and meta2["state"] == "NO_DAYS"
    missing, meta3 = fitter.station_peak_offsets(tmp_path / "absent.db", zones=zones, before_date="2026-08-21")
    assert missing == {} and meta3["state"].startswith("UNAVAILABLE")


def test_fit_g_learns_rung_one_on_the_shifted_clock_and_publishes_the_table() -> None:
    fitter = _fitter()
    import numpy as np
    rng = np.random.default_rng(5)
    samples = []
    for i in range(500):
        station = "EARLY" if i % 2 else "LATE"
        hours = float(rng.uniform(0.0, 20.0))
        samples.append({
            "station_id": station, "event_date": f"2026-09-{1 + i % 20:02d}",
            "remaining_hours": hours, "station_day_weight": 1.0,
            "crossed_bucket": int(rng.random() < min(0.9, hours / 20.0)),
            "delta_c": int(rng.integers(0, 4)),
            **{name: float(rng.normal()) for name in OL.FULL_FEATURES if name != "remaining_hours"},
        })
    offsets = {"EARLY": -2.0, "LATE": 2.0}
    body = fitter.fit_g(samples, station_clock_offsets=offsets, station_clock_meta={"state": "OK"})
    assert body[OL.STATION_CLOCK_OFFSET_KEY] == offsets and body["station_clock"]["state"] == "OK"
    shifted = [{**s, "remaining_hours": max(0.0, s["remaining_hours"] + offsets[s["station_id"]])} for s in samples]
    direct = fitter.fit_phase_rung(shifted, body["feature_fallbacks"], OL.PHASE_FEATURES)
    assert body["phase"]["bin_intercepts"] == pytest.approx(direct["bin_intercepts"])
    plain = fitter.fit_g(samples)
    assert OL.STATION_CLOCK_OFFSET_KEY not in plain


def test_the_receipt_carries_the_rise_logit_term_by_term() -> None:
    """User ruling 2026-10-02 「只读是读后端算出来的」: the board shows the
    decomposition the receipt stored instead of recomputing it, so the
    receipt's terms must add up to the very logit served, on the clock the
    rise was read at."""

    import math

    art = _shifted(RPLL=-2.4)
    feats = _features(9.0)
    _, receipt = OL.compile_observation_likelihood(
        feats, art, bucket_ordinals=BUCKETS, bucket_centers_c=CENTRES,
        running_high_c=20.6, running_high_bucket="21", station_id="RPLL",
    )
    terms = receipt["rise_terms"]
    assert terms["eta"] == pytest.approx(
        terms["intercept"] + sum(t["contribution"] for t in terms["terms"]), abs=1e-5
    )
    assert 1.0 / (1.0 + math.exp(-terms["eta"])) == pytest.approx(
        receipt["rise_probability"], abs=1e-6
    )
    assert terms["intercept"] == pytest.approx(
        M4.phase_clock_intercept(6.6, art["phase"]), abs=1e-6
    )
    assert {t["feature"] for t in terms["terms"]} == set(art["phase"]["coefficients"])
