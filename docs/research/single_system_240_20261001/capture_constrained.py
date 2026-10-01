#!/usr/bin/env python3
"""PROTOCOL.md addendum: the single-system fit with CRPS and Brier as training constraints.

Same replay as capture_240.py (loaded from capture_reg.py's source with one
line changed); arms k100 (moments), k050 (charter 216), c000 (constrained
log-loss optimum), c050 (constrained optimum shrunk half-way to the moments).

    PM_SINGLE_SYSTEM_FIT_RULE=moment_v1 python3 capture_constrained.py --stations EGLC
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if os.environ.get("PM_SINGLE_SYSTEM_FIT_RULE") != "moment_v1":
    sys.exit("set PM_SINGLE_SYSTEM_FIT_RULE=moment_v1")
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")
OLD = 'for arm in (("prod", "ll_both_reg") if FAST else ARMS):'
assert text.count(OLD) == 1
text = text.replace(OLD, "for arm in ARMS:")
R = types.ModuleType("capture_reg_constrained")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos = R.C, R.oos
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_MOMENT

R.ARMS = ("k100", "k050", "c000", "c050")
R.CUTOFF_UTC = "2026-10-01T09:00:00Z"
R.PERIODS["cons"] = lambda day: "2026-07-20" <= day <= "2026-09-30"
R.HERE = HERE
K = 15                      # integer-grid edges either side of the truth bucket (members sit well inside +-15 C)
STATS = {"fits": 0, "binding": 0, "crps_binding": 0, "brier_binding": 0, "failed": 0}


def _scores(arrays, delta, spread):
    """(truth-bucket probability, CRPS, bucket Brier) per case, one truncated kernel mixture."""
    dev, mask, centers, buckets, counts = arrays
    bw = oos.KERNEL_BANDWIDTH_C
    dressed = (centers + delta)[:, None] + spread * dev
    middle = np.where(mask, dressed, 0.0).sum(axis=1) / counts
    std = np.sqrt(np.where(mask, (dressed - middle[:, None]) ** 2, 0.0).sum(axis=1) / counts)
    shrink = np.where(std > bw, np.sqrt(np.clip(1.0 - (bw / np.where(std > 0, std, 1.0)) ** 2, 0.0, 1.0)), 1.0)
    dressed = middle[:, None] + shrink[:, None] * (dressed - middle[:, None])
    low = np.floor(np.where(mask, dressed, np.inf).min(axis=1) - 4.0 * bw)
    high = np.ceil(np.where(mask, dressed, -np.inf).max(axis=1) + 4.0 * bw)
    offsets = np.arange(-K, K + 1)
    edges = buckets[:, None] + offsets[None, :] + 0.5            # upper edge of bucket (truth + offset)
    edges = np.clip(edges, (low - 0.5)[:, None], (high + 0.5)[:, None])

    def phi(e):  # mean member CDF at edges e: (cases, n_edges)
        v = ndtr((e[:, :, None] - dressed[:, None, :]) / bw)
        return np.where(mask[:, None, :], v, 0.0).sum(axis=2) / counts[:, None]

    lo_mass = phi((low - 0.5)[:, None])[:, 0]
    total = phi((high + 0.5)[:, None])[:, 0] - lo_mass
    total = np.where(total > 0, total, 1.0)
    F = np.clip((phi(edges) - lo_mass[:, None]) / total[:, None], 0.0, 1.0)
    observed = (offsets >= 0).astype(float)[None, :]
    crps = ((F - observed) ** 2).sum(axis=1)                        # rung width 1
    p = np.diff(np.concatenate([np.zeros((len(F), 1)), F], axis=1), axis=1)
    p_last = 1.0 - F[:, -1]
    onehot = (offsets == 0).astype(float)[None, :]
    brier = ((p - onehot) ** 2).sum(axis=1) + p_last ** 2
    p_truth = p[:, K]
    return p_truth, crps, brier


def _constrained(train, params, s_m):
    weights = R._decay(train, oos.LOGLOSS_REFIT_HALF_LIFE_DAYS)
    weights = weights / weights.sum()
    arrays = R._case_arrays(train, params)
    _p0, crps0, brier0 = _scores(arrays, 0.0, s_m)
    c_cap, b_cap = float((weights * crps0).sum()), float((weights * brier0).sum())

    memo = {}

    def at(x):
        k = (round(float(x[0]), 12), round(float(x[1]), 12))
        if k not in memo:
            p, c, b = _scores(arrays, k[0], k[1])
            memo[k] = (float(-(weights * np.log(np.maximum(p, C.P_FLOOR))).sum()),
                       float((weights * c).sum()), float((weights * b).sum()))
        return memo[k]

    def ll(x):
        return at(x)[0]

    cons = [{"type": "ineq", "fun": lambda x: c_cap - at(x)[1]},
            {"type": "ineq", "fun": lambda x: b_cap - at(x)[2]}]
    fit = minimize(ll, x0=np.array([0.0, s_m]), method="SLSQP", bounds=[C.DELTA_BOUNDS, C.SPREAD_BOUNDS],
                   constraints=cons, options={"maxiter": 60, "ftol": 1e-9})
    d, s = float(fit.x[0]), float(fit.x[1])
    _p, c1, b1 = _scores(arrays, d, s)
    c1, b1 = float((weights * c1).sum()), float((weights * b1).sum())
    STATS["fits"] += 1
    if not fit.success or c1 > c_cap + 1e-6 or b1 > b_cap + 1e-6:
        STATS["failed"] += 1
        return 0.0, s_m, False, False                               # fall back to the moments (feasible)
    cb, bb = abs(c1 - c_cap) <= 1e-6 * max(1.0, c_cap), abs(b1 - b_cap) <= 1e-6 * max(1.0, b_cap)
    STATS["binding"] += int(cb or bb); STATS["crps_binding"] += int(cb); STATS["brier_binding"] += int(bb)
    return d, s, cb, bb


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
    # Parity of the full-distribution truth probability with the harness's own.
    arrays = R._case_arrays(train, params)
    assert np.allclose(_scores(arrays, 0.0, s_m)[0], R._probs_fast(arrays, 0.0, s_m), atol=1e-9)
    d15, s15 = R._fit_both_fast(train, params, oos.LOGLOSS_REFIT_HALF_LIFE_DAYS, s_m)
    dc, sc, cb, bb = _constrained(train, params, s_m)
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    arms = {"k100": params,
            "k050": C._shifted(params, 0.5 * d15, 0.5 * s_m + 0.5 * s15),
            "c000": C._shifted(params, dc, sc),
            "c050": C._shifted(params, 0.5 * dc, 0.5 * s_m + 0.5 * sc)}
    row = {
        "event_date": case["event_date"], "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        "arms": {arm: {"probability": oos._apply_parameters(values, p), "calibrated_center_c": oos._corrected_center(mean, p)}
                 for arm, p in arms.items()},
        "fit": {"s_prod": s_m, "delta_15": d15, "s_15": s15, "delta_c": dc, "s_c": sc, "crps_binding": int(cb), "brier_binding": int(bb)},
    }
    row["probability"] = row["arms"]["k100"]["probability"]
    row["calibrated_center_c"] = row["arms"]["k100"]["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "cons", "--model-backfill-arrivals", "--fast"]
    rc = R.main()
    print("constraint stats", STATS, flush=True)
    sys.exit(rc)
