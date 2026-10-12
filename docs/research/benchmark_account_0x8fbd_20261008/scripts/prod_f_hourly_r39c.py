"""Round 39c (熊先生 10-11 「用逐小时步取日最高温（先从 GFS 试），按同样的成对办法
检验它能不能把 F 拉近簇、并提高命中」).

Same 440 station-days and strict as-of reading as rounds 39/39b.  Arm A is the
production F (its members as served now; ICON has no history so on these days
it is AIFS/HRES/GFS/AIGFS/GDPS); arm B is the same F with GFS's 3-hourly run
replaced by GFS's own hourly files (``GFS_HOURLY``, every hour to f120), its
station correction fitted on its own history.  Paired hits on the truth and
paired agreement with the cluster's pre-formation first YES; plus GFS alone:
the hourly against the 3-hourly maximum of the same run against the truth.
Pass ``ukmo`` to add a third arm: A plus native UKMO (period maxima).
"""
import collections
import statistics
import sys
from pathlib import Path

here = Path(__file__)
want_ukmo = "ukmo" in sys.argv[2:]
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
sys.argv = sys.argv[:2]
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, F, FR, conn = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["F"], ns["FR"], ns["conn"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, SYSTEM_IDS  # noqa: E402

A = frozenset(F_SYSTEM_IDS)
B = frozenset((A - {SYSTEM_IDS["GFS"]}) | {SYSTEM_IDS["GFS_HOURLY"]})
ARMS = {"A production": A, "B GFS hourly": B}
if want_ukmo:
    ARMS["C + UKMO"] = frozenset(A | {SYSTEM_IDS["UKMO_GLOBAL"]})


def table(members, off):
    mod.F_SYSTEM_IDS = members
    mod._HISTORY_CACHE.clear()
    ns["_CACHE"].clear()
    out = {}
    for r in rows:
        t = int(r["listed"]) + off
        lad = ladder(G[r["key"]])
        entry = ns["prod_f"](t)["station_days"].get(f"{r['st']}|{r['ed']}")
        if lad is None or not entry or entry.get("state") != "READY":
            continue
        out[(r["st"], r["ed"])] = (bi(lad, entry["f_c"]), entry)
    return out


def cluster_pick(r):
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes" or fr["first"]["bucket"] not in r["labels"]:
        return None
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        return None
    return r["labels"].index(fr["first"]["bucket"])


def paired(a, b, key):
    w = sum(1 for x, y in zip(a, b) if y[key] and not x[key])
    l = sum(1 for x, y in zip(a, b) if x[key] and not y[key])
    n = max(1, len(a))
    d = (w - l) / n
    h = 1.96 * (w + l) ** 0.5 / n
    return d, d - h, d + h


off = 3600
tables = {name: table(members, off) for name, members in ARMS.items()}
base = "A production"
for name in ARMS:
    if name == base:
        continue
    xs, ys, cs_a, cs_b = [], [], [], []
    for r in rows:
        k = (r["st"], r["ed"])
        if k not in tables[base] or k not in tables[name]:
            continue
        y = r["labels"].index(r["truth"])
        fa, fb = tables[base][k][0], tables[name][k][0]
        xs.append({"hit": fa == y})
        ys.append({"hit": fb == y})
        ch = cluster_pick(r)
        if ch is not None:
            cs_a.append({"hit": fa == ch})
            cs_b.append({"hit": fb == ch})
    d = paired(xs, ys, "hit")
    c = paired(cs_a, cs_b, "hit")
    n, m = max(1, len(xs)), max(1, len(cs_a))
    print(f"== +1 h, {name} vs {base}: {len(xs)} station-days, F bucket differs on "
          f"{sum(1 for r in rows if (r['st'], r['ed']) in tables[base] and (r['st'], r['ed']) in tables[name] and tables[base][(r['st'], r['ed'])][0] != tables[name][(r['st'], r['ed'])][0])}")
    print(f"   hit: {base} {sum(x['hit'] for x in xs) / n:.3f}  {name} {sum(y['hit'] for y in ys) / n:.3f}"
          f"  paired {d[0]:+.3f} [{d[1]:+.3f}, {d[2]:+.3f}]")
    print(f"   = cluster first YES ({len(cs_a)}): {base} {sum(x['hit'] for x in cs_a) / m:.3f}  {name} {sum(y['hit'] for y in cs_b) / m:.3f}"
          f"  paired {c[0]:+.3f} [{c[1]:+.3f}, {c[2]:+.3f}]")

# GFS alone: the same run's hourly and 3-hourly maxima against the truth.
g3, g1 = SYSTEM_IDS["GFS"], SYSTEM_IDS["GFS_HOURLY"]
diffs, e3, e1 = [], [], []
for r in rows:
    k = (r["st"], r["ed"])
    entry = tables["B GFS hourly"].get(k)
    base_entry = tables[base].get(k)
    truth = ns["TRUTH_C"].get(k)
    if not entry or not base_entry or truth is None:
        continue
    s1 = entry[1]["systems"].get(g1)
    s3 = base_entry[1]["systems"].get(g3)
    if not s1 or not s3 or s1["run_init_utc"] != s3["run_init_utc"]:
        continue
    diffs.append(s1["tmax_c"] - s3["tmax_c"])
    e3.append(truth - s3["tmax_c"])
    e1.append(truth - s1["tmax_c"])
if diffs:
    print(f"\n== GFS alone, same run ({len(diffs)} station-days): hourly max - 3-hourly max mean {statistics.mean(diffs):+.2f} C"
          f" (median {statistics.median(diffs):+.2f}, >=1 C on {sum(1 for d in diffs if d >= 1) / len(diffs):.0%});"
          f" truth - max: 3-hourly mean {statistics.mean(e3):+.2f} MAE {statistics.mean(abs(x) for x in e3):.2f},"
          f" hourly mean {statistics.mean(e1):+.2f} MAE {statistics.mean(abs(x) for x in e1):.2f}")
