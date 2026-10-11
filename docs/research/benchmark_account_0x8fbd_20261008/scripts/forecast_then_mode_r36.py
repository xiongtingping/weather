"""Round 36 scorer (protocol 5384787a): does the market mode form around the
forecast bucket F after listing -- F first, mode second?
"""
import collections
import gzip
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

B = Path(sys.argv[1])
rows = json.load(open(B / "r34_rows.json"))
P = json.load(open(B / "r36_paths.json"))
FR = {(r["st"], r["ed"]): r for r in json.load(gzip.open(B / "first_rows.json.gz", "rt"))}
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def price_at(key, t):
    v = P.get(key)
    if not v or "t" not in v:
        return None
    best = None
    for tt, p in zip(v["t"], v["p"]):
        if tt <= t:
            best = (tt, p)
        else:
            break
    return best[1] if best and t - best[0] <= 3600 else None


def market(r, t):
    vals = [price_at(f"{r['key']}|{lab}", t) for lab in r["labels"]]
    if any(v is None for v in vals):
        return None
    s = sum(vals)
    if not 0.8 <= s <= 1.25 or max(vals) < 0.25:
        return None
    return max(range(len(vals)), key=lambda i: vals[i])


def served_mode(r, t):
    for stage in ("P1", "P0-M", "P0"):
        row = conn.execute("SELECT history_id FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND stage=?"
                           " AND state='COMMITTED' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1", (r["st"], r["ed"], stage, iso(t))).fetchone()
        if row:
            vec = dict(conn.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (row[0],)).fetchall())
            if vec:
                b = max(vec, key=lambda k: vec[k])
                return r["labels"].index(b) if b in r["labels"] else None
            return None
    return None


