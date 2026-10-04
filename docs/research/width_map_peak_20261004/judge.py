#!/usr/bin/env python3
"""PROTOCOL.md: rows/ -> the peak bin, the three scores and the verdict per arm.

    python3 judge.py
"""
import json
import os
import pickle
import sys
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from bayes_basket_target.center_width_store import _proper_score_gate  # noqa: E402
import rows as R  # noqa: E402

GRID = R.GRID
G75 = GRID.index(0.75)
MIN_PREFIX_DAYS = 8
CAP_FROM = 0.40
BOOT = 2000
SEED = 20261004
SEGMENTS = (("全期", "2026-07-31", "2026-10-01"), ("前段 07-31..08-31", "2026-07-31", "2026-08-31"),
            ("后段 09-01..10-01", "2026-09-01", "2026-10-01"))


def load():
    out = []
    for name in sorted(os.listdir(os.path.join(HERE, "rows"))):
        with open(os.path.join(HERE, "rows", name), "rb") as fh:
            out.extend(pickle.load(fh))
    return out


def prefix_of(rows, day):
    cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
    midnight = day + "T00:00:00Z"
    return [r for r in rows if r["d"] <= cut and r["known"] <= midnight]


def forward_lambda(rows, days, key):
    """One fleet lambda per scoring day, fitted on what was knowable."""
    nll = np.array([[cell[0][0] for cell in r[key]] for r in rows])        # (N, G)
    d = np.array([r["d"] for r in rows])
    known = np.array([r["known"] for r in rows])
    chosen = {}
    for day in days:
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        m = (d <= cut) & (known <= day + "T00:00:00Z")
        if len(set(d[m])) < MIN_PREFIX_DAYS:
            chosen[day] = G75
            continue
        chosen[day] = int(np.argmin(nll[m].mean(axis=0)))
    return chosen


def capped(row, kappa):
    """P@0.75 with the top bucket's excess over CAP_FROM scaled by kappa, the
    surplus handed to its two neighbours in proportion to their own mass."""
    vec = np.array(row["p75"], dtype=float)
    top = int(np.argmax(vec))
    if vec[top] > CAP_FROM and kappa < 1.0:
        surplus = (vec[top] - CAP_FROM) * (1.0 - kappa)
        vec[top] -= surplus
        sides = [i for i in (top - 1, top + 1) if 0 <= i < len(vec)]
        mass = np.array([vec[i] for i in sides])
        share = mass / mass.sum() if mass.sum() > 0 else np.full(len(sides), 1.0 / len(sides))
        for i, s in zip(sides, share):
            vec[i] += surplus * s
    keys = row["keys"]
    return R.read(vec, keys, [float(int(k)) for k in keys], row["actual"], row["truth"])


def forward_kappa(rows, days):
    d = np.array([r["d"] for r in rows])
    known = np.array([r["known"] for r in rows])
    top = np.array([max(r["p75"]) for r in rows])
    hit = np.array([1.0 if r["keys"][int(np.argmax(r["p75"]))] == r["truth"] else 0.0 for r in rows])
    chosen = {}
    for day in days:
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        m = (d <= cut) & (known <= day + "T00:00:00Z") & (top >= CAP_FROM)
        if len(set(d[(d <= cut) & (known <= day + "T00:00:00Z")])) < MIN_PREFIX_DAYS or m.sum() == 0:
            chosen[day] = 1.0
            continue
        den = (top[m] - CAP_FROM).sum()
        chosen[day] = float(np.clip((hit[m] - CAP_FROM).sum() / den, 0.0, 1.0)) if den > 0 else 1.0
    return chosen


