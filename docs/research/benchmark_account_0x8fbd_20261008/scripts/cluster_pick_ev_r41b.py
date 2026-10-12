"""Round 41b: does the cluster pick by value against the asks it could see?
Among F-1..F+1, argmax of (p_j - ask_j) and of p_j / ask_j, p from a normal
belief around F (sigma in bucket widths, chosen per fold), asks from the
official WS book at its trade time (lowest level with >= 5 shares); the
dearer-quote neighbour only when F leans that way; and the D+1-market shift
only when F leans that way.  All picks and the early ones (< 15 min after
listing).  Leave-one-event-day-out.
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

here = Path(__file__)
src = here.with_name("cluster_pick_dims_r41.py").read_text()
ns = {"__name__": "r41", "__file__": str(here.with_name("cluster_pick_dims_r41.py"))}
exec(compile(src.split('print(f"{len(events)} picks over')[0], "r41_head", "exec"), ns)
events, bucket, conn, days, mid_day = ns["events"], ns["bucket"], ns["conn"], ns["days"], ns["mid_day"]
next_door_offset = None
src_full = here.with_name("cluster_pick_dims_r41.py").read_text()
exec(compile(src_full[src_full.index("market_cache = {}"):src_full.index("def ch_market")], "r41_market", "exec"), ns)
exec(compile(src_full[src_full.index("def label_centre"):src_full.index("market_cache = {}")], "r41_label", "exec"), ns)
next_door_offset = ns["next_door_offset"]
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")  # noqa: E731
book = {}


def asks_at(ev):
    if id(ev) not in book:
        row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                           " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (ev["st"], ev["ed"], iso(ev["ts"]))).fetchone()
        out = {}
        if row:
            labels = [None] * ev["n"]
            r = ns["FR"][(ev["st"], ev["ed"])]
            for b in json.loads(row[0]):
                lab = str(b.get("bucket"))
                if lab in r["labels"]:
                    lv = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
                    if lv and 0 < lv[0] < 0.99:
                        out[r["labels"].index(lab)] = lv[0]
        book[id(ev)] = out
    return book[id(ev)]


def cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def probs(ev, sigma):
    w = ev["width"]
    return {j: cdf(((hi - ev["f"]) / w) / sigma) - cdf(((lo - ev["f"]) / w) / sigma) for j, (lo, hi) in enumerate(ev["edges"])}


def ch_ev(ev, sigma, mode):
    a = asks_at(ev)
    p = probs(ev, sigma)
    cands = [j for j in (ev["fb"] - 1, ev["fb"], ev["fb"] + 1) if j in a]
    if not cands:
        return ev["fb"]
    if mode == "edge":
        return max(cands, key=lambda j: (p[j] - a[j], j == ev["fb"]))
    return max(cands, key=lambda j: (p[j] / a[j], j == ev["fb"]))


def lean(ev):
    lo, hi = ev["edges"][ev["fb"]]
    return 1 if ev["f"] >= (lo + hi) / 2 else -1


def ch_quote_lean(ev, m):
    side = lean(ev)
    nb = ev["fb"] + side
    q = ev["now"]
    if nb in q and (ev["fb"] not in q or q[nb] > q[ev["fb"]] + m):
        return nb
    return ev["fb"]


def ch_market_lean(ev, a):
    o = next_door_offset(ev)
    if o is None:
        return ev["fb"]
    moved = bucket(ev["edges"], ev["f"] + a * o)
    return moved if (moved - ev["fb"]) == lean(ev) else ev["fb"]


def report(name, chooser, grid, subset=None):
    pool = events if subset is None else [e for e in events if subset(e)]
    picks = {}
    for d in days:
        train = [e for e in pool if e["ed"] != d]
        best = max(grid, key=lambda p: sum(chooser(e, p) == e["choice"] for e in train)) if grid else None
        for e in pool:
            if e["ed"] == d:
                picks[id(e)] = chooser(e, best)
    parts = []
    for label, sub in (("all", pool), ("first half", [e for e in pool if e["ed"] < mid_day]),
                       ("second half", [e for e in pool if e["ed"] >= mid_day])):
        n = max(1, len(sub))
        got = sum(e["choice"] == picks[id(e)] for e in sub)
        base = sum(e["choice"] == e["fb"] for e in sub)
        parts.append(f"{label} {got / n:.1%} (F {base / n:.1%})")
    print(f"{name:58s} n={len(pool):3d}  " + "   ".join(parts))


early = lambda e: (e["ts"] - e["listed"]) < 900  # noqa: E731
have_book = sum(1 for e in events if asks_at(e))
print(f"picks with a WS book at the trade: {have_book}/{len(events)}")
for subset, tag in ((None, ""), (early, " [early <15 min]")):
    report("EV: argmax p - ask over F-1..F+1, sigma per fold" + tag, lambda e, s: ch_ev(e, s, "edge"), (0.5, 0.7, 0.9, 1.1, 1.4), subset)
    report("EV: argmax p / ask over F-1..F+1, sigma per fold" + tag, lambda e, s: ch_ev(e, s, "ratio"), (0.5, 0.7, 0.9, 1.1, 1.4), subset)
    report("dearer-quoted neighbour on F's leaning side, margin m" + tag, ch_quote_lean, (0.0, 0.02, 0.05, 0.1, 0.2, 9.0), subset)
    report("D+1-market shift only toward F's leaning side, share a" + tag, ch_market_lean, (0.0, 0.25, 0.5, 0.75, 1.0), subset)
