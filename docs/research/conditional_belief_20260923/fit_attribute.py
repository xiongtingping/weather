"""Walk-forward conditional logit over the buckets, with per-family attribution.

Reads ``dataset.json`` (build_dataset.py) and writes ``results.json``.  The
design, the families, the prior, the fold rule and the trading rule are the
ones PROTOCOL.md fixed before any of this was scored.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
CLIP = 1e-4
RAW_FLOOR = 1e-6
MIN_TRAIN_DAYS = 8
BOOT = 2000
BLOCK_DAYS = 3
HURDLE = 1.05
# PROTOCOL.md amendment 3: the post-hoc robust variant bounds three things.
ROBUST = "--robust" in sys.argv
D_CAP = 3.0
Z_CAP = 3.0
Z2_CAP = 9.0
SUFFIX = "_robust" if "--robust" in sys.argv else ""
EPOCH = {"P0": "2026-09-14T05:52:13Z", "P0-M": "2026-09-18T19:10:09Z", "P1": "2026-09-18T19:10:09.000001Z"}
FAMILIES = ("T", "S", "R", "A", "O", "W", "K")
FAMILY_NAMES = {
    "T": "时间", "S": "模型自身", "R": "区域模型", "A": "TAF",
    "O": "实况", "W": "云雨风/辐射", "K": "盘口",
}


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def logit(x):
    x = np.clip(x, CLIP, 1.0 - CLIP)
    return np.log(x) - np.log1p(-x)


class Stage:
    """Padded bucket arrays for one stage, and the raw conditions per decision."""

    def __init__(self, rows):
        self.rows = rows
        n = len(rows)
        k = max(len(r["keys"]) for r in rows)
        self.n, self.k = n, k
        self.mask = np.zeros((n, k), bool)
        m = np.full((n, k), 0.5)
        p = np.full((n, k), 0.5)
        c = np.zeros((n, k))
        hs = np.zeros((n, k))
        upper = np.full((n, k), np.nan)
        self.hit = np.array([r["hit"] for r in rows])
        for i, r in enumerate(rows):
            kk = len(r["keys"])
            self.mask[i, :kk] = True
            m[i, :kk] = r["m"]
            pv = np.asarray(r["p"], float)
            p[i, :kk] = pv / pv.sum() if pv.sum() > 0 else 1.0 / kk
            c[i, :kk] = r["centers"]
            hs[i, :kk] = r["half_spread"]
            upper[i, :kk] = [np.nan if u is None else u for u in r["upper"]]
        self.m, self.p, self.c, self.hs, self.upper = m, p, c, hs, upper
        self.Lm = np.where(self.mask, logit(m), 0.0)
        self.D = np.where(self.mask, logit(p) - logit(m), 0.0)
        if ROBUST:
            self.D = np.clip(self.D, -D_CAP, D_CAP)
        self.w = np.array([r["weight"] for r in rows])
        self.day = np.array([r["date"] for r in rows])
        self.sd = np.array([f'{r["station"]}|{r["date"]}' for r in rows])
        self.at = np.array([instant(r["at"]) for r in rows])
        self.known = np.array([instant(r["label_known_at"]) for r in rows])
        self.f = {name: np.array([np.nan if r["features"].get(name) is None else float(r["features"][name]) for r in rows])
                  for name in {key for r in rows for key in r["features"]}}
        pm = np.where(self.mask, p, 0.0)
        self.mu = (pm * c).sum(1)
        self.sigma = np.sqrt(np.maximum(1e-6, (pm * (c - self.mu[:, None]) ** 2).sum(1)))
        self.z2 = np.where(self.mask, ((c - self.mu[:, None]) / self.sigma[:, None]) ** 2, 0.0)
        if ROBUST:
            self.z2 = np.minimum(self.z2, Z2_CAP)
        self.dmu = np.where(self.mask, c - self.mu[:, None], 0.0)

    def get(self, name):
        return self.f.get(name, np.full(self.n, np.nan))


def family_terms(S: Stage, stage: str):
    """Each family's terms: (name, kind, payload).

    kind "g" -> a decision-level condition (raw, NaN = missing) crossed with
    the listed bucket arrays; kind "x" -> a bucket-level array on its own.
    """

    hours = S.get("hours") / 24.0
    fam = {}
    fam["T"] = [("h", "g", hours, ("D", "Lm")), ("h2", "g", hours ** 2, ("D", "Lm"))]
    fam["S"] = [
        ("sigma_p", "g", S.get("sigma_p"), ("D", "z2")),
        ("peak_p", "g", S.get("peak_p"), ("D",)),
        ("system_span", "g", S.get("system_span"), ("D", "z2")),
        ("z2", "x", S.z2),
    ]
    treg = S.get("regional_tmax")
    has = ~np.isnan(treg)
    dist = np.where(has[:, None] & S.mask, S.c - np.nan_to_num(treg)[:, None], 0.0)
    fam["R"] = [("reg_d2", "x", dist ** 2), ("reg_abs", "x", np.abs(dist))]
    if stage == "P1":
        fam["R"] += [
            ("headroom", "g", S.get("obs_regional_headroom_c"), ("D",)),
            ("live_gap", "g", S.get("obs_regional_live_gap_c"), ("D",)),
        ]
    tx = S.get("taf_tx")
    has = ~np.isnan(tx)
    dist = np.where(has[:, None] & S.mask, S.c - np.nan_to_num(tx)[:, None], 0.0)
    fam["A"] = [("tx_d2", "x", dist ** 2), ("tx_abs", "x", np.abs(dist)),
                ("taf_age", "g", S.get("taf_age_h"), ("D",))]
    if stage == "P1":
        high = S.get("obs_running_high_c")
        high = np.where(np.isnan(high), S.get("running_high"), high)
        has = ~np.isnan(high)
        above = np.where(has[:, None] & S.mask, np.maximum(0.0, S.c - np.nan_to_num(high)[:, None]), 0.0)
        below = np.where(has[:, None] & S.mask & (S.upper <= np.nan_to_num(high)[:, None]), 1.0, 0.0)
        rem = np.nan_to_num(S.get("obs_remaining_hours"))
        fam["O"] = [
            ("below_high", "x", below), ("above_high", "x", above),
            ("above_high_x_rem", "x", above * rem[:, None]),
            ("remaining", "g", S.get("obs_remaining_hours"), ("D",)),
            ("warming", "g", S.get("obs_warming_so_far_c"), ("D",)),
            ("drawdown", "g", S.get("obs_drawdown_from_high_c"), ("D",)),
            ("momentum", "g", S.get("obs_momentum_c_per_hour"), ("D",)),
            ("since_high", "g", S.get("obs_hours_since_high_refresh"), ("D",)),
        ]
    fam["W"] = [
        ("kt", "g", S.get("kt"), ("D", "dmu")),
        ("cloud", "g", S.get("cloud"), ("D", "dmu")),
        ("precip", "g", S.get("precip"), ("D", "dmu")),
        ("wind", "g", S.get("wind"), ("D", "dmu")),
    ]
    if stage == "P1":
        fam["W"] += [
            ("obs_cloud", "g", S.get("obs_cloud_cover_fraction"), ("D", "dmu")),
            ("obs_precip", "g", S.get("obs_precipitation_present"), ("D", "dmu")),
            ("obs_dewpoint_dep", "g", S.get("obs_dewpoint_depression_c"), ("D", "dmu")),
            ("obs_wind", "g", S.get("obs_wind_speed_mps"), ("D", "dmu")),
        ]
    fam["K"] = [
        ("top_gap", "g", S.get("top_gap_m"), ("D",)),
        ("entropy", "g", S.get("entropy_m"), ("D",)),
        ("half_spread", "x", np.where(S.mask, S.hs, 0.0)),
    ]
    return fam


def columns(S: Stage, terms, train):
    """Standardised (N, K) columns for one fold; conditions scaled on the train rows."""

    bucket = {"D": S.D, "Lm": S.Lm, "z2": S.z2, "dmu": S.dmu}
    w = S.w[train]
    out = []
    for term in terms:
        name, kind = term[0], term[1]
        if kind == "x":
            out.append((name, term[2]))
            continue
        g = term[2]
        missing = np.isnan(g)
        ok = train & ~missing
        if ok.sum() < 20:
            continue
        mean = np.average(g[ok], weights=S.w[ok])
        sd = math.sqrt(np.average((g[ok] - mean) ** 2, weights=S.w[ok]))
        if sd < 1e-9:
            continue
        z = np.where(missing, 0.0, (g - mean) / sd)
        if ROBUST:
            z = np.clip(z, -Z_CAP, Z_CAP)
        for arr in term[3]:
            out.append((f"{name}*{arr}", z[:, None] * bucket[arr]))
        if missing[train].any() and (~missing[train]).any():
            out.append((f"{name}_missing*D", missing[:, None] * S.D))
    scaled = []
    for name, arr in out:
        arr = np.where(S.mask, arr, 0.0)
        vals = arr[train][S.mask[train]]
        sd = float(vals.std())
        if not math.isfinite(sd) or sd < 1e-9:
            continue
        scaled.append((name, arr / sd))
    del w
    return scaled


def fit(X, hit, mask, w, n_base, theta0=None):
    """Weighted softmax over the buckets; N(0,1) prior on the non-base terms."""

    n, k, q = X.shape
    idx = np.arange(n)
    penal = np.ones(q)
    penal[:n_base] = 0.0

    def fg(theta):
        s = X @ theta
        s = np.where(mask, s, -np.inf)
        s = s - s.max(1, keepdims=True)
        e = np.exp(s)
        z = e.sum(1)
        prob = e / z[:, None]
        nll = -(w * (s[idx, hit] - np.log(z))).sum() + 0.5 * (penal * theta ** 2).sum()
        resid = prob.copy()
        resid[idx, hit] -= 1.0
        grad = np.einsum("n,nk,nkq->q", w, resid, X) + penal * theta
        return nll, grad

    if theta0 is None or len(theta0) != q:
        start = np.zeros(q)
        start[0] = 1.0
    else:
        start = theta0
    res = minimize(fg, start, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x


def log_prob(X, theta, hit, mask):
    s = np.where(mask, X @ theta, -np.inf)
    s = s - s.max(1, keepdims=True)
    lse = np.log(np.exp(s).sum(1))
    prob = np.exp(s - lse[:, None])
    return s[np.arange(len(hit)), hit] - lse, prob


def model_specs(fam):
    present = [f for f in FAMILIES if f in fam]
    specs = {"M0": ("Lm",), "M1": ("Lm", "D")}
    for f in present:
        specs[f"+{f}"] = ("Lm", "D", f)
    specs["full"] = ("Lm", "D", *present)
    for f in present:
        specs[f"full-{f}"] = ("Lm", "D", *[g for g in present if g != f])
    return specs, present


def run_stage(stage, rows):
    S = Stage(rows)
    fam = family_terms(S, stage)
    specs, present = model_specs(fam)
    days = sorted(set(S.day))
    lp = {name: np.full(S.n, np.nan) for name in specs}
    probs = {name: np.full((S.n, S.k), np.nan) for name in specs}
    buys = {name: np.zeros(S.n) for name in (*specs, "raw_p")}
    pnl = {name: np.zeros(S.n) for name in (*specs, "raw_p")}
    cost = {name: np.zeros(S.n) for name in (*specs, "raw_p")}
    ask = np.clip(S.m + S.hs, 0.01, 0.99)
    onehot = np.zeros((S.n, S.k))
    onehot[np.arange(S.n), S.hit] = 1.0
    warm = {}
    folds = []
    for day in days:
        test = S.day == day
        first = S.at[test].min()
        train = S.known < first
        if len(set(S.day[train])) < MIN_TRAIN_DAYS:
            continue
        folds.append(day)
        fam_cols = {f: columns(S, fam[f], train) for f in present}
        base = [("Lm", S.Lm), ("D", S.D)]
        for name, spec in specs.items():
            cols = [c for c in base if c[0] in spec]
            for f in spec:
                if f in fam_cols:
                    cols += fam_cols[f]
            X = np.stack([c[1] for c in cols], axis=2)
            n_base = sum(1 for c in cols if c[0] in ("Lm", "D"))
            theta = fit(X[train], S.hit[train], S.mask[train], S.w[train], n_base, warm.get(name))
            warm[name] = theta
            lpt, prob = log_prob(X[test], theta, S.hit[test], S.mask[test])
            lp[name][test] = lpt
            probs[name][test] = prob
            buy = (prob >= HURDLE * ask[test]) & S.mask[test]
            buys[name][test] = buy.sum(1)
            pnl[name][test] = (buy * (onehot[test] - ask[test])).sum(1)
            cost[name][test] = (buy * ask[test]).sum(1)
        buy = (S.p[test] >= HURDLE * ask[test]) & S.mask[test]
        buys["raw_p"][test] = buy.sum(1)
        pnl["raw_p"][test] = (buy * (onehot[test] - ask[test])).sum(1)
        cost["raw_p"][test] = (buy * ask[test]).sum(1)
        print(stage, day, int(test.sum()), f"{time.time() - T0:.0f}s", flush=True)
    raw_m = np.log(np.maximum(S.m[np.arange(S.n), S.hit], RAW_FLOOR) / np.where(S.mask, np.maximum(S.m, RAW_FLOOR), 0).sum(1))
    raw_p = np.log(np.maximum(S.p[np.arange(S.n), S.hit], RAW_FLOOR) / np.where(S.mask, np.maximum(S.p, RAW_FLOOR), 0).sum(1))
    lp["raw_market"] = raw_m
    lp["raw_model"] = raw_p
    scored = ~np.isnan(lp["M0"])
    # The final all-data fit: coefficients to read, never scored.
    everything = np.ones(S.n, bool)
    fam_cols = {f: columns(S, fam[f], everything) for f in present}
    cols = [("Lm", S.Lm), ("D", S.D)] + [c for f in present for c in fam_cols[f]]
    X = np.stack([c[1] for c in cols], axis=2)
    theta = fit(X, S.hit, S.mask, S.w, 2)
    owner = {}
    for f in present:
        for c in fam_cols[f]:
            owner[c[0]] = f
    coef = [{"term": c[0], "family": owner.get(c[0], "base"), "coef": round(float(t), 4)} for c, t in zip(cols, theta)]
    S.probs = probs
    return S, scored, lp, buys, pnl, cost, folds, coef, present


def station_day_table(S, scored, values):
    """Weighted station-day means of per-decision values, over scored rows."""

    groups = defaultdict(list)
    for i in np.flatnonzero(scored):
        groups[S.sd[i]].append(i)
    keys = sorted(groups)
    day_of = np.array([k.split("|")[1] for k in keys])
    out = {}
    for name, arr in values.items():
        out[name] = np.array([
            np.average(arr[groups[k]], weights=S.w[groups[k]]) for k in keys
        ])
    return keys, day_of, out


def block_bootstrap(day_of, diff, rng):
    # Circular blocks: a moving block that stops at the last day under-draws
    # the final days, and with few days its interval can miss the mean.
    days = sorted(set(day_of))
    by_day = {d: np.flatnonzero(day_of == d) for d in days}
    n_blocks = math.ceil(len(days) / BLOCK_DAYS)
    stats = np.empty(BOOT)
    for b in range(BOOT):
        picked = []
        for s in rng.integers(0, len(days), n_blocks):
            for j in range(BLOCK_DAYS):
                picked.append(by_day[days[(s + j) % len(days)]])
        idx = np.concatenate(picked)
        stats[b] = diff[idx].mean()
    return [round(float(np.percentile(stats, 2.5)), 5), round(float(np.percentile(stats, 97.5)), 5)]


def summarise(stage, S, scored, lp, buys, pnl, cost, folds, coef, present, rng, subset=None):
    mask = scored if subset is None else scored & subset
    if mask.sum() == 0:
        return None
    keys, day_of, sdv = station_day_table(S, mask, lp)
    _, _, sd_pnl = station_day_table(S, mask, pnl)
    _, _, sd_cost = station_day_table(S, mask, cost)
    _, _, sd_buys = station_day_table(S, mask, buys)
    out = {
        "decisions": int(mask.sum()), "station_days": len(keys), "event_days": len(set(day_of)),
        "first_test_day": min(day_of), "last_test_day": max(day_of),
        "log_score": {k: round(float(v.mean()), 5) for k, v in sdv.items()},
    }

    def diff(a, b):
        d = sdv[a] - sdv[b]
        return {"mean": round(float(d.mean()), 5), "ci95": block_bootstrap(day_of, d, rng)}

    out["gains"] = {
        "M0_vs_market_raw": diff("M0", "raw_market"),
        "M1_vs_M0": diff("M1", "M0"),
        "full_vs_M1": diff("full", "M1"),
        "full_vs_M0": diff("full", "M0"),
        "full_vs_market_raw": diff("full", "raw_market"),
        "model_raw_vs_market_raw": diff("raw_model", "raw_market"),
    }
    out["attribution"] = {}
    for f in present:
        add = diff(f"+{f}", "M1")
        drop = diff("full", f"full-{f}")
        out["attribution"][f] = {
            "name": FAMILY_NAMES[f], "add_one": add, "drop_one": drop,
            "interaction": round(drop["mean"] - add["mean"], 5),
        }
    out["trading"] = {}
    for name in ("raw_p", "M0", "M1", "full", *[f"+{f}" for f in present]):
        p_sd, c_sd = sd_pnl[name], sd_cost[name]
        out["trading"][name] = {
            "buys_per_station_day": round(float(sd_buys[name].mean()), 3),
            "pnl_per_station_day": round(float(p_sd.mean()), 5),
            "pnl_ci95": block_bootstrap(day_of, p_sd, rng),
            "roi": None if c_sd.sum() <= 0 else round(float(p_sd.sum() / c_sd.sum()), 4),
        }
    return out


def main():
    global T0
    T0 = time.time()
    data = json.loads((HERE / "dataset.json").read_text())
    rng = np.random.default_rng(20260923)
    result = {"protocol": "PROTOCOL.md", "variant": "robust" if ROBUST else "preregistered", "stages": {}}
    for stage in ("P0", "P0-M", "P1"):
        rows = [r for r in data if r["stage"] == stage]
        S, scored, lp, buys, pnl, cost, folds, coef, present = run_stage(stage, rows)
        epoch = S.at >= instant(EPOCH[stage])
        np.savez_compressed(
            HERE / f"rows_{stage}{SUFFIX}.npz", sd=S.sd, day=S.day, w=S.w, scored=scored, epoch=epoch,
            **{f"lp_{k}": v for k, v in lp.items()}, **{f"pnl_{k}": v for k, v in pnl.items()},
            **{f"cost_{k}": v for k, v in cost.items()}, **{f"buys_{k}": v for k, v in buys.items()},
            **{f"prob_{k}": v for k, v in S.probs.items()}, mask=S.mask, hit=S.hit, m=S.m, p=S.p,
        )
        result["stages"][stage] = {
            "families": present, "folds": folds,
            "all": summarise(stage, S, scored, lp, buys, pnl, cost, folds, coef, present, rng),
            "evidence_epoch": summarise(stage, S, scored, lp, buys, pnl, cost, folds, coef, present, rng, subset=epoch),
            "coefficients_all_data": coef,
        }
        (HERE / f"results{SUFFIX}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
