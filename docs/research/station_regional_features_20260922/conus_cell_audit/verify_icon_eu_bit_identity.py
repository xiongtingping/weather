"""Charter 201 verification on real bytes: re-fetch one stored ICON-EU run with
the streamed, land-ruled code and compare every station's curve with the row
the old code stored.  Expected: identical to the digit (all nine nearest
cells are land in ICON-EU's own FR_LAND)."""
import json, sqlite3, sys
from datetime import date, datetime
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import dwd_icon_eu_point_archive as I
from bayes_basket_target import regional_point_lane as L

RUN = sys.argv[1]
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
stations = {s[0]: s for s in I.CAPTURE_STATIONS}
rows = {}
for st in stations:
    for d, hj in c.execute("select target_local_date, hourly_c_json from calibration_regional_point_run indexed by calibration_regional_point_run_scope_idx "
                           "where station_id=? and ensemble_system_id='DWD_ICON_EU_POINT' and run_init_utc=? and target_local_date>='2026-09-21'", (st, RUN)):
        rows[(st, d)] = json.loads(hj)
run = datetime.fromisoformat(RUN.replace("Z", "+00:00"))
targets = []
for (st, d) in rows:
    sid, lat, lon, tz = stations[st]
    ed = date.fromisoformat(d)
    targets.append({"station_id": st, "latitude": lat, "longitude": lon, "event_date": ed,
                    "utc_offset_hours": L.utc_offset_hours(ed, tz)})
out = I.fetch_run_day_maxima(run_init=run, targets=targets)
report = {}
for key, stored in sorted(rows.items()):
    new = L.hourly_valid_map(run, out[key]["hourly_c"])
    report[f"{key[0]}|{key[1]}"] = dict(identical=new == stored, n=len(stored))
    print(key, report[f"{key[0]}|{key[1]}"])
json.dump(dict(run=RUN, report=report), open("/Users/xiong/weather/docs/research/station_regional_features_20260922/conus_cell_audit/verify_icon_eu_bit_identity.json", "w"), indent=1)
