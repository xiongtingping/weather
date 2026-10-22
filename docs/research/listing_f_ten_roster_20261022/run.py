#!/usr/bin/env python3
"""User 10-22 「267也改为十家」: round 75's chain (listing_forecast_f_mos: L1 station values,
L2 shrunk station bias, L4 shrunk inverse-MAE) on the six charter-267 systems against the
ten charter-279 systems, every settled station-day read as of its own listing instant
(D-2 05:00Z), the chain on the history settled by then -- exactly as production builds F.
Relays (IFS 9 km, UKMO, CMA through Open-Meteo) are point values at the station's elevation:
offset 0.  UKMO native has no terrain cell: dropped (production's fail-closed rule), and run
once more with offset 0 as an arm.  Scored on station-days where both rosters give >= 3 members."""
import json, os, statistics, sys, random, sqlite3, time
from datetime import date, timedelta, datetime, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import listing_forecast_f as L, listing_forecast_f_mos as mos, listing_forecast_f_terrain as terrain
from bayes_basket_target.regional_point_lane import SYSTEM_IDS
from bayes_basket_target.deterministic_forecast_family import global_roster
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "run.out"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
SIX = sorted(SYSTEM_IDS[p] for p in ("AIFS", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL"))
RELAY = sorted(SYSTEM_IDS[p] for p in ("IFS9KM", "UKMO10", "CMA"))
UKN = SYSTEM_IDS["UKMO_GLOBAL"]
TEN = sorted(set(SIX) | set(RELAY) | {UKN})
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60); conn.execute("PRAGMA query_only=1")
table = terrain.read() if hasattr(terrain, "read") else json.load(open(terrain.TERRAIN_PATH))
roster = global_roster(conn)
today = datetime.now(timezone.utc).date()
since, until = today - timedelta(days=mos.HISTORY_DAYS), today - timedelta(days=1)
truths = L.formal_truths(conn, since=since, until=until)
t0 = time.time(); base = []
for station, _lat, _lon, zone in roster:
    day = since
    while day <= until:
        truth = truths.get((station, day.isoformat()))
        if truth is not None:
            mx = L.system_maxima(conn, station_id=station, event_date=day, as_of_utc=L.listing_instant(day), settlement_timezone=zone, systems=TEN)
            raw = {s: float(v["tmax_c"]) for s, v in mx.items()}
            base.append({"station": station, "day": day, "truth": float(truth), "zone": zone,
                         "settled": L._settled_at(day, zone), "listing": L._listing_dt(day), "raw": raw})
        day += timedelta(days=1)
P(f"history {len(base)} station-days {since}..{until} in {time.time()-t0:.0f}s")
def xst_for(raw, station, roster_ids, ukmo_zero=False):
    out = {}
    for s in roster_ids:
        if s not in raw: continue
        if s in RELAY or (ukmo_zero and s == UKN): out[s] = raw[s]; continue
        off = terrain.offset_c(table, system_id=s, station_id=station)
        if off is not None: out[s] = raw[s] + off
    return out
ARMS = {"six_267": (SIX, False), "ten_relay0": (TEN, False), "ten_relay0_ukmo0": (TEN, True)}
res = {}
for arm, (ids, uz) in ARMS.items():
    rows = [dict(r, xst=xst_for(r["raw"], r["station"], ids, uz)) for r in base]
    rows = [r for r in rows if r["xst"]]
    chain = mos.Chain(rows)
    res[arm] = {}
    for r in rows:
        if r["day"] < date(2026, 10, 1): continue   # every system captured and the chain history filled
        c = chain.centre(station=r["station"], day=r["day"], xst=r["xst"])
        if c: res[arm][(r["station"], r["day"])] = (c["mos_c"], r["truth"], len(r["xst"]))
    P(f"{arm}: scored {len(res[arm])}")
common = set.intersection(*[set(v) for v in res.values()])
P(f"common station-days {len(common)} over {len({k[1] for k in common})} days")
def bucket_hit(f, t): return round(f) == round(t)   # 1C rung proxy; F stations differ, reported as a proxy only
def stats(arm, keys):
    e = [res[arm][k][0] - res[arm][k][1] for k in keys]
    return statistics.mean(abs(x) for x in e), statistics.mean(e), sum(1 for k in keys if bucket_hit(res[arm][k][0], res[arm][k][1])) / len(keys)
keys = sorted(common)
for arm in ARMS:
    mae, bias, hit = stats(arm, keys)
    P(f"  {arm:18s} MAE {mae:.3f}  bias {bias:+.3f}  round-hit {hit:.1%}  members median {statistics.median(res[arm][k][2] for k in keys)}")
days = sorted({k[1] for k in keys})
for arm in ("ten_relay0", "ten_relay0_ukmo0"):
    d = {}
    for k in keys: d.setdefault(k[1], []).append(abs(res[arm][k][0] - res[arm][k][1]) - abs(res["six_267"][k][0] - res["six_267"][k][1]))
    rng = random.Random(267); bs = sorted(statistics.mean([x for dd in (rng.choice(days) for _ in days) for x in d[dd]]) for _ in range(1000))
    half = len(days) // 2
    h1 = statistics.mean(x for dd in days[:half] for x in d[dd]); h2 = statistics.mean(x for dd in days[half:] for x in d[dd])
    P(f"  {arm} − six MAE {statistics.mean(x for v in d.values() for x in v):+.4f} [{bs[25]:+.4f},{bs[974]:+.4f}]  halves {h1:+.4f} / {h2:+.4f}  days better {sum(1 for v in d.values() if statistics.mean(v) < 0)}/{len(days)}")
out.close()
