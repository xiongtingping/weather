import json, sqlite3, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.native_archive_lead import lead_hour_bucket
SP = "/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
tz = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
systems = ["ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_AIFS_ENS_GLOBAL", "DWD_ICON_GLOBAL_EPS", "NOAA_GEFS_05", "ECCC_GEM_GLOBAL_ENS", "NOAA_AIGEFS_025"]
t = time.time(); res = []
for st in sorted(tz):
    for s in systems:
        rows = conn.execute(
            "SELECT target_local_date, native_run_init_utc, run_cycle, extraction_version, count(*), avg(raw_tmax_c) "
            "FROM calibration_native_ensemble_historical_member_run INDEXED BY calibration_native_historical_member_scope_idx "
            "WHERE ensemble_system_id=? AND station_id=? AND target_local_date>='2026-07-24' AND target_local_date<='2026-09-24' AND qc_status='ACCEPTED' "
            "GROUP BY 1,2,3,4", (s, st)).fetchall()
        by = defaultdict(list)   # (day, cycle, bucket) -> runs
        for day, init, cyc, ev, n, mean in rows:
            if not init:
                continue
            ds = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
            ri = datetime.fromisoformat(init.replace("Z", "+00:00"))
            lead = (ds - ri).total_seconds() / 3600.0
            by[(day, cyc, lead_hour_bucket(lead))].append((lead, ev, n, mean))
        for (day, cyc, b), runs in by.items():
            if len(runs) > 1:
                res.append(dict(station=st, system=s, day=day, cycle=cyc, bucket=b, runs=[(round(l, 1), ev[-6:], n, round(m, 2)) for l, ev, n, m in sorted(runs)]))
print("elapsed", round(time.time() - t, 1), "collisions", len(res))
json.dump(res, open(SP + "lead_collisions.json", "w"))
agg = defaultdict(lambda: [0, set(), []])
for r in res:
    k = (r["system"], r["bucket"], r["cycle"])
    agg[k][0] += 1; agg[k][1].add(r["station"])
    leads = [x[0] for x in r["runs"]]
    if min(leads) < 0 <= max(leads):
        # mean difference between the same-day (negative) and previous-day run of this cycle
        neg = [x[3] for x in r["runs"] if x[0] < 0]; pos = [x[3] for x in r["runs"] if x[0] >= 0]
        agg[k][2].append(statistics.fmean(pos) - statistics.fmean(neg))
for k, (n, sts, d) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
    print(f"{k[0]:24s} {k[1]} {k[2]}: station-days {n:4d} stations {len(sts):2d}  mean(prev-day minus same-day run) {statistics.fmean(d) if d else float('nan'):+.2f}C")
