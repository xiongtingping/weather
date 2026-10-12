"""Round 39e: which of the research C's ten Open-Meteo models carry its
agreement with the cluster's first YES?  Strict-as-of bias (event day -4).
Leave-one-out over the ten, and the research C restricted to the families
production can read natively (GFS, IFS, AIFS, AIGFS, GEM, ICON, ARPEGE) --
the same data source either way, so a gap between this subset and production
F is how the numbers are made (point extraction, steps), not who is in it.
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
rows, ladder, G, bi, FR, fc, LAG = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"], ns["fc"], ns["LAG"]
HIST, TRUTH = ns["ns2"]["HIST"], ns["ns2"]["TRUTH"]
NATIVE_FAMILIES = {"gfs_global", "ecmwf_ifs025", "ecmwf_aifs025_single", "ncep_aigfs025", "gem_global",
                   "icon_global", "meteofrance_arpege_world"}


def bias(st, ed, m):
    cut = (date.fromisoformat(ed) - timedelta(days=4)).isoformat()
    v = [TRUTH[(s, d)][1] - x for (s, d, mm), x in HIST.items()
         if s == st and mm == m and d <= cut and (s, d) in TRUTH and TRUTH[(s, d)][1] is not None]
    return statistics.mean(v) if len(v) >= 3 else 0.0


def c_of(r, t, models):
    raw = {m: fc(r["st"], r["ed"], m, t) for m in models}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        return None
    cor = {m: v + bias(r["st"], r["ed"], m) for m, v in raw.items()}
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


picks = [(r, cluster_pick(r), ladder(G[r["key"]])) for r in rows]
picks = [(r, p, lad) for r, p, lad in picks if p is not None and lad is not None]
ALL = list(LAG)
arms = {"all ten": ALL, "native families (7)": [m for m in ALL if m in NATIVE_FAMILIES]}
for m in ALL:
    arms[f"without {m}"] = [x for x in ALL if x != m]
base = None
for name, models in arms.items():
    agree = hit = n = 0
    per = []
    for r, (ch, ts), lad in picks:
        c = c_of(r, ts, models)
        if c is None:
            per.append(None)
            continue
        b = bi(lad, c)
        n += 1
        agree += b == ch
        hit += b == r["labels"].index(r["truth"])
        per.append(b == ch)
    if base is None:
        base = per
        print(f"{name:42s} n={n}  = cluster {agree / max(1, n):.3f}  hit {hit / max(1, n):.3f}")
        continue
    w = sum(1 for a, b in zip(base, per) if a is not None and b is not None and b and not a)
    l = sum(1 for a, b in zip(base, per) if a is not None and b is not None and a and not b)
    k = sum(1 for a, b in zip(base, per) if a is not None and b is not None)
    d = (w - l) / max(1, k)
    h = 1.96 * (w + l) ** 0.5 / max(1, k)
    print(f"{name:42s} n={n}  = cluster {agree / max(1, n):.3f}  hit {hit / max(1, n):.3f}   vs all ten {d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]")
