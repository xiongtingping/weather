"""Round 39f: production F's agreement with the cluster's first YES as the
share of the station correction varies (lambda 0.3 is the research value; 1.0
applies each station's whole bias).  A constant per-station offset between our
grid-cell values and an elevation-adjusted point value survives 70% into F at
lambda 0.3 and none at 1.0.  Same strict as-of reading as round 39.
"""
import collections
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"]
LAMBDAS = (0.0, 0.3, 0.5, 0.7, 1.0)
stat = {lam: collections.Counter() for lam in LAMBDAS}
base = {}
for r in rows:
    fr = FR.get((r["st"], r["ed"]))
    lad = ladder(G[r["key"]])
    if lad is None:
        continue
    y = r["labels"].index(r["truth"])
    pick = None
    if fr and fr["first"]["side"] == "Yes" and fr["first"]["bucket"] in r["labels"]:
        v = list(fr["now"].values())
        if not (all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4):
            pick = r["labels"].index(fr["first"]["bucket"])
    entry = ns["prod_f"](int(r["listed"]) + 3600)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not entry or entry.get("state") != "READY":
        continue
    for lam in LAMBDAS:
        b = bi(lad, entry["raw_median_c"] + entry["fleet_c"] + lam * entry["station_corr_c"])
        s = stat[lam]
        s["n"] += 1
        s["hit"] += b == y
        if pick is not None:
            s["m"] += 1
            s["=cl"] += b == pick
for lam in LAMBDAS:
    s = stat[lam]
    print(f"lambda {lam:.1f}: hit {s['hit'] / max(1, s['n']):.3f} (n {s['n']})   = cluster first YES {s['=cl'] / max(1, s['m']):.3f} (n {s['m']})")

# Paired 0.5 vs 0.3, overall and by halves of the event-day range (the gain
# must hold in both halves to be more than a choice made on this sample).
cells = []
for r in rows:
    fr = FR.get((r["st"], r["ed"]))
    lad = ladder(G[r["key"]])
    entry = ns["prod_f"](int(r["listed"]) + 3600)["station_days"].get(f"{r['st']}|{r['ed']}")
    if lad is None or not entry or entry.get("state") != "READY":
        continue
    pick = None
    if fr and fr["first"]["side"] == "Yes" and fr["first"]["bucket"] in r["labels"]:
        v = list(fr["now"].values())
        if not (all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4):
            pick = r["labels"].index(fr["first"]["bucket"])
    f = lambda lam: bi(lad, entry["raw_median_c"] + entry["fleet_c"] + lam * entry["station_corr_c"])  # noqa: E731
    cells.append((r["ed"], r["labels"].index(r["truth"]), pick, f(0.3), f(0.5)))
days = sorted({c[0] for c in cells})
mid = days[len(days) // 2]
for label, part in (("all", cells), (f"days < {mid}", [c for c in cells if c[0] < mid]),
                    (f"days >= {mid}", [c for c in cells if c[0] >= mid])):
    for what, idx in (("hit", 1), ("= cluster", 2)):
        sub = [c for c in part if c[idx] is not None]
        w = sum(1 for c in sub if c[4] == c[idx] and c[3] != c[idx])
        l = sum(1 for c in sub if c[3] == c[idx] and c[4] != c[idx])
        n = max(1, len(sub))
        d, h = (w - l) / n, 1.96 * (w + l) ** 0.5 / n
        print(f"   lambda 0.5 vs 0.3, {label:16s} {what:9s} n={len(sub):3d}  {d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]")
