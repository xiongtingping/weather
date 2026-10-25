"""Charter 281: P0 as one distributional regression over the calibrated systems
and the deterministic forecasts.

User ruling 2026-10-25 「按 N2 形态重建 P0：保留单家证书，用一个分布回归层取代其后全部层和
F 平移，每日前推拟合并以 240 门为服务前提，升版重启」.  The model is one batched
function the fit scores and serving calls; serving needs the fit's walk-forward
to pass; anything short serves the older layers' vector and says why.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target import p0_ngr as NGR  # noqa: E402

ORDER = ["20°C or below", "21°C", "22°C", "23°C", "24°C", "25°C or higher"]
INTERVALS = {
    "20°C or below": [None, 20.5], "21°C": [20.5, 21.5], "22°C": [21.5, 22.5],
    "23°C": [22.5, 23.5], "24°C": [23.5, 24.5], "25°C or higher": [24.5, None],
}
FALLBACK = {"20°C or below": 0.05, "21°C": 0.2, "22°C": 0.4, "23°C": 0.25, "24°C": 0.08, "25°C or higher": 0.02}
SYSTEMS = {
    "ECMWF_IFS_ENS_OPEN_PF50": {"20°C or below": 0.02, "21°C": 0.18, "22°C": 0.45, "23°C": 0.25, "24°C": 0.08, "25°C or higher": 0.02},
    "NOAA_GEFS_05": {"20°C or below": 0.1, "21°C": 0.3, "22°C": 0.35, "23°C": 0.15, "24°C": 0.07, "25°C or higher": 0.03},
}
FAMILY_BY_SYSTEM = {"ECMWF_IFS_ENS_OPEN_PF50": "ECMWF", "NOAA_GEFS_05": "NOAA_GEFS"}
DET = {"state": "ok", "systems": {p: {"day_max_c": v, "run_init_utc": "2026-10-24T00:00:00Z"}
                                  for p, v in (("AIFS", 22.9), ("GFS", 23.1), ("ICON_GLOBAL", 22.7), ("GDPS", 23.4))}}


def _fit(verdict="PASS", **over):
    theta = NGR.initial_theta()
    theta[len(NGR.FAMILIES) + 1] = 0.3  # w0
    payload = {"schema": NGR.FIT_SCHEMA, "fitted_at_utc": "2026-10-25T06:00:00Z", "theta": list(theta),
               "bias": {"AIFS": 0.5, "GFS": 0.2}, "walk_forward": {"verdict": verdict}}
    payload.update(over)
    return payload


def _serve(fit, **kw):
    args = dict(fallback=FALLBACK, ordered=ORDER, intervals=INTERVALS, system_probabilities=SYSTEMS,
                family_by_system=FAMILY_BY_SYSTEM, deterministic=DET, hours_to_settlement=40.0,
                radiation_standardised=0.8, fit=fit, mode="serve")
    args.update(kw)
    return NGR.serve(**args)


def test_the_model_is_a_normal_on_the_buckets_with_the_families_seated_and_the_floor():
    geom = NGR.geometry(ORDER, INTERVALS)
    moments = NGR.family_moments(SYSTEMS, FAMILY_BY_SYSTEM, ORDER, geom)
    assert set(moments) == {"ECMWF", "NOAA_GEFS"}
    theta = NGR.initial_theta()
    theta[NGR.FAMILIES.index("NOAA_GEFS")] = -50.0  # GEFS weight ~0: the centre is ECMWF's
    m = np.array([[moments.get(f, [0.0, 1.0])[0] for f in NGR.FAMILIES]])
    s = np.array([[moments.get(f, [0.0, 1.0])[1] for f in NGR.FAMILIES]])
    has = np.array([[f in moments for f in NGR.FAMILIES]])
    q, mu, sd = NGR.predict_batch(theta, m=m, s=s, has=has, F=np.array([25.0]), Z=np.array([0.0]),
                                  L=np.array([0.0]), rad=np.array([0.0]), tlo=np.array([geom["tlo"]]),
                                  thi=np.array([geom["thi"]]), mask=np.ones((1, 6), bool))
    assert mu[0] == pytest.approx(moments["ECMWF"][0], abs=1e-3)   # w = 0: F does not move it
    assert sd[0] == pytest.approx(moments["ECMWF"][1], abs=1e-3)   # c2 = 1: sd is the spread
    assert q.sum() == pytest.approx(1.0) and q.min() >= NGR.FLOOR * 0.99
    theta[len(NGR.FAMILIES) + 1] = 0.5  # w0: halfway to F
    _q, mu2, _sd = NGR.predict_batch(theta, m=m, s=s, has=has, F=np.array([25.0]), Z=np.array([0.0]),
                                     L=np.array([0.0]), rad=np.array([0.0]), tlo=np.array([geom["tlo"]]),
                                     thi=np.array([geom["thi"]]), mask=np.ones((1, 6), bool))
    assert mu2[0] == pytest.approx((moments["ECMWF"][0] + 25.0) / 2, abs=1e-3)


def test_f_is_the_median_of_the_bias_corrected_ten_and_needs_three():
    assert NGR.det_features({"AIFS": 22.0, "GFS": 23.0}, {}) is None
    F, Z = NGR.det_features({"AIFS": 22.0, "GFS": 23.0, "GDPS": 24.0, "HRRR": 40.0}, {"AIFS": 1.0})
    assert F == pytest.approx(23.0) and Z == pytest.approx(math.log(float(np.std([23.0, 23.0, 24.0])) + 0.2))
    assert len(NGR.DET_PRODUCTS) == 10


def test_serving_needs_a_passing_fit_and_every_input_else_the_fallback_and_why():
    vector, receipt = _serve(_fit())
    assert receipt["state"] == "SERVED" and vector != FALLBACK
    assert vector == pytest.approx(receipt["ngr_probabilities"], abs=1e-9)
    assert receipt["fallback_probabilities"] == pytest.approx(FALLBACK)
    assert sum(vector.values()) == pytest.approx(1.0)
    for fit, kw, reason in (
        (None, {}, "NO_FIT"),
        (_fit(verdict="REFUSED"), {}, "GATE_NOT_PASSED"),
        (_fit(), {"mode": "shadow"}, "MODE_SHADOW"),
        (_fit(), {"deterministic": {"systems": {"AIFS": {"day_max_c": 22.0}}}}, "TOO_FEW_DETERMINISTIC"),
        (_fit(), {"system_probabilities": {}}, "NO_SEATED_FAMILY"),
        (_fit(), {"hours_to_settlement": None}, "NO_HOURS_TO_SETTLEMENT"),
    ):
        vector, receipt = _serve(fit, **kw)
        assert vector == FALLBACK and receipt["state"] == "FALLBACK" and receipt["reason"] == reason
    # A passing fit that is not served still records the regression's vector.
    _v, shadow = _serve(_fit(), mode="shadow")
    assert shadow["ngr_probabilities"] and shadow["mu_c"] is not None


def test_the_fit_scores_the_function_serving_calls():
    import fit_p0_ngr as FIT  # noqa: PLC0415

    geom = NGR.geometry(ORDER, INTERVALS)
    record = {
        "station": "SBGR", "day": "2026-10-20", "band": "far", "decision_at_utc": "2026-10-18T12:00:00Z",
        "hours": 40.0, "ordered": ORDER, "geom": geom,
        "moments": NGR.family_moments(SYSTEMS, FAMILY_BY_SYSTEM, ORDER, geom), "winner": 3,
        "fallback": [FALLBACK[k] for k in ORDER], "y_c": 23.0, "rad": 0.8,
        "det": {k: v["day_max_c"] for k, v in DET["systems"].items()},
    }
    fit = _fit()
    batch = FIT.Batch([record], fit["bias"])
    q = batch.predict(fit["theta"])[0]
    vector, _receipt = _serve(fit)
    assert [vector[k] for k in ORDER] == pytest.approx(list(q), abs=1e-12)
    assert FIT.fit_bias([record])["AIFS"] == pytest.approx(23.0 - 22.9)


def test_the_walk_forward_passes_where_f_carries_the_truth_and_is_the_verdict():
    import fit_p0_ngr as FIT  # noqa: PLC0415

    rng = np.random.default_rng(281)
    geom = NGR.geometry(ORDER, INTERVALS)
    recs = []
    for d in range(30):
        day = f"2026-10-{d + 1:02d}" if d < 31 else None
        for i in range(30):
            truth = 22.5 + rng.normal(0, 1.0)
            det_centre = truth + rng.normal(0, 0.4)          # F knows the day
            ens = {"20°C or below": 0.05, "21°C": 0.2, "22°C": 0.4, "23°C": 0.25, "24°C": 0.08, "25°C or higher": 0.02}
            winner = min(range(6), key=lambda k: abs(22.0 + k - 2 - truth) if 0 < k < 5 else 9)
            if truth < 20.5:
                winner = 0
            elif truth >= 24.5:
                winner = 5
            recs.append({
                "station": f"S{i}", "day": day, "band": "far", "decision_at_utc": f"{day}T00:00:00Z",
                "hours": 40.0, "ordered": ORDER, "geom": geom,
                "moments": NGR.family_moments({"X": ens}, {"X": "ECMWF"}, ORDER, geom), "winner": winner,
                "fallback": [ens[k] for k in ORDER], "y_c": truth, "rad": 0.0,
                "det": {"AIFS": det_centre + 0.1, "GFS": det_centre - 0.1, "GDPS": det_centre},
            })
    out = FIT.walk_forward(recs)
    assert out["n_days"] >= 20 and out["logscore"]["delta"] < 0 and out["verdict"] == "PASS"


def test_a_real_p0_compile_serves_the_regression_and_keeps_the_fallback(monkeypatch):
    from bayes_basket_target import native_ensemble_oos, station_season  # noqa: PLC0415
    from bayes_basket_target import native_ensemble_p0 as p0  # noqa: PLC0415
    from tests.test_native_ensemble_p0 import (  # noqa: PLC0415
        _install_mixed_cycle_calibrations, _mixed_packet,
    )

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)
    monkeypatch.setattr(NGR, "read_deterministic", lambda conn, **kw: DET)

    def compile_once(fit):
        monkeypatch.setattr(NGR, "load_fit_artifact", lambda path=None: fit)
        conn = sqlite3.connect(":memory:")
        _install_mixed_cycle_calibrations(conn)
        compiled = p0.compile_native_ensemble_p0(conn, forward_packet=_mixed_packet(),
                                                 decision_at_utc="2026-07-01T13:00:00Z")
        return compiled["packet"]["native_probability_envelope"]

    served = compile_once(_fit())
    plain = compile_once(None)
    receipt = json.loads(served["lineage"][NGR.LINEAGE_KEY])
    if receipt["state"] != "SERVED":
        pytest.skip(f"fixture decision has no regression inputs: {receipt.get('reason')}")
    assert {k: float(v) for k, v in served["probabilities"].items()} == pytest.approx(receipt["ngr_probabilities"], abs=1e-9)
    assert receipt["fallback_probabilities"] == pytest.approx({k: float(v) for k, v in plain["probabilities"].items()}, abs=1e-9)
    plain_receipt = json.loads(plain["lineage"][NGR.LINEAGE_KEY])
    assert plain_receipt["state"] == "FALLBACK" and plain_receipt["reason"] == "NO_FIT"
    assert "deterministic_consensus_json" not in served["lineage"]


def test_the_consensus_shift_is_gone_and_the_versions_moved():
    for path in ("bayes_basket_target/deterministic_consensus_shift.py",
                 "scripts/fit_deterministic_consensus_shift.py",
                 "scripts/fit_deterministic_consensus_shift_sweep.sh"):
        assert not (ROOT / path).exists(), path
    for path in ("bayes_basket_target/native_ensemble_p0.py", "bayes_basket_target/materialization.py",
                 "bayes_basket_target/p1_compiler.py", "bayes_basket_target/probability_packet.py",
                 "bayes_basket_target/production_worker.py", "bayes_basket_target/station_day_board.py"):
        assert "deterministic_consensus" not in (ROOT / path).read_text(encoding="utf-8"), path
    from bayes_basket_target import native_ensemble_p0 as P0  # noqa: PLC0415
    from bayes_basket_target import serving_arithmetic as sa  # noqa: PLC0415
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v33"
    for p0v, p0mv, p1v in (("v32", "v35", "v45"), ("v33", "v36", "v46")):
        declared = sa.version_effective_from("P0", f"calibrated_native_cross_ensemble_p0_{p0v}")
        assert declared and declared == sa.version_effective_from("P0-M", f"p0m_taf_likelihood_{p0mv}") \
            == sa.version_effective_from("P1", f"p1_joint_likelihood_{p1v}")


def test_the_daily_fit_is_a_managed_read_only_sweep():
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert re.search(r'"fit_p0_ngr",\s*"scripts/fit_p0_ngr_sweep\.sh",\s*86400\.0', daemon)
    assert '"fit_p0_ngr": "none"' in daemon
    sweep = (ROOT / "scripts" / "fit_p0_ngr_sweep.sh").read_text(encoding="utf-8")
    assert "single_instance fit_p0_ngr" in sweep and "scripts/fit_p0_ngr.py" in sweep
    source = (ROOT / "scripts" / "fit_p0_ngr.py").read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert not re.search(r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM", source, re.I)


def test_the_board_shows_the_regressions_move_against_the_fallback():
    from bayes_basket_target import station_day_board as B  # noqa: PLC0415

    _vector, receipt = _serve(_fit())
    axis = [{"bucket": k, "ordinal": i, "centre_c": 20.0 + i} for i, k in enumerate(ORDER)]
    brief = B._ngr_brief({NGR.LINEAGE_KEY: json.dumps(receipt)}, axis)
    assert brief["state"] == "SERVED" and brief["move"]["post"]["mean_c"] != brief["move"]["pre"]["mean_c"]
    assert brief["deterministic"]["AIFS"] == pytest.approx(22.9)
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'body: (stage === "P0" ? sdNgrBlock(e.ngr) : sdStageMove(e, stage))' in source
    assert '"p0_ngr": collect_p0_ngr()' in source and "sdConsensus" not in source


def test_a_served_regressions_centre_is_the_envelopes_model_centre(monkeypatch):
    """补记一 (user 2026-10-25 「重新拟合」): P0-M's TAF likelihood subtracts it."""
    from bayes_basket_target import native_ensemble_oos, station_season  # noqa: PLC0415
    from bayes_basket_target import native_ensemble_p0 as p0  # noqa: PLC0415
    from tests.test_native_ensemble_p0 import (  # noqa: PLC0415
        _install_mixed_cycle_calibrations, _mixed_packet,
    )

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)
    monkeypatch.setattr(NGR, "read_deterministic", lambda conn, **kw: DET)
    monkeypatch.setattr(NGR, "load_fit_artifact", lambda path=None: _fit())
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    envelope = p0.compile_native_ensemble_p0(conn, forward_packet=_mixed_packet(),
                                             decision_at_utc="2026-07-01T13:00:00Z")["packet"]["native_probability_envelope"]
    receipt = json.loads(envelope["lineage"][NGR.LINEAGE_KEY])
    assert receipt["state"] == "SERVED"
    assert float(envelope["lineage"]["model_center_c"]) == pytest.approx(receipt["mu_c"], abs=1e-9)


