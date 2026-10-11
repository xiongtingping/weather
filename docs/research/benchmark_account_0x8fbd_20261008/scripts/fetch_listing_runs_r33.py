"""Round 33 data: the ten deterministic models' runs for every listed station-day
2026-09-22..10-12 (the listing back-test and the bias history), into the shared
<bench>/first_runs.json cache (same layout as fetch_first_runs.py).  Runs: D-3 18Z,
D-2 00Z, and D-2 06Z / 12Z when the market was listed late enough to have them
within an hour.  Stops at the first rate-limit reply or after argv[2] seconds.
"""
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
BUDGET = float(sys.argv[2]) if len(sys.argv) > 2 else 540.0
MODELS = ["gfs_global", "icon_global", "ecmwf_ifs025", "gem_global", "ukmo_global_deterministic_10km", "ecmwf_aifs025_single",
          "meteofrance_arpege_world", "jma_gsm", "cma_grapes_global", "ncep_aigfs025"]
G = json.load(open(B / "first_gamma.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
OUT = B / "first_runs.json"
have = json.load(open(OUT)) if OUT.exists() else {}
CLIENT = httpx.Client(timeout=40.0, limits=httpx.Limits(max_connections=3, max_keepalive_connections=3))
need = {}
for k, g in G.items():
    if "error" in g or not g.get("listed"):
        continue
    st, ed = k.split("|")
    if not "2026-09-22" <= ed <= "2026-10-12" or st not in tz:
        continue
    listed = datetime.fromisoformat(g["listed"].replace("Z", "+00:00"))
    d2 = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    for run in (d2 - timedelta(hours=6), d2, d2 + timedelta(hours=6), d2 + timedelta(hours=12)):
        if run <= d2 or run + timedelta(hours=3.5) <= listed + timedelta(hours=1):
            need.setdefault(f"{st}|{run.strftime('%Y-%m-%dT%H:%M')}", set()).add(ed)
todo = [k for k in need if k not in have or "error" in have[k] or any(
    ed not in (have[k].get("tmax", {}).get(m, {}) or {}) for m in have[k].get("models", [])[:1] for ed in need[k])]
print(f"(station, run) keys {len(need)}, to fetch {len(todo)}", file=sys.stderr)


def fetch(key):
    st, run = key.split("|")
    c = station_coordinates(st)
    models = list(MODELS)
    for attempt in range(12):
        try:
            j = CLIENT.get("https://single-runs-api.open-meteo.com/v1/forecast", params={
                "latitude": c["latitude"], "longitude": c["longitude"], "hourly": "temperature_2m", "models": ",".join(models),
                "run": run, "forecast_days": 4, "timezone": tz[st]}).json()
        except Exception:
            time.sleep(2 + 2 * attempt)
            continue
        if isinstance(j, dict) and j.get("error"):
            reason = str(j.get("reason"))
            if "limit" in reason.lower() or "concurrent" in reason.lower():
                return key, {"error": "rate_limited"}
            m = re.search(r"Model: ([a-z0-9_]+)", reason)
            if m:
                bad = m.group(1)
                drop = [x for x in models if x == bad or bad.endswith(x) or x.endswith(bad.split("_", 1)[-1])]
                models = [x for x in models if x not in drop] if drop else models[:-1]
                if not models:
                    return key, {"error": "no_models"}
                continue
            return key, {"error": reason[:120]}
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
        old = have.get(key, {})
        if "tmax" in old:  # merge dates fetched before
            for m, dd in old["tmax"].items():
                out.setdefault(m, {}).update({d: v for d, v in dd.items() if d not in out.get(m, {})})
        return key, {"tmax": out, "models": models}
    return key, {"error": "retries"}


t0 = time.time()
stopped = None
with ThreadPoolExecutor(max_workers=3) as pool:
    for n, (k, v) in enumerate(pool.map(fetch, todo)):
        if v.get("error") == "rate_limited":
            stopped = "rate limited"
        elif time.time() - t0 > BUDGET:
            stopped = "time budget"
        if stopped:
            pool.shutdown(wait=False, cancel_futures=True)
            break
        have[k] = v
json.dump(have, open(OUT, "w"))
left = [k for k in need if k not in have or "error" in have[k]]
print(f"done {n + 1 if todo else 0} this run; stopped: {stopped}; keys still missing {len(left)}", file=sys.stderr)
