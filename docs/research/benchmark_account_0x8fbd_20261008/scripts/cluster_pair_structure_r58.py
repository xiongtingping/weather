"""Round 58 (熊先生 10-12 design ask: YES+NO pair lane, pair_cost <= 1 / 0.95, post-only,
mode/mean buckets, single-side risk only on the mode YES): how does the cluster itself pair?

Per station-day x bucket over the cluster's BUY fills (``cluster_fills_r45.json``, 8 wallets),
on the first-rows station-days (ladder, listing time and truth known):
  YES / NO shares and average prices; matched pairs = min(YES, NO); pair_cost = avg YES +
  avg NO over the matched shares; unmatched side and share of the bucket's shares; maker share
  per side; minutes from listing; settlement P&L split into the locked part (matched x (1 -
  pair_cost)) and the directional part (the unmatched shares at settlement).
Buckets classified by offset from F's bucket at listing + 5 min (the production F, round 57
cache; the market has no mode at listing) and from the truth.  Also the unmatched ratio over
time (15 min / 1 h / 3 h / 12 h / final) and the pair cost of the pairs completed within each.
"""
import collections
import gzip
import json
import statistics
import sys
from pathlib import Path

import numpy as np

B = Path(sys.argv[1])
FILLS = json.load(open(B / "cluster_fills_r45.json"))
FR = {(r["st"], r["ed"]): r for r in json.load(gzip.open(B / "first_rows.json.gz", "rt"))}
CACHE = json.load(open(B / "f_v3_backtest_r57.json"))


def edges_of(r):
    out = [[*r["bounds"][lab]] for lab in r["labels"]]
    w = float(r.get("width") or 1.0)
    if out[0][0] is None:
        out[0][0] = out[0][1] - w
    if out[-1][1] is None:
        out[-1][1] = out[-1][0] + w
    return out


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


groups = collections.defaultdict(list)
for f in FILLS:
    key = (f["station"], f["event_date"])
    if key in FR and f["bucket"] in FR[key]["labels"]:
        groups[(key, f["bucket"])].append(f)

