#!/usr/bin/env python3
"""PROTOCOL_fit.md: single-system shape chosen by LogLoss (kernel share c) or a
four-button sinh-arcsinh family, on the served P0 replay.

Same replay as ``capture_sd.py`` (``single_system_logloss_reg_20260924/
capture_reg.py`` loaded from source, every arm scored, --fast's day-end cut).
``base`` is production (``oos._fit_parameters`` + ``_apply_parameters``) and is
asserted equal to it; the other arms refit their own parameters on the same
training cases with production's leash (H15 weights, L-BFGS-B maxiter 60,
1e-6 floor, kappa 0.5 back to each arm's own start).

    python3 capture_fit.py --stations ZUUU,EGLC
"""
from __future__ import annotations

import math
import sys
import types
from datetime import date
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")
OLD = 'for arm in (("prod", "ll_both_reg") if FAST else ARMS):'
assert text.count(OLD) == 1
text = text.replace(OLD, "for arm in ARMS:")
R = types.ModuleType("capture_reg_fit")
R.__file__ = str(SOURCE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
C, oos = R.C, R.oos
assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS, "production rule must be charter 216"

ARMS = ("base", "dress", "fit_c0", "dress_fit", "shash")
R.ARMS = ARMS
R.CUTOFF_UTC = "2026-10-02T09:00:00Z"
R.PERIODS["fit"] = lambda day: "2026-07-20" <= day <= "2026-10-01"
R.HERE = HERE

KAPPA = oos.LOGLOSS_REFIT_KAPPA                    # 0.5
HALF_LIFE = oos.LOGLOSS_REFIT_HALF_LIFE_DAYS       # 15
MAXITER = oos.LOGLOSS_REFIT_MAXITER                # 60
P_FLOOR = oos.LOGLOSS_REFIT_P_FLOOR                # 1e-6
BW0 = oos.KERNEL_BANDWIDTH_C                       # 0.5
DELTA_B = oos.LOGLOSS_REFIT_DELTA_BOUNDS_C         # (-3, 3)
S_B = (0.25, 5.0)
C_B = (0.0, 0.999)
EPS_B = (-1.5, 1.5)
TAU_B = (0.4, 2.5)
TAIL_C = 2.5
PARITY_FITS = 30


def _pstd(values):
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _moment_params(params):
    """The pre-216 (moment) parameters production started its refit from."""
    shift = float(params.get("logloss_shift_c") or 0.0)
    out = dict(params)
    out["bias_c"] = float(params["bias_c"]) - shift
    out["bias_bin_shifts_c"] = [float(v) - shift for v in params["bias_bin_shifts_c"]]
    out["spread_factor"] = float(params["moment_spread_factor"])
    return out


def _arrays(train, moment):
    width = max(len(c["values"]) for c in train)
    dev = np.zeros((len(train), width)); mask = np.zeros((len(train), width), dtype=bool)
    centers = np.zeros(len(train)); buckets = np.zeros(len(train)); d = np.zeros(len(train))
    for row, case in enumerate(train):
        cur = [float(v) for v in case["values"]]
        mean = sum(cur) / len(cur)
        centers[row] = mean + oos._resolved_bias_c(mean, moment)
        dev[row, :len(cur)] = [v - mean for v in cur]
        mask[row, :len(cur)] = True
        buckets[row] = int(oos._temperature_bucket(float(case["actual_tmax_c"])))
        d[row] = _pstd(cur)
    days = [date.fromisoformat(str(c["event_date"])[:10]) for c in train]
    anchor = max(days)
    w = np.array([0.5 ** ((anchor - day).days / HALF_LIFE) for day in days])
    return {"dev": dev, "mask": mask, "centers": centers, "buckets": buckets, "d": d,
            "counts": mask.sum(axis=1), "w": w}


def _kernel_bw(c, total_sd):
    """Kernel bandwidth from the kernel's share ``c`` of the variance above the floor.

    b^2 = 0.5^2 + c * (S^2 - 0.5^2) with S the stretched member sd: c = 0 is
    production's fixed kernel, c -> 1 collapses the members onto a Gaussian of
    the same total width.  Linear in c, so the fit has a gradient at c = 0 --
    the ``max(0.5, c*S)`` floor tried first was flat there and pinned c.
    """
    total_var = np.square(total_sd)
    return np.sqrt(BW0 * BW0 + c * np.maximum(0.0, total_var - BW0 * BW0))


def _c_of_bw(bw, total_sd):
    total_var = total_sd * total_sd
    return 0.0 if total_var <= BW0 * BW0 else max(0.0, min(0.999, (bw * bw - BW0 * BW0) / (total_var - BW0 * BW0)))


def _probs_cs(a, delta, s, c):
    """Truth-bucket probability of the kernel-share form, every case at once.

    Mirrors ``oos._probabilities(center + s*(v-mean), bandwidth_c=_kernel_bw(c, s*d))``.
    """
    mask, counts = a["mask"], a["counts"]
    dressed = (a["centers"] + delta)[:, None] + s * a["dev"]
    middle = np.where(mask, dressed, 0.0).sum(axis=1) / counts
    std = np.sqrt(np.where(mask, (dressed - middle[:, None]) ** 2, 0.0).sum(axis=1) / counts)
    bw = _kernel_bw(c, s * a["d"])
    shrink = np.where(std > bw, np.sqrt(np.clip(1.0 - (bw / np.where(std > 0, std, 1.0)) ** 2, 0.0, 1.0)), 1.0)
    dressed = middle[:, None] + shrink[:, None] * (dressed - middle[:, None])
    low = np.floor(np.where(mask, dressed, np.inf).min(axis=1) - 4.0 * bw)
    high = np.ceil(np.where(mask, dressed, -np.inf).max(axis=1) + 4.0 * bw)
    bucket = a["buckets"]

    def _mean(hi, lo):
        v = ndtr((hi[:, None] - dressed) / bw[:, None]) - ndtr((lo[:, None] - dressed) / bw[:, None])
        return np.where(mask, v, 0.0).sum(axis=1) / counts

    mass = _mean(bucket + 0.5, bucket - 0.5)
    total = _mean(high + 0.5, low - 0.5)
    inside = (bucket >= low) & (bucket <= high) & (total > 0.0)
    return np.where(inside, mass / np.where(total > 0.0, total, 1.0), 0.0)


def _shash_cdf(x, mu, sigma, eps, tau):
    return ndtr(np.sinh(tau * np.arcsinh((x - mu) / sigma) - eps))


SHASH_WINDOW_C = 20.0   # the family is truncated to centre +-20C and renormalised, in fit and in serving alike


def _probs_shash(a, delta, s, eps, tau):
    mu = a["centers"] + delta
    sigma = np.sqrt(s * s * a["d"] ** 2 + BW0 * BW0)
    b = a["buckets"]
    lo = np.floor(mu - SHASH_WINDOW_C); hi = np.ceil(mu + SHASH_WINDOW_C)
    total = _shash_cdf(hi + 0.5, mu, sigma, eps, tau) - _shash_cdf(lo - 0.5, mu, sigma, eps, tau)
    mass = _shash_cdf(b + 0.5, mu, sigma, eps, tau) - _shash_cdf(b - 0.5, mu, sigma, eps, tau)
    inside = (b >= lo) & (b <= hi) & (total > 0.0)
    return np.where(inside, mass / np.where(total > 0.0, total, 1.0), 0.0)


def _nll(w, p):
    return float(-(w * np.log(np.maximum(p, P_FLOOR))).sum() / w.sum())


def _fit(a, objective, x0, bounds):
    fit = minimize(objective, x0=np.array(x0, dtype=float), method="L-BFGS-B",
                   bounds=bounds, options={"maxiter": MAXITER})
    return [float(v) for v in fit.x]


def _leash(start, fitted):
    return [KAPPA * s0 + (1.0 - KAPPA) * f for s0, f in zip(start, fitted)]


def _vector_cs(values, moment, delta, s, c):
    cur = [float(v) for v in values]
    mean = sum(cur) / len(cur)
    center = oos._corrected_center(mean, moment) + delta
    corrected = [center + s * (v - mean) for v in cur]
    bw = float(_kernel_bw(c, s * _pstd(cur)))
    return oos._probabilities(corrected, bandwidth_c=bw), center, bw


def _vector_shash(values, moment, delta, s, eps, tau):
    cur = [float(v) for v in values]
    mean = sum(cur) / len(cur)
    mu = oos._corrected_center(mean, moment) + delta
    sigma = math.sqrt(s * s * _pstd(cur) ** 2 + BW0 * BW0)
    lo, hi = int(math.floor(mu - SHASH_WINDOW_C)), int(math.ceil(mu + SHASH_WINDOW_C))
    edges = np.arange(lo - 0.5, hi + 1.5, 1.0)
    mass = np.diff(_shash_cdf(edges, mu, sigma, eps, tau))
    mass = mass / mass.sum()
    return {str(lo + i): float(v) for i, v in enumerate(mass) if v > 0.0}, mu, sigma


def _mix_climate(member, p):
    """Production's own mixture (reliability 1.0): climate keys join at weight zero."""
    climate = oos._probabilities([float(v) for v in p["climate_samples_c"]])
    rel = float(p["reliability_weight"])
    keys = set(member) | set(climate)
    mixed = {k: rel * member.get(k, 0.0) + (1.0 - rel) * climate.get(k, 0.0) for k in keys}
    total = sum(mixed.values())
    return {k: v / total for k, v in sorted(mixed.items(), key=lambda kv: int(kv[0]))}


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


def _tail(prob, actual):
    mean = sum(int(k) * v for k, v in prob.items())
    mass = sum(v for k, v in prob.items() if abs(int(k) - mean) >= TAIL_C)
    hit = 1.0 if abs(int(oos._temperature_bucket(actual)) - mean) >= TAIL_C else 0.0
    return mean, mass, hit


class _State:
    parity_checks = 0


def _parity(train, moment, a):
    """Vectorised losses equal the per-case references on a few cases."""
    for i, case in enumerate(train[:4]):
        vals = [float(v) for v in case["values"]]
        key = oos._temperature_bucket(float(case["actual_tmax_c"]))
        for (delta, s, c) in ((0.0, float(moment["spread_factor"]), 0.0), (0.3, 1.7, 0.6)):
            vec, _m, _bw = _vector_cs(vals, moment, delta, s, c)
            ours = float(_probs_cs(a, delta, s, c)[i])
            assert abs(vec.get(key, 0.0) - ours) <= 1e-9, ("cs", vec.get(key, 0.0), ours)
        for (delta, s, eps, tau) in ((0.0, float(moment["spread_factor"]), 0.0, 1.0), (-0.4, 1.3, 0.5, 0.8)):
            vec, _mu, _sg = _vector_shash(vals, moment, delta, s, eps, tau)
            ours = float(_probs_shash(a, delta, s, eps, tau)[i])
            assert abs(vec.get(key, 0.0) - ours) <= 1e-6, ("shash", vec.get(key, 0.0), ours)
    # The production (delta, s) refit objective is this module's cs objective at c=0.
    prod = oos._truth_bucket_probabilities(oos._refit_case_arrays(train, moment), delta_c=0.2, spread=1.4)
    assert np.allclose(prod, _probs_cs(a, 0.2, 1.4, 0.0), atol=1e-12)


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
    if params.get("fit_rule") != oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS:
        raise RuntimeError(f"not a 216 fit: {params.get('fit_rule')}")
    moment = _moment_params(params)
    a = _arrays(train, moment)
    if _State.parity_checks < PARITY_FITS:
        _parity(train, moment, a)
        _State.parity_checks += 1
    w = a["w"]
    s_mom = float(moment["spread_factor"])
    m_spread = float(params.get("train_mean_spread_c") or 0.0)
    values = [float(v) for v in case["values"]]
    mean = sum(values) / len(values)
    actual = float(case["actual_tmax_c"])
    d = _pstd(values)

    # --- fits -------------------------------------------------------------
    # The dress rule b^2 = (s^2-1) d^2 in the share coordinate, at the scope's mean spread.
    c_dress = _c_of_bw(math.sqrt(max(0.0, s_mom * s_mom - 1.0)) * m_spread, s_mom * m_spread) if s_mom > 1.0 else 0.0
    obj_cs = lambda x: _nll(w, _probs_cs(a, x[0], x[1], x[2]))
    raw_c0 = _fit(a, obj_cs, [0.0, s_mom, 0.0], [DELTA_B, S_B, C_B])
    raw_df = _fit(a, obj_cs, [0.0, s_mom, c_dress], [DELTA_B, S_B, C_B])
    obj_sh = lambda x: _nll(w, _probs_shash(a, x[0], x[1], x[2], x[3]))
    raw_sh = _fit(a, obj_sh, [0.0, s_mom, 0.0, 1.0], [DELTA_B, S_B, EPS_B, TAU_B])
    ap_c0 = _leash([0.0, s_mom, 0.0], raw_c0)
    ap_df = _leash([0.0, s_mom, c_dress], raw_df)
    ap_sh = _leash([0.0, s_mom, 0.0, 1.0], raw_sh)

    # --- vectors ----------------------------------------------------------
    arms, diag = {}, {}
    prod = oos._apply_parameters(values, params)
    arms["base"] = {"probability": prod, "calibrated_center_c": oos._corrected_center(mean, params)}
    diag["base"] = {"s": float(params["spread_factor"]), "c": 0.0, "bw": BW0}
    s_b = float(params["spread_factor"])
    bw_dress = max(BW0, math.sqrt(max(0.0, s_b * s_b - 1.0)) * d) if s_b > 1.0 else BW0
    member = oos._probabilities([arms["base"]["calibrated_center_c"] + s_b * (v - mean) for v in values], bandwidth_c=bw_dress)
    arms["dress"] = {"probability": _mix_climate(member, params), "calibrated_center_c": arms["base"]["calibrated_center_c"]}
    diag["dress"] = {"s": s_b, "c": _c_of_bw(bw_dress, s_b * d), "bw": bw_dress}
    for name, ap, raw in (("fit_c0", ap_c0, raw_c0), ("dress_fit", ap_df, raw_df)):
        vec, center, bw = _vector_cs(values, moment, *ap)
        arms[name] = {"probability": _mix_climate(vec, params), "calibrated_center_c": center}
        diag[name] = {"delta": ap[0], "s": ap[1], "c": ap[2], "bw": bw, "raw_delta": raw[0], "raw_s": raw[1], "raw_c": raw[2]}
    vec, mu, sigma = _vector_shash(values, moment, *ap_sh)
    vec = _mix_climate(vec, params)
    arms["shash"] = {"probability": vec, "calibrated_center_c": sum(int(k) * v for k, v in vec.items())}
    diag["shash"] = {"delta": ap_sh[0], "s": ap_sh[1], "eps": ap_sh[2], "tau": ap_sh[3], "sigma": sigma,
                     "raw_delta": raw_sh[0], "raw_s": raw_sh[1], "raw_eps": raw_sh[2], "raw_tau": raw_sh[3]}
    for name in ARMS:
        prob = arms[name]["probability"]
        vmean, tail_mass, tail_hit = _tail(prob, actual)
        diag[name].update({"modes": _modes(prob), "tail_mass": tail_mass, "tail_hit": tail_hit,
                           "vsd": math.sqrt(sum((int(k) - vmean) ** 2 * v for k, v in prob.items()))})
    row = {
        "event_date": case["event_date"], "actual_tmax_c": actual,
        "error_scale": float(params.get("train_residual_sigma_c") or params["train_rmse_c"]),
        "train_n": len(train),
        "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"]) for c in train},
        "arms": arms,
        "fit": {"d": d, "M": m_spread, "ratio": d / m_spread if m_spread > 0 else -1.0, "s_mom": s_mom,
                "sigma": float(params.get("train_residual_sigma_c") or 0.0), "n_eff": float(params.get("train_n_effective") or 0.0),
                **{f"{a_}_{k}": float(v) for a_, dd in diag.items() for k, v in dd.items()}},
    }
    row["probability"] = arms["base"]["probability"]
    row["calibrated_center_c"] = arms["base"]["calibrated_center_c"]
    self.memo[key] = row
    return row


R.RegForecasts.forecast = forecast

if __name__ == "__main__":
    sys.argv += ["--period", "fit", "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
