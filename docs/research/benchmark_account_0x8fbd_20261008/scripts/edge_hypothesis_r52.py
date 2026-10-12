"""Round 52 (熊先生 10-12 「有没有可能是edge？」): is the cluster's bucket the
best VALUE rather than the most LIKELY bucket?

82% of its pre-formation first YES are taker fills: it hits asks somebody left
on the book, so what it buys is bounded by what was offered and at what price.
Hypothesis: it picks argmax_j (p_j - ask_j) over F-1..F+1, p its belief; F+1
when F's bucket is quoted dear and the warmer neighbour cheap.  Round 41b
tried this on 70 picks with a normal belief; here with the ensemble shape on
F, our first P0 and a normal, on every pick with an official WS book within
15 minutes of the trade (before preferred, flagged), plus the seeded ladder.
(1) the seeded ask ladder: the first WS book after listing -- which buckets
    carry asks, the ask-implied centre against F and the ladder centre L, the
    sum of asks (overround); (2) at the trade: edge_j = p_j - ask_j, argmax
    against the pick, and on the picks that left F whether the chosen bucket
    had the better edge, whether F's bucket had an ask at all, and the fill
    price against F's ask; (3) taker share and realised edge by class; (4) a
    price-aware rule out of sample (leave one event day out) against F.
"""
import collections
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("skew_mean_median_mode_r46.py").read_text()
ns = {"__name__": "r46", "__file__": str(here.with_name("skew_mean_median_mode_r46.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("days = sorted({r[\"ev\"][\"ed\"] for r in rows})")[0], "r46_head", "exec"), ns)
rows, bucket, recentre, first_p0 = ns["rows"], ns["bucket"], ns["recentre"], ns["first_p0"]
r45 = ns["ns"]
labels_of, FR_ALL = r45["labels_of"], r45["FR_ALL"]
conn = r45["ns"]["ns"]["conn"]
FR = {(r["st"], r["ed"]): r for r in FR_ALL}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ts_of(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def book_from(buckets_json, labels):
    asks, bids = [None] * len(labels), [None] * len(labels)
    for b in json.loads(buckets_json):
        lab = str(b.get("bucket"))
        if lab not in labels:
            continue
        j = labels.index(lab)
        lv = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
        asks[j] = lv[0] if lv else None
        bids[j] = float(b["bid_price"]) if b.get("bid_available") and b.get("bid_price") else None
    return asks, bids


def book_near(st, ed, ts, labels, window=900):
    before = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                          " AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (st, ed, iso(ts))).fetchone()
    if before and ts - ts_of(before[0]) <= window:
        a, b = book_from(before[1], labels)
        return a, b, "before", ts - ts_of(before[0])
    after = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                         " AND state='COMMITTED' AND observed_at>? ORDER BY observed_at LIMIT 1", (st, ed, iso(ts))).fetchone()
    if after and ts_of(after[0]) - ts <= window:
        a, b = book_from(after[1], labels)
        return a, b, "after", ts_of(after[0]) - ts
    return None, None, None, None


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


# ---------------------------------------------------------------- (1) the seeded ladder
print("(1) the seeded ask ladder: first WS book after listing, markets of the 227 picks")
seed = []
for r in rows:
    ev = r["ev"]
    labels = labels_of[(ev["st"], ev["ed"])]
    first = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                         " AND state='COMMITTED' ORDER BY observed_at LIMIT 1", (ev["st"], ev["ed"])).fetchone()
    if not first:
        continue
    t0 = ts_of(first[0])
    if t0 - ev["listed"] > 2 * 3600:
        continue
    asks, bids = book_from(first[1], labels)
    centres = [(a + b) / 2 for a, b in ev["edges"]]
    quoted = [(j, a) for j, a in enumerate(asks) if a is not None and a < 0.99]
    if len(quoted) < 3:
        continue
    tot = sum(a for _j, a in quoted)
    centre = sum(centres[j] * a for j, a in quoted) / tot
    L = (ev["edges"][1][0] + ev["edges"][-2][1]) / 2.0
    cheapest_centre_bucket = min(range(ev["n"]), key=lambda j: (asks[j] if asks[j] is not None else 9) if abs(j - bucket(ev["edges"], centre)) <= 1 else 9)
    seed.append({"r": r, "asks": asks, "bids": bids, "n_quoted": len(quoted), "sum": tot, "centre": centre, "L": L,
                 "before_trade": t0 <= ev["ts"], "dearest": max(quoted, key=lambda q: q[1])[0], "minutes": (t0 - ev["listed"]) / 60})
print(f"  {len(seed)} markets with a book within 2 h of listing ({sum(s['before_trade'] for s in seed)} before the cluster's first YES);"
      f" buckets with asks {statistics.mean(s['n_quoted'] for s in seed):.1f} of {statistics.mean(s['r']['ev']['n'] for s in seed):.1f};"
      f" sum of asks {statistics.median(s['sum'] for s in seed):.2f} (median); bids anywhere {statistics.mean(any(b is not None for b in s['bids']) for s in seed):.0%}")
d_f = [s["centre"] - s["r"]["f"] for s in seed]
d_L = [s["centre"] - s["L"] for s in seed]
print(f"  ask-weighted centre - F {statistics.mean(d_f):+.2f} C (sd {statistics.pstdev(d_f):.2f}); - L {statistics.mean(d_L):+.2f} (sd {statistics.pstdev(d_L):.2f});"
      f" dearest bucket = F's {statistics.mean(s['dearest'] == s['r']['ev']['fb'] for s in seed):.0%}, = L's {statistics.mean(s['dearest'] == bucket(s['r']['ev']['edges'], s['L']) for s in seed):.0%},"
      f" = cluster's pick {statistics.mean(s['dearest'] == s['r']['ev']['choice'] for s in seed):.0%}")
pre = [s for s in seed if s["before_trade"]]
if pre:
    print(f"  books before the first YES ({len(pre)}): F's bucket quoted {statistics.mean(s['asks'][s['r']['ev']['fb']] is not None for s in pre):.0%},"
          f" F+1 quoted {statistics.mean(s['r']['ev']['fb'] + 1 < s['r']['ev']['n'] and s['asks'][s['r']['ev']['fb'] + 1] is not None for s in pre):.0%};"
          f" ask on F {statistics.mean(s['asks'][s['r']['ev']['fb']] for s in pre if s['asks'][s['r']['ev']['fb']] is not None):.2f},"
          f" on F+1 {statistics.mean(s['asks'][s['r']['ev']['fb'] + 1] for s in pre if s['r']['ev']['fb'] + 1 < s['r']['ev']['n'] and s['asks'][s['r']['ev']['fb'] + 1] is not None):.2f},"
          f" on F-1 {statistics.mean(s['asks'][s['r']['ev']['fb'] - 1] for s in pre if s['r']['ev']['fb'] >= 1 and s['asks'][s['r']['ev']['fb'] - 1] is not None):.2f}")

# ---------------------------------------------------------------- (2) edge at the trade
print("\n(2) edge at the trade: book within 15 min (before preferred)")
BEL = {}
for r in rows:
    ev = r["ev"]
    labels = labels_of[(ev["st"], ev["ed"])]
    asks, bids, side, gap = book_near(ev["st"], ev["ed"], ev["ts"], labels)
    r["book"] = (asks, bids, side, gap)
    bel = {}
    if ev["ensp"]:
        p = np.array([ev["ensp"].get(j, 0.0) for j in range(ev["n"])])
        if p.sum() > 0:
            bel["ENS@F"] = recentre(p / p.sum(), ev["edges"], r["f"])
    p0 = first_p0(ev["st"], ev["ed"], labels)
    if p0 is not None:
        bel["P0first"] = p0
    centres = np.array([(a + b) / 2 for a, b in ev["edges"]])
    w = ev["edges"][ev["fb"]][1] - ev["edges"][ev["fb"]][0]
    z = (centres - r["f"]) / (1.1 * w)
    nb = np.exp(-0.5 * z * z)
    bel["normal 1.1 bucket"] = nb / nb.sum()
    r["bel"] = bel
withbook = [r for r in rows if r["book"][0] is not None]
print(f"  picks with a book: {len(withbook)} ({sum(r['book'][2] == 'before' for r in withbook)} before, {sum(r['book'][2] == 'after' for r in withbook)} within 15 min after)")
cls_all = collections.Counter(max(-2, min(2, r["ev"]["choice"] - r["ev"]["fb"])) for r in withbook)
print(f"  their classes: F-1 {cls_all[-1]}, F {cls_all[0]}, F+1 {cls_all[1]}, further {cls_all[-2] + cls_all[2]}")
for bname in ("ENS@F", "P0first", "normal 1.1 bucket"):
    sub = [r for r in withbook if bname in r["bel"]]
    agree_edge = agree_p = agree_ratio = 0
    n_cands = 0
    dep_better = dep_worse = dep_noask_f = dep_total = 0
    f_noedge_dep = f_noedge_n = f_edge_dep = f_edge_n = 0
    for r in sub:
        ev = r["ev"]
        asks = r["book"][0]
        p = r["bel"][bname]
        cands = [j for j in range(max(0, ev["fb"] - 1), min(ev["n"], ev["fb"] + 2))]
        quoted = [j for j in cands if asks[j] is not None]
        if len(quoted) < 2:
            continue
        n_cands += 1
        edge = {j: p[j] - asks[j] for j in quoted}
        ratio = {j: p[j] / asks[j] for j in quoted}
        agree_edge += max(edge, key=edge.get) == ev["choice"]
        agree_ratio += max(ratio, key=ratio.get) == ev["choice"]
        agree_p += max(cands, key=lambda j: p[j]) == ev["choice"]
        fb = ev["fb"]
        if ev["choice"] != fb and abs(ev["choice"] - fb) == 1:
            dep_total += 1
            if asks[fb] is None:
                dep_noask_f += 1
            elif ev["choice"] in edge:
                if edge[ev["choice"]] > edge[fb]:
                    dep_better += 1
                else:
                    dep_worse += 1
        if asks[fb] is not None:
            if p[fb] - asks[fb] <= 0:
                f_noedge_n += 1
                f_noedge_dep += ev["choice"] != fb
            else:
                f_edge_n += 1
                f_edge_dep += ev["choice"] != fb
    print(f"  belief {bname:18s} n {n_cands:3d}: argmax(p - ask) = pick {agree_edge / n_cands:.1%}, argmax(p / ask) {agree_ratio / n_cands:.1%}, argmax p {agree_p / n_cands:.1%};"
          f" one-bucket departures {dep_total}: chosen bucket had the better edge {dep_better}, worse {dep_worse}, F unquoted {dep_noask_f};"
          f" left F when F had no edge {f_noedge_dep}/{f_noedge_n}, when F had edge {f_edge_dep}/{f_edge_n}")
# fill price against F's ask on F+1 / F-1 departures
for side_, label in ((1, "F+1"), (-1, "F-1")):
    g = [r for r in withbook if r["ev"]["choice"] - r["ev"]["fb"] == side_ and r["book"][0][r["ev"]["fb"]] is not None]
    if g:
        q = [FR[(r["ev"]["st"], r["ev"]["ed"])]["first"]["price"] for r in g]
        af = [r["book"][0][r["ev"]["fb"]] for r in g]
        print(f"  {label} departures with F quoted (n {len(g)}): paid {statistics.mean(q):.2f}, F's ask {statistics.mean(af):.2f}; paid < F's ask on {statistics.mean(a < b for a, b in zip(q, af)):.0%}")

# ---------------------------------------------------------------- (3) taker share and realised edge by class
print("\n(3) by class, all 227 picks: taker share, fill price, belief on the bought bucket, realised edge p - price")
for c, label in ((-1, "F-1"), (0, "F"), (1, "F+1")):
    g = [r for r in rows if r["ev"]["choice"] - r["ev"]["fb"] == c]
    fr = [FR[(r["ev"]["st"], r["ev"]["ed"])]["first"] for r in g]
    pe = [r["bel"]["ENS@F"][r["ev"]["choice"]] for r in g if "ENS@F" in r["bel"]]
    qe = [FR[(r["ev"]["st"], r["ev"]["ed"])]["first"]["price"] for r in g if "ENS@F" in r["bel"]]
    hit = [r["ev"]["truth"] == r["ev"]["choice"] for r in g if r["ev"]["truth"] is not None]
    print(f"  {label:4s} n {len(g):3d}: taker {statistics.mean(f['taker'] for f in fr):.0%}, price {statistics.mean(f['price'] for f in fr):.3f},"
          f" ENS@F p(bought) {statistics.mean(pe):.3f}, p - price {statistics.mean(a - b for a, b in zip(pe, qe)):+.3f}, hit {statistics.mean(hit):.1%}"
          f" (realised value per share at truth: {statistics.mean(hit) - statistics.mean(f['price'] for f in fr):+.3f})")

# ---------------------------------------------------------------- (4) a price-aware rule out of sample
print("\n(4) out of sample, leave one event day out, picks with a book and both neighbours quoted")
sub = [r for r in withbook if "ENS@F" in r["bel"] and all(r["book"][0][j] is not None for j in range(max(0, r["ev"]["fb"] - 1), min(r["ev"]["n"], r["ev"]["fb"] + 2)))]
days = sorted({r["ev"]["ed"] for r in sub})
for bname in ("ENS@F", "normal 1.1 bucket"):
    picks = []
    for r in sub:
        tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
        # choose the mixing weight w in p - w*ask on the training days
        best_w = max((0.0, 0.25, 0.5, 0.75, 1.0), key=lambda w: (sum(max(range(max(0, x["ev"]["fb"] - 1), min(x["ev"]["n"], x["ev"]["fb"] + 2)),
                                                                            key=lambda j: x["bel"][bname][j] - w * x["book"][0][j]) == x["ev"]["choice"] for x in tr), -w))
        picks.append(max(range(max(0, r["ev"]["fb"] - 1), min(r["ev"]["n"], r["ev"]["fb"] + 2)), key=lambda j: r["bel"][bname][j] - best_w * r["book"][0][j]))
    same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    print(f"  belief {bname:18s} n {len(sub)}: argmax(p - w ask), w by LODO = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)}")

# (5) the asker's belief: on books that precede the cluster's first YES, the dearest central ask vs F and the pick.
print("\n(5) books before the cluster's first YES: whose bucket does the dearest ask sit on?")
pre = [s for s in seed if s["before_trade"]]
for label, g in (("before the trade", pre), ("after the trade (the cluster may have lifted it)", [s for s in seed if not s["before_trade"]])):
    if not g:
        continue
    dear_pick = statistics.mean(s["dearest"] == s["r"]["ev"]["choice"] for s in g)
    dear_f = statistics.mean(s["dearest"] == s["r"]["ev"]["fb"] for s in g)
    dear_f_when_differ = [s["dearest"] == s["r"]["ev"]["choice"] for s in g if s["r"]["ev"]["choice"] != s["r"]["ev"]["fb"]]
    tr = [s for s in g if s["r"]["ev"]["truth"] is not None]
    print(f"  {label:48s} n {len(g):3d}: dearest ask = cluster's pick {dear_pick:.0%}, = F {dear_f:.0%};"
          f" where pick != F: dearest = pick {statistics.mean(dear_f_when_differ) if dear_f_when_differ else 0:.0%} (n {len(dear_f_when_differ)});"
          f" dearest hits truth {statistics.mean(s['dearest'] == s['r']['ev']['truth'] for s in tr):.0%} vs F {statistics.mean(s['r']['ev']['fb'] == s['r']['ev']['truth'] for s in tr):.0%} vs cluster {statistics.mean(s['r']['ev']['choice'] == s['r']['ev']['truth'] for s in tr):.0%}")
    spread = [sorted((a for a in s["asks"] if a is not None), reverse=True) for s in g]
    print(f"      dearest ask {statistics.mean(x[0] for x in spread):.3f}, second {statistics.mean(x[1] for x in spread):.3f}, third {statistics.mean(x[2] for x in spread):.3f};"
          f" minutes after listing {statistics.median(s['minutes'] for s in g):.0f} (median)")
