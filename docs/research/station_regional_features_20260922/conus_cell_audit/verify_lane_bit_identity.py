"""Charter 200 verification on real bytes: re-fetch one stored HRRR run with the
new lane code and compare every station's hourly curve with the row the old
code stored.  Expected: identical to the digit at every station but KSFO."""
import json, sqlite3, sys
from datetime import date, datetime, timezone
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import noaa_conus_archive as A
from bayes_basket_target import regional_point_lane as L

RUN = sys.argv[1] if len(sys.argv) > 1 else "2026-09-21T12:00:00Z"
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
stations = {s[0]: s for s in A.CAPTURE_STATIONS["HRRR"]}
rows = {}
for st in stations:
    for d, hj in c.execute("select target_local_date, hourly_c_json from calibration_regional_point_run indexed by calibration_regional_point_run_scope_idx "
                           "where station_id=? and ensemble_system_id='NCEP_HRRR_POINT' and run_init_utc=? and target_local_date>='2026-09-20'", (st, RUN)):
        rows[(st, d)] = json.loads(hj)
run = datetime.fromisoformat(RUN.replace("Z", "+00:00"))
targets = []
for (st, d) in rows:
    sid, lat, lon, tz = stations[st]
    ed = date.fromisoformat(d)
    targets.append({"station_id": st, "latitude": lat, "longitude": lon, "event_date": ed,
                    "utc_offset_hours": L.utc_offset_hours(ed, tz), "settlement_timezone": tz})
out = L.fetch_noaa_run_for_targets(product="HRRR", run_init=run, targets=targets)
report = {}
for key, stored in sorted(rows.items()):
    new = L.hourly_valid_map(run, out[key]["hourly_c"])
    same = {k: stored.get(k) for k in new} == new and set(stored) <= set(new)
    diff = [round(new[k] - stored[k], 3) for k in stored if k in new]
    report[f"{key[0]}|{key[1]}"] = dict(identical=same, n=len(stored), max_abs_diff=max(abs(x) for x in diff) if diff else None,
                                        mean_diff=round(sum(diff) / len(diff), 3) if diff else None,
                                        source_url=out[key]["source_url"])
    print(key, report[f"{key[0]}|{key[1]}"])
json.dump(dict(run=RUN, report=report), open("/Users/xiong/weather/docs/research/station_regional_features_20260922/conus_cell_audit/verify_lane_bit_identity.json", "w"), indent=1)
