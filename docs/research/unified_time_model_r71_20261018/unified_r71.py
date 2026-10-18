"""Round 71 (PROTOCOL.md, committed 21319076 before scoring): one time-indexed
model against the served P0 -> P0-M -> P1 chain, same instants, strict
walk-forward.

    python3 -I docs/research/unified_time_model_r71_20261018/unified_r71.py > .../unified_r71.out
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as br  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ROWS = ROOT / "docs" / "research" / "d0_obs_model_20261015" / "rows_d0.jsonl.gz"
BLOCKS = {"07–10": range(7, 11), "11–14": range(11, 15), "15–18": range(15, 19)}
MIN_TRAIN_DAYS = 10
REFIT_EVERY = 2
L2 = 1.0
FLOOR = 1e-3
DRAWS = 2000
SEED = 20261018
FEATURES = ("b_minus_h", "p0_mean_minus_h", "p0_sd", "t_minus_h", "t_minus_fc", "fc_rise", "to_peak", "momentum",
            "warm_gap", "dew_dep", "wind", "cloud_low", "precip")


def block_of(hour):
    return next(name for name, hrs in BLOCKS.items() if hour in hrs)


def p0_series(conn, station, day):
    out = []
    for at, probs, iv in conn.execute(
            "SELECT decision_at_utc, probabilities_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
            " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P0'"
            " ORDER BY decision_at_utc", (station, day)):
        if not iv:
            continue
        p, iv = json.loads(probs), json.loads(iv)
        mids, ws = [], []
        for b, v in p.items():
            lo, hi = iv.get(b, [None, None])
            if lo is None and hi is None:
                continue
            width = (hi - lo) if lo is not None and hi is not None else 1.0
            mid = (lo + hi) / 2 if lo is not None and hi is not None else (hi - width / 2 if lo is None else lo + width / 2)
            mids.append(mid)
            ws.append(float(v))
        tot = sum(ws)
        if tot <= 0:
            continue
        m = sum(x * w for x, w in zip(mids, ws)) / tot
        sd = math.sqrt(max(sum(w * (x - m) ** 2 for x, w in zip(mids, ws)) / tot, 1e-6))
        out.append((str(at), m, sd))
    return out


def build():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    labels = {(str(k[0]), str(k[1])): str(v["bucket"]) for k, v in knowable_labels(conn).items()}
    raw = [json.loads(line) for line in gzip.open(ROWS, "rt")]
    p0_cache, served_cache = {}, {}
    rows = []
    for r in raw:
        key = (r["station"], r["day"])
        settled = labels.get(key)
        if settled is None or settled not in r["intervals"] or r.get("high") is None or r.get("base") is None:
            continue
        if key not in p0_cache:
            p0_cache[key] = p0_series(conn, *key)
        series = p0_cache[key]
        i = bisect.bisect_right([s[0] for s in series], r["at"]) - 1
        if i < 0:
            continue
        _at, p0m, p0sd = series[i]
        stage, served, order, rung = br._served_belief_vector(conn, r["station"], r["day"], r["at"], served_cache)
        if served is None or set(served) != set(r["intervals"]):
            continue
        H = float(r["high"])
        feats = {
            "b_minus_h": r["base"] - H, "p0_mean_minus_h": p0m - H, "p0_sd": p0sd,
            "t_minus_h": (r["t"] - H) if r.get("t") is not None else None, "t_minus_fc": r.get("t_minus_fc"),
            "fc_rise": r.get("fc_rise"),
            "to_peak": None if r.get("fc_peak_h") is None else max(-6.0, min(12.0, r["fc_peak_h"] - r["hour"])),
            "momentum": r.get("momentum"),
            "warm_gap": None if r.get("warmed") is None or r.get("fc_warmed") is None else r["warmed"] - r["fc_warmed"],
            "dew_dep": r.get("dew_dep"), "wind": r.get("wind"), "cloud_low": r.get("cloud_low"),
            "precip": 1.0 if r.get("precip_now") else 0.0,
        }
        keys = sorted(r["intervals"], key=lambda b: -1e9 if r["intervals"][b][0] is None else r["intervals"][b][0])
        rows.append({"station": r["station"], "day": r["day"], "hour": r["hour"], "block": block_of(r["hour"]),
                     "H": H, "keys": keys, "iv": r["intervals"], "settled": settled, "feats": feats,
                     "served": served, "stage": stage, "rung": rung,
                     "market": (r.get("market") or {}).get("q"),
                     "unit": "F" if "°F" in settled else "C"})
    return rows


def design(rows, stats=None):
    raw = np.array([[r["feats"][f] if r["feats"][f] is not None else np.nan for f in FEATURES] for r in rows], float)
    miss = np.isnan(raw).astype(float)
    filled = np.where(np.isnan(raw), 0.0, raw)
    if stats is None:
        mu = np.nanmean(np.where(miss > 0, np.nan, raw), axis=0)
        mu = np.where(np.isnan(mu), 0.0, mu)
        sd = np.nanstd(np.where(miss > 0, np.nan, raw), axis=0)
        sd = np.where(~(sd > 0), 1.0, sd)
        keep = miss.sum(axis=0) > 0
        stats = (mu, sd, keep)
    mu, sd, keep = stats
    z = np.where(miss > 0, 0.0, (filled - mu) / sd)
    X = np.hstack([np.ones((len(rows), 1)), z, miss[:, keep]])
    return X, stats


def edges(rows):
    """Per row, the truth bucket's (lo, hi) and every bucket's edges padded."""
    lo_t = np.array([(-np.inf if r["iv"][r["settled"]][0] is None else r["iv"][r["settled"]][0]) for r in rows])
    hi_t = np.array([(np.inf if r["iv"][r["settled"]][1] is None else r["iv"][r["settled"]][1]) for r in rows])
    K = np.array([len(r["keys"]) for r in rows], float)
    H = np.array([r["H"] for r in rows])
    return lo_t, hi_t, K, H


def cdf(v, H, mu, sig):
    out = ndtr((v - mu) / sig)
    return np.where(v < H, 0.0, out)


def nll(theta, X, lo_t, hi_t, K, H):
    d = X.shape[1]
    a, g = theta[:d], theta[d:]
    mu = H + X @ a
    sig = np.exp(np.clip(X @ g, -3.0, 3.0))
    p = cdf(hi_t, H, mu, sig) - cdf(lo_t, H, mu, sig)
    p = np.clip(p, 0.0, 1.0)
    pf = (np.maximum(p, FLOOR)) / (1.0 + K * FLOOR)
    return float(-np.mean(np.log(pf)) + L2 / len(H) * (np.sum(a[1:] ** 2) + np.sum(g[1:] ** 2)))


def fit(train):
    X, stats = design(train)
    lo_t, hi_t, K, H = edges(train)
    d = X.shape[1]
    theta0 = np.zeros(2 * d)
    theta0[0] = 0.5
    theta0[d] = math.log(1.0)
    res = minimize(nll, theta0, args=(X, lo_t, hi_t, K, H), method="L-BFGS-B", options={"maxiter": 400})
    return res.x, stats


def predict(r, theta, stats):
    X, _ = design([r], stats)
    d = X.shape[1]
    mu = r["H"] + float(X[0] @ theta[:d])
    sig = math.exp(max(-3.0, min(3.0, float(X[0] @ theta[d:]))))
    def F(v):
        if v is None:
            return 1.0
        return 0.0 if v < r["H"] else float(ndtr((v - mu) / sig))
    out = {}
    for b in r["keys"]:
        lo, hi = r["iv"][b]
        out[b] = max(0.0, F(hi) - (0.0 if lo is None else F(lo)))
    s = sum(out.values()) or 1.0
    return {b: v / s for b, v in out.items()}


def floor(v):
    f = {b: max(FLOOR, p) for b, p in v.items()}
    s = sum(f.values())
    return {b: p / s for b, p in f.items()}


def scores(vec, r):
    keys, settled = r["keys"], r["settled"]
    v = floor(vec)
    ls = -math.log(v[settled])
    brier = sum((v[b] - (1.0 if b == settled else 0.0)) ** 2 for b in keys)
    rung = r["rung"] or 1.0
    crps = br._ladder_rps(v, keys, settled) * rung
    return ls, crps, brier


def boot(groups, stat):
    days = sorted(groups)
    rng = random.Random(SEED)
    vals = sorted(v for v in (stat([groups[days[rng.randrange(len(days))]] for _ in days]) for _ in range(DRAWS)) if v is not None)
    return stat([groups[d] for d in days]), (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def main():
    rows = build()
    days = sorted({r["day"] for r in rows})
    print(f"rows {len(rows)} station-days {len({(r['station'], r['day']) for r in rows})} days {days[0]}..{days[-1]} ({len(days)})")
    print("served stage at instant:", {s: sum(r['stage'] == s for r in rows) for s in ('P0', 'P0-M', 'P1')})
    out = []
    models, last_fit = {}, None
    for i, day in enumerate(days):
        cut = (date.fromisoformat(day) - timedelta(days=2)).isoformat()
        train = [r for r in rows if r["day"] <= cut]
        if len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        if last_fit is None or i - last_fit >= REFIT_EVERY:
            models = {name: fit([r for r in train if r["block"] == name]) for name in BLOCKS}
            last_fit = i
        for r in [r for r in rows if r["day"] == day]:
            theta, stats = models[r["block"]]
            u = predict(r, theta, stats)
            rec = {"day": day, "block": r["block"], "unit": r["unit"], "stage": r["stage"]}
            rec["u"] = scores(u, r)
            rec["s"] = scores(r["served"], r)
            rec["m"] = scores(r["market"], r) if r["market"] and set(r["market"]) == set(r["keys"]) else None
            if r["block"] == "15–18":
                hi_ord = next(j for j, b in enumerate(r["keys"])
                              if (r["iv"][b][0] is None or r["H"] >= r["iv"][b][0])
                              and (r["iv"][b][1] is None or r["H"] < r["iv"][b][1]))
                above = r["keys"][hi_ord + 1:]
                rec["eve"] = (sum(floor(u)[b] for b in above), sum(floor(r["served"])[b] for b in above),
                              1.0 if r["settled"] in above else 0.0)
            out.append(rec)
    sdays = sorted({x["day"] for x in out})
    print(f"scored {len(out)} instants over {len(sdays)} days {sdays[0]}..{sdays[-1]}")

    def by(sub):
        g = defaultdict(list)
        for x in sub:
            g[x["day"]].append(x)
        return g

    def diff(a, b, k):
        def f(gs):
            xs = [x[a][k] - x[b][k] for g in gs for x in g if x[a] is not None and x[b] is not None]
            return sum(xs) / len(xs) if xs else None
        return f

    def line(title, sub, a, b):
        g = by(sub)
        if len(g) < 3:
            return f"{title}: 不足"
        parts = []
        for k, name in ((0, "ΔLogScore"), (1, "ΔCRPS°C"), (2, "ΔBrier")):
            m, ci = boot(g, diff(a, b, k))
            parts.append(f"{name} {m:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]")
        n = sum(1 for x in sub if x[a] is not None and x[b] is not None)
        return f"{title} (n={n}, 日={len(g)}): " + " · ".join(parts)

    def means(sub, a):
        xs = [x[a][0] for x in sub if x[a] is not None]
        return sum(xs) / len(xs) if xs else float("nan")

    print("\n== 主检验：统一 − 服务链")
    print(line("全部 07–18 时", out, "u", "s"))
    print(f"   LogScore 统一 {means(out, 'u'):.4f} / 服务链 {means(out, 's'):.4f}")
    print("\n== 分时段（描述）")
    for name in BLOCKS:
        sub = [x for x in out if x["block"] == name]
        print(line(f"统一−服务链 {name}", sub, "u", "s"))
    print("\n== 对市场（描述；只用有完整梯子的时刻）")
    with_m = [x for x in out if x["m"] is not None]
    print(line("统一 − 市场", with_m, "u", "m"))
    print(line("服务链 − 市场", with_m, "s", "m"))
    for name in BLOCKS:
        sub = [x for x in with_m if x["block"] == name]
        print("   " + line(f"统一−市场 {name}", sub, "u", "m"))
        print("   " + line(f"服务链−市场 {name}", sub, "s", "m"))
    print("\n== 按当时服务阶段（描述）")
    for st in ("P0", "P0-M", "P1"):
        print(line(f"统一−服务链｜服务={st}", [x for x in out if x["stage"] == st], "u", "s"))
    print("\n== C/F（描述）")
    for u in ("C", "F"):
        print(line(f"统一−服务链 {u}", [x for x in out if x["unit"] == u], "u", "s"))
    eve = [x["eve"] for x in out if "eve" in x]
    print(f"\n== 15–18 时「高于已测最高」：统一说 {np.mean([e[0] for e in eve]):.4f} · 服务链说 {np.mean([e[1] for e in eve]):.4f} · 实际 {np.mean([e[2] for e in eve]):.4f} (n={len(eve)})")


if __name__ == "__main__":
    main()
