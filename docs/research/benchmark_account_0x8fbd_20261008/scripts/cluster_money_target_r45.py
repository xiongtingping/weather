"""Round 45 (熊先生 10-12 「继续研究打穿簇/对比的选桶！」).

Rounds 40-44 aimed at the bucket of the cluster's FIRST YES on a station-day.
The cluster keeps buying after it: the first fill is whichever order filled
first, not necessarily where its money goes.  Here the target is the money.

(45a) For each of the 227 pre-formation first YES (rounds 40-44, F = the
recalibrated production F read as of the trade), the cluster's YES buy USDC
per bucket (all eight wallets, ``cluster_fills_r45.json``) over 15 min / 1 h /
3 h after its first YES and over the whole pre-event period: its MAIN bucket
(most USDC), its money-weighted centre (USDC x bucket centre), the share of
its money on F's bucket.  Does F match the main bucket more often than the
first pick?  How far is the money centre from F (mean, sd, MAE)?  Truth hits
of main bucket vs first pick vs F.
(45b) Per wallet: each wallet's own first YES on a station-day within 2 h of
listing, and its own main bucket in that window, against F.
(45c) The money centre as a continuous target: centre - F regressed on
conditions known at the trade (each member's deviation from the corrected
median, spread, skew, station correction, the recalibration, |lat|, F unit,
F's position in its bucket); day-block bootstrap intervals; leave-one-event-
day-out: does bucket(F + predicted offset) match the main bucket / the first
pick more often than F?
"""
import collections
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, bucket, prod_f, FR_ALL = ns["events"], ns["bucket"], ns["prod_f"], ns["FR_ALL"]
LAT = {s: abs(lat) for s, lat, _lon, _z in ns["ns"]["ROSTER"]}
FILLS = json.load(open(B / "cluster_fills_r45.json"))
NEWEST = max(f["ts"] for f in FILLS)
labels_of = {(r["st"], r["ed"]): r["labels"] for r in FR_ALL}
by_sd = collections.defaultdict(list)
for f in FILLS:
    by_sd[(f["station"], f["event_date"])].append(f)
SYSTEMS = ["ECMWF_AIFS_SINGLE_POINT", "ECMWF_IFS_HRES_NATIVE_POINT", "NOAA_GFS_025_NATIVE_POINT",
           "NOAA_AIGFS_025_NATIVE_POINT", "ECCC_GDPS_15KM_NATIVE_POINT", "DWD_ICON_GLOBAL_13KM_NATIVE_POINT"]


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


WINDOWS = {"15 min": 900, "1 h": 3600, "3 h": 3 * 3600, "pre-event": None}
unmapped = collections.Counter()
rows = []
for ev in events:
    labels = labels_of.get((ev["st"], ev["ed"]))
    if not labels:
        continue
    e = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    centres = [(lo + hi) / 2 for lo, hi in ev["edges"]]
    fills = [f for f in by_sd[(ev["st"], ev["ed"])] if f["outcome"] == "Yes" and f["ts"] >= ev["ts"]]
    win = {}
    for name, span in WINDOWS.items():
        usd = collections.Counter()
        for f in fills:
            if span is not None and f["ts"] > ev["ts"] + span:
                continue
            if span is None and f["ts"] > ev["ts"] + 2 * 86400:
                continue
            if f["bucket"] not in labels:
                unmapped[f["bucket"]] += 1
                continue
            usd[labels.index(f["bucket"])] += f["price"] * f["size"]
        tot = sum(usd.values())
        if tot <= 0:
            continue
        main = max(usd, key=lambda j: (usd[j], -abs(j - ev["fb"])))
        win[name] = {"main": main, "centre": sum(centres[j] * u for j, u in usd.items()) / tot, "usd": usd, "tot": tot,
                     "share_f": usd.get(ev["fb"], 0.0) / tot, "share_first": usd.get(ev["choice"], 0.0) / tot}
    corrected = {s: v["tmax_c"] + v.get("bias_c", 0.0) for s, v in e["systems"].items()}
    med = statistics.median(corrected.values())
    lo, hi = ev["edges"][ev["fb"]]
    x = [corrected[s] - med if s in corrected else 0.0 for s in SYSTEMS]
    x += [float(np.std(list(corrected.values()))), statistics.mean(corrected.values()) - med, e["station_corr_c"],
          e["recal_c"] or 0.0, LAT.get(ev["st"], 30.0) / 30.0, 1.0 if ev["unit"] == "F" else 0.0, (ev["f"] - lo) / (hi - lo) - 0.5]
    rows.append({"ev": ev, "win": win, "x": np.array(x), "fbase": e["f_base_c"], "f": ev["f"], "centres": centres,
                 "bb": bucket(ev["edges"], e["f_base_c"])})
