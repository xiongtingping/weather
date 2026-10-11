"""Round 29 (熊先生 10-11: direction = model momentum, TAF, and the gap between
observed and forecast cloud / rain / radiation on the day): the forecast side
and the satellite side for every station, 2026-09-11..10-10.

Forecast: Open-Meteo previous-runs (aggregator_republisher; research only),
hourly *_previous_day1 -- what the run of the day before said for each hour:
temperature_2m, cloud_cover, cloud_cover_low, shortwave_radiation,
precipitation.  Satellite: Open-Meteo's satellite archive (measured
shortwave), the product by longitude as the station board picks it; none west
of 60W.  One request per station each.  Writes <bench>/obsfc.json.
"""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.station_day_board import _satellite_product  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest, station_coordinates  # noqa: E402

B = Path(sys.argv[1])
START, END = "2026-09-11", "2026-10-10"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
FIELDS = ["temperature_2m", "cloud_cover", "cloud_cover_low", "shortwave_radiation", "precipitation"]


def get(url):
    for i in range(5):
        r = subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            if isinstance(j, dict) and j.get("error"):
                return {"error": str(j.get("reason"))[:200]}
            return j
        except Exception:
            time.sleep(3 + 3 * i)
    return {"error": "fetch_failed"}


out_path = B / "obsfc.json"
out = json.load(open(out_path)) if out_path.exists() else {}
t0 = time.time()
for st in coordinate_registry_manifest()["station_ids"]:
    if st not in tz or (st in out and "error" not in out[st].get("fc", {})):
        continue
    c = station_coordinates(st)
    lat, lon, zone = c["latitude"], c["longitude"], tz[st]
    q = urlencode({"latitude": lat, "longitude": lon, "timezone": zone, "start_date": START, "end_date": END,
                   "hourly": ",".join(f + "_previous_day1" for f in FIELDS)})
    fc = get(f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}")
    product = _satellite_product(lon)
    sat = {"product": None}
    if product:
        q2 = urlencode({"latitude": lat, "longitude": lon, "timezone": zone, "start_date": START, "end_date": END,
                        "hourly": "shortwave_radiation", "models": product})
        s = get(f"https://satellite-api.open-meteo.com/v1/archive?{q2}")
        sat = {"product": product, "time": (s.get("hourly") or {}).get("time"),
               "sw": (s.get("hourly") or {}).get("shortwave_radiation"), "error": s.get("error")}
    out[st] = {"tz": zone, "lat": lat, "lon": lon,
               "fc": {"time": (fc.get("hourly") or {}).get("time"),
                      **{f: (fc.get("hourly") or {}).get(f + "_previous_day1") for f in FIELDS}, "error": fc.get("error")},
               "sat": sat}
    print(st, "fc", len(out[st]["fc"]["time"] or []), "sat", product, len(sat.get("time") or []), round(time.time() - t0), "s",
          file=sys.stderr)
    json.dump(out, open(out_path, "w"))
print("stations", len(out), file=sys.stderr)
