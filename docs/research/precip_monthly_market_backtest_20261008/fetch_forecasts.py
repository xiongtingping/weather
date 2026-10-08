"""Open-Meteo previous-runs precipitation at the two settlement sites.

best_match is what production's forecast_physics lane requests.  For each
valid hour, ``precipitation_previous_dayN`` is the value the run issued about
24*N hours earlier predicted, so it can be cut strictly at a decision time.
"""
import json, subprocess, time, pathlib

HERE = pathlib.Path(__file__).resolve().parent
SITES = {  # ACIS threaded records the markets resolve on
    "SEW": (47.6872, -122.2553),  # Seattle City Area = Sand Point WFO since 1986
    "NYC": (40.7789, -73.9692),   # Central Park
}
VARS = ",".join(["precipitation"] + [f"precipitation_previous_day{n}" for n in range(1, 8)])
RANGES = [("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31"), ("2026-01-01", "2026-10-14")]

for site, (lat, lon) in SITES.items():
    for a, b in RANGES:
        url = (f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&hourly={VARS}&start_date={a}&end_date={b}&timezone=GMT")
        out = HERE / "raw" / f"prevruns_{site}_{a[:4]}.json"
        subprocess.run(["curl", "-s", "-m", "180", url, "-o", str(out)], check=True)
        d = json.load(open(out))
        h = d["hourly"]
        print(site, a, h["time"][0], h["time"][-1], len(h["time"]),
              [sum(x is not None for x in h[k]) for k in h if k != "time"], d["latitude"], d["longitude"])
        time.sleep(2)
