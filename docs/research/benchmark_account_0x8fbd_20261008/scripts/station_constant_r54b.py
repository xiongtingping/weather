"""Round 54b: can the Open-Meteo station-value difference be reproduced natively?

Delta_OM = Open-Meteo station value - native nearest-land-cell value is a
stable station x model constant (round 54).  Two native candidates for it:
(a) round 39h's terrain offset, lapse rate x (cell height - station height)
    -- correlation with the per-station mean Delta_OM, model by model;
(b) a learned station constant: the station's mean Delta_OM over OTHER event
    days (leave one event day out), applied to the native member.
Each gives F' = F + median(member + constant) - median(member) over the
members with a constant; scored against the cluster's pick and the truth on
the 227 picks, next to F and to the Open-Meteo values themselves.
"""
import collections
import json
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("point_extraction_r54.py").read_text()
ns = {"__name__": "r54", "__file__": str(here.with_name("point_extraction_r54.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("# (1) Delta per model")[0], "r54_head", "exec"), ns)
rows, bucket, pairs_for, ROSTER, ALL_TRUTHS, paired = (ns[k] for k in ("rows", "bucket", "pairs_for", "ROSTER", "ALL_TRUTHS", "paired"))
from datetime import datetime, timezone  # noqa: E402

TERRAIN = json.load(open(here.parent.parent / "terrain_offsets_r39h.json"))["systems"]
for r in rows:
    ev = r["ev"]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r["pairs"] = pairs_for(ev["st"], ev["ed"], as_of, ROSTER[ev["st"]][2])

# (a) terrain offset vs Delta_OM, per station x model
print("(a) round 39h terrain offset (lapse x (cell - station), C) against the per-station mean Delta_OM")
per = collections.defaultdict(list)
for r in rows:
    for system, nat, om in r["pairs"]:
        per[(r["ev"]["st"], system)].append(om - nat)
for system in sorted({s for _st, s in per}):
    xs, ys = [], []
    for (st, s_), lst in per.items():
        if s_ != system or len(lst) < 3:
            continue
        t = TERRAIN.get(system, {}).get(st)
        if t and t.get("offset_c") is not None:
            xs.append(float(t["offset_c"]))
            ys.append(statistics.mean(lst))
    if len(xs) >= 5:
        x, y = np.array(xs), np.array(ys)
        b = np.polyfit(x, y, 1)
        print(f"    {system:36s} stations {len(x):2d}: corr {np.corrcoef(x, y)[0, 1]:+.2f}; Delta_OM = {b[0]:+.2f} x terrain offset {b[1]:+.2f};"
              f" mean Delta_OM {y.mean():+.2f}, mean terrain offset {x.mean():+.2f}; sd Delta_OM {y.std():.2f}, sd of residual {np.std(y - np.polyval(b, x)):.2f}")

# (b) F' with a native constant per member
days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]


def score(label, fn):
    sub = [(r, fn(r)) for r in rows]
    sub = [(r, v) for r, v in sub if v is not None]
    same = [bucket(r["ev"]["edges"], v) == r["ev"]["choice"] for r, v in sub]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r, _v in sub]
    tr = [(r, v) for r, v in sub if r["ev"]["truth"] is not None]
    hits = statistics.mean(bucket(r["ev"]["edges"], v) == r["ev"]["truth"] for r, v in tr)
    fh = statistics.mean(r["ev"]["fb"] == r["ev"]["truth"] for r, _v in tr)
    def half(flag):
        ss = [s for s, (r, _v) in zip(same, sub) if (r["ev"]["ed"] < mid) == flag]
        bb = [b for b, (r, _v) in zip(base, sub) if (r["ev"]["ed"] < mid) == flag]
        return statistics.mean(ss) - statistics.mean(bb) if ss else float("nan")
    h1, h2 = half(True), half(False)
    print(f"    {label:58s} n {len(sub):3d}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
          f" hits {hits:.1%} vs F {fh:.1%} ({hits - fh:+.3f})")


def shifted(r, constant):
    """F + median(member + constant_m) - median(member), over members with a constant (>= 3)."""
    ev = r["ev"]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    from bayes_basket_target import listing_forecast_f as F  # noqa: PLC0415
    native = F.system_maxima(ns["conn"], station_id=ev["st"], event_date=__import__("datetime").date.fromisoformat(ev["ed"]), as_of_utc=as_of, settlement_timezone=ROSTER[ev["st"]][2])
    # Every member stays in the median; those with a constant are shifted by it.
    vals, adj, shifted_n = [], [], 0
    for system, entry in native.items():
        c = constant(ev, system)
        vals.append(float(entry["tmax_c"]))
        adj.append(float(entry["tmax_c"]) + (c or 0.0))
        shifted_n += c is not None
    if len(vals) < 3 or shifted_n == 0:
        return None
    return r["f"] + statistics.median(adj) - statistics.median(vals)


print("\n(b) F with each native member shifted by a station constant")
score("terrain offset (round 39h), every member", lambda r: shifted(r, lambda ev, s: (TERRAIN.get(s, {}).get(ev["st"]) or {}).get("offset_c")))
score("terrain offset x 0.5", lambda r: shifted(r, lambda ev, s: None if (TERRAIN.get(s, {}).get(ev["st"]) or {}).get("offset_c") is None else 0.5 * TERRAIN[s][ev["st"]]["offset_c"]))


def learned(ev, system, min_days=3):
    lst = [om - nat for r in rows if r["ev"]["st"] == ev["st"] and r["ev"]["ed"] != ev["ed"] for s_, nat, om in r["pairs"] if s_ == system]
    return statistics.mean(lst) if len(lst) >= min_days else None


score("learned Delta_OM station constant (other days), members with one", lambda r: shifted(r, learned))
# the Open-Meteo values themselves on the same members, as the ceiling of what a reproduced constant could reach
def om_direct(r):
    pr = r["pairs"]
    if len(pr) < 3:
        return None
    return r["f"] + statistics.median(om for _s, _n, om in pr) - statistics.median(n for _s, n, _o in pr)


score("Open-Meteo values themselves (same-run pairs, >= 3 members)", om_direct)
# how much of Delta_OM does the learned constant capture (station constant vs day-to-day part)?
tot, within = [], []
for (st, system), lst in per.items():
    if len(lst) >= 4:
        m = statistics.mean(lst)
        tot += lst
        within += [v - m for v in lst]
print(f"\n    Delta_OM variance split: total sd {np.std(tot):.2f} C, within-station (day-to-day) sd {np.std(within):.2f} C -> station-constant share {1 - np.var(within) / np.var(tot):.0%}")
