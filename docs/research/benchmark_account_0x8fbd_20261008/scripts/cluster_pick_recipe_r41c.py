"""Round 41c: F's own recipe vs the cluster's pick (single bucket).
(a) members: F without each member in turn (GEM/GDPS first); (b) the station
bias window 5/10/20/30/60 days x lambda 0.5/0.7/1.0; (c) truth recency: the
bias may use a settled day as soon as its local day has ended (live F does;
the backtest so far cut two calendar days back).  Leave-one-event-day-out for
the grid; halves of the event days; all picks and the early ones.
"""
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, bucket = ns["events"], ns["bucket"]
r39 = ns["ns"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, SYSTEM_IDS  # noqa: E402

ALL_TRUTHS, ROSTER, conn, tz = r39["ALL_TRUTHS"], r39["ROSTER"], r39["conn"], r39["tz"]
days = sorted({e["ed"] for e in events})
mid_day = days[len(days) // 2]
BASE_MEMBERS = frozenset(F_SYSTEM_IDS)
cache = {}


def compute_at(ts, members, station_days, recency):
    key = (int(ts) // 600, members, station_days, recency)
    if key not in cache:
        now = datetime.fromtimestamp(ts, timezone.utc)
        mod.F_SYSTEM_IDS = members
        mod.STATION_DAYS = station_days
        if recency == "local_day_ended":
            def truths(c, since, until, now=now):
                out = {}
                for (st, d), v in ALL_TRUTHS.items():
                    if not (since.isoformat() <= d <= until.isoformat()) or st not in tz:
                        continue
                    end = datetime.combine(date.fromisoformat(d) + timedelta(days=1), datetime.min.time(),
                                           tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
                    if end <= now:
                        out[(st, d)] = v
                return out
        else:
            cut = (now.date() - timedelta(days=2)).isoformat()
            def truths(c, since, until, cut=cut):
                return {k: v for k, v in ALL_TRUTHS.items() if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
        mod.formal_truths = truths
        cache[key] = mod.compute(conn, now=now, roster=ROSTER)
    return cache[key]


def f_of(ev, members=BASE_MEMBERS, station_days=60, recency="two_days", lam=0.5):
    e = compute_at(ev["ts"], members, station_days, recency)["station_days"].get(f"{ev['st']}|{ev['ed']}")
    if not e or e.get("state") != "READY":
        return None
    return bucket(ev["edges"], e["raw_median_c"] + e["fleet_c"] + lam * e["station_corr_c"])


def show(name, chooser, grid=None, pool=None):
    pool = pool or events
    picks = {}
    for d in days:
        train = [e for e in pool if e["ed"] != d]
        best = None
        if grid:
            best = max(grid, key=lambda p: sum((chooser(e, p) if chooser(e, p) is not None else e["fb"]) == e["choice"] for e in train))
        for e in pool:
            if e["ed"] == d:
                b = chooser(e, best)
                picks[id(e)] = e["fb"] if b is None else b
    parts = []
    for label, sub in (("all", pool), ("1st half", [e for e in pool if e["ed"] < mid_day]),
                       ("2nd half", [e for e in pool if e["ed"] >= mid_day])):
        n = max(1, len(sub))
        parts.append(f"{label} {sum(e['choice'] == picks[id(e)] for e in sub) / n:.1%} (F {sum(e['choice'] == e['fb'] for e in sub) / n:.1%})")
    print(f"{name:60s} n={len(pool):3d}  " + "   ".join(parts))


early = [e for e in events if e["ts"] - e["listed"] < 900]
for pool, tag in ((events, ""), (early, " [early]")):
    show("production F (recomputed)" + tag, lambda e, p: f_of(e), pool=pool)
    for product in ("GDPS", "ICON_GLOBAL", "AIGFS", "GFS", "IFS_HRES", "AIFS"):
        sysid = SYSTEM_IDS[product]
        show(f"(a) without {product}" + tag, lambda e, p, s=sysid: f_of(e, members=BASE_MEMBERS - {s}), pool=pool)
    show("(c) truth as soon as the local day ends" + tag, lambda e, p: f_of(e, recency="local_day_ended"), pool=pool)
    show("(b) station window x lambda, chosen per fold" + tag,
         lambda e, p: f_of(e, station_days=p[0], lam=p[1]),
         grid=[(w, l) for w in (5, 10, 20, 30, 60) for l in (0.5, 0.7, 1.0)], pool=pool)
    show("(a+c) without GDPS, truth as the local day ends" + tag,
         lambda e, p: f_of(e, members=BASE_MEMBERS - {SYSTEM_IDS["GDPS"]}, recency="local_day_ended"), pool=pool)
