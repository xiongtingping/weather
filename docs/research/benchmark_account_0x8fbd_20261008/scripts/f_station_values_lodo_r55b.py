"""Round 55b: the station-value F -- honest lambda, and the extra members.

Round 55 built F on station values (GFS from its native 13 km flux grid with
the lapse-rate downscaling, the other members with the same downscaling) and
found lambda 0.75 matching the cluster 55.5% vs 45.8% -- lambda picked in
sample from three.  Here: (1) the whole lambda curve 0.3..1.0 and the lambda
chosen leave one event day out (the honest number); (2) the research F's four
extra models, where native: ARPEGE (0.25 degree, GFS-grid terrain offset) and
UKMO 10 km (GFS terrain offset as an approximation) added to the six; (3) the
same with the fleet shift re-estimated on each member set.  All as of the
trade; biases on station values over the station's settled history.
"""
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("f_station_values_r55.py").read_text()
ns = {"__name__": "r55", "__file__": str(here.with_name("f_station_values_r55.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("# ---------------------------------------------------------------- F_v3")[0], "r55_head", "exec"), ns)
rows, bucket, prod_f, ROSTER, ALL_TRUTHS, TERRAIN, cache, gfs_cells = (ns[k] for k in ("rows", "bucket", "prod_f", "ROSTER", "ALL_TRUTHS", "TERRAIN", "cache", "cells"))
conn = ns["conn"]
research_c, FRm = ns["research_c"], ns["FRm"]
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, feature_at  # noqa: E402

GFS_ID = "NOAA_GFS_025_NATIVE_POINT"
EXTRA = {"METEOFRANCE_ARPEGE_025_NATIVE_POINT": "ARPEGE", "UKMO_GLOBAL_10KM_NATIVE_POINT": "UKMO"}
SETS = {"six": sorted(F_SYSTEM_IDS), "six + ARPEGE": sorted(F_SYSTEM_IDS) + ["METEOFRANCE_ARPEGE_025_NATIVE_POINT"],
        "six + UKMO": sorted(F_SYSTEM_IDS) + ["UKMO_GLOBAL_10KM_NATIVE_POINT"], "six + ARPEGE + UKMO": sorted(F_SYSTEM_IDS) + list(EXTRA)}
LAMS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0]


def gfs_v3(st, ed):
    run = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    return cache.get(run.strftime("%Y-%m-%dT%H:%MZ"), {}).get("values", {}).get(st, {}).get(ed)


const = {}
for key, entry in cache.items():
    if key.startswith("_"):
        continue
    run = datetime.strptime(key, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    for st, days_ in entry["values"].items():
        for day, v in days_.items():
            nat = conn.execute("SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id=? AND station_id=? AND target_local_date=?"
                               " AND run_init_utc=? ORDER BY archive_query_at_utc DESC LIMIT 1", (GFS_ID, st, day, run.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
            if nat:
                const.setdefault(st, []).append(v - float(nat[0]))
const = {st: statistics.mean(v) for st, v in const.items() if len(v) >= 3}


def station_value(system, st, raw):
    if system == GFS_ID:
        return raw + const.get(st, 0.0)
    table = TERRAIN.get(system) or TERRAIN.get(GFS_ID, {})  # ARPEGE shares the GFS grid; UKMO approximated by it
    t = table.get(st)
    return raw + (float(t["offset_c"]) if t and t.get("offset_c") is not None else 0.0)


_HIST = {}


def history_point(st, past, zone, systems):
    key = (st, past, tuple(systems))
    if key not in _HIST:
        feats = feature_at(conn, station_id=st, event_date=past.isoformat(), as_of_utc=F.listing_instant(past),
                           settlement_timezone=zone, systems=list(systems))
        _HIST[key] = {s: float(f["day_max_c"]) for s, f in feats.items()}
    return _HIST[key]


_C = {}


def fleet_c(D, systems):
    key = (D, tuple(systems))
    if key not in _C:
        errs = []
        for k in range(2, 32):
            past = D - timedelta(days=k)
            for st, (_lat, _lon, zone) in ROSTER.items():
                tr = ALL_TRUTHS.get((st, past.isoformat()))
                if tr is None:
                    continue
                pt = history_point(st, past, zone, systems)
                if len(pt) >= 3:
                    errs.append(tr - statistics.median(station_value(s, st, v) for s, v in pt.items()))
        _C[key] = statistics.mean(errs) if len(errs) >= 200 else 0.8
    return _C[key]


def parts(r, systems):
    """(raw median, c, corr) on station values for one pick and member set."""
    ev = r["ev"]
    st, ed = ev["st"], ev["ed"]
    zone = ROSTER[st][2]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    feats = feature_at(conn, station_id=st, event_date=ed, as_of_utc=as_of, settlement_timezone=zone, systems=list(systems))
    if len(feats) < 3:
        return None
    D = date.fromisoformat(ed)
    run_key = (D - timedelta(days=2)).strftime("%Y-%m-%dT%H")
    members = {}
    for s, f in feats.items():
        v = float(f["day_max_c"])
        if s == GFS_ID and gfs_v3(st, ed) is not None and str(f["run_init_utc"])[:13] == run_key:
            members[s] = gfs_v3(st, ed)
        else:
            members[s] = station_value(s, st, v)
    raw = statistics.median(members.values())
    biases = {}
    for s in members:
        errs = []
        for k in range(2, 61):
            past = D - timedelta(days=k)
            tr = ALL_TRUTHS.get((st, past.isoformat()))
            if tr is None:
                continue
            pt = history_point(st, past, zone, systems)
            if s in pt:
                errs.append(tr - station_value(s, st, pt[s]))
        biases[s] = statistics.mean(errs) if len(errs) >= 3 else 0.0
    corr = statistics.median(members[s] + biases[s] for s in members) - raw
    return raw, fleet_c(D, systems), corr, len(members)


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
print(f"{len(rows)} picks; production F = cluster {statistics.mean(r['ev']['fb'] == r['ev']['choice'] for r in rows):.1%}, hits {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r in rows if r['ev']['truth'] is not None):.1%}")
for set_name, systems in SETS.items():
    P = {}
    for r in rows:
        p = parts(r, systems)
        if p:
            P[id(r)] = p
    sub = [r for r in rows if id(r) in P]
    print(f"\n== members {set_name}: n {len(sub)}, members per pick {statistics.mean(P[id(r)][3] for r in sub):.1f}, c on station values {statistics.mean(P[id(r)][1] for r in sub):+.2f}")
    print(f"   {'lambda':>6s} {'=cluster':>9s} {'vs F':>24s} {'halves':>14s} {'hits':>6s} {'vs F':>7s}")
    per_lam = {}
    for lam in LAMS:
        vals = {id(r): P[id(r)][0] + P[id(r)][1] + lam * P[id(r)][2] for r in sub}
        per_lam[lam] = vals
        same = [bucket(r["ev"]["edges"], vals[id(r)]) == r["ev"]["choice"] for r in sub]
        base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
        tr = [r for r in sub if r["ev"]["truth"] is not None]
        hits = statistics.mean(bucket(r["ev"]["edges"], vals[id(r)]) == r["ev"]["truth"] for r in tr)
        fh = statistics.mean(r["ev"]["fb"] == r["ev"]["truth"] for r in tr)
        h1 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] < mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] < mid)
        h2 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] >= mid)
        print(f"   {lam:6.2f} {statistics.mean(same):9.1%} {paired(same, base):>24s} {h1:+.3f}/{h2:+.3f}{'**' if h1 > 0 and h2 > 0 else '  '} {hits:6.1%} {hits - fh:+.3f}")
    # LODO over lambda
    picks, chosen = [], {}
    for r in sub:
        tr_ = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
        best = max(LAMS, key=lambda l: (sum(bucket(x["ev"]["edges"], per_lam[l][id(x)]) == x["ev"]["choice"] for x in tr_), -abs(l - 0.5)))
        chosen[best] = chosen.get(best, 0) + 1
        picks.append(bucket(r["ev"]["edges"], per_lam[best][id(r)]))
    same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    trp = [(p, r) for p, r in zip(picks, sub) if r["ev"]["truth"] is not None]
    h1 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] < mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] < mid)
    h2 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] >= mid)
    print(f"   LODO lambda: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
          f" hits {statistics.mean(p == r['ev']['truth'] for p, r in trp):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for _p, r in trp):.1%}; chosen {dict(sorted(chosen.items(), key=lambda kv: -kv[1]))}")
    # how much of the research F's offset this set reproduces
    s1 = [r for r in sub if (r["ev"]["st"], r["ev"]["ed"]) in FRm and "1 h" in r["win"]]
    rc = {}
    for r in s1:
        try:
            rc[id(r)] = research_c(FRm[(r["ev"]["st"], r["ev"]["ed"])], r["ev"]["ts"])
        except Exception:  # noqa: BLE001
            pass
    s1 = [r for r in s1 if rc.get(id(r)) is not None]
    x = np.array([per_lam[0.75][id(r)] - r["f"] for r in s1])
    y = np.array([rc[id(r)] - r["f"] for r in s1])
    z = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in s1])
    A = np.c_[x, np.ones(len(x))]
    b = np.linalg.lstsq(A, z, rcond=None)[0]
    print(f"   lambda 0.75: corr with (research F - F) {np.corrcoef(x, y)[0, 1]:.2f}; money centre - F = {b[0]:+.2f} x (F_v3 - F) {b[1]:+.2f}, R^2 {1 - (z - A @ b).var() / z.var():.3f}")
