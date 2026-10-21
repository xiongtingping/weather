"""M4 evaluator (PROTOCOL_M4.md, committed 6ae60173 before any reading).

    --smoke   row counts and finite probabilities only, no scores
    (no flag) the single judgement run: writes reading_m4_<UTC>.json

Reuses the M3 evaluator's data, fits and predictions (arm ⑥ for the centre
and spread, arm ① for the D0 baseline); D2's degrees of freedom are fitted at
each refit on the training rows' standardised residuals.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate as e  # noqa: E402

F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
DF_GRID = (3, 4, 5, 7, 10, 15, 30)
FLOOR = 1e-3
ARMS4 = ("D0", "D1", "D2")
PAIRS4 = {"D1-D0": ("D1", "D0"), "D2-D1": ("D2", "D1")}


def bucket_edges(t_c: float, f_station: bool) -> tuple[float, float]:
    """The settlement bucket holding t_c, edges in C (PROTOCOL_M4 section 2)."""

    if not f_station:
        k = math.floor(t_c + 0.5)
        return k - 0.5, k + 0.5
    tf = t_c * 1.8 + 32.0
    r = math.floor(tf + 0.5)
    b = r - (r % 2)
    return ((b - 0.5) - 32.0) / 1.8, ((b + 1.5) - 32.0) / 1.8


def ladder(mu: float, sd: float, f_station: bool) -> list[tuple[float, float]]:
    lo, hi = mu - 8 * sd - 2, mu + 8 * sd + 2
    out, t = [], lo
    a, b = bucket_edges(t, f_station)
    while a < hi:
        out.append((a, b))
        a, b = bucket_edges(b + 1e-6, f_station)
    return out


def cdf(x: np.ndarray, mu: float, sd: float, df: float | None) -> np.ndarray:
    if df is None:
        return stats.norm.cdf(x, mu, sd)
    scale = sd * math.sqrt((df - 2) / df)
    return stats.t.cdf((x - mu) / scale, df)


def score_one(truth: float, mu: float, sd: float, df: float | None, f_station: bool) -> tuple[float, float, float]:
    edges = ladder(mu, sd, f_station)
    lo = np.array([a for a, _ in edges]); hi = np.array([b for _, b in edges])
    p = cdf(hi, mu, sd, df) - cdf(lo, mu, sd, df)
    p = np.maximum(p, FLOOR); p = p / p.sum()
    idx = int(np.searchsorted(hi, truth, side="right"))
    if idx >= len(edges) or not (lo[idx] <= truth < hi[idx]):
        p_true, onehot = FLOOR, np.zeros_like(p)
    else:
        p_true = float(p[idx]); onehot = np.zeros_like(p); onehot[idx] = 1.0
    brier = float(((p - onehot) ** 2).sum())
    pit = float(cdf(np.array([truth]), mu, sd, df)[0])
    return -math.log(p_true), brier, pit


def fit_df(z: np.ndarray) -> float:
    best, arg = -np.inf, DF_GRID[-1]
    for df in DF_GRID:
        scale = math.sqrt((df - 2) / df)
        ll = float(stats.t.logpdf(z / scale, df).sum() - z.size * math.log(scale))
        if ll > best:
            best, arg = ll, df
    return arg


def walk(D) -> list[dict]:
    n_days = len(D.dates)
    out, model, fitted_at, seen, dfs = [], None, -e.REFIT_EVERY, None, {}
    for k in range(e.WARMUP_DAYS, n_days):
        if model is None or k - fitted_at >= e.REFIT_EVERY:
            m = D.day < k
            model, fitted_at = e.fit_all(D, m), k
            seen = np.bincount(D.st[m], minlength=len(D.stations)) > 0
            tr = np.flatnonzero(m)
            for H in e.CHECK_HOURS:
                mu, sd = e.predict(D, model, tr, H, seen)["AB_station"]
                dfs[H] = fit_df((D.y[tr] - mu) / sd)
        rows = np.flatnonzero(D.day == k)
        if not rows.size:
            continue
        for H in e.CHECK_HOURS:
            pr = e.predict(D, model, rows, H, seen)
            for j, i in enumerate(rows):
                out.append({"i": int(i), "H": H, "df": dfs[H],
                            "D0": (float(pr["mean"][0][j]), float(pr["mean"][1][j])),
                            "D1": (float(pr["AB_station"][0][j]), float(pr["AB_station"][1][j]))})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    rows = json.loads(e.DAYS.read_text())
    D = e.Data(rows)
    tmax = np.array([r["tmax"] for r in rows]); tfc = np.array([r["tmax_fc"] for r in rows])
    fst = np.array([r["station"] in F_STATIONS for r in rows])
    res = walk(D)
    sc = {a: np.empty((len(res), 3)) for a in ARMS4}
    for n, x in enumerate(res):
        i = x["i"]
        for a, (mu_y, sd), df in (("D0", x["D0"], None), ("D1", x["D1"], None), ("D2", x["D1"], x["df"])):
            sc[a][n] = score_one(float(tmax[i]), float(tfc[i] + mu_y), float(sd), df, bool(fst[i]))
    if args.smoke:
        print(json.dumps({"predictions": len(res), "finite": bool(all(np.isfinite(sc[a]).all() for a in ARMS4)),
                          "df_seen": sorted({x["df"] for x in res}), "note": "smoke only -- no scores printed"}))
        return 0
    rng = np.random.default_rng(e.SEED)
    summary = {}
    H_of = np.array([x["H"] for x in res]); day_of = np.array([D.day[x["i"]] for x in res])
    for H in e.CHECK_HOURS:
        sel = H_of == H
        days, inv = np.unique(day_of[sel], return_inverse=True)
        nd = days.size; cnt = np.bincount(inv, minlength=nd).astype(float)
        entry = {"n": int(sel.sum()), "days": int(nd), "df": sorted({x["df"] for x, s in zip(res, sel) if s}),
                 "mean": {a: {"logscore": round(float(sc[a][sel, 0].mean()), 4), "brier": round(float(sc[a][sel, 1].mean()), 4)}
                          for a in ARMS4},
                 "pit_ks": {a: round(float(stats.kstest(sc[a][sel, 2], "uniform").statistic), 4) for a in ARMS4},
                 "coverage": {a: {str(c): round(float(np.mean(np.abs(sc[a][sel, 2] - 0.5) <= c / 200)), 3) for c in (50, 80, 95)}
                              for a in ARMS4},
                 "pairs": {}}
        for name, (a, b) in PAIRS4.items():
            d = sc[a][sel, :2] - sc[b][sel, :2]
            sums = np.column_stack([np.bincount(inv, weights=d[:, j], minlength=nd) for j in range(2)])
            draws = np.empty((e.DRAWS, 2))
            for t in range(e.DRAWS):
                starts = rng.integers(0, nd, size=math.ceil(nd / e.BLOCK))
                pick = ((starts[:, None] + np.arange(e.BLOCK)[None, :]) % nd).ravel()
                draws[t] = sums[pick].sum(axis=0) / cnt[pick].sum()
            entry["pairs"][name] = {"mean": [round(float(v), 4) for v in d.mean(axis=0)],
                                    "ci95": [[round(float(np.quantile(draws[:, j], q)), 4) for q in (0.025, 0.975)] for j in range(2)]}
        summary[str(H)] = entry
    reading = {"schema": "tmax_physics_m4_reading_v1", "protocol": "6ae60173",
               "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "data": {"station_days": D.n, "stations": len(D.stations)}, "summary": summary}
    stamp = reading["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_m4_{stamp}.json").write_text(json.dumps(reading, indent=1))
    print("written", f"reading_m4_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
