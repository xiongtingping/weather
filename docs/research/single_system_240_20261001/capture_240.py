#!/usr/bin/env python3
"""Charter 240 re-test of the single-system layer (PROTOCOL.md).

The replay is ``single_system_logloss_reg_20260924/capture_reg.py``, loaded
from its source with exactly one line changed (its --fast mode scores two
arms; here every kappa arm is scored, with --fast's day-end cut kept).  The
arms: one H15 log-loss refit per scope fit (production's ``_logloss_refit``
objective), shrunk toward the moments by kappa in {1, .75, .5, .25, 0}.

    PM_SINGLE_SYSTEM_FIT_RULE=moment_v1 python3 capture_240.py --stations EGLC,KLGA
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if os.environ.get("PM_SINGLE_SYSTEM_FIT_RULE") != "moment_v1":
    sys.exit("set PM_SINGLE_SYSTEM_FIT_RULE=moment_v1: the k100 arm must be the moment fit")
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")
OLD = 'for arm in (("prod", "ll_both_reg") if FAST else ARMS):'
assert text.count(OLD) == 1
text = text.replace(OLD, "for arm in ARMS:")
R = types.ModuleType("capture_reg_240")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos = R.C, R.oos
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_MOMENT

KAPPAS = {"k100": 1.0, "k075": 0.75, "k050": 0.5, "k025": 0.25, "k000": 0.0}
R.ARMS = tuple(KAPPAS)
R.CUTOFF_UTC = "2026-10-01T09:00:00Z"
R.PERIODS["c240"] = lambda day: "2026-07-20" <= day <= "2026-09-30"
R.HERE = HERE                         # main() writes HERE / "pass1_c240"


def forecast(self, scope, case, knowable):
    train = self.train(scope, case["event_date"], knowable)
    key = (scope, case["event_date"], case["member_set_sha256"], tuple(c["event_date"] for c in train))
    if key in self.memo:
        return self.memo[key]
    if len(train) < R.replay.MIN_TRAIN:
        self.memo[key] = None
        return None
    params = oos._fit_parameters(train)
    if float(params.get("reliability_weight", 1.0)) != 1.0:
        raise RuntimeError("climate blend in play; the vectorised fit assumes none")
    C._parity(train, params)
    s_m = float(params["spread_factor"])
    d15, s15 = R._fit_both_fast(train, params, oos.LOGLOSS_REFIT_HALF_LIFE_DAYS, s_m)
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    arms = {name: (params if k == 1.0 else C._shifted(params, (1 - k) * d15, k * s_m + (1 - k) * s15))
            for name, k in KAPPAS.items()}
    row = {
        "event_date": case["event_date"],
        "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                      for c in train},
        "arms": {arm: {"probability": oos._apply_parameters(values, p),
                       "calibrated_center_c": oos._corrected_center(mean, p)} for arm, p in arms.items()},
        "fit": {"s_prod": s_m, "delta_15": d15, "s_15": s15},
    }
    row["probability"] = row["arms"]["k100"]["probability"]
    row["calibrated_center_c"] = row["arms"]["k100"]["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "c240", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
