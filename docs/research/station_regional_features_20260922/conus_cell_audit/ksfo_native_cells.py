"""KSFO HRRR, native bytes (read-only): the day maximum at the cell production
reads (eccodes nearest = a water cell in HRRR's own LAND mask) against the
nearest land cell, both against settled truth.  One 12Z run per target day
(the previous day's, lead ~19 h), the local peak window 11:00-18:59 PDT
(f30..f37), TMP 2 m by byte range; the land cell comes from the run's own
LAND:surface message.  Output: ksfo_native_cells.json."""
import json, math, os, sqlite3, sys
from datetime import date, datetime, timedelta, timezone
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import noaa_conus_archive as A

HERE = os.path.dirname(os.path.abspath(__file__))
LAT, LON = 37.61961, -122.36561
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
truth = {}
for d, rev, t in c.execute("select event_date, truth_revision_no, settled_tmax_c from basket_station_day_settlement_label where station_id='KSFO'"):
    if d not in truth or rev > truth[d][0]:
        truth[d] = (rev, float(t))
rows = []
day = date(2026, 9, 8)
while day <= date(2026, 9, 21):
    run = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - timedelta(days=1)
    land_pt = None; water, land = [], []
    for fh in range(30, 38):
        url = A.file_url("HRRR", run, fh)
        span = A._field_range(url)
        msg = A._range_get(url, span[0], span[1])
        if land_pt is None:
            lm = A.land_mask_message("HRRR", run, fh) if hasattr(A, "land_mask_message") else None
            if lm is None:
                raw = A._get(url + ".idx").decode().splitlines(); offs = [int(l.split(":")[1]) for l in raw]
                i = next(k for k, l in enumerate(raw) if ":LAND:surface:" in l)
                lm = A._range_get(url, offs[i], offs[i + 1])
            land_pt = A.nearest_land_points(lm, [(LAT, LON)])[0] if hasattr(A, "nearest_land_points") else None
        kw, dw = A.nearest_point_kelvin(msg, latitude=LAT, longitude=LON)
        water.append(kw - 273.15)
        if land_pt is not None:
            kl, dl = A.nearest_point_kelvin(msg, latitude=land_pt["latitude"], longitude=land_pt["longitude"])
            land.append(kl - 273.15)
    T = truth.get(day.isoformat(), (None, None))[1]
    rows.append(dict(day=day.isoformat(), run=run.isoformat(), truth=T, water_max=round(max(water), 2),
                     land_max=round(max(land), 2) if land else None, land_point=land_pt))
    print(rows[-1]["day"], T, rows[-1]["water_max"], rows[-1]["land_max"], flush=True)
    day += timedelta(days=1)
ok = [r for r in rows if r["truth"] is not None and r["land_max"] is not None]
ew = np.array([r["truth"] - r["water_max"] for r in ok]); el = np.array([r["truth"] - r["land_max"] for r in ok])
summary = dict(n=len(ok), water_mean_truth_minus=round(float(ew.mean()), 2), water_sd=round(float(ew.std()), 2),
               land_mean_truth_minus=round(float(el.mean()), 2), land_sd=round(float(el.std()), 2),
               water_mae=round(float(np.abs(ew).mean()), 2), land_mae=round(float(np.abs(el).mean()), 2))
print(summary)
json.dump(dict(rows=rows, summary=summary), open(os.path.join(HERE, "ksfo_native_cells.json"), "w"), indent=1)