data = []
for r in rows:
    F = r["labels"].index(r["legs"]["F"])
    y = r["labels"].index(r["truth"])
    y_, m_, d_ = (int(x) for x in r["ed"].split("-"))
    ev06 = int(datetime(y_, m_, d_, 6, tzinfo=ZoneInfo(tz[r["st"]])).timestamp())
    lt = int(r["listed"])
    grid = list(range(lt + 900, ev06 + 1, 900))
    modes = [(t, market(r, t)) for t in grid]
    formed = [(t, m) for t, m in modes if m is not None]
    fr = FR.get((r["st"], r["ed"]))
    cl = None
    if fr and fr["first"]["side"] == "Yes":
        v = list(fr["now"].values())
        pre = not (all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4)
        if pre and fr["first"]["bucket"] in r["labels"]:
            cl = r["labels"].index(fr["first"]["bucket"])
    data.append({"r": r, "F": F, "y": y, "lt": lt, "ev06": ev06, "modes": modes, "formed": formed, "cl": cl,
                 "mid": (len(r["labels"]) - 1) // 2})
print(f"station-days {len(data)}; ever formed before event-day 06:00 {sum(1 for d in data if d['formed'])}")

# (1) formation time
ft = sorted((d["formed"][0][0] - d["lt"]) / 3600 for d in data if d["formed"])
q = lambda a, p: a[int(p * (len(a) - 1))]
print(f"\n== (1) hours from listing to the first formed ladder: p10 {q(ft, .1):.2f}  p25 {q(ft, .25):.2f}  median {q(ft, .5):.2f}  p75 {q(ft, .75):.2f}  p90 {q(ft, .9):.2f}")

# (2) mode = F by checkpoint
print("\n== (2) among formed ladders: mode = F / F-1 / F+1 / our served mode / ladder middle (share)")
for name, off in (("listing +1h", 3600), ("+2h", 7200), ("+4h", 14400), ("+8h", 28800), ("+24h", 86400), ("event-day 06:00", None)):
    cnt = collections.Counter()
    for d in data:
        t = d["ev06"] if off is None else d["lt"] + off
        if t > d["ev06"]:
            continue
        m = market(d["r"], t)
        if m is None:
            cnt["unformed"] += 1
            continue
        cnt["n"] += 1
        cnt["F"] += m == d["F"]
        cnt["F-1"] += m == d["F"] - 1
        cnt["F+1"] += m == d["F"] + 1
        cnt["mid"] += m == d["mid"]
        sm = served_mode(d["r"], t)
        if sm is not None:
            cnt["n_s"] += 1
            cnt["served"] += m == sm
    n = max(1, cnt["n"])
    print(f"   {name:16s} formed {cnt['n']:3d} (unformed {cnt['unformed']:3d})  mode=F {cnt['F'] / n:.2f}  F-1 {cnt['F-1'] / n:.2f}  F+1 {cnt['F+1'] / n:.2f}"
          f"  ladder middle {cnt['mid'] / n:.2f}  our served mode {cnt['served'] / max(1, cnt['n_s']):.2f} (n={cnt['n_s']})")

# (3) mismatch at formation, and the direction of mode changes
print("\n== (3) when the first formed mode is not F")
mis = [d for d in data if d["formed"] and d["formed"][0][1] != d["F"]]
end = collections.Counter()
for d in mis:
    m = market(d["r"], d["ev06"])
    if m is None:
        continue
    end["n"] += 1
    end["to F"] += m == d["F"]
    end["stayed"] += m == d["formed"][0][1]
    end["elsewhere"] += m not in (d["F"], d["formed"][0][1])
print(f"   station-days {len(mis)}; at event-day 06:00 (formed {end['n']}): mode moved to F {end['to F'] / max(1, end['n']):.2f}, stayed {end['stayed'] / max(1, end['n']):.2f}, elsewhere {end['elsewhere'] / max(1, end['n']):.2f}")
by_day = collections.defaultdict(lambda: [0, 0])
for d in data:
    prev = None
    for t, m in d["formed"]:
        if prev is not None and m != prev:
            a, b = abs(prev - d["F"]), abs(m - d["F"])
            if a != b:
                by_day[d["r"]["ed"]][0] += b < a
                by_day[d["r"]["ed"]][1] += 1
        prev = m
keys = list(by_day)
S = np.array([by_day[k] for k in keys], dtype=float)
rng = np.random.default_rng(36)
idx = rng.integers(0, len(keys), size=(2000, len(keys)))
T = S[idx].sum(axis=1)
bs = np.sort(T[:, 0] / T[:, 1])
print(f"   mode changes (not equidistant from F): {int(S[:, 1].sum())}; toward F {S[:, 0].sum() / S[:, 1].sum():.3f} [{bs[50]:.3f}, {bs[1949]:.3f}]")

# (4) did the cluster's own buying make the mode?
print("\n== (4) station-days with a pre-formation cluster first YES on bucket X: first formed mode = X / = F")
for grp, f in (("X = F", lambda d: d["cl"] == d["F"]), ("X != F", lambda d: d["cl"] is not None and d["cl"] != d["F"]), ("no early cluster YES", lambda d: d["cl"] is None)):
    sel = [d for d in data if f(d) and d["formed"]]
    if not sel:
        continue
    mx = sum(1 for d in sel if d["cl"] is not None and d["formed"][0][1] == d["cl"]) / len(sel)
    mf = sum(1 for d in sel if d["formed"][0][1] == d["F"]) / len(sel)
    e06 = [(d, market(d["r"], d["ev06"])) for d in sel]
    e06 = [(d, m) for d, m in e06 if m is not None]
    mx6 = sum(1 for d, m in e06 if d["cl"] is not None and m == d["cl"]) / max(1, len(e06))
    mf6 = sum(1 for d, m in e06 if m == d["F"]) / max(1, len(e06))
    print(f"   {grp:22s} n={len(sel):3d}  first formed mode = X {mx:.2f}, = F {mf:.2f}   |   event-day 06:00 mode = X {mx6:.2f}, = F {mf6:.2f}")

# (5) accuracy
print("\n== (5) hit the truth: F (fixed at listing) vs the market mode at each checkpoint (formed ladders)")
for name, off in (("+2h", 7200), ("+8h", 28800), ("+24h", 86400), ("event-day 06:00", None)):
    a = b = n = 0
    for d in data:
        t = d["ev06"] if off is None else d["lt"] + off
        if t > d["ev06"]:
            continue
        m = market(d["r"], t)
        if m is None:
            continue
        n += 1
        a += d["F"] == d["y"]
        b += m == d["y"]
    print(f"   {name:16s} n={n:3d}  F {a / n:.3f}   market mode {b / n:.3f}")
