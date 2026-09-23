"""Station regional features, step 3: per-run local-day maxima from the cached
single-run responses, stamped with an honest knowable time, and a fidelity
check of the backfill against the production lanes' own forward rows.

A (model, run, station, local_date) row exists only when the run holds all
24 hourly values of that local day (no partial days: "breakfast is not the
day's high").  knowable_at = run_init + LAG[model]; LAG is the production
lane's measured publication delay where a lane exists (calibration_regional_
point_run knowable_at - run_init, p50..p90) and a conservative ECMWF/UKMO
dissemination delay otherwise.  analyze.py re-runs everything with +6 h.

Output: runs.json (committed with the study; derived numbers only).
"""
import collections, glob, json, os, sqlite3, sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/srf/cache"
HERE = os.path.dirname(os.path.abspath(__file__))
TZ = {"SBGR": "America/Sao_Paulo", "ZUUU": "Asia/Shanghai", "KSEA": "America/Los_Angeles",
      "FACT": "Africa/Johannesburg", "KSFO": "America/Los_Angeles", "LTAC": "Europe/Istanbul"}
# hours after init at which a production capture can hold the run
LAG = {"cma_grapes_global": 11.0,        # aggregator holds CMA 10-11 h after init (cma lane docstring, 09-19)
       "icon_eu": 5.0,                   # DWD lane p10 4.4 / p50 6.6 h
       "ncep_hrrr_conus": 2.5,           # NOAA S3 lane p50 2.2 h
       "ncep_nbm_conus": 2.5,            # NOAA S3 lane p50 2.4 h
       "ecmwf_ifs": 8.0, "ecmwf_ifs025": 8.0, "ukmo_global_deterministic_10km": 8.0, "ecmwf_aifs025_single": 8.0}

rows = []
for model in sorted(os.listdir(SP)):
    for fn in sorted(glob.glob(os.path.join(SP, model, "*.json"))):
        js = json.load(open(fn))
        if "points" not in js:
            continue
        run = datetime.fromisoformat(js["run"])
        for p in js["points"]:
            st = p["station"]; tz = ZoneInfo(TZ[st])
            series = {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v for t, v in zip(p["time"], p["t2m"]) if v is not None}
            if not series:
                continue
            last = max(series)
            loc0 = run.astimezone(tz).date()
            for k in range(0, 6):
                d = loc0 + timedelta(days=k)
                start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
                end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
                if start < run or end - timedelta(hours=1) > last:
                    continue
                hrs = []
                t = start
                while t < end:
                    hrs.append(series.get(t)); t += timedelta(hours=1)
                if any(v is None for v in hrs):
                    continue
                rows.append(dict(model=model, st=st, run=run.strftime("%Y-%m-%dT%H:%MZ"), date=d.isoformat(),
                                 tmax=round(max(hrs), 2), lead_to_day_start_h=round((start - run).total_seconds() / 3600, 1),
                                 knowable=(run + timedelta(hours=LAG[model])).strftime("%Y-%m-%dT%H:%MZ"),
                                 elev=p.get("elevation")))
                if model == "ecmwf_aifs025_single":
                    # what the official open-data stream carries: 2t at 6-hourly steps only (no hourly, no max field)
                    six = [series[t_] for t_ in series if start <= t_ < end and t_.hour % 6 == 0]
                    rows.append(dict(model=model + "_6h", st=st, run=run.strftime("%Y-%m-%dT%H:%MZ"), date=d.isoformat(),
                                     tmax=round(max(six), 2), lead_to_day_start_h=round((start - run).total_seconds() / 3600, 1),
                                     knowable=(run + timedelta(hours=LAG[model])).strftime("%Y-%m-%dT%H:%MZ"),
                                     elev=p.get("elevation")))
print("rows", len(rows), collections.Counter((r["model"], r["st"]) for r in rows))

# fidelity: backfill vs the production lanes' forward rows (same run, same local date, whole day ahead)
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
MAP = {"cma_grapes_global": "CMA_GRAPES_GLOBAL_POINT", "icon_eu": "DWD_ICON_EU_POINT",
       "ncep_hrrr_conus": "NCEP_HRRR_POINT", "ncep_nbm_conus": "NCEP_NBM_POINT"}
idx = {(r["model"], r["st"], r["run"], r["date"]): r["tmax"] for r in rows}
fid = {}
for model, sysid in MAP.items():
    for st in TZ:
        diffs = []
        for run_init, d, tm, ev, hw, hc in c.execute(
                "select run_init_utc, target_local_date, forecast_tmax_c, extraction_version, hours_wanted, hours_covered "
                "from calibration_regional_point_run where station_id=? and ensemble_system_id=? and target_local_date<='2026-09-21' "
                "and lead_hours_to_local_day_start>=0", (st, sysid)):
            if "derived_cycle" in ev or hw != hc:      # CMA v1/v2 rows carry a mislabelled run (cma docstring)
                continue
            key = (model, st, run_init.replace(":00Z", "Z").replace("T", "T")[:16] + "Z", d)
            key = (model, st, datetime.fromisoformat(run_init.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%MZ"), d)
            if key in idx:
                diffs.append(idx[key] - float(tm))
        if diffs:
            diffs.sort(); n = len(diffs)
            mean = sum(diffs) / n
            sd = (sum((x - mean) ** 2 for x in diffs) / n) ** 0.5
            fid[f"{model}|{st}"] = dict(n=n, mean_backfill_minus_lane=round(mean, 3), sd=round(sd, 3),
                                        p05=round(diffs[n // 20], 2), p95=round(diffs[min(n - 1, 19 * n // 20)], 2))
print(json.dumps(fid, indent=1))
json.dump(dict(lag_hours=LAG, rows=rows, fidelity_vs_production_lane=fid), open(os.path.join(HERE, "runs.json"), "w"))