def boot_gap(cells_by_arm, days_index, k, lo, hi, base):
    """hit - predicted in [lo, hi) per arm, and each arm's gap minus the base arm's."""
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, k, size=(BOOT, k))
    sums = {}
    for arm, cells in cells_by_arm.items():
        num, den, pred = np.zeros(k), np.zeros(k), np.zeros(k)
        for day_code, cell in zip(days_index, cells):
            for p, y in cell[1]:
                if lo <= p < hi:
                    num[day_code] += y - p
                    pred[day_code] += p
                    den[day_code] += 1.0
        sums[arm] = (num, den, pred)
    out = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        base_b = sums[base][0][idx].sum(1) / sums[base][1][idx].sum(1)
        for arm, (num, den, pred) in sums.items():
            n = den.sum()
            if n == 0:
                out[arm] = None
                continue
            gap_b = num[idx].sum(1) / den[idx].sum(1)
            diff_b = gap_b - base_b
            out[arm] = {"n": int(n), "pred": pred.sum() / n, "hit": (pred.sum() + num.sum()) / n, "gap": num.sum() / n,
                        "ci": [float(v) for v in np.nanpercentile(gap_b, [2.5, 97.5])],
                        "vs_base": float(num.sum() / n - sums[base][0].sum() / sums[base][1].sum()),
                        "vs_base_ci": [float(v) for v in np.nanpercentile(diff_b, [2.5, 97.5])]}
    return out


def boot_mean(values, days_index, k):
    s = np.bincount(days_index, values, k)
    c = np.bincount(days_index, np.ones(len(values)), k)
    idx = np.random.default_rng(SEED).integers(0, k, size=(BOOT, k))
    b = s[idx].sum(1) / c[idx].sum(1)
    return float(values.mean()), [float(v) for v in np.percentile(b, [2.5, 97.5])]


