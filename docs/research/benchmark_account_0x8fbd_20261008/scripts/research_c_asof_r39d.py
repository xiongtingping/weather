"""Round 39d: is the research C's 51% agreement with the cluster's first YES
partly its look-ahead?  Rounds 32-37 fitted the station bias on truths up to
event day -2 (not settled at listing).  Same 440 station-days: the research C
with that cut, and with a strict cut (event day -4, every station's day ended
before the listing instant); hits on the truth and agreement with the
cluster's pre-formation first YES, paired.
"""
import collections
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

here = Path(__file__)
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"]
ns2 = {}
src2 = here.with_name("listing_buy_r33.py").read_text()
fc, LAG, HIST, TRUTH = (ns["fc"], ns["LAG"], None, None)
# The r33 head is already executed inside r39's head as ``ns2`` there; reach it.
r33 = {k: v for k, v in ns.items()}
HIST = ns["ns2"]["HIST"]
TRUTH = ns["ns2"]["TRUTH"]


def bias(st, ed, m, lag_days):
    cut = (date.fromisoformat(ed) - timedelta(days=lag_days)).isoformat()
    v = [TRUTH[(s, d)][1] - x for (s, d, mm), x in HIST.items()
         if s == st and mm == m and d <= cut and (s, d) in TRUTH and TRUTH[(s, d)][1] is not None]
    return statistics.mean(v) if len(v) >= 3 else 0.0


def research_c(r, t, lag_days):
    raw = {m: fc(r["st"], r["ed"], m, t) for m in LAG}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        return None
    cor = {m: v + bias(r["st"], r["ed"], m, lag_days) for m, v in raw.items()}
    med_c, med_r = statistics.median(cor.values()), statistics.median(raw.values())
    return med_r + 0.8 + 0.3 * (med_c - med_r)


def cluster_pick(r):
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes" or fr["first"]["bucket"] not in r["labels"]:
        return None
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        return None
    return r["labels"].index(fr["first"]["bucket"]), fr["ts"]


hits = collections.Counter()
for r in rows:
    lad = ladder(G[r["key"]])
    if lad is None:
        continue
    t = int(r["listed"]) + 3600
    y = r["labels"].index(r["truth"])
    a, b = research_c(r, t, 2), research_c(r, t, 4)
    if a is None or b is None:
        continue
    ba, bb = bi(lad, a), bi(lad, b)
    hits["n"] += 1
    hits["lag2 hit"] += ba == y
    hits["lag4 hit"] += bb == y
    hits["lag4 only"] += bb == y and ba != y
    hits["lag2 only"] += ba == y and bb != y
    pick = cluster_pick(r)
    if pick is not None:
        ch, ts = pick
        ca, cb = research_c(r, ts, 2), research_c(r, ts, 4)
        if ca is None or cb is None:
            continue
        hits["m"] += 1
        hits["lag2 =cl"] += bi(lad, ca) == ch
        hits["lag4 =cl"] += bi(lad, cb) == ch
        hits["lag4 only cl"] += bi(lad, cb) == ch and bi(lad, ca) != ch
        hits["lag2 only cl"] += bi(lad, ca) == ch and bi(lad, cb) != ch
n, m = max(1, hits["n"]), max(1, hits["m"])
d = (hits["lag4 only"] - hits["lag2 only"]) / n
h = 1.96 * (hits["lag4 only"] + hits["lag2 only"]) ** 0.5 / n
dc = (hits["lag4 only cl"] - hits["lag2 only cl"]) / m
hc = 1.96 * (hits["lag4 only cl"] + hits["lag2 only cl"]) ** 0.5 / m
print(f"research C, station bias through event-2 (look-ahead) vs event-4 (strict), {hits['n']} station-days at +1 h:")
print(f"   hit: lag2 {hits['lag2 hit'] / n:.3f}  lag4 {hits['lag4 hit'] / n:.3f}  paired {d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]")
print(f"   = cluster first YES at its fill ({hits['m']}): lag2 {hits['lag2 =cl'] / m:.3f}  lag4 {hits['lag4 =cl'] / m:.3f}"
      f"  paired {dc:+.3f} [{dc - hc:+.3f}, {dc + hc:+.3f}]")
