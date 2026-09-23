"""A production-shaped term: shift the served P0 centre on heavy pre-peak rain forecasts.

The walk-forward logit could not learn a term from 40–70 wet station-days.
A served term would be a fixed rule instead, like the radiation term: when
the forecast puts at least T mm before the 15:00 peak hour, move the served
distribution δ°C colder through ``apply_center_width_calibration`` (the
production shift), everything else untouched.  Scored by RPS against the
served vector, station-day weighted, with two controls the zero-bucket
memory demands: the same |δ| with a random sign, and the same δ on every
day regardless of rain.  (T, δ) is chosen on the first half of the days and
reported on the second half.  Forecast side is the short-lead archive
(``hist_``), a ceiling on what an as-of value can do.
Usage: ``python3 fixed_shift_test.py P0|P0-M``; writes ``fixed_shift_<stage>.json``.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = HERE.parent / "conditional_belief_20260923"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT))
import fit_attribute as FA  # noqa: E402
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402

STAGE = next(a for a in sys.argv[1:] if a in ("P0", "P0-M", "P1"))
THRESHOLDS = (5.0, 10.0, 20.0)
DELTAS = (0.25, 0.5, 0.75, 1.0)
SPLIT_DAY = "2026-09-08"
T0 = time.time()


def rps_of(vec, hit):
    v = np.asarray(vec, float)
    v = v / v.sum()
    F = np.cumsum(v)
    H = (np.arange(len(v)) >= hit).astype(float)
    return float(((F - H) ** 2).sum() / max(1, len(v) - 1))


def shifted(row, delta_c):
    prob = {k: float(v) for k, v in zip(row["keys"], row["p"])}
    total = sum(prob.values()) or 1.0
    prob = {k: v / total for k, v in prob.items()}
    if delta_c == 0.0:
        return [prob[k] for k in row["keys"]]
    out, _receipt = apply_center_width_calibration(
        prob, delta_c=delta_c, width_lambda=1.0, ordered_keys=row["keys"], key_centers_c=row["centers"])
    return [out[k] for k in row["keys"]]


def main():
    data = json.loads((BASE / "dataset.json").read_text())
    fc = json.loads((HERE / "forecast_timing_features.json").read_text())
    rows = [(r, t) for r, t in zip(data, fc) if r["stage"] == STAGE and t.get("hist_precip_before_peak_mm") is not None]
    n = len(rows)
    w = np.array([r["weight"] for r, _ in rows])
    day = np.array([r["date"] for r, _ in rows])
    sd = np.array([f'{r["station"]}|{r["date"]}' for r, _ in rows])
    pre = np.array([t["hist_precip_before_peak_mm"] for _, t in rows])
    base = np.array([rps_of(r["p"], r["hit"]) for r, _ in rows])
    rng = np.random.default_rng(20260923)
    sign_by_sd = {k: rng.choice([-1.0, 1.0]) for k in set(sd)}
    signs = np.array([sign_by_sd[k] for k in sd])
    results = {}
    for T in THRESHOLDS:
        wet = pre >= T
        for delta in DELTAS:
            for arm in ("rule", "random_sign", "all_days"):
                vals = base.copy()
                for i, (r, _t) in enumerate(rows):
                    if arm == "rule" and wet[i]:
                        vals[i] = rps_of(shifted(r, -delta), r["hit"])
                    elif arm == "random_sign" and wet[i]:
                        vals[i] = rps_of(shifted(r, signs[i] * delta), r["hit"])
                    elif arm == "all_days":
                        vals[i] = rps_of(shifted(r, -delta), r["hit"])
                results[(T, delta, arm)] = vals - base
        print(STAGE, "T", T, f"{time.time() - T0:.0f}s", flush=True)

    def summary(diff, mask):
        keys, day_of, tab = FA.station_day_table(_S(mask, sd, w), np.ones(mask.sum(), bool), {"d": diff[mask]})
        d = tab["d"]
        return {"mean": round(float(d.mean()), 6), "ci95": [round(x, 6) for x in FA.block_bootstrap(day_of, d, rng)],
                "station_days": len(keys)}

    class _S:
        def __init__(self, mask, sd_all, w_all):
            self.sd = sd_all[mask]
            self.w = w_all[mask]

    first = day <= SPLIT_DAY
    second = ~first
    out = {"stage": STAGE, "rows": n, "split_day": SPLIT_DAY,
           "wet_station_days": {str(T): round(float(sum(w[pre >= T])), 1) for T in THRESHOLDS},
           "base_rps_station_day": round(float(np.average(base, weights=w)), 5),
           "arms": {}}
    best = None
    for (T, delta, arm), diff in results.items():
        entry = {"all": summary(diff, np.ones(n, bool)), "first_half": summary(diff, first), "second_half": summary(diff, second)}
        out["arms"][f"T{T:g}_d{delta:g}_{arm}"] = entry
        if arm == "rule" and (best is None or entry["first_half"]["mean"] < best[1]):
            best = ((T, delta), entry["first_half"]["mean"])
    out["chosen_on_first_half"] = {"T": best[0][0], "delta": best[0][1], "first_half_mean": best[1],
                                   "second_half": out["arms"][f"T{best[0][0]:g}_d{best[0][1]:g}_rule"]["second_half"],
                                   "second_half_random_sign": out["arms"][f"T{best[0][0]:g}_d{best[0][1]:g}_random_sign"]["second_half"],
                                   "second_half_all_days": out["arms"][f"T{best[0][0]:g}_d{best[0][1]:g}_all_days"]["second_half"]}
    (HERE / f"fixed_shift_{STAGE}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({"base": out["base_rps_station_day"], "wet": out["wet_station_days"], "chosen": out["chosen_on_first_half"]}, indent=1))
    for name, e in out["arms"].items():
        if name.endswith("_rule"):
            rs = out["arms"][name.replace("_rule", "_random_sign")]
            al = out["arms"][name.replace("_rule", "_all_days")]
            print(f"  {name:22s} all {e['all']['mean']:+.5f} [{e['all']['ci95'][0]:+.5f},{e['all']['ci95'][1]:+.5f}]  2nd {e['second_half']['mean']:+.5f} [{e['second_half']['ci95'][0]:+.5f},{e['second_half']['ci95'][1]:+.5f}]   random-sign all {rs['all']['mean']:+.5f}   all-days all {al['all']['mean']:+.5f}")


if __name__ == "__main__":
    main()
