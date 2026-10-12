"""Round 49 data (熊先生 10-12 「预报带的云雨、辐射等物理信息呢」).

Forecast physics per station x local day as a bot could read it around the
listing: Open-Meteo previous-runs (aggregator, research only, never P0/P1),
models ECMWF IFS 0.25 and GFS, hourly cloud_cover, shortwave_radiation and
precipitation at ``previous_day2`` (~48 h before each hour: around the
listing, later for western afternoons) and ``previous_day3`` (~72 h: strictly
before), at the station's coordinates in its settlement timezone,
2026-09-18..2026-10-14.  Daily aggregates per model x lead: daytime (09-17
local) mean cloud %, daytime mean shortwave W/m2, day total precipitation mm.
Every request is charged to the production quota ledger's research lane
first (weight 4: two models, > 2 weeks).  Writes ``<bench>/physics_r49.json``.
"""
import json
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

B = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).parent))
import om_research_quota as omq  # noqa: E402

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
ROSTER = {st: (lat, lon, zone) for st, lat, lon, zone in global_roster(conn)}
OUT = B / "physics_r49.json"
have = json.load(open(OUT)) if OUT.exists() else {}
MODELS = ("ecmwf_ifs025", "gfs_seamless")
VARS = ("cloud_cover", "shortwave_radiation", "precipitation")
LEADS = ("previous_day2", "previous_day3")
t0 = time.time()
for st, (lat, lon, zone) in sorted(ROSTER.items()):
    if st in have:
        continue
    if time.time() - t0 > 520:
        break
    omq.charge(4)
    url = "https://previous-runs-api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": zone, "start_date": "2026-09-18", "end_date": "2026-10-14",
        "models": ",".join(MODELS), "hourly": ",".join(f"{v}_{l}" for v in VARS for l in LEADS)})
    j = None
    for attempt in range(4):
        r = subprocess.run(["curl", "-s", "-m", "90", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            break
        except ValueError:
            time.sleep(3 + 3 * attempt)
    if not j or j.get("error"):
        if j and omq.on_reply(j) == "day":
            print("open-meteo day limit", file=sys.stderr)
            break
        print(st, "failed", str(j)[:160], file=sys.stderr)
        continue
    h = j["hourly"]
    times = h["time"]
    out = {}
    for m in MODELS:
        for l in LEADS:
            daily = {}
            for v in VARS:
                col = h.get(f"{v}_{l}_{m}")
                if col is None:
                    continue
                for t, x in zip(times, col):
                    if x is None:
                        continue
                    d, hour = t[:10], int(t[11:13])
                    rec = daily.setdefault(d, {"cloud": [], "sw": [], "precip": []})
                    if v == "precipitation":
                        rec["precip"].append(x)
                    elif 9 <= hour <= 17:
                        rec["cloud" if v == "cloud_cover" else "sw"].append(x)
            out[f"{m}|{l}"] = {d: {"cloud": round(sum(r_["cloud"]) / len(r_["cloud"]), 1) if r_["cloud"] else None,
                                   "sw": round(sum(r_["sw"]) / len(r_["sw"]), 1) if r_["sw"] else None,
                                   "precip": round(sum(r_["precip"]), 2) if len(r_["precip"]) >= 20 else None}
                               for d, r_ in daily.items()}
    have[st] = out
    json.dump(have, open(OUT, "w"))
    print(st, {k: len(v) for k, v in out.items()}, file=sys.stderr)
print("stations", len(have), file=sys.stderr)
