"""Charter 279: the deterministic consensus F as a P0 centre feature -- shadow.

User ruling 2026-10-22 「改，先影子，尝试测试逐站偏差！放在p0卡片里对比」, then 「统一用十家」
(补记二) and 「影子也要进p1.把逐站弱化。把舰队突出」 (补记三): every P0, P0-M and P1
compile records what the ten-system consensus would move its centre by under
the fleet bias and the station bias, and the vector it would serve; the served
vector is untouched.  The daily fit scores each variant walk-forward under
charter 240.
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
        "fleet_wide": {"bias_c": 0.90, "kappa": 0.35},
        "station_wide": {"bias_c": 0.90, "kappa": 0.32, "station_bias_c": {"SBGR": 0.40}},
    }
    variants.update(over)
    return {"schema": DCS.FIT_SCHEMA, "fitted_at_utc": "2026-10-22T06:00:00Z",
            "families": {family: {"variants": variants}}}


def test_the_stage_families_split_p0_at_36_hours_and_p1_has_its_own():
    assert DCS.stage_family("P0", 54.6) == "P0_far"
    assert DCS.stage_family("P0", 36.0) == "P0_near"
    assert DCS.stage_family("P0", 12.0) == "P0_near"
    assert DCS.stage_family("P0", 11.9) is None
    assert DCS.stage_family("P0-M", 5.0) == "P0M"
    assert DCS.stage_family("P1", 5.0) == "P1"
    assert DCS.stage_family("P0", None) is None
    assert DCS.STAGE_FAMILIES == ("P0_far", "P0_near", "P0M", "P1")
    assert DCS.CENSOR_BY_FAMILY["P0M"] is True and DCS.CENSOR_BY_FAMILY["P1"] is False


def test_f_is_the_median_of_the_ten_and_needs_three_systems():
    assert len(DCS.ROSTERS["wide"]) == 10 and list(DCS.ROSTERS) == ["wide"]
    assert DCS.VARIANTS == ("fleet_wide", "station_wide") and DCS.PRIMARY_VARIANT == "fleet_wide"
    read = _read(AIFS=19.1, IFS9KM=19.2, UKMO10=19.3, CMA=15.5, GDPS=19.9, ICON_GLOBAL=18.2)
    assert read["n"] == {"wide": 6}
    assert read["f_c"]["wide"] == pytest.approx((19.1 + 19.2) / 2)       # the CMA outlier does not pull the median
    assert set(read["systems"]) == {"AIFS", "IFS9KM", "UKMO10", "CMA", "GDPS", "ICON_GLOBAL"}
    thin = _read(AIFS=19.1, GDPS=19.9)
    assert thin["f_c"]["wide"] is None and thin["n"]["wide"] == 2         # two systems: no F
    # A system outside the roster (ARPEGE, HRRR) is not read.
    outside = _read(AIFS=19.1, IFS9KM=19.2, UKMO10=19.3, ARPEGE=10.0, HRRR=30.0)
    assert outside["n"]["wide"] == 3 and outside["f_c"]["wide"] == pytest.approx(19.2)


def test_the_receipt_moves_a_shadow_by_kappa_times_the_corrected_gap_and_serves_nothing():
    served = dict(SERVED)
    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0), fit=_fit(),
                                 family="P0_far", station_id="SBGR", served_probability=served,
                                 ordered=ORDER, intervals=INTERVALS)
    assert receipt["state"] == "SHADOW" and receipt["served_delta_c"] == 0.0 and served == SERVED
    assert receipt["rule"] == DCS.RULE_VERSION and receipt["primary"] == "fleet_wide"
    assert set(receipt["variants"]) == {"fleet_wide", "station_wide"}
    centre = receipt["centre_c"]
    fleet = receipt["variants"]["fleet_wide"]
    station = receipt["variants"]["station_wide"]
    assert fleet["state"] == station["state"] == "READ"
    f = (23.0 + 23.2) / 2
    assert fleet["delta_c"] == pytest.approx(0.35 * (f + 0.90 - centre), abs=1e-3)
    assert station["bias_source"] == "station" and station["bias_c"] == pytest.approx(0.40)
    assert station["delta_c"] == pytest.approx(0.32 * (f + 0.40 - centre), abs=1e-3)
    # The two shadows differ, both sit warm of the served vector, and each is a floored ladder vector.
    assert fleet["shadow_centre_c"] > centre and station["shadow_centre_c"] > centre
    assert fleet["shadow_centre_c"] != station["shadow_centre_c"]
    for v in (fleet, station):
        assert sum(v["shadow_probabilities"].values()) == pytest.approx(1.0, abs=1e-6)
        assert min(v["shadow_probabilities"].values()) >= 1e-3 - 1e-9
    # A station without its own bias falls back to the fleet's and says so.
    other = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0), fit=_fit(),
                               family="P0_far", station_id="ZUUU", served_probability=SERVED,
                               ordered=ORDER, intervals=INTERVALS)
    assert other["variants"]["station_wide"]["bias_source"] == "fleet_fallback"
    assert other["variants"]["station_wide"]["bias_c"] == pytest.approx(0.90)


def test_the_shift_is_capped_and_the_silent_states_are_named():
    read = _read(AIFS=40.0, IFS9KM=40.0, UKMO10=40.0, CMA=40.0)
    receipt = DCS.shadow_receipt(read=read, fit=_fit(), family="P0_far", station_id="SBGR",
                                 served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert receipt["variants"]["fleet_wide"]["delta_c"] == DCS.CAP_C and receipt["variants"]["fleet_wide"]["clipped"]
    no_fit = DCS.shadow_receipt(read=read, fit=None, family="P0_far", station_id="SBGR",
                                served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert no_fit["state"] == "NO_FIT" and no_fit["variants"]["fleet_wide"]["state"] == "NO_FIT"
    thin = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.1), fit=_fit(), family="P0_far", station_id="SBGR",
                              served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert thin["variants"]["fleet_wide"]["state"] == "TOO_FEW_SYSTEMS"
    assert thin["variants"]["station_wide"]["state"] == "TOO_FEW_SYSTEMS"
    p1 = DCS.shadow_receipt(read=read, fit=_fit(), family=None, station_id="SBGR",
                            served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert p1["state"] == "NOT_READ_AT_STAGE" and p1["variants"] == {}
    broken = DCS.shadow_receipt(read={"state": "ERROR", "error": "x"}, fit=_fit(), family="P0_far", station_id="SBGR",
                                served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    assert broken["state"] == "NO_READ"


def test_a_cold_shift_reapplies_the_observed_bound_by_the_stage_rule():
    """The day already reached 22C: the two coldest buckets are dead.  A cold
    shift moves mass onto them; P0-M censors it onto 22C (charter 241), P1
    renormalises over the live buckets.  Dead buckets stay at zero either way."""

    dead = ("20°C or below", "21°C")
    served = {"20°C or below": 0.0, "21°C": 0.0, "22°C": 0.5, "23°C": 0.3, "24°C": 0.15, "25°C or higher": 0.05}
    censored = DCS.shift_served(served, ORDER, INTERVALS, -1.0, excluded=dead, censor=True)
    proportional = DCS.shift_served(served, ORDER, INTERVALS, -1.0, excluded=dead, censor=False)
    for v in (censored, proportional):
        assert v["20°C or below"] == 0.0 and v["21°C"] == 0.0
        assert sum(v.values()) == pytest.approx(1.0, abs=1e-9)
    assert censored["22°C"] > proportional["22°C"]            # the dead mass lands on 22C
    assert proportional["23°C"] > censored["23°C"]            # spread over the live ones instead
    r1 = DCS.shadow_receipt(read=_read(AIFS=19.0, IFS9KM=19.2, UKMO10=19.4, CMA=18.0), fit=_fit(family="P1"),
                            family="P1", station_id="SBGR", served_probability=served, ordered=ORDER,
                            intervals=INTERVALS, excluded=dead)
    fleet = r1["variants"]["fleet_wide"]
    assert fleet["delta_c"] < 0 and r1["excluded"] == list(dead)
    assert fleet["shadow_probabilities"]["21°C"] == 0.0 and fleet["shadow_probabilities"]["20°C or below"] == 0.0


def test_the_packet_bundle_carries_only_the_family_and_the_station():
    fit = _fit(family="P1", station_wide={"bias_c": 0.9, "kappa": 0.3, "station_bias_c": {"SBGR": 0.4, "ZUUU": 1.2, "EDDM": 1.5}})
    bundle = DCS.packet_bundle(_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=fit, family="P1", station_id="sbgr")
    assert bundle["family"] == "P1" and bundle["read"]["f_c"]["wide"] == pytest.approx(23.2)
    variants = bundle["fit"]["families"]["P1"]["variants"]
    assert variants["station_wide"]["station_bias_c"] == {"SBGR": 0.4}
    assert "station_bias_c" not in variants["fleet_wide"]
    assert bundle["fit"]["fitted_at_utc"] == fit["fitted_at_utc"]
    json.dumps(bundle)                                         # rides on a packet
    assert DCS.packet_bundle(_read(AIFS=1.0), fit=None, family="P1", station_id="X")["fit"]["families"]["P1"]["variants"] == {}


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
                            "f_c": {"wide": f}, "y_c": y, "p": p, "ordered": ORDER,
                            "intervals": INTERVALS, "winner": winner})
    return records


def test_the_fit_recovers_the_bias_and_kappa_and_the_walk_forward_passes_where_the_feature_is_real():
    records = _synthetic_records()
    params = DCS.fit_parameters(records, family="P0_far")
    assert set(params) == set(DCS.VARIANTS)
    assert params["fleet_wide"]["bias_c"] == pytest.approx(0.8, abs=0.15)
    assert params["fleet_wide"]["kappa"] == pytest.approx(0.5, abs=0.12)
    assert params["station_wide"]["station_bias_c"] and params["station_wide"]["station_bias_prior_days"] == 10.0
    # Before the eighth event day nothing is fitted.
    assert DCS.fit_parameters(records, family="P0_far", before_day="2026-09-05") == {}
    forward = DCS.walk_forward(records, family="P0_far")
    fleet = forward["fleet_wide"]
    assert fleet["n_days"] >= DCS.VERDICT_MIN_EVENT_DAYS
    assert fleet["logscore"]["delta"] < 0 and fleet["logscore"]["ci"][1] < 0
    assert fleet["verdict"] == "PASS" and fleet["days_better"] > fleet["n_days"] // 2
    assert "S0" in fleet["stations"]
    # Pure noise: nothing to find, and the verdict says so.
    noise = _synthetic_records(kappa_true=0.0, bias_true=0.0, seed=7)
    assert DCS.walk_forward(noise, family="P0_far")["fleet_wide"]["verdict"] in {"REFUSED", "INSUFFICIENT"}


def test_the_same_period_walk_forward_scores_only_later_decisions_on_parameters_from_all_days():
    records = _synthetic_records()
    for r in records:
        r["decision_at_utc"] = r["day"] + "T06:00:00Z"
    whole = DCS.walk_forward(records, family="P0_far")["fleet_wide"]
    later = DCS.walk_forward(records, family="P0_far", score_from_utc="2026-09-20T00:00:00Z")["fleet_wide"]
    assert DCS.SCORE_FROM_DECISION_UTC == "2026-10-01T00:00:00Z"
    assert later["n_days"] == 11 and whole["n_days"] > later["n_days"]     # 09-20..09-30 scored
    assert later["n"] == 11 * 6
    # The scored days keep the parameters fitted on every earlier day: run on the
    # later days alone, only the last three reach the eight-day fit floor.
    only = DCS.walk_forward([r for r in records if r["day"] >= "2026-09-20"], family="P0_far")["fleet_wide"]
    assert only["n"] == 3 * 6 < later["n"]


def test_the_verdict_is_charter_240s():
    base = {"n_days": 25, "logscore": {"ci": [-0.05, -0.01]}, "crps": {"ci": [-0.02, 0.01]}, "brier": {"ci": [-0.01, 0.0]}}
    assert DCS.verdict_of(base) == "PASS"
    assert DCS.verdict_of({**base, "n_days": 19}) == "INSUFFICIENT"
    assert DCS.verdict_of({**base, "logscore": {"ci": [-0.05, 0.001]}}) == "REFUSED"
    assert DCS.verdict_of({**base, "crps": {"ci": [0.001, 0.02]}}) == "REFUSED"


def test_a_real_p0_compile_serves_the_primary_shift_and_keeps_the_vector_it_moved(monkeypatch):
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
        family = DCS.stage_family(kw["stage"], kw["hours_to_settlement"])
        return DCS.shadow_receipt(read=read, fit=_fit(family=family or "P0_far"), family=family,
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
    receipt = json.loads(loud_envelope["lineage"][DCS.LINEAGE_KEY])
    assert receipt["family"] in {"P0_far", "P0_near"}
    fleet = receipt["variants"]["fleet_wide"]
    assert fleet["state"] == "READ" and fleet["delta_c"] == DCS.CAP_C
    # 补记七: the served vector is the primary variant's shadow, the receipt keeps what it moved.
    assert receipt["state"] == receipt["serve_state"] == "SERVED" and receipt["served_variant"] == "fleet_wide"
    assert receipt["served_delta_c"] == DCS.CAP_C
    served = {k: float(v) for k, v in loud_envelope["probabilities"].items()}
    assert served != {k: float(v) for k, v in quiet_envelope["probabilities"].items()}
    assert served == pytest.approx(fleet["shadow_probabilities"], abs=1e-7)
    assert receipt["pre_shift_probabilities"] == pytest.approx(
        {k: float(v) for k, v in quiet_envelope["probabilities"].items()}, abs=1e-9)
    assert receipt["served_centre_c"] == pytest.approx(fleet["shadow_centre_c"], abs=1e-3)
    # Shadow mode serves the vector untouched.
    monkeypatch.setattr(DCS, "MODE", "shadow")
    shadow_envelope = compile_once(loud)
    assert shadow_envelope["probabilities"] == quiet_envelope["probabilities"]
    shadow_receipt = json.loads(shadow_envelope["lineage"][DCS.LINEAGE_KEY])
    assert shadow_receipt["state"] == "SHADOW" and shadow_receipt["serve_state"] == "MODE_SHADOW"
    assert shadow_receipt["served_delta_c"] == 0.0


def test_only_p0_beyond_12_hours_serves_and_a_silent_primary_serves_unshifted():
    assert DCS.SERVE_FAMILIES == frozenset({"P0_far", "P0_near"}) and DCS.MODE == "serve"
    read = _read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0)
    for family in ("P0M", "P1"):
        receipt = DCS.shadow_receipt(read=read, fit=_fit(family=family), family=family, station_id="SBGR",
                                     served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
        vector, out = DCS.serve(receipt, SERVED, ORDER, INTERVALS)
        assert vector == SERVED and out["state"] == "SHADOW" and out["serve_state"] == "FAMILY_NOT_SERVED"
        assert out["served_delta_c"] == 0.0
    near = DCS.shadow_receipt(read=read, fit=_fit(family="P0_near"), family="P0_near", station_id="SBGR",
                              served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    vector, out = DCS.serve(near, SERVED, ORDER, INTERVALS)
    assert out["state"] == "SERVED" and vector != SERVED
    assert out["served_delta_c"] == near["variants"]["fleet_wide"]["delta_c"]
    assert sum(vector.values()) == pytest.approx(1.0) and min(vector.values()) >= 1e-3 - 1e-9
    thin = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.1), fit=_fit(), family="P0_far", station_id="SBGR",
                              served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    vector, out = DCS.serve(thin, SERVED, ORDER, INTERVALS)
    assert vector == SERVED and out["serve_state"] == "PRIMARY_TOO_FEW_SYSTEMS" and out["served_delta_c"] == 0.0
    no_fit = DCS.shadow_receipt(read=read, fit=None, family="P0_far", station_id="SBGR",
                                served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    vector, out = DCS.serve(no_fit, SERVED, ORDER, INTERVALS)
    assert vector == SERVED and out["serve_state"] == "PRIMARY_NO_FIT"


def test_the_fit_scores_a_served_p0_on_the_vector_it_moved_and_reads_the_residual_after_serving():
    import fit_deterministic_consensus_shift as fit  # noqa: PLC0415

    served = DCS.serve(DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=_fit(),
                                          family="P0_far", station_id="SBGR", served_probability=SERVED,
                                          ordered=ORDER, intervals=INTERVALS), SERVED, ORDER, INTERVALS)[1]
    assert fit._pre_shift(json.dumps(served)) == pytest.approx(SERVED)
    shadow_only = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=_fit(),
                                     family="P0M", station_id="SBGR", served_probability=SERVED,
                                     ordered=ORDER, intervals=INTERVALS)
    assert fit._pre_shift(json.dumps(shadow_only)) is None and fit._pre_shift(None) is None
    assert "deterministic_consensus_json" in fit.ENVELOPES_SQL
    source = (ROOT / "scripts" / "fit_deterministic_consensus_shift.py").read_text(encoding="utf-8")
    assert "walk_forward_after_p0_serving" in source and "DCS.SERVE_FROM_DECISION_UTC" in source
    from bayes_basket_target import native_ensemble_p0 as P0  # noqa: PLC0415
    from bayes_basket_target import serving_arithmetic as sa  # noqa: PLC0415
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v31"
    declared = sa.version_effective_from("P0", P0.POSTERIOR_VERSION)
    assert declared == DCS.SERVE_FROM_DECISION_UTC
    assert declared == sa.version_effective_from("P0-M", "p0m_taf_likelihood_v34") \
        == sa.version_effective_from("P1", "p1_joint_likelihood_v44")


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
    assert receipt["variants"]["fleet_wide"]["state"] == "READ"
    assert receipt["variants"]["station_wide"]["state"] == "READ"


def test_a_p1_compile_carries_the_receipt_on_envelope_and_packet_and_serves_the_same(monkeypatch):
    from bayes_basket_target import p1_compiler  # noqa: PLC0415
    from bayes_basket_target.probability_packet import ALLOWED_KEYS, build_probability_envelope  # noqa: PLC0415
    from tests.test_charter_150_p1_joint_likelihood import _artifact, _observation, _registry  # noqa: PLC0415
    from tests.test_charter_241_p0m_observed_floor_censoring import _p0m  # noqa: PLC0415

    monkeypatch.setattr(p1_compiler, "load_deterministic_consensus_fit", lambda: _fit(family="P1"))
    root = _p0m(excluded=(), high=None)
    zone_rows = []
    for product, value in (("AIFS", 38.6), ("IFS9KM", 38.8), ("UKMO10", 38.9), ("CMA", 37.0), ("GFS", 39.0)):
        curve = {f"2026-07-23T{h:02d}:00:00Z": value - abs(h - 12) * 0.3 for h in range(0, 24)}
        zone_rows.append({"ensemble_system_id": SYSTEM_IDS[product], "run_init_utc": "2026-07-22T12:00:00Z",
                          "knowable_at_utc": "2026-07-22T18:00:00Z", "lead_hours_to_local_day_start": 6.0,
                          "hourly_c": curve})

    def compile_once(runs):
        return p1_compiler.compile_station_p1_packets(
            predecessor=root, observations=[_observation(root, "a", "2026-07-23T15:00:00Z", 37.6)],
            registry_rows=_registry(root), compilation_at_utc="2026-07-23T15:00:00Z",
            observation_likelihood_artifact=_artifact(), regional_runs=runs,
        )

    plain = compile_once(None)[-1]
    shadowed = compile_once(zone_rows)[-1]
    assert DCS.LINEAGE_KEY not in plain["expected_envelope"]["lineage"] and plain["deterministic_consensus"] is None
    assert shadowed["expected_envelope"]["probabilities"] == plain["expected_envelope"]["probabilities"]
    receipt = json.loads(shadowed["expected_envelope"]["lineage"][DCS.LINEAGE_KEY])
    assert receipt["state"] == "SHADOW" and receipt["family"] == "P1" and receipt["served_delta_c"] == 0.0
    assert receipt["n"]["wide"] >= 3 and receipt["variants"]["fleet_wide"]["state"] == "READ"
    assert "37" in receipt.get("excluded", []) and receipt["variants"]["fleet_wide"]["shadow_probabilities"]["37"] == 0.0
    # The rebuild from the packet's allowed keys reproduces the envelope with its receipt.
    assert "deterministic_consensus" in ALLOWED_KEYS
    packet = {k: v for k, v in shadowed.items() if k in ALLOWED_KEYS}
    packet.update(stage="P1", bridge_sha256=root.bridge_sha256,
                  checkpoint_id=shadowed["expected_envelope"]["checkpoint_id"],
                  posterior_version=shadowed["expected_envelope"]["posterior_version"])
    rebuilt = build_probability_envelope(packet)
    assert rebuilt.lineage[DCS.LINEAGE_KEY] == shadowed["expected_envelope"]["lineage"][DCS.LINEAGE_KEY]


def test_the_board_brief_carries_the_comparison_without_the_vectors():
    from bayes_basket_target.station_day_board import _consensus_brief  # noqa: PLC0415

    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4, CMA=20.0), fit=_fit(),
                                 family="P0_far", station_id="SBGR", served_probability=SERVED,
                                 ordered=ORDER, intervals=INTERVALS)
    brief = _consensus_brief({DCS.LINEAGE_KEY: json.dumps(receipt)})
    assert brief["family"] == "P0_far" and brief["f_c"]["wide"] == pytest.approx(23.1) and brief["n"]["wide"] == 4
    assert set(brief["variants"]) == set(DCS.VARIANTS)
    fleet, station = brief["variants"]["fleet_wide"], brief["variants"]["station_wide"]
    assert fleet["delta_c"] != station["delta_c"] and fleet["shadow_mode"] and "shadow_probabilities" not in fleet
    assert brief["systems"]["CMA"]["day_max_c"] == 20.0
    assert _consensus_brief({}) is None


def test_the_dashboard_reads_the_fit_artifact(tmp_path, monkeypatch):
    import calibration_dashboard as dashboard  # noqa: PLC0415

    artifact = tmp_path / "fit.json"
    payload = {"schema": DCS.FIT_SCHEMA, "fitted_at_utc": "2026-10-22T06:00:00Z", "window": ["2026-08-23", "2026-10-21"],
               "primary": "fleet_wide", "counts": {"stations": 45},
               "families": {"P0_far": {"n_records": 700, "n_days": 30, "n_stations": 45, "f_bias_c": {"wide": 0.9},
                                       "variants": {"fleet_wide": {"bias_c": 0.9, "kappa": 0.33, "n_days": 30, "n_decisions": 700,
                                                                   "walk_forward": {"n": 600, "n_days": 22, "verdict": "PASS",
                                                                                    "logscore": {"before": 1.9, "after": 1.86, "delta": -0.04, "ci": [-0.07, -0.02]},
                                                                                    "crps": {"delta": -0.02, "ci": [-0.03, -0.01]}, "brier": {"delta": -0.01, "ci": [-0.02, 0.0]},
                                                                                    "days_better": 17, "stations_better": 30, "stations": {"ZUUU": {"n": 20, "delta_logscore": -0.1}}}}}}}}
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(DCS, "FIT_ARTIFACT_PATH", artifact)
    out = dashboard.collect_deterministic_consensus()
    assert out["available"] and out["families"]["P0_far"]["variants"]["fleet_wide"]["walk_forward"]["verdict"] == "PASS"
    assert "stations" not in out["families"]["P0_far"]["variants"]["fleet_wide"]["walk_forward"]
    monkeypatch.setattr(DCS, "FIT_ARTIFACT_PATH", tmp_path / "missing.json")
    assert dashboard.collect_deterministic_consensus()["available"] is False
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "sdConsensusBlock(e.consensus, stage)" in source and '"deterministic_consensus": collect_deterministic_consensus()' in source
    assert "fleet_core" not in source
    block = source[source.index("function sdConsensusBlock"):source.index("function sdProbExplain(){")]
    # 补记三: the fleet is the card's line, the station a small grey one; P1 is not skipped.
    assert 'if(stage === "P1") return ""' not in block and 'stage === "P1" ||' not in block
    # 补记六: the result leads (shifted centre and bucket); F, the shift and the
    # station value are one small line; the walk-forward is a verdict in words.
    assert 'class="sd-cons-v"' in block and "fleet.shadow_centre_c" in block and 'class="sd-cons-p"' in block
    assert "挪了更准" in block and "score_from_decision_utc" in block and "fleet.f_cal_c" in block
    assert "foot: sdConsensusFoot" not in source and ".sd-cons-v{font-size:26px" in source


def test_the_daily_fit_is_a_managed_sweep_that_writes_no_database():
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert re.search(r'"fit_deterministic_consensus_shift",\s*"scripts/fit_deterministic_consensus_shift_sweep\.sh",\s*86400\.0', daemon)
    assert '"fit_deterministic_consensus_shift": "none"' in daemon
    sweep = ROOT / "scripts" / "fit_deterministic_consensus_shift_sweep.sh"
    assert sweep.exists() and "fit_deterministic_consensus_shift.py" in sweep.read_text(encoding="utf-8")


def test_the_board_leads_with_the_served_p0_and_details_the_move():
    """补记七 (user 2026-10-25 「不应该突出原来的概率向量，应该突出f进p0后的概率向量，明细里应该体现挪动的明细」)."""
    from bayes_basket_target import station_day_board as B  # noqa: PLC0415

    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=_fit(), family="P0_far",
                                 station_id="SBGR", served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    vector, served = DCS.serve(receipt, SERVED, ORDER, INTERVALS)
    lineage = {"model_center_c": "22.0", DCS.LINEAGE_KEY: json.dumps(served)}
    assert B._served_shift_c(lineage[DCS.LINEAGE_KEY]) == pytest.approx(served["served_delta_c"])
    assert B._served_shift_c(json.dumps(receipt)) == 0.0
    brief = B._consensus_brief(lineage)
    detail = brief["served"]
    assert detail["pre"]["mean_c"] == pytest.approx(receipt["centre_c"])
    assert detail["post"]["mean_c"] == pytest.approx(served["served_centre_c"])
    assert detail["pre"]["mode"] == "22°C" and len(detail["buckets"]) == 5
    assert {b["bucket"]: b["post"] for b in detail["buckets"]} == pytest.approx(
        {k: vector[k] for k in (b["bucket"] for b in detail["buckets"])}, abs=1e-7)
    assert B._consensus_brief({DCS.LINEAGE_KEY: json.dumps(receipt)})["served"] is None
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    block = source[source.index("function sdConsensusServed("):][:2500]
    for label in ("F 进 P0 · 挪", "挪前", "挪后", "<th>桶</th>"):
        assert label in block
    assert "if(served && c.served) return sdConsensusServed(" in source


def test_p0m_and_p1_cards_say_how_much_of_their_root_is_f_and_the_shadow_is_one_grey_line():
    """User 2026-10-25 「在 P0-M 和 P1 卡的根中心旁加一个小标签「根含 F +0.23」」「弱化下方的影子」."""
    from bayes_basket_target import station_day_board as B  # noqa: PLC0415

    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=_fit(), family="P0_far",
                                 station_id="SBGR", served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    _vector, served = DCS.serve(receipt, SERVED, ORDER, INTERVALS)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope (checkpoint_id, content_sha256, station_id, event_date,"
                 " stage, state, decision_at_utc, lineage_json)")
    rows = [
        ("p0", "a", "P0", {B_KEY: json.dumps(served)} if (B_KEY := DCS.LINEAGE_KEY) else {}),
        ("p0s", "b", "P0", {DCS.LINEAGE_KEY: json.dumps(receipt)}),
        ("m", "c", "P0-M", {"p0_root_checkpoint_id": "p0", "p0_root_content_sha256": "a"}),
    ]
    for ck, sha, stage, lineage in rows:
        conn.execute("INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?)",
                     (ck, sha, "ZUUU", "2026-10-25", stage, "COMMITTED", "2026-10-25T01:38:59Z", json.dumps(lineage)))
    shift = served["served_delta_c"]
    assert B._root_p0_served(conn, {"p0_root_checkpoint_id": "p0", "p0_root_content_sha256": "a"},
                             "ZUUU", "2026-10-25") == (pytest.approx(shift), "2026-10-25T01:38:59Z")
    assert B._root_p0_served(conn, {"p0_root_checkpoint_id": "p0s", "p0_root_content_sha256": "b"},
                             "ZUUU", "2026-10-25") == (0.0, None)
    via_p0m = {"lineage": {"prediction_root_checkpoint_id": "m", "prediction_root_content_sha256": "c",
                           "prediction_root_stage": "P0-M"}}
    assert B._p1_root_f(conn, via_p0m, "ZUUU", "2026-10-25") == (pytest.approx(shift), "2026-10-25T01:38:59Z")
    via_p0 = {"lineage": {"prediction_root_checkpoint_id": "p0s", "prediction_root_content_sha256": "b",
                          "prediction_root_stage": "P0"}}
    assert B._p1_root_f(conn, via_p0, "ZUUU", "2026-10-25") == (None, None)
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "根含 F ${sdSg(e.root_f_shift_c, 2)}" in source
    block = source[source.index("function sdConsensusBlock(c, stage){"):][:3500]
    assert 'class="sd-cons-sh"' in block and "影子（未服务）· 再挪" in block
    assert '<span>影子</span><span class="sd-cons-v">' not in block


def test_p0m_and_p1_replay_their_recorded_steps_on_the_root_before_f_moved_it():
    """User 2026-10-25 「p0m和p1也和p0一样，增加这个信息」: the stage's own likelihoods,
    replayed on the root P0 before and after F, rebuild the served vector and the no-F one."""
    from bayes_basket_target import station_day_board as B  # noqa: PLC0415

    axis = [{"bucket": k, "ordinal": i, "centre_c": 20.0 + i} for i, k in enumerate(ORDER)]
    receipt = DCS.shadow_receipt(read=_read(AIFS=23.0, IFS9KM=23.2, UKMO10=23.4), fit=_fit(), family="P0_far",
                                 station_id="SBGR", served_probability=SERVED, ordered=ORDER, intervals=INTERVALS)
    p0, served = DCS.serve(receipt, SERVED, ORDER, INTERVALS)
    taf = {k: 1.0 + 0.1 * i for i, k in enumerate(ORDER)}
    p0m = B._f_step(p0, taf, {"20°C or below"}, censor=True)
    detail = B._f_inherited(p0, served["pre_shift_probabilities"], [(taf, {"20°C or below"}, True)], p0m, axis)
    assert detail is not None and detail["post"]["mean_c"] > detail["pre"]["mean_c"]
    assert detail["delta_c"] == pytest.approx(detail["post"]["mean_c"] - detail["pre"]["mean_c"], abs=1e-3)
    assert {b["bucket"]: b["post"] for b in detail["buckets"]} == pytest.approx(
        {b["bucket"]: p0m[b["bucket"]] for b in detail["buckets"]})
    # A served vector the recorded steps do not rebuild shows nothing.
    off = dict(p0m)
    off["22°C"] += 0.01
    assert B._f_inherited(p0, served["pre_shift_probabilities"], [(taf, {"20°C or below"}, True)], off, axis) is None
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '(stage !== "P0" ? sdFInherited(e) : "")' in source and 'sdMoveBox("F 经根 · 挪"' in source
