"""Charter 201, step 2: every P0 carries the deterministic feature's shadow; none serves.

delta = k * ((R + b) - c), capped at 1.5 C: R the newest family run knowable
at the decision covering the afternoon, b its own debias, c the served mean,
k the station x source shrink the daily gate fitted.  The receipt says what
each source would move the centre by and, for the station's candidate, the
distribution the served layers would make of the shifted input.  The served
vector is never touched, and nothing in the shadow can stop a compile.

The gate is pre-registered: >= 20 event days, event-day upper bounds below
zero for dRPS, dRPS without the two best days and dRPS against the random-
sign control, and the non-zero-bucket log score not worse.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target import deterministic_feature_correction as DFC  # noqa: E402
from bayes_basket_target import regional_point_lane as L  # noqa: E402

ORDER = ["20°C or below", "21°C", "22°C", "23°C", "24°C", "25°C or higher"]
INTERVALS = {
    "20°C or below": [None, 20.5], "21°C": [20.5, 21.5], "22°C": [21.5, 22.5],
    "23°C": [22.5, 23.5], "24°C": [23.5, 24.5], "25°C or higher": [24.5, None],
}
SERVED = {"20°C or below": 0.05, "21°C": 0.2, "22°C": 0.4, "23°C": 0.25, "24°C": 0.08, "25°C or higher": 0.02}


def _aifs_row(run="2026-09-22T12:00:00Z", knowable="2026-09-22T18:00:00Z", peak=24.0):
    # SBGR 2026-09-23: 03, 09, 15, 21 local are 06Z, 12Z, 18Z, 00Z.
    curve = {"2026-09-23T06:00:00Z": 15.0, "2026-09-23T12:00:00Z": 19.0,
             "2026-09-23T18:00:00Z": peak, "2026-09-24T00:00:00Z": 18.0}
    return {"ensemble_system_id": "ECMWF_AIFS_SINGLE_POINT", "run_init_utc": run,
            "knowable_at_utc": knowable, "lead_hours_to_local_day_start": 15.0, "hourly_c": curve}


def _debias(bias):
    return {"systems": {"ECMWF_AIFS_SINGLE_POINT": {"stations": {"SBGR": {
        "station": {"bias_c": bias, "correction_form": "constant", "train_days": 30,
                    "fit_case_count": 30, "fit_through_date": "2026-09-21"},
        "cycles": {},
    }}}}}


def _gate(k, verdict="PASS", selected=True):
    return {"schema": DFC.GATE_SCHEMA, "fitted_at_utc": "2026-09-23T05:00:00Z", "stations": {"SBGR": {
        "sources": {"ECMWF_AIFS_SINGLE_POINT": {"k": k, "verdict": verdict}},
        "selected_system": "ECMWF_AIFS_SINGLE_POINT" if selected else None,
        "candidate_system": "ECMWF_AIFS_SINGLE_POINT",
    }}}


def _receipt(**kw):
    layers: list[dict] = []

    def served_layers(shifted):
        layers.append(dict(shifted))
        return dict(shifted)

    base = dict(
        station_id="SBGR", event_date="2026-09-23", decision_at_utc="2026-09-22T20:00:00Z",
        settlement_timezone="America/Sao_Paulo", served_probability=dict(SERVED),
        pre_layer_probability=dict(SERVED), ordered=ORDER, intervals=INTERVALS,
        served_layers=served_layers, rows=[_aifs_row()],
    )
    base.update(kw)
    return DFC.shadow_receipt(None, **base), layers


def test_the_receipt_reads_the_source_and_shifts_only_a_shadow():
    served_before = dict(SERVED)
    receipt, layers = _receipt(gate=_gate(0.5), debias_artifact=_debias(-2.0))
    centre = DFC.served_mean_c(SERVED, ORDER, INTERVALS)
    entry = receipt["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert entry["state"] == "READ" and entry["day_max_c"] == 24.0 and entry["bias_c"] == -2.0
    assert entry["gap_c"] == pytest.approx(22.0 - centre, abs=1e-4)
    assert entry["delta_c"] == pytest.approx(0.5 * (22.0 - centre), abs=1e-4)
    assert receipt["state"] == "SHADOW" and receipt["candidate"] == "ECMWF_AIFS_SINGLE_POINT"
    # The candidate's shadow is the served layers re-run on the shifted input.
    expected = DFC.shift_distribution(SERVED, ORDER, INTERVALS, entry["delta_c"])
    assert layers == [expected]
    assert receipt["shadow_probabilities"] == {k: round(v, 8) for k, v in expected.items()}
    assert SERVED == served_before, "the served vector is never touched"


def test_the_shift_is_capped():
    receipt, _layers = _receipt(gate=_gate(1.0), debias_artifact=_debias(-10.0 + 9.0 - 5.0))
    entry = receipt["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert entry["clipped"] is True and entry["delta_c"] == -DFC.CAP_C


def test_no_gate_record_means_k_zero_and_no_shadow_vector():
    receipt, layers = _receipt(gate={"schema": DFC.GATE_SCHEMA, "stations": {}}, debias_artifact=_debias(-2.0))
    entry = receipt["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert entry["k"] == 0.0 and entry["delta_c"] == 0.0 and entry["gate_state"] == "NO_RECORD"
    assert receipt["candidate"] is None and "shadow_probabilities" not in receipt and layers == []


def test_a_source_without_a_fitted_debias_is_read_but_not_used():
    receipt, _layers = _receipt(gate=_gate(0.5), debias_artifact={"systems": {}})
    entry = receipt["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert entry["state"] == "NO_DEBIAS" and "delta_c" not in entry
    assert receipt["candidate"] is None


def test_a_run_not_yet_knowable_is_not_read():
    receipt, _layers = _receipt(
        gate=_gate(0.5), debias_artifact=_debias(0.0),
        rows=[_aifs_row(knowable="2026-09-22T21:00:00Z")],
    )
    assert receipt["sources"] == {}


def test_nothing_in_the_shadow_can_stop_a_compile():
    def broken(_shifted):
        raise RuntimeError("layer exploded")

    receipt, _layers = _receipt(gate=_gate(0.5), debias_artifact=_debias(-2.0), served_layers=broken)
    assert receipt["state"] == "ERROR" and "layer exploded" in receipt["error"]


# ---------------------------------------------------------------------------
# the gate fitter, on a synthetic settled record
# ---------------------------------------------------------------------------

def _ssot(tmp_path, *, informative: bool, days: int = 45):
    """A station whose served centre misses by what the source knows (or not)."""

    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(db)
    L.ensure_schema(conn)
    conn.executescript("""
    CREATE TABLE calibration_station_registry(station_id TEXT, timezone_name TEXT);
    CREATE TABLE basket_station_day_settlement_label(station_id TEXT, event_date TEXT, truth_revision_no INT,
        settled_tmax_c REAL);
    CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT, official_tmax_c REAL,
        source_lane TEXT, qc_status TEXT, authority_scope TEXT, revision_no INT);
    CREATE TABLE basket_probability_envelope(envelope_id TEXT, station_id TEXT, event_date TEXT, state TEXT,
        decision_at_utc TEXT, fresh_until_utc TEXT, stage TEXT, settlement_timezone TEXT, probabilities_json TEXT,
        bucket_ordinals_json TEXT, lineage_json TEXT);
    CREATE INDEX basket_probability_envelope_asof_idx ON basket_probability_envelope(station_id,event_date,state,
        decision_at_utc,fresh_until_utc,stage);
    """)
    conn.execute("INSERT INTO calibration_station_registry VALUES('SBGR','America/Sao_Paulo')")
    today = date(2026, 9, 23)
    import random  # noqa: PLC0415

    rng = random.Random(7)
    ordinals = {k: i for i, k in enumerate(ORDER)}
    for i in range(days, 0, -1):
        day = today - timedelta(days=i)
        truth = 22.0 + rng.choice([-1.0, 0.0, 1.0, 2.0, -2.0])
        forecast = truth + 3.0 if informative else 22.0 + rng.choice([-2.0, -1.0, 1.0, 2.0]) + 3.0
        conn.execute("INSERT INTO basket_station_day_settlement_label VALUES(?,?,?,?)",
                     ("SBGR", day.isoformat(), 0, truth))
        conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?)",
                     ("SBGR", day.isoformat(), truth, "AWC", "ACCEPTED", "FORMAL_NATIVE_ELIGIBLE", 0))
        run = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=12)
        L.write_run(conn, product="AIFS", station_id="SBGR", event_date=day,
                    settlement_timezone="America/Sao_Paulo", run_init=run,
                    hourly_c={12 + 6 * j: (forecast if j == 3 else forecast - 6) for j in range(4)},
                    hours_wanted=4, archive_query_at=run + timedelta(hours=6), source_url="gcs://x",
                    backfill=True)
        decision = (run + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"e{i}", "SBGR", day.isoformat(), "COMMITTED", decision, decision, "P0", "America/Sao_Paulo",
             json.dumps(SERVED), json.dumps(ordinals),
             json.dumps({"bucket_intervals_c_json": json.dumps(INTERVALS)})),
        )
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("informative,verdict", [(True, "PASS"), (False, "FAIL")])
def test_the_gate_passes_only_a_source_that_knows_the_miss(tmp_path, monkeypatch, informative, verdict):
    import fit_deterministic_feature_gate as G  # noqa: PLC0415
    from bayes_basket_target import regional_point_debias as D  # noqa: PLC0415

    monkeypatch.setattr(D, "MIN_TRAIN_DAYS", 3)
    db = _ssot(tmp_path, informative=informative)
    payload = G.fit(db, now=datetime(2026, 9, 23, 6, tzinfo=timezone.utc))
    stat = payload["stations"]["SBGR"]["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert stat["verdict"] == verdict, stat
    if informative:
        assert stat["k"] > 0.5 and payload["stations"]["SBGR"]["selected_system"] == "ECMWF_AIFS_SINGLE_POINT"
    else:
        assert payload["stations"]["SBGR"]["selected_system"] is None


def test_a_real_compile_serves_the_same_vector_whatever_the_shadow_says(monkeypatch):
    """Compile the mixed-cycle fixture twice: once with the shadow reading a
    source that would move the centre by a whole degree, once with the shadow
    switched off.  The served vector is the same to the byte; only the
    lineage says what the correction would have served."""

    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from bayes_basket_target import native_ensemble_oos, station_season  # noqa: PLC0415
    from bayes_basket_target import native_ensemble_p0 as p0  # noqa: PLC0415
    from tests.test_native_ensemble_p0 import (  # noqa: PLC0415
        _install_mixed_cycle_calibrations, _mixed_packet,
    )

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)
    real = DFC.shadow_receipt

    def loud(conn, **kw):
        zone = ZoneInfo(kw["settlement_timezone"])
        start = datetime.combine(date.fromisoformat(kw["event_date"]), datetime.min.time(), tzinfo=zone)
        curve = {
            (start + timedelta(hours=h)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"): 40.0
            for h in range(24)
        }
        row = {"ensemble_system_id": "DWD_ICON_EU_POINT", "run_init_utc": "2026-06-30T00:00:00Z",
               "knowable_at_utc": "2026-06-30T05:00:00Z", "lead_hours_to_local_day_start": 20.0,
               "hourly_c": curve}
        station = kw["station_id"]
        gate = {"schema": DFC.GATE_SCHEMA, "stations": {station: {
            "sources": {"DWD_ICON_EU_POINT": {"k": 1.0, "verdict": "PASS"}},
            "selected_system": "DWD_ICON_EU_POINT", "candidate_system": "DWD_ICON_EU_POINT"}}}
        debias = {"systems": {"DWD_ICON_EU_POINT": {"stations": {station: {
            "station": {"bias_c": 0.0, "correction_form": "constant", "train_days": 30,
                        "fit_case_count": 30, "fit_through_date": "2026-06-29"}, "cycles": {}}}}}}
        return real(conn, **dict(kw, rows=[row], gate=gate, debias_artifact=debias))

    def compile_once(shadow):
        monkeypatch.setattr(p0, "deterministic_feature_shadow_receipt", shadow)
        conn = sqlite3.connect(":memory:")
        _install_mixed_cycle_calibrations(conn)
        compiled = p0.compile_native_ensemble_p0(
            conn, forward_packet=_mixed_packet(), decision_at_utc="2026-07-01T13:00:00Z",
        )
        return compiled["packet"]["native_probability_envelope"]

    loud_envelope = compile_once(loud)
    quiet_envelope = compile_once(lambda conn, **kw: {"state": "OFF"})
    assert loud_envelope["probabilities"] == quiet_envelope["probabilities"]
    receipt = json.loads(loud_envelope["lineage"][DFC.LINEAGE_KEY])
    assert receipt["state"] == "SHADOW" and receipt["candidate"] == "DWD_ICON_EU_POINT"
    assert receipt["candidate_delta_c"] == DFC.CAP_C
    assert receipt["shadow_probabilities"] != {
        k: round(float(v), 8) for k, v in loud_envelope["probabilities"].items()
    }


def test_the_debias_the_shadow_reads_follows_the_daily_refit(tmp_path):
    from bayes_basket_target.regional_point_debias import SCHEMA  # noqa: PLC0415
    import os  # noqa: PLC0415

    path = tmp_path / "debias.json"
    path.write_text(json.dumps({"schema": SCHEMA, "systems": {"A": {}}}))
    assert set(DFC.load_debias_artifact(path)["systems"]) == {"A"}
    path.write_text(json.dumps({"schema": SCHEMA, "systems": {"B": {}}}))
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert set(DFC.load_debias_artifact(path)["systems"]) == {"B"}
    path.write_text(json.dumps({"schema": "other", "systems": {}}))
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 2_000_000))
    assert DFC.load_debias_artifact(path) is None
