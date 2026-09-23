"""Station regional features, step 1: run-resolved point forecasts from the
Open-Meteo single-runs endpoint (the same endpoint the CMA lane reads in
production), one request per (model, run) for all requested stations.

Why this source for the research backfill: every value is addressed by its
run, so a row can honestly say when it became knowable (run init + the
model's publication lag, applied later in build_features.py).  The
"historical forecast" archive is NOT used -- its series is not resolved by
run (cma_grapes_point_archive docstring; regional-hires Phase 0 lesson).

Raw responses are cached under the scratchpad (never a production dir); the
derived per-run local-day maxima are written by build_features.py.
Paced (one request at a time, --sleep between) so the production CMA lane
that shares this endpoint is never starved.  Resumable; bounded by
--max-seconds.

usage: fetch_single_runs.py MODEL CYCLES STATIONS START END [--hours 96]
       [--max-seconds 540] [--sleep 0.6]
"""
import argparse, json, os, sys, time
from datetime import datetime, timedelta, timezone
import httpx

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/srf/cache"
BASE = "https://single-runs-api.open-meteo.com/v1/forecast"
COORD = {  # station_registry._COORDINATES
    "SBGR": (-23.432, -46.469), "ZUUU": (30.576, 103.95), "KSEA": (47.44467, -122.31442),
    "FACT": (-33.965, 18.602), "KSFO": (37.61961, -122.36561), "LTAC": (40.128, 32.995),
}

ap = argparse.ArgumentParser()
ap.add_argument("model"); ap.add_argument("cycles"); ap.add_argument("stations")
ap.add_argument("start"); ap.add_argument("end")
ap.add_argument("--hours", type=int, default=96)
ap.add_argument("--max-seconds", type=float, default=540)
ap.add_argument("--sleep", type=float, default=0.6)
a = ap.parse_args()
cycles = [int(x) for x in a.cycles.split(",")]
stations = a.stations.split(",")
d0 = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
d1 = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
out_dir = os.path.join(SP, a.model)
os.makedirs(out_dir, exist_ok=True)
client = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
t0 = time.time(); n_req = n_ok = n_na = n_err = n_skip = 0
day = d0
runs = []
while day <= d1:
    for cy in cycles:
        runs.append(day + timedelta(hours=cy))
    day += timedelta(days=1)
tag = "-".join(stations)
for run in runs:
    if time.time() - t0 > a.max_seconds:
        break
    fn = os.path.join(out_dir, f"{tag}_{run:%Y%m%dT%H}.json")
    if os.path.exists(fn):
        n_skip += 1
        continue
    params = {"latitude": ",".join(str(COORD[s][0]) for s in stations),
              "longitude": ",".join(str(COORD[s][1]) for s in stations),
              "hourly": "temperature_2m", "models": a.model, "run": run.strftime("%Y-%m-%dT%H:%M"),
              "forecast_hours": a.hours, "timezone": "UTC"}
    for attempt in range(4):
        n_req += 1
        try:
            r = client.get(BASE, params=params)
            try:
                js = r.json()
            except ValueError:
                js = {}
            reason = str(js.get("reason") or "") if isinstance(js, dict) else ""
            if "not available" in reason:
                json.dump({"not_available": reason, "fetched_at": datetime.now(timezone.utc).isoformat()}, open(fn, "w"))
                n_na += 1
                break
            if "Too many" in reason or r.status_code == 429:
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code} {reason[:120]}")
            items = js if isinstance(js, list) else [js]
            payload = {"model": a.model, "run": run.isoformat(), "stations": stations,
                       "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "points": [{"station": s, "lat": it.get("latitude"), "lon": it.get("longitude"),
                                   "elevation": it.get("elevation"),
                                   "time": it["hourly"]["time"], "t2m": it["hourly"]["temperature_2m"]}
                                  for s, it in zip(stations, items)]}
            json.dump(payload, open(fn, "w"))
            n_ok += 1
            break
        except Exception as e:  # transport outage (proxy egress drop): back off, retry
            if attempt == 3:
                n_err += 1
                print("ERR", run, repr(e)[:160], flush=True)
            time.sleep(5 * (attempt + 1))
    time.sleep(a.sleep)
print(f"{a.model} {tag}: runs {len(runs)} skip {n_skip} ok {n_ok} not_available {n_na} err {n_err} requests {n_req} "
      f"{time.time() - t0:.0f}s", flush=True)
