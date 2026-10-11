"""Round 37 (熊先生 10-11 「比较确定性模型通过偏差校准后的f桶和我系统目前实现的六大全球成员桶选出的众数桶有多大区别？」).

Same 440 station-days as rounds 34/36 (event days 09-28..10-10).  At several
instants after listing: F (ten deterministic models, median with the station
bias from days <= d-2; Open-Meteo single runs, research only), C (the cluster
calibration: raw median + 0.8 C + 0.3 x station bias), and our served mode
(the production served vector at that instant, P1 > P0-M > P0, i.e. the six
global ensemble members' P0 in the first days).  Agreement, offsets, hits on
the truth, who is right when they differ, closeness to the market mode at
06:00 local on the event day.  Descriptive; our served vectors span several
versions over the window (charter 255).
"""
import collections
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

B = Path(sys.argv[1])
src = Path(__file__).with_name("forecast_then_mode_r36.py").read_text()
ns = {"__name__": "r36", "__file__": str(Path(__file__).with_name("forecast_then_mode_r36.py"))}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("data = []")[0], "r36_head", "exec"), ns)
rows, market, conn, iso, tz = ns["rows"], ns["market"], ns["conn"], ns["iso"], ns["tz"]
src2 = Path(__file__).with_name("listing_buy_r33.py").read_text()
ns2 = {"__name__": "r33", "__file__": str(Path(__file__).with_name("listing_buy_r33.py")), "sys": sys}
exec(compile(src2.split("WINDOWS = {")[0], "r33_head", "exec"), ns2)
G, ladder, bi, fc, bias, LAG, ts_ = (ns2[k] for k in ("G", "ladder", "bi", "fc", "bias", "LAG", "ts_"))
from zoneinfo import ZoneInfo


def served(r, t):
    for stage in ("P1", "P0-M", "P0"):
        row = conn.execute("SELECT history_id, decision_at_utc FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND stage=?"
                           " AND state='COMMITTED' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1", (r["st"], r["ed"], stage, iso(t))).fetchone()
        if row:
            vec = dict(conn.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (row[0],)).fetchall())
            if not vec:
                return None
            tot = sum(vec.values())
            b = max(vec, key=lambda k: vec[k])
            if b not in r["labels"]:
                return None
            return {"stage": stage, "mode": r["labels"].index(b), "p_mode": vec[b] / tot, "age_h": (t - ts_(row[1])) / 3600,
                    "vec": {k: v / tot for k, v in vec.items()}}
    return None


def det(r, t):
    raw = {m: fc(r["st"], r["ed"], m, t) for m in LAG}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        return None
    lad = ladder(G[r["key"]])
    cor = {m: v + bias(r["st"], r["ed"], m) for m, v in raw.items()}
    med_c, med_r = statistics.median(cor.values()), statistics.median(raw.values())
    return {"F": bi(lad, med_c), "C": bi(lad, med_r + 0.8 + 0.3 * (med_c - med_r)), "med_c": med_c, "lad": lad}


