#!/usr/bin/env python3
"""PROTOCOL.md data B: the current P0 chain replayed, scored at every width
lambda of the grid.

The replay is ``downstream_after_247_20261002/capture_down.py``'s (production
``_fit_parameters`` / ``_apply_parameters`` under dress_v1, the seat at kappa
0.25, the current bridge and radiation shift), with one arm and two changes:

* the seat's weights go through production's charter 242 overlay
  (``fusion_blend.overlay_system_weights``), fed walk-forward station params:
  for a decision on UTC day D, ``fit_theta`` / ``days_by_system`` over the
  station's facts knowable at D 00:00Z, tau 5 (the production value);
* the width layer is today's P0 layer for every decision, and the replay's own
  lambda loop (``replay.LAMBDA_GRID``) scores it at each lambda of the grid.

    PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1 python3 capture_b.py --stations ZUUU,EGLC

``SW_NO_242=1`` keeps the seat weights (parity against capture_down's base).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")
OLD = 'for arm in (("prod", "ll_both_reg") if FAST else ARMS):'
assert text.count(OLD) == 1
text = text.replace(OLD, "for arm in ARMS:")
OLD_CALL = "cooling_shift_sigma=cooling, latitude=latitude, tables=tables, actual=actual,\n"
assert text.count(OLD_CALL) == 1
text = text.replace(OLD_CALL, "cooling_shift_sigma=cooling, latitude=latitude, tables=tables, actual=actual, _ctx=(station, event_date, tz, at, family),\n")
OLD_PRED = "predictions_by_system={s: {event_date: day_rows[s]} for s in pool},"
assert text.count(OLD_PRED) == 1
text = text.replace(OLD_PRED, 'predictions_by_system={s: {event_date: {**day_rows[s], "decision_at_utc": C._iso(at)}} for s in pool},')
OLD_SCORED = 'scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"]}'
assert text.count(OLD_SCORED) == 1
text = text.replace(OLD_SCORED, 'scored[arm] = {"served": result["served"], "lam": result["lam"], "nf": result["nf"], "seated": result["seated"], "fusion_state": result["fusion_state"]}')
R = types.ModuleType("capture_reg_sw")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos, replay = R.C, R.oos, R.replay
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS
assert oos.SINGLE_SYSTEM_KERNEL_RULE == oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS, "run with PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1"

from bayes_basket_target import cross_consensus as CC  # noqa: E402
from bayes_basket_target import fusion_blend as FB  # noqa: E402

assert (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX) == (1.0, 1.5, 0.4)
assert (CC.RADIATION_SHIFT_SIGMA_WARM, CC.RADIATION_SHIFT_SIGMA_COLD) == (0.18, 0.06)
GRID = (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.1, 1.2, 1.35, 1.5)
replay.LAMBDA_GRID = GRID
R.ARMS = ("base",)
R.CUTOFF_UTC = "2026-10-02T09:00:00Z"
R.PERIODS["b"] = lambda day: "2026-07-20" <= day <= "2026-10-01"
R.PERIODS["b_no242"] = R.PERIODS["b"]
R.HERE = HERE
TAU = 5.0
# The certified cross artifacts' family cap (855,981 of 856,237 cap entries are 0.75;
# the replay module's DEFAULT_FAMILY_WEIGHT_CAPS is empty, which fails every overlay).
FAMILY_CAP = 0.75
NO_242 = bool(os.environ.get("SW_NO_242"))
ORIGINAL_BLEND = replay._blend_and_score

_conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
LAYERS: dict[str, object] = {}
_fconn = sqlite3.connect(f"file:{FB.FUSION_BLEND_TRAINING_DB}?mode=ro", uri=True, timeout=30)
_fconn.row_factory = sqlite3.Row
FACTS = [FB.WeightFact(row) for row in _fconn.execute("SELECT * FROM station_model_weight_fact")]
_fconn.close()
SYSTEMS = sorted({s for f in FACTS for s in f.pool})
PARAMS: dict[str, dict] = {}   # station -> {"params": {station: [rows...]}, "vetoes": []}


def _station_tables(station: str) -> dict:
    """Walk-forward params, one row per UTC day at 00:00Z, as production's
    nightly fit would have written them (strict=False as in the fit script)."""
    if station in PARAMS:
        return PARAMS[station]
    rows = [f for f in FACTS if f.station == station]
    out = []
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    while day <= datetime(2026, 10, 2, tzinfo=timezone.utc):
        known = FB.knowable_facts(rows, day, strict=False)
        if known:
            out.append({"params_id": f"wf-{station}-{day:%Y%m%d}", "computed_at": day,
                        "computed_at_utc": day.isoformat(), "tau": TAU,
                        "theta": FB.fit_theta(known, SYSTEMS), "days": FB.days_by_system(known)})
        day += timedelta(days=1)
    PARAMS[station] = {"params": {station: out}, "vetoes": []}
    return PARAMS[station]


def blend(*, weights, day_rows, cooling_shift_sigma, latitude, tables, actual, _ctx):
    station, event_date, tz, at, family = _ctx
    if station not in LAYERS:
        layer = oos.p0_gate_tables(_conn, station_id=station)["center_width_layer"]
        assert layer is not None and abs(layer.width_lambda - 0.75) < 1e-12 and abs(layer.delta_c) < 1e-12
        LAYERS[station] = layer
    served_w, state = dict(weights), "SEAT_NO_242"
    if not NO_242:
        FB.load_serving_tables = lambda path=None: _station_tables(station)  # noqa: E731
        served_w, receipt = FB.overlay_system_weights(
            station_id=station, event_date=event_date, decision_at_utc=C._iso(at), timezone_name=tz,
            system_weights=weights, family_by_system={s: family[s] for s in weights},
            family_caps={family[s]: FAMILY_CAP for s in weights}, lead_by_system={}, mode=FB.FUSION_BLEND_MODE_BLEND)
        state = receipt["state"]
        assert state in ("APPLIED", "SEAT_NO_TRAINING_DAYS", "SEAT_NO_STATION_PARAMS", "SEAT_BLEND_VOTED_OUT_A_MEMBER", "SEAT_FEWER_THAN_TWO_FAMILIES"), receipt
    tables2 = dict(tables)
    tables2["center_width_layer"] = LAYERS[station]
    result = ORIGINAL_BLEND(weights=served_w, day_rows=day_rows, cooling_shift_sigma=cooling_shift_sigma,
                            latitude=latitude, tables=tables2, actual=actual)
    positive = [s for s, v in served_w.items() if float(v) > oos.EPS]
    result["nf"] = len({family[s] for s in positive})
    result["seated"] = len(positive)
    result["fusion_state"] = state
    return result


replay._blend_and_score = blend


def forecast(self, scope, case, knowable):
    train = self.train(scope, case["event_date"], knowable)
    key = (scope, case["event_date"], case["member_set_sha256"], tuple(c["event_date"] for c in train))
    if key in self.memo:
        return self.memo[key]
    if len(train) < replay.MIN_TRAIN:
        self.memo[key] = None
        return None
    params = oos._fit_parameters(train)
    if params.get("kernel_rule") != oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS:
        raise RuntimeError(f"not a dress fit: {params.get('kernel_rule')}")
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    prob = oos._apply_parameters(values, params)
    one = {"probability": prob, "calibrated_center_c": oos._corrected_center(mean, params)}
    row = {
        "event_date": case["event_date"], "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        "arms": {"base": one},
        "fit": {"s": float(params["spread_factor"])},
    }
    row["probability"] = prob
    row["calibrated_center_c"] = one["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "b_no242" if NO_242 else "b", "--model-backfill-arrivals", "--fast"]
    rc = R.main()
    _conn.close()
    sys.exit(rc)
