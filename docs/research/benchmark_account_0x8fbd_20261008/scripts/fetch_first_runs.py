"""Round 32 data: the deterministic forecasts the cluster could have had at its
first fill.  Open-Meteo single-runs (aggregator_republisher; research only):
for every station-day in first_rows.json.gz, the runs D-3 18Z and D-2 00Z (plus
D-2 06Z and 12Z when the first fill came after 09Z), ten global models in one
request, hourly 2 m temperature; stored as the local-day maximum of the event
date.  A model missing for a run is dropped and the request retried.
Writes <bench>/first_runs.json; resumable.  --early-only: only station-days whose
first fill came before the market formed.  Stops at the first rate-limit reply.
"""
import gzip
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

B = Path(sys.argv[1])
MODELS = ["gfs_global", "icon_global", "ecmwf_ifs025", "gem_global", "ukmo_global_deterministic_10km", "ecmwf_aifs025_single",
          "meteofrance_arpege_world", "jma_gsm", "cma_grapes_global", "ncep_aigfs025"]
R = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
OUT = B / "first_runs.json"
have = json.load(open(OUT)) if OUT.exists() else {}
CLIENT = httpx.Client(timeout=40.0, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))

EARLY_ONLY = "--early-only" in sys.argv


def formed(r):
    vals = list(r["now"].values())
    return all(v is not None for v in vals) and 0.7 <= sum(vals) <= 1.4


need = {}
for r in R:
    if EARLY_ONLY and formed(r):
        continue
    ed = datetime.fromisoformat(r["ed"]).replace(tzinfo=timezone.utc)
    d2 = ed - timedelta(days=2)
    runs = [d2 - timedelta(hours=6), d2]
    if datetime.fromtimestamp(r["ts"], timezone.utc).hour >= 9:
        runs += [d2 + timedelta(hours=6), d2 + timedelta(hours=12)]
    for run in runs:
        key = f"{r['st']}|{run.strftime('%Y-%m-%dT%H:%M')}"
        need.setdefault(key, set()).add(r["ed"])
todo = [k for k in need if k not in have or "error" in have[k]]
print(f"(station, run) requests {len(need)}, to fetch {len(todo)}", file=sys.stderr)


def fetch(key):
    st, run = key.split("|")
    c = station_coordinates(st)
    models = list(MODELS)
    for attempt in range(12):
        try:
            r = CLIENT.get("https://single-runs-api.open-meteo.com/v1/forecast", params={
                "latitude": c["latitude"], "longitude": c["longitude"], "hourly": "temperature_2m", "models": ",".join(models),
                "run": run, "forecast_days": 4, "timezone": tz[st]})
            j = r.json()
        except Exception:
            time.sleep(2 + 2 * attempt)
            continue
        if isinstance(j, dict) and j.get("error"):
            m = re.search(r"Model: ([a-z0-9_]+)", str(j.get("reason")))
            if m:
                bad = m.group(1)
                drop = [x for x in models if x == bad or bad.endswith(x) or x.endswith(bad.split("_", 1)[-1])]
                models = [x for x in models if x not in drop] if drop else models[:-1]
                if not models:
                    return key, {"error": "no_models"}
                continue
            if "limit" in str(j.get("reason")).lower():
                return key, {"error": "rate_limited"}
            return key, {"error": str(j.get("reason"))[:120]}
        h = j.get("hourly") or {}
        out = {}
        for m in models:
            col = h.get(f"temperature_2m_{m}") if len(models) > 1 else h.get("temperature_2m")
            if not col:
                continue
            for ed in need[key]:
                vals = [v for t, v in zip(h["time"], col) if t.startswith(ed) and v is not None]
                if len(vals) >= 20:
                    out.setdefault(m, {})[ed] = max(vals)
        return key, {"tmax": out, "models": models}
    return key, {"error": "retries"}


t0 = time.time()
with ThreadPoolExecutor(max_workers=3) as pool:
    for n, (k, v) in enumerate(pool.map(fetch, todo)):
        have[k] = v
        if time.time() - t0 > 540:
            print("time budget reached -- stopping; rerun to continue", file=sys.stderr)
            pool.shutdown(wait=False, cancel_futures=True)
            break
        if v.get("error") == "rate_limited":
            print("rate limited -- stopping; rerun later", file=sys.stderr)
            pool.shutdown(wait=False, cancel_futures=True)
            break
        if (n + 1) % 300 == 0:
            json.dump(have, open(OUT, "w"))
            print(n + 1, round(time.time() - t0), "s", file=sys.stderr)
json.dump(have, open(OUT, "w"))
ok = sum(1 for v in have.values() if "error" not in v)
print(f"ok {ok} of {len(have)}; errors {sorted({v['error'] for v in have.values() if 'error' in v})[:5]}", file=sys.stderr)