def test_the_likelihood_refits_read_the_regressions_roots(tmp_path):
    import build_p0_ngr_root_replay as RB  # noqa: PLC0415
    import fit_p1_observation_likelihood as P1FIT  # noqa: PLC0415
    import fit_taf_likelihood as TAFFIT  # noqa: PLC0415

    replay = tmp_path / "replay.db"
    out = sqlite3.connect(replay)
    out.executescript(RB.SCHEMA)
    out.executescript(RB.P1_ROOT_SCHEMA)
    out.execute("INSERT INTO root VALUES ('p0-a','ZUUU','2026-10-20','2026-10-19T00:00:00Z',22.4,1.1,?,'2026-10-19')",
                (json.dumps({"22": 0.6, "23": 0.4}),))
    out.execute("INSERT INTO p1_root VALUES ('p0m-a','P0-M','2026-10-20',?, 'x')", (json.dumps({"22": 0.3, "23": 0.7}),))
    out.commit()

    class Base:
        def root(self, ck):
            return ("21.0", json.dumps({"22": 0.5, "23": 0.5}), json.dumps({"22": 0, "23": 1}), "{}")

        def p0m_rows(self, since, through):
            return []

        def p0_roots(self, station, day):
            return []

    source = TAFFIT.ReplayRootSource(Base(), replay)
    assert source.root("p0-a") == (22.4, json.dumps({"22": 0.6, "23": 0.4}), json.dumps({"22": 0, "23": 1}), "{}")
    assert source.root("p0-missing") is None
    assert "replay" in P1FIT.RootCache.__init__.__code__.co_varnames
    for sweep, needle in (("scripts/taf_likelihood_refit_sweep.sh", '--root-replay "$REPLAY"'),
                          ("scripts/p1_likelihood_refit_sweep.sh", '--root-replay "$OUT/p0_ngr_root_replay.db"')):
        text = (ROOT / sweep).read_text(encoding="utf-8")
        assert needle in text and "build_p0_ngr_root_replay.py --live" in text
    assert RB.P0M_ON_NEW_ROOT == frozenset({"p0m_taf_likelihood_v36"})
