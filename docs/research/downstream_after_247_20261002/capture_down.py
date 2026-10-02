#!/usr/bin/env python3
"""PROTOCOL.md: downstream layers re-judged on the charter 247 single-system form.

Same replay as ``single_system_stretch_vs_dress_20261002/capture_prod.py``
(production ``_fit_parameters`` + ``_apply_parameters`` under
``PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1``).  Every arm shares the one set of
single-system vectors and changes exactly one downstream layer inside the
blend: the consensus bridge's ramp, the radiation shift's size, the width
layer's lambda, or the charter 239 slice.  ``base`` is the production chain
with the slice resolved the way serving resolves it (seated count, hours to
settlement) -- the older replays never resolved it.

    PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1 python3 capture_down.py --stations ZUUU
"""
from __future__ import annotations

import dataclasses
import math
import sys
import types
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
text = text.replace(OLD_CALL, "cooling_shift_sigma=cooling, latitude=latitude, tables=tables, actual=actual, _ctx=(event_date, tz, at, arm),\n")
# The radiation signal is resolved as of the decision instant only when the
# predictions carry it (physics_shift_long_20261002/augment.py found every
# replay row with cooling=None for want of it).
OLD_PRED = "predictions_by_system={s: {event_date: day_rows[s]} for s in pool},"
assert text.count(OLD_PRED) == 1
text = text.replace(OLD_PRED, 'predictions_by_system={s: {event_date: {**day_rows[s], "decision_at_utc": C._iso(at)}} for s in pool},')
OLD_SCORED = 'scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"]}'
assert text.count(OLD_SCORED) == 1
text = text.replace(OLD_SCORED, 'scored[arm] = {"mean": result["mean"], "served": result["served"], "vec": result["vec"], **{k: result[k] for k in ("cw_active", "slice", "hours_to_settlement", "seated", "cooling_shift_sigma") if k in result}}')
R = types.ModuleType("capture_reg_down")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos, replay = R.C, R.oos, R.replay
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS
assert oos.SINGLE_SYSTEM_KERNEL_RULE == oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS, "run with PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1"

from bayes_basket_target import cross_consensus as CC  # noqa: E402
from bayes_basket_target.served_shape import with_width_slice  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

ARMS = ("base", "bridge_off", "bridge_old", "bridge_early", "bridge_lmax06",
        "rad_off", "rad_half", "rad_x15",
        "lam_0.6", "lam_0.9", "lam_1.0", "lam_1.2", "slice_1.1", "slice_1.2", "slice_1.3")
BRIDGE = {  # (gap start, gap full, lambda max)
    "bridge_off": (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, 0.0),
    "bridge_old": (1.5, 2.0, CC.CONSENSUS_BRIDGE_LAMBDA_MAX),
    "bridge_early": (0.5, 1.0, CC.CONSENSUS_BRIDGE_LAMBDA_MAX),
    "bridge_lmax06": (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, 0.6),
}
assert (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX) == (1.0, 1.5, 0.4)
assert (CC.RADIATION_SHIFT_SIGMA_WARM, CC.RADIATION_SHIFT_SIGMA_COLD) == (0.18, 0.06)
R.ARMS = ARMS
R.CUTOFF_UTC = "2026-10-02T09:00:00Z"
R.PERIODS["down"] = lambda day: "2026-07-20" <= day <= "2026-10-01"
R.HERE = HERE
ORIGINAL_BLEND = replay._blend_and_score
STATE = {"cw_active": 0, "slice_applied": 0, "calls": 0}


def _pstd(values):
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def blend(*, weights, day_rows, cooling_shift_sigma, latitude, tables, actual, _ctx):
    event_date, tz, at, arm = _ctx
    layer = tables["center_width_layer"]
    slices = tables.get("center_width_slices")
    if arm.startswith("slice_") and layer is not None:
        # Counterfactual: the charter 239 slice as if it had existed all along
        # (the as-of table carries none before 2026-10-01), at the arm's lambda.
        slices = {"thin_far": {"width_lambda": float(arm[6:]), "max_seated_systems": 3, "min_hours_to_settlement": 36.0}}
    seated = sum(1 for v in weights.values() if float(v) > oos.EPS)
    hours = hours_to_settlement_if_known(event_date, as_of_utc=C._iso(at), settlement_timezone=tz)
    layer = with_width_slice(layer, slices, seated_system_count=seated, hours_to_settlement=hours)
    if arm.startswith("lam_") and layer is not None:
        layer = dataclasses.replace(layer, width_lambda=float(arm[4:]), width_tier_lambdas=None, width_tier_boundaries=None)
    if arm == "base":
        STATE["calls"] += 1
        STATE["cw_active"] += layer is not None
        STATE["slice_applied"] += bool(layer is not None and layer.width_slice is not None)
    cooling = cooling_shift_sigma
    if arm == "rad_off":
        cooling = None
    elif arm == "rad_half" and cooling:
        cooling = float(cooling) * 0.5
    elif arm == "rad_x15" and cooling:
        cooling = float(cooling) * 1.5
    saved = (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX)
    if arm in BRIDGE:
        CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX = BRIDGE[arm]
    try:
        tables2 = dict(tables)
        tables2["center_width_layer"] = layer
        result = ORIGINAL_BLEND(weights=weights, day_rows=day_rows, cooling_shift_sigma=cooling, latitude=latitude, tables=tables2, actual=actual)
    finally:
        CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX = saved
    if arm == "base":
        result["cw_active"] = layer is not None
        result["slice"] = layer.width_slice if layer is not None else None
        result["hours_to_settlement"] = hours
        result["seated"] = seated
        result["cooling_shift_sigma"] = cooling_shift_sigma
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
        "arms": {arm: one for arm in ARMS},
        "fit": {"d": _pstd(values), "M": float(params.get("train_mean_spread_c") or 0.0), "s": float(params["spread_factor"])},
    }
    row["probability"] = prob
    row["calibrated_center_c"] = one["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

# The extra keys ``blend`` hangs on the base result ride into the row: the
# harness copies mean/served/vec, so wrap its loop output by patching the
# scored dict builder through the result object itself.
_orig_capture_station = R.capture_station


def capture_station(conn, station, tz):
    rows = _orig_capture_station(conn, station, tz)
    print(f"  {station}: width layer active on {STATE['cw_active']}/{STATE['calls']} base calls, slice applied on {STATE['slice_applied']}", flush=True)
    return rows


R.capture_station = capture_station

if __name__ == "__main__":
    sys.argv += ["--period", "down", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
