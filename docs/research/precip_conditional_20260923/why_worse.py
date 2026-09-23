"""Why does the fixed cold shift get worse out of sample?  Three cuts on P0.

1. The heavy pre-peak rain forecast days (hist ≥10 mm) split by half of the
   sample and by whether rain actually came (present-weather / ASOS mm):
   residuals in °C, raw and station-demeaned.
2. The rule's form: cold shift × width scale grid through the production
   transform, RPS on all / first half / second half.
3. The forecast's own quality at the threshold: how often ≥10 mm before the
   peak verified.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = HERE.parent / "conditional_belief_20260923"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT))
import fit_attribute as FA  # noqa: E402
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402

SPLIT_DAY = "2026-09-08"


def rps_of(vec, hit):
    v = np.asarray(vec, float)
    v = v / v.sum()
    F = np.cumsum(v)
    H = (np.arange(len(v)) >= hit).astype(float)
    return float(((F - H) ** 2).sum() / max(1, len(v) - 1))


def transform(row, delta_c, lam):
    prob = {k: float(v) for k, v in zip(row["keys"], row["p"])}
    total = sum(prob.values()) or 1.0
    prob = {k: v / total for k, v in prob.items()}
    if delta_c == 0.0 and lam == 1.0:
        return [prob[k] for k in row["keys"]]
    out, _ = apply_center_width_calibration(prob, delta_c=delta_c, width_lambda=lam,
                                            ordered_keys=row["keys"], key_centers_c=row["centers"])
    return [out[k] for k in row["keys"]]


def main():
    data = json.loads((BASE / "dataset.json").read_text())
    fc = json.loads((HERE / "forecast_timing_features.json").read_text())
    pf = json.loads((HERE / "precip_features.json").read_text())
    tm = json.loads((HERE / "timing_features.json").read_text())
    rows = [(r, f, p, t) for r, f, p, t in zip(data, fc, pf, tm) if r["stage"] == "P0" and f.get("hist_precip_before_peak_mm") is not None]
    w = np.array([r["weight"] for r, *_ in rows])
    day = np.array([r["date"] for r, *_ in rows])
    station = np.array([r["station"] for r, *_ in rows])
    sd = np.array([f'{r["station"]}|{r["date"]}' for r, *_ in rows])
    pre = np.array([f["hist_precip_before_peak_mm"] for _, f, *_ in rows])
    settled = np.array([np.nan if p["settled_tmax_c"] is None else p["settled_tmax_c"] for _, _, p, _ in rows])
    mu = np.array([float((np.array(r["p"]) / np.sum(r["p"]) * np.array(r["centers"])).sum()) for r, *_ in rows])
    sigma = np.array([float(np.sqrt((np.array(r["p"]) / np.sum(r["p"]) * (np.array(r["centers"]) - m) ** 2).sum())) for (r, *_), m in zip(rows, mu)])
    res = settled - mu
    dm = res.copy()
    for st in set(station):
        sel = station == st
        ok = sel & ~np.isnan(res)
        dm[sel] = res[sel] - np.average(res[ok], weights=w[ok])
    present = np.array([np.nan if p["truth_present"] is None else p["truth_present"] for _, _, p, _ in rows])
    pre15_obs = np.array([np.nan if t.get("pre15_present") is None else t["pre15_present"] for *_, t in rows])
    truth_mm = np.array([np.nan if p["truth_mm"] is None else p["truth_mm"] for _, _, p, _ in rows])
    first = day <= SPLIT_DAY
    heavy = pre >= 10.0

    def cell(mask, label):
        if mask.sum() < 3:
            return
        ww = w[mask]
        print(f"   {label:44s} n={ww.sum():6.1f}  raw {np.average(res[mask], weights=ww):+.3f}  demeaned {np.average(dm[mask], weights=ww):+.3f}  σ {np.average(sigma[mask], weights=ww):.2f}  |res|>1σ {np.average(np.abs(res[mask]) > sigma[mask], weights=ww):.2f}")

    print("== 1. heavy pre-peak forecast days (hist pre ≥10 mm), P0, residual = settled − centre (°C)")
    cell(heavy, "all heavy")
    cell(heavy & first, "first half (≤09-08)")
    cell(heavy & ~first, "second half")
    cell(heavy & (pre15_obs == 1), "verified: rain reported before 15:00")
    cell(heavy & (pre15_obs == 0), "not verified: no rain before 15:00")
    cell(heavy & first & (pre15_obs == 1), "first half, verified")
    cell(heavy & ~first & (pre15_obs == 1), "second half, verified")
    cell(heavy & ~first & (pre15_obs == 0), "second half, not verified")
    cell(~heavy & (pre < 1.0), "dry forecast (<1 mm before peak)")
    print("   verified share among heavy days:", round(float(np.average(pre15_obs[heavy & ~np.isnan(pre15_obs)], weights=w[heavy & ~np.isnan(pre15_obs)])), 3),
          " first half", round(float(np.average(pre15_obs[heavy & first & ~np.isnan(pre15_obs)], weights=w[heavy & first & ~np.isnan(pre15_obs)])), 3),
          " second half", round(float(np.average(pre15_obs[heavy & ~first & ~np.isnan(pre15_obs)], weights=w[heavy & ~first & ~np.isnan(pre15_obs)])), 3))
    asos = heavy & ~np.isnan(truth_mm)
    if asos.sum():
        print("   ASOS heavy days: observed ≥10 mm share", round(float(np.average(truth_mm[asos] >= 10, weights=w[asos])), 3), "n", round(float(w[asos].sum()), 1))
    # Per-station view of the heavy days: who carries the signal.
    print("   by station (heavy days ≥3 station-days): n, demeaned residual")
    for st in sorted(set(station[heavy])):
        m = heavy & (station == st)
        if w[m].sum() >= 3:
            print(f"      {st} n={w[m].sum():.0f} {np.average(dm[m], weights=w[m]):+.2f}  first {np.average(dm[m & first], weights=w[m & first]) if w[m & first].sum() else float('nan'):+.2f} second {np.average(dm[m & ~first], weights=w[m & ~first]) if w[m & ~first].sum() else float('nan'):+.2f}")

    print("== 2. form of the rule on heavy days: cold shift × width scale, ΔRPS vs served (station-day weighted)")
    base = np.array([rps_of(r["p"], r["hit"]) for r, *_ in rows])
    rng = np.random.default_rng(7)

    class S:
        pass

    def summ(diff, mask):
        s = S()
        s.sd, s.w = sd[mask], w[mask]
        keys, day_of, tab = FA.station_day_table(s, np.ones(mask.sum(), bool), {"d": diff[mask]})
        d = tab["d"]
        lo, hi = FA.block_bootstrap(day_of, d, rng)
        return f"{d.mean():+.5f} [{lo:+.5f},{hi:+.5f}]"

    for T in (5.0, 10.0):
        wet = pre >= T
        for delta in (0.0, 0.25, 0.5):
            for lam in (1.0, 1.15, 1.3):
                if delta == 0.0 and lam == 1.0:
                    continue
                vals = base.copy()
                for i, (r, *_) in enumerate(rows):
                    if wet[i]:
                        vals[i] = rps_of(transform(r, -delta, lam), r["hit"])
                diff = vals - base
                print(f"   T≥{T:g} shift −{delta:<4g} width ×{lam:<4g}  all {summ(diff, np.ones(len(rows), bool))}  first {summ(diff, first)}  second {summ(diff, ~first)}")
    # Verified-only oracle: shift only the heavy days where rain did come before 15:00.
    vals = base.copy()
    for i, (r, *_) in enumerate(rows):
        if heavy[i] and pre15_obs[i] == 1:
            vals[i] = rps_of(transform(r, -0.5, 1.0), r["hit"])
    print(f"   oracle: shift −0.5 only on heavy days that verified   all {summ(vals - base, np.ones(len(rows), bool))}  second {summ(vals - base, ~first)}")
    vals = base.copy()
    for i, (r, *_) in enumerate(rows):
        if heavy[i] and pre15_obs[i] == 1:
            vals[i] = rps_of(transform(r, -1.0, 1.0), r["hit"])
    print(f"   oracle: shift −1.0 only on heavy days that verified   all {summ(vals - base, np.ones(len(rows), bool))}  second {summ(vals - base, ~first)}")


if __name__ == "__main__":
    main()