rows = []
for ((st, ed), lab), fl in groups.items():
    r = FR[(st, ed)]
    labels, edges = r["labels"], edges_of(r)
    j = labels.index(lab)
    p = CACHE.get(str((int(r["listed"]) + 300) // 600 * 600), {}).get("station_days", {}).get(f"{st}|{ed}")
    fb = bucket(edges, p["f"]) if p else None
    vb = bucket(edges, p["v3"]) if p and p.get("v3") is not None else None
    yb = labels.index(r["truth"]) if r.get("truth") in labels else None
    listed = float(r["listed"])
    fl.sort(key=lambda f: f["ts"])
    yes = [f for f in fl if f["outcome"] == "Yes"]
    no = [f for f in fl if f["outcome"] == "No"]
    ys, ns = sum(f["size"] for f in yes), sum(f["size"] for f in no)
    yc, nc = sum(f["size"] * f["price"] for f in yes), sum(f["size"] * f["price"] for f in no)
    row = {"st": st, "ed": ed, "j": j, "fb": fb, "vb": vb, "yb": yb, "n": len(labels), "ys": ys, "ns": ns, "yc": yc, "nc": nc,
           "y_maker": sum(f["size"] for f in yes if not f["taker"]), "n_maker": sum(f["size"] for f in no if not f["taker"]),
           "first_min": (fl[0]["ts"] - listed) / 60}
    # trajectory: cumulative shares by horizon
    for h, sec in (("15m", 900), ("1h", 3600), ("3h", 10800), ("12h", 43200)):
        yh = [f for f in yes if f["ts"] - listed <= sec]
        nh = [f for f in no if f["ts"] - listed <= sec]
        row[f"ys_{h}"], row[f"ns_{h}"] = sum(f["size"] for f in yh), sum(f["size"] for f in nh)
        row[f"yc_{h}"], row[f"nc_{h}"] = sum(f["size"] * f["price"] for f in yh), sum(f["size"] * f["price"] for f in nh)
    rows.append(row)

print(f"{len(rows)} station-day x bucket groups over {len({(x['st'], x['ed']) for x in rows})} station-days")
both = [x for x in rows if x["ys"] > 0 and x["ns"] > 0]
tot_cost = sum(x["yc"] + x["nc"] for x in rows)
print(f"groups with both sides {len(both)} ({len(both) / len(rows):.0%}), holding {sum(x['yc'] + x['nc'] for x in both) / tot_cost:.0%} of the cluster's buy USDC")


def pair_stats(xs, ys_k="ys", ns_k="ns", yc_k="yc", nc_k="nc"):
    out = []
    for x in xs:
        if x[ys_k] <= 0 or x[ns_k] <= 0:
            continue
        m = min(x[ys_k], x[ns_k])
        pc = x[yc_k] / x[ys_k] + x[nc_k] / x[ns_k]
        un = abs(x[ys_k] - x[ns_k]) / (x[ys_k] + x[ns_k])
        out.append((m, pc, un, "YES" if x[ys_k] > x[ns_k] else "NO"))
    return out


ps = pair_stats(both)
w = np.array([m for m, _pc, _u, _s in ps])
pc = np.array([p for _m, p, _u, _s in ps])
un = np.array([u for _m, _p, u, _s in ps])
print(f"pair cost (share-weighted over matched shares): mean {np.average(pc, weights=w):.3f}, median {np.median(pc):.3f};"
      f" share of matched shares at pair cost <= 0.95: {w[pc <= 0.95].sum() / w.sum():.0%}, <= 1.00: {w[pc <= 1.0].sum() / w.sum():.0%}")
print(f"unmatched ratio |YES - NO| / (YES + NO) in two-sided groups: median {np.median(un):.2f}, mean {un.mean():.2f};"
      f" excess side YES {sum(1 for *_x, s in ps if s == 'YES')} / NO {sum(1 for *_x, s in ps if s == 'NO')}")
print(f"maker share of shares: YES {sum(x['y_maker'] for x in rows) / sum(x['ys'] for x in rows):.0%}, NO {sum(x['n_maker'] for x in rows) / sum(x['ns'] for x in rows):.0%}")

print("\nby bucket offset from F's bucket at listing (proxy for the mode before the market forms):")
print(f"  {'offset':>6s} {'groups':>6s} {'USDC':>7s} {'YES sh':>8s} {'NO sh':>8s} {'two-sided':>9s} {'pair cost':>9s} {'excess YES':>10s} {'locked P&L':>10s} {'dir. P&L':>9s} {'P&L/USDC':>8s}")
for off in (-3, -2, -1, 0, 1, 2, 3):
    g = [x for x in rows if x["fb"] is not None and max(-3, min(3, x["j"] - x["fb"])) == off and x["yb"] is not None]
    if not g:
        continue
    gp = pair_stats(g)
    wm = sum(m for m, *_ in gp)
    pcw = sum(m * p for m, p, *_ in gp) / wm if wm else float("nan")
    locked = sum(m * (1 - p) for m, p, *_ in gp)
    dir_pnl = 0.0
    for x in g:
        m = min(x["ys"], x["ns"])
        ey, en = x["ys"] - m, x["ns"] - m
        ay = x["yc"] / x["ys"] if x["ys"] else 0
        an = x["nc"] / x["ns"] if x["ns"] else 0
        hit = x["j"] == x["yb"]
        dir_pnl += ey * ((1.0 if hit else 0.0) - ay) + en * ((0.0 if hit else 1.0) - an)
    usd = sum(x["yc"] + x["nc"] for x in g)
    print(f"  {off:+6d} {len(g):6d} {usd:7.0f} {sum(x['ys'] for x in g):8.0f} {sum(x['ns'] for x in g):8.0f} {len(gp) / len(g):9.0%} {pcw:9.3f}"
          f" {sum(1 for *_x, s in gp if s == 'YES') / max(1, len(gp)):10.0%} {locked:10.0f} {dir_pnl:9.0f} {(locked + dir_pnl) / usd:+8.1%}")

print("\nby bucket offset from the truth (where the money ends up):")
for off in (-2, -1, 0, 1, 2):
    g = [x for x in rows if x["yb"] is not None and max(-2, min(2, x["j"] - x["yb"])) == off]
    print(f"  truth{off:+d}: groups {len(g)}, YES sh {sum(x['ys'] for x in g):.0f}, NO sh {sum(x['ns'] for x in g):.0f}, YES share of USDC {sum(x['yc'] for x in g) / max(1e-9, sum(x['yc'] + x['nc'] for x in g)):.0%}")

print("\ntrajectory (two-sided groups at the horizon): unmatched ratio and pair cost of the pairs formed so far")
for h in ("15m", "1h", "3h", "12h"):
    gp = pair_stats(rows, f"ys_{h}", f"ns_{h}", f"yc_{h}", f"nc_{h}")
    started = sum(1 for x in rows if x[f"ys_{h}"] + x[f"ns_{h}"] > 0)
    if gp:
        wm = np.array([m for m, *_ in gp])
        print(f"  {h:>4s}: groups started {started}, two-sided {len(gp)} ({len(gp) / max(1, started):.0%}); pair cost {np.average([p for _m, p, *_ in gp], weights=wm):.3f};"
              f" unmatched median {np.median([u for _m, _p, u, _s in gp]):.2f}")
print(f"  final: two-sided {len(both)} of {len(rows)}; pair cost {np.average(pc, weights=w):.3f}; unmatched median {np.median(un):.2f}")
print(f"\nfirst fill minutes after listing (median by group): {statistics.median(x['first_min'] for x in rows):.0f}")
