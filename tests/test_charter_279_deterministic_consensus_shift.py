"""Charter 279: the deterministic consensus F as a P0 centre feature -- shadow.

User ruling 2026-10-22 「改，先影子，尝试测试逐站偏差！放在p0卡片里对比」.  Every P0
and P0-M compile records what the consensus would move its centre by under the
fleet bias and the station bias (two rosters each) and the vector it would
serve; the served vector is untouched.  The daily fit scores each variant
walk-forward under charter 240.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target import deterministic_consensus_shift as DCS  # noqa: E402
from bayes_basket_target.regional_point_lane import SYSTEM_IDS  # noqa: E402

ORDER = ["20°C or below", "21°C", "22°C", "23°C", "24°C", "25°C or higher"]
INTERVALS = {
    "20°C or below": [None, 20.5], "21°C": [20.5, 21.5], "22°C": [21.5, 22.5],
    "23°C": [22.5, 23.5], "24°C": [23.5, 24.5], "25°C or higher": [24.5, None],
}
SERVED = {"20°C or below": 0.05, "21°C": 0.2, "22°C": 0.4, "23°C": 0.25, "24°C": 0.08, "25°C or higher": 0.02}


def _features(**values):
    return {SYSTEM_IDS[product]: {"day_max_c": value, "run_init_utc": "2026-10-20T00:00:00Z",
                                  "knowable_at_utc": "2026-10-20T05:00:00Z", "lead_hours_to_local_day_start": 20.0}
            for product, value in values.items()}


def _read(**values):
    read = DCS.consensus_from_features(_features(**values))
    read.update(state="ok", as_of_utc="2026-10-20T09:00:00Z")
    return read


def _fit(family="P0_far", **over):
    variants = {
        "fleet_core": {"bias_c": 0.85, "kappa": 0.33},
        "station_core": {"bias_c": 0.85, "kappa": 0.30, "station_bias_c": {"SBGR": 0.40}},
        "fleet_wide": {"bias_c": 0.90, "kappa": 0.35},
        "station_wide": {"bias_c": 0.90, "kappa": 0.32, "station_bias_c": {}},
    }
    variants.update(over)
    return {"schema": DCS.FIT_SCHEMA, "fitted_at_utc": "2026-10-22T06:00:00Z",
            "families": {family: {"variants": variants}}}


def test_the_stage_families_split_p0_at_36_hours_and_p1_is_not_read():
    assert DCS.stage_family("P0", 54.6) == "P0_far"
    assert DCS.stage_family("P0", 36.0) == "P0_near"
    assert DCS.stage_family("P0", 12.0) == "P0_near"
    assert DCS.stage_family("P0", 11.9) is None
    assert DCS.stage_family("P0-M", 5.0) == "P0M"
    assert DCS.stage_family("P1", 5.0) is None
    assert DCS.stage_family("P0", None) is None


def test_f_is_the_median_of_the_roster_and_needs_three_systems():
    read = _read(AIFS=19.1, IFS9KM=19.2, UKMO10=19.3, CMA=15.5, GDPS=19.9, ICON_GLOBAL=18.2)
    assert read["n"] == {"core": 4, "wide": 6}
    assert read["f_c"]["core"] == pytest.approx((19.1 + 19.2) / 2)       # the CMA outlier does not pull the median
    assert read["f_c"]["wide"] == pytest.approx((19.1 + 19.2) / 2)
    assert set(read["systems"]) == {"AIFS", "IFS9KM", "UKMO10", "CMA", "GDPS", "ICON_GLOBAL"}
    thin = _read(AIFS=19.1, IFS9KM=19.2, GDPS=19.9)
    assert thin["f_c"]["core"] is None and thin["n"]["core"] == 2         # two core systems: no core F
    assert thin["f_c"]["wide"] == pytest.approx(19.2)
    assert DCS.ROSTERS["core"] < DCS.ROSTERS["wide"] and len(DCS.ROSTERS["wide"]) == 10


def test_the_receipt_moves_a_shadow_by_kappa_times_the_corrected_gap_and_serves_nothing():
    served = dict(SERVED)
    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0), fit=_fit(),
                                 family="P0_far", station_id="SBGR", served_probability=served,
                                 ordered=ORDER, intervals=INTERVALS)
    assert receipt["state"] == "SHADOW" and receipt["served_delta_c"] == 0.0 and served == SERVED
    assert receipt["rule"] == DCS.RULE_VERSION and receipt["primary"] == "fleet_core"
    centre = receipt["centre_c"]
    fleet = receipt["variants"]["fleet_core"]
    station = receipt["variants"]["station_core"]
    assert fleet["state"] == station["state"] == "READ"
    f = (23.0 + 23.2) / 2
    assert fleet["delta_c"] == pytest.approx(0.33 * (f + 0.85 - centre), abs=1e-3)
    assert station["bias_source"] == "station" and station["bias_c"] == pytest.approx(0.40)
    assert station["delta_c"] == pytest.approx(0.30 * (f + 0.40 - centre), abs=1e-3)
    # The two shadows differ, both sit warm of the served vector, and each is a floored ladder vector.
    assert fleet["shadow_centre_c"] > centre and station["shadow_centre_c"] > centre
    assert fleet["shadow_centre_c"] != station["shadow_centre_c"]
    for v in (fleet, station):
        assert sum(v["shadow_probabilities"].values()) == pytest.approx(1.0, abs=1e-6)
        assert min(v["shadow_probabilities"].values()) >= 1e-3 - 1e-9
    # The station variant without a station entry falls back to the fleet bias and says so.
    assert receipt["variants"]["station_wide"]["bias_source"] == "fleet_fallback"


def test_the_shift_is_capped_and_the_silent_states_are_named():
    read = _read(AIFS=40.0, IFS9KM=40.0, UKMO10=40.0, CMA=40.0)
    receipt = DCS.shadow_receipt(read=read, fit=_fit(), family="P0_far", station_id="SBGR",
                                 served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert receipt["variants"]["fleet_core"]["delta_c"] == DCS.CAP_C and receipt["variants"]["fleet_core"]["clipped"]
    no_fit = DCS.shadow_receipt(read=read, fit=None, family="P0_far", station_id="SBGR",
                                served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert no_fit["state"] == "NO_FIT" and no_fit["variants"]["fleet_core"]["state"] == "NO_FIT"
    thin = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.1), fit=_fit(), family="P0_far", station_id="SBGR",
                              served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert thin["variants"]["fleet_core"]["state"] == "TOO_FEW_SYSTEMS"
    assert thin["variants"]["fleet_wide"]["state"] == "TOO_FEW_SYSTEMS"
    p1 = DCS.shadow_receipt(read=read, fit=_fit(), family=None, station_id="SBGR",
                            served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert p1["state"] == "NOT_READ_AT_STAGE" and p1["variants"] == {}
    broken = DCS.shadow_receipt(read={"state": "ERROR", "error": "x"}, fit=_fit(), family="P0_far", station_id="SBGR",
                                served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert broken["state"] == "NO_READ"


def _synthetic_records(*, kappa_true=0.5, bias_true=0.8, days=30, stations=6, seed=279):
    """Station-days whose truth sits kappa_true of the way from the served centre to F + bias_true."""

    rng = random.Random(seed)
    records = []
    for d in range(days):
        day = f"2026-09-{d + 1:02d}" if d < 30 else f"2026-10-{d - 29:02d}"
        for s in range(stations):
            centre = 22.0 + rng.uniform(-1.5, 1.5)
            f = centre + rng.uniform(-2.5, 2.5) - bias_true           # F runs cold of where it points
            y = centre + kappa_true * (f + bias_true - centre) + rng.gauss(0.0, 0.6)
            # the served vector: a bell on the ladder about the centre
            raw = {k: math.exp(-0.5 * ((c - centre) / 1.1) ** 2) for k, c in zip(ORDER, [20.0, 21, 22, 23, 24, 25.0])}
            total = sum(raw.values())
            p = {k: v / total for k, v in raw.items()}
            winner = min(ORDER, key=lambda k: abs({"20°C or below": 20.0, "25°C or higher": 25.0}.get(k, float(k[:2])) - y))
            records.append({"station": f"S{s}", "day": day, "family": "P0_far", "centre_c": centre,
                            "f_c": {"core": f, "wide": f}, "y_c": y, "p": p, "ordered": ORDER,
                            "intervals": INTERVALS, "winner": winner})
    return records


def test_the_fit_recovers_the_bias_and_kappa_and_the_walk_forward_passes_where_the_feature_is_real():
    records = _synthetic_records()
    params = DCS.fit_parameters(records, family="P0_far")
    assert set(params) == set(DCS.VARIANTS)
    assert params["fleet_core"]["bias_c"] == pytest.approx(0.8, abs=0.15)
    assert params["fleet_core"]["kappa"] == pytest.approx(0.5, abs=0.12)
    assert params["station_core"]["station_bias_c"] and params["station_core"]["station_bias_prior_days"] == 10.0
    # Before the eighth event day nothing is fitted.
    assert DCS.fit_parameters(records, family="P0_far", before_day="2026-09-05") == {}
    forward = DCS.walk_forward(records, family="P0_far")
    fleet = forward["fleet_core"]
    assert fleet["n_days"] >= DCS.VERDICT_MIN_EVENT_DAYS
    assert fleet["logscore"]["delta"] < 0 and fleet["logscore"]["ci"][1] < 0
    assert fleet["verdict"] == "PASS" and fleet["days_better"] > fleet["n_days"] // 2
    assert "S0" in fleet["stations"]
    # Pure noise: nothing to find, and the verdict says so.
    noise = _synthetic_records(kappa_true=0.0, bias_true=0.0, seed=7)
    assert DCS.walk_forward(noise, family="P0_far")["fleet_core"]["verdict"] in {"REFUSED", "INSUFFICIENT"}


def test_the_verdict_is_charter_240s():
    base = {"n_days": 25, "logscore": {"ci": [-0.05, -0.01]}, "crps": {"ci": [-0.02, 0.01]}, "brier": {"ci": [-0.01, 0.0]}}
    assert DCS.verdict_of(base) == "PASS"
    assert DCS.verdict_of({**base, "n_days": 19}) == "INSUFFICIENT"
    assert DCS.verdict_of({**base, "logscore": {"ci": [-0.05, 0.001]}}) == "REFUSED"
    assert DCS.verdict_of({**base, "crps": {"ci": [0.001, 0.02]}}) == "REFUSED"


def test_a_real_p0_compile_carries_the_receipt_and_serves_the_same_vector(monkeypatch):
    from bayes_basket_target import native_ensemble_oos, station_season  # noqa: PLC0415
    from bayes_basket_target import native_ensemble_p0 as p0  # noqa: PLC0415
    from tests.test_native_ensemble_p0 import (  # noqa: PLC0415
        _install_mixed_cycle_calibrations, _mixed_packet,
    )

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)

    def loud(conn, **kw):
        read = _read(AIFS=45.0, IFS9KM=45.0, UKMO10=45.0, CMA=45.0, GDPS=45.0)
        return DCS.shadow_receipt(read=read, fit=_fit(family=DCS.stage_family(kw["stage"], kw["hours_to_settlement"]) or "P0_far"),
                                  family=DCS.stage_family(kw["stage"], kw["hours_to_settlement"]),
                                  station_id=kw["station_id"], served_probability=kw["served_probability"],
                                  ordered=kw["ordered"], intervals=kw["intervals"])

    def compile_once(shadow):
        monkeypatch.setattr(p0, "deterministic_consensus_shadow_receipt", shadow)
        conn = sqlite3.connect(":memory:")
        _install_mixed_cycle_calibrations(conn)
        compiled = p0.compile_native_ensemble_p0(
            conn, forward_packet=_mixed_packet(), decision_at_utc="2026-07-01T13:00:00Z",
        )
        return compiled["packet"]["native_probability_envelope"]

    loud_envelope = compile_once(loud)
    quiet_envelope = compile_once(lambda conn, **kw: {"state": "OFF"})
    assert loud_envelope["probabilities"] == quiet_envelope["probabilities"]
    receipt = json.loads(loud_envelope["lineage"][DCS.LINEAGE_KEY])
    assert receipt["state"] == "SHADOW" and receipt["served_delta_c"] == 0.0
    assert receipt["family"] in {"P0_far", "P0_near"}
    fleet = receipt["variants"]["fleet_core"]
    assert fleet["state"] == "READ" and fleet["delta_c"] == DCS.CAP_C
    assert fleet["shadow_probabilities"] != {k: round(float(v), 8) for k, v in loud_envelope["probabilities"].items()}


def test_p0m_records_the_shadow_on_its_own_served_vector_and_serves_the_same(monkeypatch):
    from bayes_basket_target import materialization  # noqa: PLC0415
    from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: PLC0415
    from tests.test_bayes_basket_target_materialization import _envelope  # noqa: PLC0415

    base = _envelope()
    ordered = sorted(base.bucket_ordinals, key=lambda k: base.bucket_ordinals[k])
    intervals = {name: [29.5 + i, 30.5 + i] for i, name in enumerate(ordered)}
    root = ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {**base.lineage, "model_center_c": "31", "bucket_intervals_c_json": json.dumps(intervals)},
    })
    monkeypatch.setattr(materialization, "load_deterministic_consensus_fit", lambda: _fit(family="P0M"))

    def seat(read):
        return materialization.materialize_p0m_observed(
            root, p0_root=root, forecast_revision_id="taf-279", forecast_source_object_sha256="e" * 64,
            taf_tx_c=None, decision_at_utc="2026-07-23T05:00:00Z", fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64, deterministic_consensus_read=read,
        )

    plain = seat(None)
    assert DCS.LINEAGE_KEY not in plain.lineage
    shadowed = seat(_read(AIFS=36.0, IFS9KM=36.2, UKMO10=36.4, CMA=35.0))
    assert dict(shadowed.probabilities) == dict(plain.probabilities)
    receipt = json.loads(shadowed.lineage[DCS.LINEAGE_KEY])
    assert receipt["state"] == "SHADOW" and receipt["family"] == "P0M" and receipt["served_delta_c"] == 0.0
    assert receipt["centre_c"] == pytest.approx(DCS.served_mean_c(plain.probabilities, ordered, intervals), abs=1e-3)
    assert receipt["variants"]["fleet_core"]["state"] == "READ"
    assert receipt["variants"]["station_core"]["state"] == "READ"


def test_the_board_brief_carries_the_comparison_without_the_vectors():
    from bayes_basket_target.station_day_board import _consensus_brief  # noqa: PLC0415

    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0), fit=_fit(),
                                 family="P0_far", station_id="SBGR", served_probability=SERVED,
                                 ordered=ORDER, intervals=INTERVALS)
    brief = _consensus_brief({DCS.LINEAGE_KEY: json.dumps(receipt)})
    assert brief["family"] == "P0_far" and brief["f_c"]["core"] == pytest.approx(23.1)
    assert set(brief["variants"]) == set(DCS.VARIANTS)
    fleet, station = brief["variants"]["fleet_core"], brief["variants"]["station_core"]
    assert fleet["delta_c"] != station["delta_c"] and fleet["shadow_mode"] and "shadow_probabilities" not in fleet
    assert brief["systems"]["CMA"]["day_max_c"] == 20.0
    assert _consensus_brief({}) is None


def test_the_dashboard_reads_the_fit_artifact(tmp_path, monkeypatch):
    import calibration_dashboard as dashboard  # noqa: PLC0415

    artifact = tmp_path / "fit.json"
    payload = {"schema": DCS.FIT_SCHEMA, "fitted_at_utc": "2026-10-22T06:00:00Z", "window": ["2026-08-23", "2026-10-21"],
               "primary": "fleet_core", "counts": {"stations": 45},
               "families": {"P0_far": {"n_records": 700, "n_days": 30, "n_stations": 45, "f_bias_c": {"core": 0.85, "wide": 0.9},
                                       "variants": {"fleet_core": {"bias_c": 0.85, "kappa": 0.33, "n_days": 30, "n_decisions": 700,
                                                                   "walk_forward": {"n": 600, "n_days": 22, "verdict": "PASS",
                                                                                    "logscore": {"before": 1.9, "after": 1.86, "delta": -0.04, "ci": [-0.07, -0.02]},
                                                                                    "crps": {"delta": -0.02, "ci": [-0.03, -0.01]}, "brier": {"delta": -0.01, "ci": [-0.02, 0.0]},
                                                                                    "days_better": 17, "stations_better": 30, "stations": {"ZUUU": {"n": 20, "delta_logscore": -0.1}}}}}}}}
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(DCS, "FIT_ARTIFACT_PATH", artifact)
    out = dashboard.collect_deterministic_consensus()
    assert out["available"] and out["families"]["P0_far"]["variants"]["fleet_core"]["walk_forward"]["verdict"] == "PASS"
    assert "stations" not in out["families"]["P0_far"]["variants"]["fleet_core"]["walk_forward"]
    monkeypatch.setattr(DCS, "FIT_ARTIFACT_PATH", tmp_path / "missing.json")
    assert dashboard.collect_deterministic_consensus()["available"] is False
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "sdConsensusBlock(e.consensus, stage)" in source and '"deterministic_consensus": collect_deterministic_consensus()' in source


def test_the_daily_fit_is_a_managed_sweep_that_writes_no_database():
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert re.search(r'"fit_deterministic_consensus_shift",\s*"scripts/fit_deterministic_consensus_shift_sweep\.sh",\s*86400\.0', daemon)
    assert '"fit_deterministic_consensus_shift": "none"' in daemon
    sweep = ROOT / "scripts" / "fit_deterministic_consensus_shift_sweep.sh"
    assert sweep.exists() and "fit_deterministic_consensus_shift.py" in sweep.read_text(encoding="utf-8")