def main():
    rows = load()
    days = sorted({r["d"] for r in rows})
    lam_p = forward_lambda(rows, days, "P")
    lam_b = forward_lambda(rows, days, "B")
    kap = forward_kappa(rows, days)
    arms = {
        "PRE": [r["pre"] for r in rows],
        "P@0.75": [r["P"][G75] for r in rows],
        "P@fwd": [r["P"][lam_p[r["d"]]] for r in rows],
        "B@0.75": [r["B"][G75] for r in rows],
        "B@fwd": [r["B"][lam_b[r["d"]]] for r in rows],
        "C@fwd": [capped(r, kap[r["d"]]) for r in rows],
    }
    result = {"decisions": len(rows), "station_days": len({(r["st"], r["d"]) for r in rows}), "event_days": len(days),
              "forward": {"P": {d: GRID[g] for d, g in lam_p.items()}, "B": {d: GRID[g] for d, g in lam_b.items()},
                          "kappa": kap}}
    print(f"决策 {len(rows)}  站日 {result['station_days']}  事件日 {len(days)}  {days[0]}..{days[-1]}")
    late = [d for d in days if d >= "2026-09-01"]
    print("前推 λ（后段）  P:", sorted({GRID[lam_p[d]] for d in late}), " B:", sorted({GRID[lam_b[d]] for d in late}),
          " κ:", f"{min(kap[d] for d in late):.2f}..{max(kap[d] for d in late):.2f}")
    nll_p = np.array([[cell[0][0] for cell in r["P"]] for r in rows]).mean(0)
    nll_b = np.array([[cell[0][0] for cell in r["B"]] for r in rows]).mean(0)
    print("样本内 LogScore 最小处  P:", GRID[int(np.argmin(nll_p))], f"{nll_p.min():.5f}", " B:", GRID[int(np.argmin(nll_b))],
          f"{nll_b.min():.5f}", f" P@0.75 {nll_p[G75]:.5f}  B@0.75 {nll_b[G75]:.5f}")
    result["in_sample"] = {"P": dict(zip(map(str, GRID), map(float, nll_p))), "B": dict(zip(map(str, GRID), map(float, nll_b)))}

    day_code = {d: i for i, d in enumerate(days)}
    result["peak"] = {}
    for scope, keep in (("全部提前量", lambda r: True), (">36h", lambda r: r["bin"] <= 1)):
        m = [keep(r) for r in rows]
        codes = [day_code[r["d"]] for r, ok in zip(rows, m) if ok]
        sub = {arm: [c for c, ok in zip(cells, m) if ok] for arm, cells in arms.items()}
        result["peak"][scope] = {}
        print(f"\n== 查峰 [{scope}]  决策 {sum(m)}")
        for lo, hi, name in ((0.4, 0.5, "0.4–0.5"), (0.5, 1.01, "≥0.5")):
            table = boot_gap(sub, codes, len(days), lo, hi, "P@0.75")
            result["peak"][scope][name] = table
            print(f"   p {name}\n   臂        桶数   预测    命中    命中−预测 [95%]              对 P@0.75 [95%]")
            for arm, t in table.items():
                if t is None:
                    print(f"   {arm:<8} 无")
                    continue
                star = "*" if (t["ci"][0] > 0 or t["ci"][1] < 0) else " "
                star2 = "*" if (t["vs_base_ci"][0] > 0 or t["vs_base_ci"][1] < 0) else " "
                print(f"   {arm:<8} {t['n']:5d}  {t['pred']:.3f}  {t['hit']:.3f}  {t['gap']*100:+6.1f} [{t['ci'][0]*100:+6.1f},{t['ci'][1]*100:+6.1f}]{star}"
                      f"   {t['vs_base']*100:+6.1f} [{t['vs_base_ci'][0]*100:+6.1f},{t['vs_base_ci'][1]*100:+6.1f}]{star2}")
        share = {arm: float(np.mean([c[2] >= 0.5 for c in cells])) for arm, cells in sub.items()}
        z2 = {arm: float(np.mean([c[3] for c in cells])) for arm, cells in sub.items()}
        result["peak"][scope]["top_at_least_half"] = share
        result["peak"][scope]["z2"] = z2
        print("   最高桶≥0.5 的决策占比  " + "  ".join(f"{a} {v:.1%}" for a, v in share.items()))
        print("   E[z²]                 " + "  ".join(f"{a} {v:.3f}" for a, v in z2.items()))

    result["scores"] = {}
    for name, start, end in SEGMENTS:
        m = [start <= r["d"] <= end for r in rows]
        clusters = [(r["st"], r["d"]) for r, ok in zip(rows, m) if ok]
        codes = np.array([day_code[r["d"]] for r, ok in zip(rows, m) if ok])
        base = [tuple(c[0]) for c, ok in zip(arms["P@0.75"], m) if ok]
        result["scores"][name] = {}
        print(f"\n== 三分数 对 P@0.75 [{name}]  决策 {sum(m)}")
        print("   臂        ΔLogScore [站日自助]                 [事件日自助]             ΔCRPS [站日自助]                    ΔBrier [站日自助]")
        for arm, cells in arms.items():
            if arm == "P@0.75":
                continue
            cand = [tuple(c[0]) for c, ok in zip(cells, m) if ok]
            gate = _proper_score_gate(base, cand, clusters)
            d_log = np.array([c[0] - b[0] for b, c in zip(base, cand)])
            _, ci_day = boot_mean(d_log, codes, len(days))
            result["scores"][name][arm] = {**gate, "ci_logscore_event_day": ci_day}

            def f(v, ci):
                star = "*" if (ci[0] > 0 or ci[1] < 0) else " "
                return f"{v:+.5f} [{ci[0]:+.5f},{ci[1]:+.5f}]{star}"

            print(f"   {arm:<8} {f(gate['d_logscore'], gate['ci_logscore'])}  [{ci_day[0]:+.5f},{ci_day[1]:+.5f}]"
                  f"  {f(gate['d_crps'], gate['ci_crps'])}  {f(gate['d_brier'], gate['ci_brier'])}")

    # The verdict, as PROTOCOL.md wrote it.
    print("\n== 判决")
    result["verdict"] = {}
    for arm in ("B@0.75", "B@fwd", "C@fwd", "P@fwd"):
        peak_ok = True
        for scope in ("全部提前量", ">36h"):
            t = result["peak"][scope]["≥0.5"][arm]
            base_t = result["peak"][scope]["≥0.5"]["P@0.75"]
            peak_ok &= bool(t is not None and t["ci"][0] <= 0.0 <= t["ci"][1] and abs(t["gap"]) < abs(base_t["gap"])
                            and (t["vs_base_ci"][0] > 0 or t["vs_base_ci"][1] < 0))
        full = result["scores"]["全期"][arm]
        not_worse = all(full[k] is not None and full[k][0] <= 0.0 for k in ("ci_logscore", "ci_crps", "ci_brier"))
        halves = all(result["scores"][n][arm]["d_logscore"] <= 0.001 for n in ("前段 07-31..08-31", "后段 09-01..10-01"))
        result["verdict"][arm] = {"peak_fixed": peak_ok, "scores_not_worse": bool(not_worse and halves)}
        print(f"   {arm:<8} 峰修好 {'是' if peak_ok else '否'}   分数不变差 {'是' if (not_worse and halves) else '否'}")
    with open(os.path.join(HERE, "results.json"), "w") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    main()
