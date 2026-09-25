import math, sqlite3, statistics, sys
from datetime import date, timedelta
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_ensemble_oos as oos
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
cases_by = oos._load_cases(conn, station_id="ZUUU", as_of_utc="2026-09-25T01:23:48.250Z")
def window(key):
    cases = cases_by[key]
    hold = [c for c in cases if c.get("holdout_eligible")]
    hs = str(hold[-oos.FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS]["event_date"])
    start = (date.fromisoformat(hs[:10]) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
    return sorted((c for c in cases if start <= str(c["event_date"])), key=lambda c: str(c["event_date"]))
for key in [("NOAA_AIGEFS_025", "DESCENT", "h00_12", "12Z"), ("ECMWF_AIFS_ENS_GLOBAL", "DESCENT", "h00_12", "18Z"), ("NOAA_GEFS_05", "DESCENT", "h00_12", "18Z")]:
    S = window(key)
    oos.SINGLE_SYSTEM_FIT_RULE = oos.SINGLE_SYSTEM_FIT_RULE_MOMENT
    p = oos._fit_parameters(S)
    rows = []
    for c in S:
        v = [float(x) for x in c["values"]]; m = sum(v) / len(v); sd = statistics.pstdev(v)
        centre = m + oos._resolved_bias_c(m, p); r = float(c["actual_tmax_c"]) - centre
        rows.append((str(c["event_date"])[:10], m, sd, float(c["actual_tmax_c"]), r))
    sds = [x[2] for x in rows]; res = [x[4] for x in rows]
    rms = math.sqrt(statistics.fmean([r * r for r in res]))
    corr = statistics.correlation(sds, [abs(r) for r in res])
    z = [abs(r) / max(sd, 0.1) for (_, _, sd, _, r) in rows]
    print(f"\n{key[0]} {key[2]} {key[3]}: n={len(rows)} bias={p['bias_c']:+.2f} spread_factor={p['spread_factor']:.2f} residual_rms={rms:.2f} "
          f"member_sd median={statistics.median(sds):.2f} p10={sorted(sds)[len(sds)//10]:.2f} p90={sorted(sds)[9*len(sds)//10]:.2f} corr(|res|,sd)={corr:+.2f}")
    # split by member sd tercile: residual rms in each
    srt = sorted(rows, key=lambda x: x[2]); k = len(srt) // 3
    for name, grp in (("low-sd", srt[:k]), ("mid-sd", srt[k:2*k]), ("high-sd", srt[2*k:])):
        print(f"   {name}: member_sd {statistics.fmean([g[2] for g in grp]):.2f}  residual_rms {math.sqrt(statistics.fmean([g[4]**2 for g in grp])):.2f}  "
              f"ratio {math.sqrt(statistics.fmean([g[4]**2 for g in grp]))/statistics.fmean([g[2] for g in grp]):.2f}")
    if key[0] == "NOAA_AIGEFS_025":
        worst = sorted(rows, key=lambda x: -abs(x[4]))[:6]
        print("   worst:", [(d, round(m, 1), round(sd, 2), t, round(r, 1)) for d, m, sd, t, r in worst])
