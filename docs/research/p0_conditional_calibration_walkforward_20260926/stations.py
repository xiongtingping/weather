"""Per-station model-vs-market proper scores (user: 「哪些站有稳定的概率优势？」).

Stable = both scores ahead, 3-day-block CI clear of zero, ahead in both halves,
BH q <= 0.10 across the 45 stations; plus the split-half test (pick on one half,
read the other).  P0 from today's-arithmetic replay (the gap study's cache),
P0-M/P1 as served; truth = the label table's standing revision.  Read-only, prints.
"""
import pickle, sys, sqlite3
from collections import Counter, defaultdict
from datetime import date
import numpy as np
sys.path.insert(0, "/Users/xiong/weather"); sys.path.insert(0, "/Users/xiong/weather/docs/research/model_market_gap_decomposition_20260926")
import decompose as D

CACHE = "/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/decompose_cache.pkl"
served, _, today, _ = pickle.load(open(CACHE, "rb"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=ON"); labels = D.load_labels(conn); conn.close()

def rescore(items):
    out = []
    for d in items:
        lab = labels.get((d["station"], d["date"]))
        if lab is None or lab[0] not in d["keys"]: continue
        h = d["keys"].index(lab[0])
        lp, bp = D.score(d["p"], d["keys"], h); lm, bm = D.score(d["m"], d["keys"], h, floor=False)
        out.append({"station": d["station"], "date": d["date"], "dls": lp - lm, "dbr": bp - bm})
    return out

rng = np.random.default_rng(20260926)
def station_table(rows, label):
    by = defaultdict(list)
    for r in rows: by[r["station"]].append(r)
    alldays = sorted({r["date"] for r in rows}); mid = alldays[len(alldays) // 2]
    res = []
    for s, items in by.items():
        # per-day mean (station-day equal weight within station)
        per = defaultdict(list)
        for r in items: per[r["date"]].append(r)
        days = sorted(per)
        dls = np.array([np.mean([r["dls"] for r in per[d]]) for d in days]); dbr = np.array([np.mean([r["dbr"] for r in per[d]]) for d in days])
        first = date.fromisoformat(alldays[0])
        blk = np.array([(date.fromisoformat(d) - first).days // 3 for d in days]); ub = sorted(set(blk))
        bl = np.array([dls[blk == b].sum() for b in ub]); bb = np.array([dbr[blk == b].sum() for b in ub]); bn = np.array([(blk == b).sum() for b in ub])
        draws_l, draws_b = [], []
        for _ in range(2000):
            pk = rng.integers(0, len(ub), len(ub))
            draws_l.append(bl[pk].sum() / bn[pk].sum()); draws_b.append(bb[pk].sum() / bn[pk].sum())
        draws_l = np.array(draws_l); draws_b = np.array(draws_b)
        h1 = dls[np.array(days) < mid].mean() if (np.array(days) < mid).any() else np.nan
        h2 = dls[np.array(days) >= mid].mean() if (np.array(days) >= mid).any() else np.nan
        res.append({"station": s, "n": len(days), "dls": dls.mean(), "dbr": dbr.mean(),
                    "ls_ci": np.percentile(draws_l, [2.5, 97.5]), "br_ci": np.percentile(draws_b, [2.5, 97.5]),
                    "p_ls": float((draws_l >= 0).mean()), "h1": h1, "h2": h2, "win_days": float((dls < 0).mean())})
    # BH on one-sided p (model better)
    res.sort(key=lambda x: x["p_ls"]); m = len(res)
    for i, x in enumerate(res): x["bh"] = min(1.0, x["p_ls"] * m / (i + 1))
    for i in range(m - 2, -1, -1): res[i]["bh"] = min(res[i]["bh"], res[i + 1]["bh"])
    res.sort(key=lambda x: x["dls"])
    print(f"\n==== {label}: {len(res)} stations, days {alldays[0]}..{alldays[-1]} (halves split at {mid})")
    print("station  n   ΔLS    [3d-block CI]      ΔBrier  [CI]            half1   half2  win-days  BH-q  stable?")
    for x in res:
        stable = x["dls"] < 0 and x["dbr"] < 0 and x["ls_ci"][1] < 0 and x["br_ci"][1] < 0 and x["h1"] < 0 and x["h2"] < 0 and x["bh"] <= 0.10
        if x["dls"] < 0.02 or stable:
            print(f"{x['station']:6s} {x['n']:3d} {x['dls']:+.3f} [{x['ls_ci'][0]:+.3f},{x['ls_ci'][1]:+.3f}]  {x['dbr']:+.3f} [{x['br_ci'][0]:+.3f},{x['br_ci'][1]:+.3f}]  {x['h1']:+.3f} {x['h2']:+.3f}  {x['win_days']:.0%}  {x['bh']:.2f}  {'YES' if stable else ''}")
    neg = [x for x in res if x["dls"] < 0]
    print(f"stations with ΔLS<0: {len(neg)}; with LS CI upper<0: {sum(1 for x in res if x['ls_ci'][1] < 0)}; worse with CI lower>0: {sum(1 for x in res if x['ls_ci'][0] > 0)}")
    # split-half selection: pick on half 1 (ΔLS<0 & ΔBr<0 in half1), evaluate half 2 pooled
    h1sel = [x["station"] for x in res if x["h1"] < 0]
    h2sel = [x["station"] for x in res if x["h2"] < 0]
    def pooled(stations, half):
        vals = [r["dls"] for r in rows if r["station"] in stations and ((r["date"] < mid) if half == 1 else (r["date"] >= mid))]
        per = defaultdict(list)
        for r in rows:
            if r["station"] in stations and ((r["date"] < mid) if half == 1 else (r["date"] >= mid)): per[(r["station"], r["date"])].append(r["dls"])
        return np.mean([np.mean(v) for v in per.values()]) if per else float("nan"), len(per)
    a = pooled(set(h1sel), 2); b = pooled(set(h2sel), 1)
    print(f"split-half: stations better in half1 = {len(h1sel)} {sorted(h1sel)} -> their half2 ΔLS {a[0]:+.3f} ({a[1]} sd); better in half2 = {len(h2sel)} -> their half1 ΔLS {b[0]:+.3f}")
    hs = np.array([[x["h1"], x["h2"]] for x in res if not np.isnan(x["h1"]) and not np.isnan(x["h2"])])
    from scipy.stats import spearmanr
    print("rank correlation of station ΔLS between halves: %.2f" % spearmanr(hs[:, 0], hs[:, 1]).correlation)
    return res

p0 = rescore([d for d in today if d["bin"] in (2, 3, 4)])
station_table(p0, "P0 today's arithmetic replay, strict 24-60h")
p1 = rescore([d for d in served if d["stage"] == "P1" and d["bin"] == 0 and d["date"] >= "2026-09-01"])
station_table(p1, "P1 as served, 0-12h, 09-01..09-24")
p1b = rescore([d for d in served if d["stage"] == "P1" and d["bin"] == 1 and d["date"] >= "2026-09-01"])
station_table(p1b, "P1 as served, 12-24h, 09-01..09-24")
p0m = rescore([d for d in served if d["stage"] == "P0-M" and d["bin"] in (2, 3) and d["date"] >= "2026-09-01"])
station_table(p0m, "P0-M as served, 24-48h, 09-01..09-24")
