"""Round 28 (熊先生 10-11 「继续打，做逆势的字面回测」): paper backtests of the
contrarian pattern round 27 found (the leg the cluster takes got cheaper on its
own side over the hour before the fill).

Events: rounds 25-27's first-taker events (research window + forward), truth
refreshed from the SSOT's settlement labels now; minute paths from
fetch_minute_paths.py.  "d60" = the leg's own-side price now minus 60 minutes
ago, strictly before the fill's minute (cents).

 A. The cluster's own legs by d60 group, at the price on the book at the fill.
 B. Following them with d60 as a filter: entry at the leg's minute mid ten
    minutes after the fill plus 2c (round 10's pricing), when the path reaches.
 C. Our own pick at the same instants, no cluster choice: among YES on the mode
    and its two neighbours and NO on the mode, the leg whose own side fell most
    over the hour (d60 <= -1c), bought at the book; against a random pick from
    the same set and against the cluster's own leg.
Day-clustered bootstrap intervals; halves H1 (<= 09-29), H2 (09-30..10-07),
F (forward, >= 10-08).
"""
import bisect
import collections
import random
import sqlite3
import statistics
import sys
from pathlib import Path

B = Path(sys.argv[1])
src = Path(__file__).with_name("replicate_fit.py").read_text()
head = src.split("# ---------------------------------------------------------------- A. single centres")[0]
ns = {"__name__": "rf", "__file__": str(Path(__file__).with_name("replicate_fit.py"))}
sys.argv = sys.argv[:2]
exec(compile(head, "replicate_fit_head", "exec"), ns)
ALL, minute_feats, MINUTE = ns["ALL"], ns["minute_feats"], ns["MINUTE"]
random.seed(2828)

conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA query_only=1")
labels = {(st, ed): b for st, ed, b in conn.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE event_date >= '2026-09-20'"
    " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
    " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}


def truth_ord(e):
    lab = labels.get((e["st"], e["ed"]))
    if lab is None:
        return None
    inv = {v: k for k, v in e["labels"].items()}
    return inv.get(lab)


def period(e):
    return "F" if e["ed"] >= "2026-10-08" else ("H1" if e["ed"] <= "2026-09-29" else "H2")


def leg_price(e, o, side):
    return e["ask"].get(o) if side == "Yes" else e["no_price"].get(o)


def leg_hit(e, o, side, t):
    hit = 1.0 if t == o else 0.0
    return hit if side == "Yes" else 1.0 - hit


def mid_after(e, label, side, seconds=600):
    path = MINUTE.get(f"{e['st']}|{e['ed']}|{label}")
    if not path or not path["t"]:
        return None
    t, pr = path["t"], path["p"]
    target = int(e["ts"]) + seconds
    i = bisect.bisect_right(t, target) - 1
    if i < 0 or t[i] < int(e["ts"]) or target - t[i] > 300:
        return None
    return pr[i] if side == "Yes" else 1.0 - pr[i]


def stats(rows, label):
    """rows: (event, cents, roi)."""
    if len(rows) < 25:
        return f"   {label:52s} n={len(rows)}"
    cents = [c for _e, c, _r in rows]
    roi = [r for _e, _c, r in rows]
    days = collections.defaultdict(list)
    for e, c, r in rows:
        days[e["ed"]].append(r)
    keys = list(days)
    boot = []
    for _ in range(1000):
        s = [x for k in (random.choice(keys) for _ in keys) for x in days[k]]
        boot.append(sum(s) / len(s))
    boot.sort()
    per = {p: [r for e, _c, r in rows if period(e) == p] for p in ("H1", "H2", "F")}
    ps = " / ".join(f"{100*statistics.mean(v):+.1f}%" if v else "-" for v in per.values())
    pos = sum(1 for v in days.values() if sum(v) > 0)
    return (f"   {label:52s} n={len(rows):4d}  {statistics.mean(cents):+5.1f}c/share  ROI {100*statistics.mean(roi):+5.1f}%"
            f" [{100*boot[25]:+.1f},{100*boot[974]:+.1f}]  H1/H2/F {ps}  days+ {pos}/{len(days)}")


groups = {"fell >= 1c": lambda d: d <= -1.0, "fell 0-1c": lambda d: -1.0 < d < 0, "flat": lambda d: d == 0,
          "rose": lambda d: d > 0}
A = collections.defaultdict(list)
Bf = collections.defaultdict(list)
C = collections.defaultdict(list)
for e in ALL:
    t = truth_ord(e)
    if t is None:
        continue
    o, side = e["chosen"]["ord"], e["chosen"]["side"]
    label = e["labels"][o]
    y = 1 if side == "Yes" else -1
    mf = minute_feats(e, label)
    if mf["d60"] != mf["d60"]:
        continue
    d60 = round(mf["d60"] * y * 100, 6)
    price = leg_price(e, o, side)
    hit = leg_hit(e, o, side, t)
    if price and 0 < price < 1:
        for g, keep in groups.items():
            if keep(d60):
                A[g].append((e, 100 * (hit - price), (hit - price) / price))
        A["all"].append((e, 100 * (hit - price), (hit - price) / price))
    m10 = mid_after(e, label, side)
    if m10 is not None and 0 < m10 + 0.02 < 1:
        entry = m10 + 0.02
        row = (e, 100 * (hit - entry), (hit - entry) / entry)
        Bf["all followed"].append(row)
        for g, keep in groups.items():
            if keep(d60):
                Bf[g].append(row)
        c12 = e["unit"] == "C" and 12 <= e["lead"] <= 48
        if c12:
            Bf["C, 12-48h"].append(row)
            if d60 < 0:
                Bf["C, 12-48h, fell"].append(row)
            elif d60 > 0:
                Bf["C, 12-48h, rose"].append(row)
    # C. our own contrarian pick at this instant
    mode = e["mode_ord"]
    cand = []
    for oo in (mode - 1, mode, mode + 1):
        if oo in e["labels"]:
            cand.append((oo, "Yes"))
    if mode in e["no_price"]:
        cand.append((mode, "No"))
    scored = []
    for oo, ss in cand:
        f = minute_feats(e, e["labels"][oo])
        if f["d60"] != f["d60"]:
            continue
        pr = leg_price(e, oo, ss)
        if not pr or not 0 < pr < 1:
            continue
        scored.append((f["d60"] * (1 if ss == "Yes" else -1) * 100, oo, ss, pr))
    if scored:
        best = min(scored)
        if best[0] <= -1.0:
            h = leg_hit(e, best[1], best[2], t)
            C["contrarian pick (fell most, >= 1c)"].append((e, 100 * (h - best[3]), (h - best[3]) / best[3]))
            kind = ("YES" if best[2] == "Yes" else "NO") + (" mode" if best[1] == mode else " beside")
            C[f"  of which {kind}"].append((e, 100 * (h - best[3]), (h - best[3]) / best[3]))
        rnd = random.choice(scored)
        h = leg_hit(e, rnd[1], rnd[2], t)
        C["random pick from the same set"].append((e, 100 * (h - rnd[3]), (h - rnd[3]) / rnd[3]))
        worst = max(scored)
        if worst[0] >= 1.0:
            h = leg_hit(e, worst[1], worst[2], t)
            C["momentum pick (rose most, >= 1c)"].append((e, 100 * (h - worst[3]), (h - worst[3]) / worst[3]))

print("== A. the cluster's own legs by the hour's move on their side, at the book price at the fill")
for g in ("all", "fell >= 1c", "fell 0-1c", "flat", "rose"):
    print(stats(A[g], g))
print("\n== B. following the cluster's leg: entry = minute mid 10 min after the fill + 2c")
for g in ("all followed", "fell >= 1c", "fell 0-1c", "flat", "rose", "C, 12-48h", "C, 12-48h, fell", "C, 12-48h, rose"):
    print(stats(Bf[g], g))
print("\n== C. our own pick at the same instants, at the book (no cluster choice)")
for g in ("contrarian pick (fell most, >= 1c)", "  of which YES mode", "  of which YES beside", "  of which NO mode",
          "momentum pick (rose most, >= 1c)", "random pick from the same set"):
    print(stats(C[g], g))
print(f"\n(settled events used: {len(A['all'])}; labels read up to {max(ed for _s, ed in labels)})")
