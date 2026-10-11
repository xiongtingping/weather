"""Round 29 part 3 (熊先生 10-11 「云雨比预报的多温度肯定偏冷啊，你的方法不对吗」):
the same instants and gaps as obs_vs_forecast.py, against two references:

  truth - forecast   the settled Tmax minus the day-before forecast's daily max
                     (previous_day1 hourly temperature, local day) -- the physics
  truth - market     the settled Tmax minus the market's mean at the instant
                     (normalised mid x bucket centre, C) -- what is left to trade

  market move        the market's mean at the instant minus its mean at local
                     06:00 -- how much of the day's news the price already took

grouped by each gap's tercile (cloudier/wetter/duller/colder than forecast ...
clearer/drier/sunnier/warmer).  truth - forecast has the station's own
leave-one-day-out mean removed (the grid point's standing bias).  Means in C
with day-clustered intervals.
"""
import collections
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

src = Path(__file__).with_name("obs_vs_forecast.py").read_text()
ns = {"__name__": "ovf", "__file__": str(Path(__file__).with_name("obs_vs_forecast.py"))}
exec(compile(src.split("rows = []")[0], "obs_vs_forecast_head", "exec"), ns)
OF, labels, truth_c, board, market_at, signals, centre_c, iso = (
    ns["OF"], ns["labels"], ns["truth_c"], ns["board"], ns["market_at"], ns["signals"], ns["centre_c"], ns["iso"])
random.seed(29293)


def forecast_max(st, ed):
    f = OF[st]["fc"]
    vals = [f["temperature_2m"][i] for i, t in enumerate(f["time"]) if t.startswith(ed) and f["temperature_2m"][i] is not None]
    return max(vals) if len(vals) >= 20 else None


def market_mean(st, ed, at_local):
    mk = market_at(st, ed, iso(at_local))
    if not mk:
        return None
    q, _ords = mk
    cs = {b: centre_c(b) for b in q}
    if any(v is None for v in cs.values()):
        return None
    return sum(q[b] * cs[b] for b in q) / sum(q.values())


rows = []
for (st, ed), lab in sorted(labels.items()):
    if st not in OF or (st, ed) not in truth_c:
        continue
    fmax = forecast_max(st, ed)
    if fmax is None:
        continue
    zone = ZoneInfo(OF[st]["tz"])
    try:
        metar = board._metar(st, ed, zone)
    except Exception:
        continue
    m06 = market_mean(st, ed, datetime.fromisoformat(ed).replace(hour=6, tzinfo=zone))
    for H in (10, 12, 14):
        at_local = datetime.fromisoformat(ed).replace(hour=H, tzinfo=zone)
        mmean = market_mean(st, ed, at_local)
        if mmean is None:
            continue
        sig = signals(st, ed, at_local, metar)
        rows.append(dict(st=st, ed=ed, H=H, d_fc_raw=truth_c[(st, ed)] - fmax, d_mkt=truth_c[(st, ed)] - mmean,
                         move=None if m06 is None else mmean - m06, sig=sig))
# the grid point's standing bias: the station's mean truth - forecast on its other days
per = collections.defaultdict(dict)
for r in rows:
    per[r["st"]][r["ed"]] = r["d_fc_raw"]
for r in rows:
    other = [v for d, v in per[r["st"]].items() if d != r["ed"]]
    r["d_fc"] = r["d_fc_raw"] - (statistics.mean(other) if len(other) >= 5 else 0.0)
rows = [r for r in rows if len(per[r["st"]]) >= 6]
print(f"instances {len(rows)}  station-days {len({(r['st'], r['ed']) for r in rows})}")


def ci(sel, key):
    days = collections.defaultdict(list)
    for r in sel:
        days[r["ed"]].append(r[key])
    keys = list(days)
    boot = []
    for _ in range(800):
        s = [x for k in (random.choice(keys) for _ in keys) for x in days[k]]
        boot.append(sum(s) / len(s))
    boot.sort()
    return statistics.mean(r[key] for r in sel), boot[20], boot[779]


NAMES = {"cloud": ("cloudier than forecast", "clearer than forecast"),
         "rain": ("wetter than forecast", "drier than forecast"),
         "rad": ("duller than forecast (satellite)", "sunnier than forecast"),
         "temp": ("colder than forecast so far", "warmer than forecast so far")}
for H in (10, 12, 14):
    print(f"\n== local {H}:00   (each gap + = warmer; terciles; means in C [95%])")
    for name, (neg, pos) in NAMES.items():
        sub = [r for r in rows if r["H"] == H and r["sig"].get(name) is not None]
        if name == "rain":
            groups = [(neg, [r for r in sub if r["sig"]["rain"] < 0]), ("as forecast", [r for r in sub if r["sig"]["rain"] == 0]),
                      (pos, [r for r in sub if r["sig"]["rain"] > 0])]
        else:
            sub.sort(key=lambda r: r["sig"][name])
            k = len(sub) // 3
            groups = [(neg, sub[:k]), ("middle", sub[k:2 * k]), (pos, sub[2 * k:])]
        for label, g in groups:
            if len(g) < 25:
                print(f"   {name:5s} {label:34s} n={len(g)}")
                continue
            a, alo, ahi = ci(g, "d_fc")
            b, blo, bhi = ci(g, "d_mkt")
            gm = [r for r in g if r["move"] is not None]
            mv = statistics.mean(r["move"] for r in gm) if gm else float("nan")
            print(f"   {name:5s} {label:34s} n={len(g):4d}  truth-forecast {a:+.2f} [{alo:+.2f},{ahi:+.2f}]"
                  f"  market move since 06 {mv:+.2f} (n={len(gm)})  truth-market {b:+.2f} [{blo:+.2f},{bhi:+.2f}]")

# ------------------------------------------------------------ where the forecast miss went
# On instances with a 06:00 book: truth - forecast = (market at 06 - forecast) + (move 06 -> instant) + (truth - market)
print("\n== decomposition: truth - forecast  =  market at 06 already off the forecast  +  move since 06  +  left over (truth - market)")
for H in (12, 14):
    print(f"   local {H}:00")
    for name, (neg, pos) in NAMES.items():
        sub = [r for r in rows if r["H"] == H and r["sig"].get(name) is not None and r["move"] is not None]
        if name == "rain":
            groups = [(neg, [r for r in sub if r["sig"]["rain"] < 0]), (pos, [r for r in sub if r["sig"]["rain"] > 0])]
        else:
            sub.sort(key=lambda r: r["sig"][name])
            k = len(sub) // 3
            groups = [(neg, sub[:k]), (pos, sub[2 * k:])]
        for label, g in groups:
            if len(g) < 25:
                continue
            tot = statistics.mean(r["d_fc"] for r in g)
            mv = statistics.mean(r["move"] for r in g)
            left = statistics.mean(r["d_mkt"] for r in g)
            print(f"     {name:5s} {label:34s} n={len(g):4d}  {tot:+.2f} = {tot - mv - left:+.2f} (at 06) {mv:+.2f} (move) {left:+.2f} (left)")
