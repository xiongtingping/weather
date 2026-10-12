"""Round 39g: members {production, production + UKMO} x station-correction
share {0.3, 0.5}, each paired against production at 0.3 -- hits on the truth
and agreement with the cluster's first YES.  Same strict as-of reading.
"""
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, SYSTEM_IDS  # noqa: E402

MEMBERS = {"production": frozenset(F_SYSTEM_IDS), "+UKMO": frozenset(F_SYSTEM_IDS | {SYSTEM_IDS["UKMO_GLOBAL"]})}


def picks(r):
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes" or fr["first"]["bucket"] not in r["labels"]:
        return None
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        return None
    return r["labels"].index(fr["first"]["bucket"])


cells = {}
for name, members in MEMBERS.items():
    mod.F_SYSTEM_IDS = members
    mod._HISTORY_CACHE.clear()
    ns["_CACHE"].clear()
    for r in rows:
        lad = ladder(G[r["key"]])
        e = ns["prod_f"](int(r["listed"]) + 3600)["station_days"].get(f"{r['st']}|{r['ed']}")
        if lad is None or not e or e.get("state") != "READY":
            continue
        for lam in (0.3, 0.5):
            cells.setdefault((r["st"], r["ed"]), {})[(name, lam)] = bi(lad, e["raw_median_c"] + e["fleet_c"] + lam * e["station_corr_c"])
base = ("production", 0.3)
for arm in [(m, l) for m in MEMBERS for l in (0.3, 0.5)]:
    out = []
    for what in ("hit", "= cluster"):
        w = l = n = 0
        for r in rows:
            c = cells.get((r["st"], r["ed"]))
            if not c or base not in c or arm not in c:
                continue
            target = r["labels"].index(r["truth"]) if what == "hit" else picks(r)
            if target is None:
                continue
            n += 1
            a, b = c[base] == target, c[arm] == target
            w += b and not a
            l += a and not b
        rate = sum(1 for r in rows if (c := cells.get((r["st"], r["ed"]))) and arm in c and
                   ((r["labels"].index(r["truth"]) if what == "hit" else picks(r)) == c[arm])) / max(1, n)
        d, h = (w - l) / max(1, n), 1.96 * (w + l) ** 0.5 / max(1, n)
        out.append(f"{what} {rate:.3f} ({d:+.3f} [{d - h:+.3f}, {d + h:+.3f}], n {n})")
    print(f"{arm[0]:10s} lambda {arm[1]}: " + "   ".join(out))