XN = [f"dev {s.split('_')[1]}" for s in SYSTEMS] + ["spread", "skew", "station corr", "recal", "|lat|/30", "F unit", "position"]
print(f"{len(rows)} of {len(events)} picks with a ladder; fills to {NEWEST}; unmapped bucket labels {sum(unmapped.values())}")

print("\n(45a) the cluster's money after its first YES")
for name in WINDOWS:
    sub = [r for r in rows if name in r["win"]]
    if not sub:
        continue
    ev = [r["ev"] for r in sub]
    main = [r["win"][name]["main"] for r in sub]
    f_main = [r["ev"]["fb"] == m for r, m in zip(sub, main)]
    first_main = [r["ev"]["choice"] == m for r, m in zip(sub, main)]
    fb_main = [r["bb"] == m for r, m in zip(sub, main)]
    d = np.array([r["win"][name]["centre"] - r["f"] for r in sub])
    db = np.array([r["win"][name]["centre"] - r["fbase"] for r in sub])
    tr = [(r, m) for r, m in zip(sub, main) if r["ev"]["truth"] is not None]
    print(f"  {name:9s} n {len(sub):3d}: main bucket = first pick {statistics.mean(first_main):.1%};"
          f" F = main {statistics.mean(f_main):.1%} (F = first pick {statistics.mean([r['ev']['fb'] == r['ev']['choice'] for r in sub]):.1%});"
          f" F_base = main {statistics.mean(fb_main):.1%}")
    print(f"            money on F's bucket {statistics.mean(r['win'][name]['share_f'] for r in sub):.1%}, on the first pick's {statistics.mean(r['win'][name]['share_first'] for r in sub):.1%};"
          f"  centre - F {d.mean():+.2f} C (sd {d.std():.2f}, MAE {np.abs(d).mean():.2f}); centre - F_base {db.mean():+.2f} (sd {db.std():.2f}, MAE {np.abs(db).mean():.2f})")
    print(f"            truth hits (n {len(tr)}): main {statistics.mean(r['ev']['truth'] == m for r, m in tr):.1%},"
          f" first pick {statistics.mean(r['ev']['truth'] == r['ev']['choice'] for r, _m in tr):.1%}, F {statistics.mean(r['ev']['truth'] == r['ev']['fb'] for r, _m in tr):.1%}")
    off = collections.Counter(max(-2, min(2, m - r["ev"]["fb"])) for r, m in zip(sub, main))
    print(f"            main - F: <=-2 {off[-2]}, -1 {off[-1]}, 0 {off[0]}, +1 {off[1]}, >=+2 {off[2]}")

print("\n(45b) per wallet: its own first YES within 2 h of listing, and its own main bucket in that window, against F")
evmap = {(r["ev"]["st"], r["ev"]["ed"]): r for r in rows}
per = collections.defaultdict(lambda: {"n": 0, "first": 0, "main": 0, "nm": 0, "usd": 0.0})
for (st, ed), r in evmap.items():
    labels = labels_of[(st, ed)]
    listed = r["ev"]["listed"]
    for w in {f["w"] for f in by_sd[(st, ed)]}:
        mine = sorted((f for f in by_sd[(st, ed)] if f["w"] == w and f["outcome"] == "Yes" and listed <= f["ts"] <= listed + 7200
                       and f["bucket"] in labels), key=lambda f: f["ts"])
        if not mine:
            continue
        # F as of this wallet's own first fill
        e = prod_f(mine[0]["ts"])["station_days"].get(f"{st}|{ed}")
        if not e or e.get("state") != "READY":
            continue
        fb = bucket(r["ev"]["edges"], e["f_c"])
        usd = collections.Counter()
        for f in mine:
            usd[labels.index(f["bucket"])] += f["price"] * f["size"]
        p = per[w[:10]]
        p["n"] += 1
        p["first"] += labels.index(mine[0]["bucket"]) == fb
        p["main"] += max(usd, key=usd.get) == fb
        p["usd"] += sum(usd.values())
