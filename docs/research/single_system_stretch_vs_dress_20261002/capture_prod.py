#!/usr/bin/env python3
"""Verification of the production dress_v1 form on the served P0 replay.

The arm is production itself -- ``oos._fit_parameters`` (charter 216 refit
whose objective is now the dressed distribution) and ``oos._apply_parameters``
-- run with ``PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1``.  Same replay as
``capture_fit.py`` so rows join on (station, event_date, at) to the stored
``base`` (stretch) and ``dress`` (stretch-fitted parameters under the dress
kernel) arms.  ``score_prod.py`` does the join.

    PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1 python3 capture_prod.py --stations ZUUU
"""
from __future__ import annotations

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
R = types.ModuleType("capture_reg_prod")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos = R.C, R.oos
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS, "production rule must be charter 216"
assert oos.SINGLE_SYSTEM_KERNEL_RULE == oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS, "run with PM_SINGLE_SYSTEM_KERNEL_RULE=dress_v1"

ARMS = ("base",)
R.ARMS = ARMS
R.CUTOFF_UTC = "2026-10-02T09:00:00Z"
R.PERIODS["prod"] = lambda day: "2026-07-20" <= day <= "2026-10-01"
R.HERE = HERE


def _pstd(values):
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _modes(prob):
    keys = sorted(prob, key=int)
    v = [prob[k] for k in keys]
    n = 0
    for i, x in enumerate(v):
        left = v[i - 1] if i else -1.0
        right = v[i + 1] if i + 1 < len(v) else -1.0
        if x >= 0.02 and x > left and x >= right:
            n += 1
    return n


def forecast(self, scope, case, knowable):
    train = self.train(scope, case["event_date"], knowable)
    key = (scope, case["event_date"], case["member_set_sha256"], tuple(c["event_date"] for c in train))
    if key in self.memo:
        return self.memo[key]
    if len(train) < R.replay.MIN_TRAIN:
        self.memo[key] = None
        return None
    params = oos._fit_parameters(train)
    if params.get("kernel_rule") != oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS:
        raise RuntimeError(f"not a dress fit: {params.get('kernel_rule')}")
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    prob = oos._apply_parameters(values, params)
    d = _pstd(values)
    m_spread = float(params.get("train_mean_spread_c") or 0.0)
    row = {
        "event_date": case["event_date"], "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        "arms": {"base": {"probability": prob, "calibrated_center_c": oos._corrected_center(mean, params)}},
        "fit": {"d": d, "M": m_spread, "ratio": d / m_spread if m_spread > 0 else -1.0,
                "s": float(params["spread_factor"]), "s_mom": float(params["moment_spread_factor"]),
                "bw": oos._member_kernel_bandwidth_c(values, float(params["spread_factor"]), params),
                "modes": float(_modes(prob))},
    }
    row["probability"] = prob
    row["calibrated_center_c"] = row["arms"]["base"]["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "prod", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
