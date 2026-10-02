#!/usr/bin/env python3
"""PROTOCOL.md: stretch vs dress, constant vs day-spread-aware factor, on the served P0 replay.

The replay is ``single_system_logloss_reg_20260924/capture_reg.py`` loaded from
its source with one line changed (every arm scored, --fast's day-end cut kept).
Parameters are production's own ``oos._fit_parameters`` (charter 216 rule); no
arm refits anything -- the arms differ only in how (delta, s) turn members into
bucket probabilities.  ``base`` is asserted equal to ``oos._apply_parameters``.

    python3 capture_sd.py --stations ZUUU,EGLC
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
R = types.ModuleType("capture_reg_sd")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos = R.C, R.oos
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS, "production rule must be charter 216"

ARMS = ("base", "dress", "beta", "dress_beta")
BETA = 0.5
R.ARMS = ARMS
R.CUTOFF_UTC = "2026-10-02T09:00:00Z"
R.PERIODS["sd"] = lambda day: "2026-07-20" <= day <= "2026-10-01"
R.HERE = HERE


def _pstd(values):
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def arm_probability(values, p, arm):
    current = [float(v) for v in values]
    mean = sum(current) / len(current)
    center = oos._corrected_center(mean, p)
    s = float(p["spread_factor"])
    d = _pstd(current)
    if arm in ("beta", "dress_beta"):
        m_spread = float(p.get("train_mean_spread_c") or 0.0)
        if d > 0 and m_spread > 0:
            s = min(5.0, max(0.25, s * (m_spread / d) ** (1.0 - BETA)))
    corrected = [center + s * (v - mean) for v in current]
    bw = oos.KERNEL_BANDWIDTH_C
    if arm in ("dress", "dress_beta") and s > 1.0 and len(current) > 1:
        bw = max(bw, math.sqrt(max(0.0, (s * s - 1.0)) * d * d))
    if len(current) == 1:
        bw = max(oos.KERNEL_BANDWIDTH_C, float(p.get("train_residual_sigma_c") or p.get("train_rmse_c") or 0.0))
    member = oos._probabilities(corrected, bandwidth_c=bw)
    # Production's own mixture (reliability 1.0): the climate sample's buckets
    # join the key set at weight zero, exactly as ``_apply_parameters`` does.
    climate = oos._probabilities([float(v) for v in p["climate_samples_c"]])
    rel = float(p["reliability_weight"])
    keys = set(member) | set(climate)
    mixed = {k: rel * member.get(k, 0.0) + (1.0 - rel) * climate.get(k, 0.0) for k in keys}
    total = sum(mixed.values())
    return {k: v / total for k, v in sorted(mixed.items(), key=lambda kv: int(kv[0]))}, s, bw


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
    if float(params.get("reliability_weight", 1.0)) != 1.0:
        raise RuntimeError("climate blend in play; the arms assume none")
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    prod = oos._apply_parameters(values, params)
    arms, diag = {}, {}
    for arm in ARMS:
        prob, s_eff, bw = arm_probability(values, params, arm)
        arms[arm] = {"probability": prob, "calibrated_center_c": oos._corrected_center(mean, params)}
        diag[arm] = {"s": round(s_eff, 4), "bw": round(bw, 4), "modes": _modes(prob)}
    base = arms["base"]["probability"]
    assert set(base) == set(prod) and max(abs(base[k] - prod[k]) for k in prod) < 1e-12, "base arm must be production"
    d = _pstd(values)
    m_spread = float(params.get("train_mean_spread_c") or 0.0)
    row = {
        "event_date": case["event_date"], "actual_tmax_c": float(case["actual_tmax_c"]),
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        "arms": arms,
        "fit": {"d": round(d, 4), "M": round(m_spread, 4), "ratio": round(d / m_spread, 4) if m_spread > 0 else -1.0,
                "s": round(float(params["spread_factor"]), 4), "sigma": round(float(params.get("train_residual_sigma_c") or 0.0), 4),
                "rule_216": 1.0 if params.get("fit_rule") == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS else 0.0, **{f"{a}_{k}": v for a, dd in diag.items() for k, v in dd.items()}},
    }
    row["probability"] = arms["base"]["probability"]
    row["calibrated_center_c"] = arms["base"]["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "sd", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
