"""Round 39b: paired, same station-days -- does adding ARPEGE (and, going
forward, ICON) to the listing-time F change its hits or its agreement with the
cluster's first YES?  ICON has no history (DWD keeps one run a cycle), so on
these past days the seven-system F is the five plus ARPEGE where its runs
exist (from 09-27).  Strict as-of as in round 39.
"""
import collections
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, F, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["F"], ns["FR"]
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, SYSTEM_IDS  # noqa: E402

SEVEN = frozenset(F_SYSTEM_IDS)
FIVE = frozenset(SYSTEM_IDS[p] for p in ("AIFS", "IFS_HRES", "GFS", "AIGFS", "GDPS"))


def f_bucket(r, t, members):
    F.F_SYSTEM_IDS = members
    ns["F_SYSTEM_IDS"] = members
    import bayes_basket_target.listing_forecast_f as mod
    mod.F_SYSTEM_IDS = members
    mod._HISTORY_CACHE.clear()
    ns["_CACHE"].clear()
    entry = ns["prod_f"](t)["station_days"].get(f"{r['st']}|{r['ed']}")
    lad = ladder(G[r["key"]])
    if not entry or entry.get("state") != "READY" or lad is None:
        return None
    return bi(lad, entry["f_c"])


# One compute per (members, 10-minute instant), then all rows read from it.
def table(members, off):
    import bayes_basket_target.listing_forecast_f as mod
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
        out[(r["st"], r["ed"])] = (bi(lad, entry["f_c"]), entry["n_systems"])
    return out


for name, off in (("+1 h", 3600),):
    a, b = table(FIVE, off), table(SEVEN, off)
    pair = collections.Counter()
    clus = collections.Counter()
    for r in rows:
        k = (r["st"], r["ed"])
        if k not in a or k not in b:
            continue
        y = r["labels"].index(r["truth"])
        fa, fb = a[k][0], b[k][0]
        pair["n"] += 1
        pair["five"] += fa == y
        pair["seven"] += fb == y
        pair["seven wins"] += (fb == y) and fa != y
        pair["five wins"] += (fa == y) and fb != y
        pair["differ"] += fa != fb
        pair["with ARPEGE"] += b[k][1] > a[k][1]
        fr = FR.get(k)
        if fr and fr["first"]["side"] == "Yes" and fr["first"]["bucket"] in r["labels"]:
            v = list(fr["now"].values())
            if not (all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4):
                ch = r["labels"].index(fr["first"]["bucket"])
                clus["n"] += 1
                clus["=five"] += ch == fa
                clus["=seven"] += ch == fb
                clus["seven only"] += ch == fb and ch != fa
                clus["five only"] += ch == fa and ch != fb
    n = max(1, pair["n"])
    d = (pair["seven wins"] - pair["five wins"]) / n
    half = 1.96 * (pair["seven wins"] + pair["five wins"]) ** 0.5 / n
    print(f"== {name}: {pair['n']} station-days ({pair['with ARPEGE']} with ARPEGE in hand), F bucket differs on {pair['differ']}")
    print(f"   hit: five {pair['five'] / n:.3f}  seven {pair['seven'] / n:.3f}  paired {d:+.3f} [{d - half:+.3f}, {d + half:+.3f}]"
          f"  (seven-only wins {pair['seven wins']}, five-only wins {pair['five wins']})")
    m = max(1, clus["n"])
    dc = (clus["seven only"] - clus["five only"]) / m
    hc = 1.96 * (clus["seven only"] + clus["five only"]) ** 0.5 / m
    print(f"   = cluster first YES ({clus['n']}): five {clus['=five'] / m:.3f}  seven {clus['=seven'] / m:.3f}  paired {dc:+.3f} [{dc - hc:+.3f}, {dc + hc:+.3f}]")
