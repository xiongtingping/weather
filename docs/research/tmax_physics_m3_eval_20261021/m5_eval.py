"""M5 evaluator (PROTOCOL_M5.md, committed b15e347b before any reading):
arm 7 = arm 6 + phys x DPD06, against arm 6, walk-forward as M3.

    --smoke   counts and finite predictions only
    (no flag) the single judgement run: writes reading_m5_<UTC>.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate as e  # noqa: E402


def augmented(D, H: int, cphys: np.ndarray) -> np.ndarray:
    c = cphys[D.st]
    return np.column_stack([D.F[H], c * D.G[H] * D.DPD])


def fit_pair(D, m, H, cphys):
    six = e.fit_linear(D, m, H, cphys, True)
    keep = D.F[H]
    D.F[H] = augmented(D, H, cphys)
    try:
        seven = e.fit_linear(D, m, H, cphys, True)
    finally:
        D.F[H] = keep
    return six, seven


def predict_arm(D, fm, rows, H, cphys, seen, extra: bool):
    st = D.st[rows]
    c = np.where(seen[st], cphys[st], cphys[-1])
    F = D.F[H][rows]
    if extra:
        F = np.column_stack([F, c * D.G[H][rows] * D.DPD[rows]])
    X = np.column_stack([F, c * D.G[H][rows]])
    icpt = np.where(np.isnan(fm["icpt"][st]), fm["icpt_fallback"], fm["icpt"][st])
    mu = icpt + np.einsum("ij,ij->i", X, fm["coef"][st])
    sd = np.where(seen[st], fm["sd"][st], fm["sd"][-1])
    return mu, sd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    rows = json.loads(e.DAYS.read_text())
    D = e.Data(rows)
    D.DPD = np.array([0.0 if r["dew_obs"][0] is None else r["t_obs"][0] - r["dew_obs"][0] for r in rows])
    n_days = len(D.dates)
    out = {H: {"rows": [], "six": ([], []), "seven": ([], [])} for H in e.CHECK_HOURS}
    fitted_at, fits, cphys, seen = -e.REFIT_EVERY, None, None, None
    for k in range(e.WARMUP_DAYS, n_days):
        if fits is None or k - fitted_at >= e.REFIT_EVERY:
            m = D.day < k
            cphys = e.fit_phys(D, m)
            fits = {H: fit_pair(D, m, H, cphys) for H in e.CHECK_HOURS}
            seen = np.bincount(D.st[m], minlength=len(D.stations)) > 0
            fitted_at = k
        rows_k = np.flatnonzero(D.day == k)
        if not rows_k.size:
            continue
        for H in e.CHECK_HOURS:
            six, seven = fits[H]
            out[H]["rows"].append(rows_k)
            for name, fm, extra in (("six", six, False), ("seven", seven, True)):
                mu, sd = predict_arm(D, fm, rows_k, H, cphys, seen, extra)
                out[H][name][0].append(mu); out[H][name][1].append(sd)
    for H in e.CHECK_HOURS:
        out[H]["rows"] = np.concatenate(out[H]["rows"])
        for name in ("six", "seven"):
            out[H][name] = (np.concatenate(out[H][name][0]), np.concatenate(out[H][name][1]))
    if args.smoke:
        print(json.dumps({"predictions": int(sum(out[H]["rows"].size for H in e.CHECK_HOURS)),
                          "finite": bool(all(np.isfinite(out[H][n][i]).all() for H in e.CHECK_HOURS
                                             for n in ("six", "seven") for i in (0, 1))),
                          "dpd06_mean": round(float(D.DPD.mean()), 2), "note": "smoke only -- no scores printed"}))
        return 0
    rng = np.random.default_rng(e.SEED)
    summary = {}
    for H in e.CHECK_HOURS:
        rws = out[H]["rows"]; y = D.y[rws]
        s6 = e.scores(y, *out[H]["six"]); s7 = e.scores(y, *out[H]["seven"])
        d = s7 - s6
        days, inv = np.unique(D.day[rws], return_inverse=True)
        nd = days.size; cnt = np.bincount(inv, minlength=nd).astype(float)
        sums = np.column_stack([np.bincount(inv, weights=d[:, j], minlength=nd) for j in range(3)])
        draws = np.empty((e.DRAWS, 3))
        for t in range(e.DRAWS):
            starts = rng.integers(0, nd, size=math.ceil(nd / e.BLOCK))
            pick = ((starts[:, None] + np.arange(e.BLOCK)[None, :]) % nd).ravel()
            draws[t] = sums[pick].sum(axis=0) / cnt[pick].sum()
        summary[str(H)] = {"n": int(rws.size), "days": int(nd),
                           "six": [round(float(v), 4) for v in s6.mean(axis=0)],
                           "seven": [round(float(v), 4) for v in s7.mean(axis=0)],
                           "seven_minus_six": [round(float(v), 4) for v in d.mean(axis=0)],
                           "ci95": [[round(float(np.quantile(draws[:, j], q)), 4) for q in (0.025, 0.975)] for j in range(3)]}
    reading = {"schema": "tmax_physics_m5_reading_v1", "protocol": "b15e347b",
               "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "metrics": ["LogScore", "CRPS", "MAE"], "summary": summary}
    stamp = reading["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_m5_{stamp}.json").write_text(json.dumps(reading, indent=1))
    print("written", f"reading_m5_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