FR = ns["FR"]
INSTANTS = [("listing +5 min", 300), ("+1 h", 3600), ("+3 h", 3 * 3600), ("+6 h", 6 * 3600)]
for name, off in INSTANTS:
    stat = collections.Counter()
    offs = collections.Counter()
    hits = collections.Counter()
    tgap = []
    for r in rows:
        t = int(r["listed"]) + off
        d = det(r, t)
        s = served(r, t)
        stat["n"] += 1
        if d is None:
            stat["no deterministic"] += 1
            continue
        if s is None:
            stat["no served vector"] += 1
            continue
        stat["both"] += 1
        stat[f"stage {s['stage']}"] += 1
        y = r["labels"].index(r["truth"])
        F, C, M = d["F"], d["C"], s["mode"]
        offs[max(-3, min(3, F - M))] += 1
        stat["F=ours"] += F == M
        stat["C=ours"] += C == M
        hits["F"] += F == y
        hits["C"] += C == y
        hits["ours"] += M == y
        if F != M:
            stat["F!=ours"] += 1
            hits["F right when differ"] += F == y
            hits["ours right when differ"] += M == y
        y_, m_, dd = (int(x) for x in r["ed"].split("-"))
        ev06 = int(datetime(y_, m_, dd, 6, tzinfo=ZoneInfo(tz[r["st"]])).timestamp())
        mk = market(r, ev06)
        if mk is not None:
            stat["n_mk"] += 1
            hits["F = later market mode"] += F == mk
            hits["C = later market mode"] += C == mk
            hits["ours = later market mode"] += M == mk
        # temperature gap: our served mean (bucket centres) minus the corrected deterministic median
        e = d["lad"]["edges"]
        cen = {lab: (e[i][0] + e[i][1]) / 2 for i, lab in enumerate(r["labels"])}
        mean_ours = sum(p * cen[k] for k, p in s["vec"].items() if k in cen)
        tgap.append(mean_ours - d["med_c"])
        stat["age_sum"] += s["age_h"]
    n = max(1, stat["both"])
    print(f"\n== {name}: station-days {stat['n']}; both available {stat['both']} (no deterministic {stat['no deterministic']}, no served vector {stat['no served vector']});"
          f" our stage {', '.join(f'{k[6:]} {v}' for k, v in stat.items() if k.startswith('stage '))}; our vector age {stat['age_sum'] / n:.1f} h")
    print(f"   same bucket: F = our mode {stat['F=ours'] / n:.2f}   C (cluster calibration) = our mode {stat['C=ours'] / n:.2f}")
    print("   F minus our mode (buckets): " + "  ".join(f"{k:+d}: {100 * offs[k] / n:.0f}%" for k in range(-3, 4)))
    print(f"   hit the truth: F {hits['F'] / n:.3f}   C {hits['C'] / n:.3f}   our mode {hits['ours'] / n:.3f}")
    nd = max(1, stat["F!=ours"])
    print(f"   when F != our mode ({stat['F!=ours']}): truth at F {hits['F right when differ'] / nd:.3f}, at our mode {hits['ours right when differ'] / nd:.3f}")
    nm = max(1, stat["n_mk"])
    print(f"   = market mode at 06:00 local on the event day: F {hits['F = later market mode'] / nm:.3f}   C {hits['C = later market mode'] / nm:.3f}   our mode {hits['ours = later market mode'] / nm:.3f}")
    if tgap:
        print(f"   our served mean minus the corrected deterministic median: mean {statistics.mean(tgap):+.2f} C, median {statistics.median(tgap):+.2f} C, |gap| >= 1 C {sum(1 for g in tgap if abs(g) >= 1) / len(tgap):.0%}")

# the cluster's pre-formation first YES against both, same station-days
print("\n== the cluster's pre-formation first YES (station-days where it had one): bucket = F / = C / = our mode at its fill")
cnt = collections.Counter()
for r in rows:
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes":
        continue
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        continue
    if fr["first"]["bucket"] not in r["labels"]:
        continue
    t = fr["ts"]
    d = det(r, t)
    s = served(r, t)
    if d is None or s is None:
        continue
    ch = r["labels"].index(fr["first"]["bucket"])
    cnt["n"] += 1
    cnt["=F"] += ch == d["F"]
    cnt["=C"] += ch == d["C"]
    cnt["=ours"] += ch == s["mode"]
    y = r["labels"].index(r["truth"])
    cnt["cluster hit"] += ch == y
    cnt["ours hit"] += s["mode"] == y
n = max(1, cnt["n"])
print(f"   n={cnt['n']}: = F {cnt['=F'] / n:.2f}  = C {cnt['=C'] / n:.2f}  = our mode {cnt['=ours'] / n:.2f}   hit the truth: cluster {cnt['cluster hit'] / n:.3f}  our mode {cnt['ours hit'] / n:.3f}")
