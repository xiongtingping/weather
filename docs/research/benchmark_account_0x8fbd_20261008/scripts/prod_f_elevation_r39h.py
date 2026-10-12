"""Round 39h (熊先生 10-12 「在格点值上做显式海拔订正」): F with each member's
terrain offset (lapse rate 6.5 C/km x (cell height - station height); AWC
station elevations; each member's own terrain at the cell its rows sample) vs
production F (lambda 0.5, unadjusted).  Same 440 station-days, strict as-of,
paired hits on the truth and agreement with the cluster's first YES.
"""
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402

sys.path.insert(0, str(here.parent))
from terrain_offsets import read as read_offsets  # noqa: E402

OFFSETS = read_offsets()
_plain = mod.system_maxima


def _adjusted(conn, *, station_id, event_date, as_of_utc, settlement_timezone):
    out = _plain(conn, station_id=station_id, event_date=event_date, as_of_utc=as_of_utc,
                 settlement_timezone=settlement_timezone)
    for system, entry in out.items():
        entry["tmax_c"] = round(entry["tmax_c"] + OFFSETS.get(system, {}).get(station_id, 0.0), 3)
    return out


def pick_of(r):
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes" or fr["first"]["bucket"] not in r["labels"]:
        return None
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        return None
    return r["labels"].index(fr["first"]["bucket"])


cells = {}
for adjust in (False, True):
    mod.system_maxima = _adjusted if adjust else _plain
    mod._HISTORY_CACHE.clear()
    ns["_CACHE"].clear()
    for r in rows:
        lad = ladder(G[r["key"]])
        e = ns["prod_f"](int(r["listed"]) + 3600)["station_days"].get(f"{r['st']}|{r['ed']}")
        if lad is None or not e or e.get("state") != "READY":
            continue
        for lam in (0.3, 0.5, 0.7):
            cells.setdefault((r["st"], r["ed"]), {})[(adjust, lam)] = bi(
                lad, e["raw_median_c"] + e["fleet_c"] + lam * e["station_corr_c"])
base = (False, 0.5)
for arm in [(False, 0.3), (True, 0.3), (True, 0.5), (True, 0.7)]:
    out = []
    for what in ("hit", "= cluster"):
        w = l = n = got = 0
        for r in rows:
            c = cells.get((r["st"], r["ed"]))
            if not c or base not in c or arm not in c:
                continue
            target = r["labels"].index(r["truth"]) if what == "hit" else pick_of(r)
            if target is None:
                continue
            n += 1
            a, b = c[base] == target, c[arm] == target
            got += b
            w += b and not a
            l += a and not b
        d, h = (w - l) / max(1, n), 1.96 * (w + l) ** 0.5 / max(1, n)
        out.append(f"{what} {got / max(1, n):.3f} ({d:+.3f} [{d - h:+.3f}, {d + h:+.3f}], n {n})")
    label = ("terrain-adjusted" if arm[0] else "unadjusted") + f" lambda {arm[1]}"
    print(f"{label:28s} vs production (unadjusted 0.5): " + "   ".join(out))