for w, p in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
    print(f"  {w}: station-days {p['n']:3d}, USDC {p['usd']:8.0f}; its first YES = F {p['first'] / p['n']:.1%}, its main bucket = F {p['main'] / p['n']:.1%}")

print("\n(45c) the money centre (1 h window) as the target: centre - F on conditions known at the trade")
sub = [r for r in rows if "1 h" in r["win"]]
X = np.array([r["x"] for r in sub])
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])
mu, sd = X.mean(0), X.std(0)
sd = np.where(sd > 1e-9, sd, 1.0)
A = np.c_[(X - mu) / sd, np.ones(len(X))]


def ridge(A_, y_, lam=2.0):
    P = lam * np.eye(A_.shape[1])
    P[-1, -1] = 0.0
    return np.linalg.solve(A_.T @ A_ + P, A_.T @ y_)


beta = ridge(A, y)
days = sorted({r["ev"]["ed"] for r in sub})
rng = np.random.default_rng(11)
boots = []
for _ in range(300):
    pick = rng.choice(days, size=len(days), replace=True)
    idx = [i for d in pick for i, r in enumerate(sub) if r["ev"]["ed"] == d]
    boots.append(ridge(A[idx], y[idx]))
boots = np.array(boots)
resid = y - A @ beta
print(f"  n {len(sub)}, mean offset {y.mean():+.2f} C, sd {y.std():.2f}; in-sample R^2 {1 - resid.var() / y.var():.3f}; per +1 sd (C):")
for i, name in enumerate(XN):
    lo_, hi_ = np.percentile(boots[:, i], [2.5, 97.5])
    print(f"    {name:14s} {beta[i]:+.3f} [{lo_:+.3f}, {hi_:+.3f}]{' *' if lo_ > 0 or hi_ < 0 else ''}")
pred = {}
for d in days:
    tr = [i for i, r in enumerate(sub) if r["ev"]["ed"] != d]
    b = ridge(A[tr], y[tr])
    for i, r in enumerate(sub):
        if r["ev"]["ed"] == d:
            pred[i] = float(A[i] @ b)
oos = np.array([pred[i] for i in range(len(sub))])
print(f"  leave-one-day-out: centre MAE {np.abs(y - oos).mean():.3f} vs F {np.abs(y).mean():.3f} vs F + mean offset"
      f" {np.mean([abs(y[i] - np.mean(np.delete(y, [j for j, r in enumerate(sub) if r['ev']['ed'] == sub[i]['ev']['ed']]))) for i in range(len(sub))]):.3f} C")
mid = days[len(days) // 2]
for target in ("main", "first"):
    got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == (r["win"]["1 h"]["main"] if target == "main" else r["ev"]["choice"]) for i, r in enumerate(sub)]
    base = [r["ev"]["fb"] == (r["win"]["1 h"]["main"] if target == "main" else r["ev"]["choice"]) for r in sub]
    h1 = [g for g, r in zip(got, sub) if r["ev"]["ed"] < mid]
    h2 = [g for g, r in zip(got, sub) if r["ev"]["ed"] >= mid]
    print(f"  bucket(F + predicted offset) = {target:5s} {statistics.mean(got):.1%} (halves {statistics.mean(h1):.1%} / {statistics.mean(h2):.1%}) vs F {statistics.mean(base):.1%}   {paired(got, base)}")
