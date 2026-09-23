"""Station regional features, fidelity check: is the research AIFS Single
series (Open-Meteo single-runs, ecmwf_aifs025_single) the same numbers as
ECMWF's official open-data aifs-single 0p25 2t field (GCS mirror of
ecmwf-forecasts, the path a production lane would read)?

Byte-range reads of the 2t message only (~0.55 MB each), decoded with the
repo's shared eccodes nearest-point reader; nothing is written outside the
scratchpad.  Compares the 6-hourly official values with the research series
at the same valid times for SBGR and FACT.
"""
import json, os, sys
from datetime import datetime, timedelta, timezone
import httpx
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.noaa_conus_archive import nearest_point_kelvin

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/srf/cache/ecmwf_aifs025_single"
BASE = "https://storage.googleapis.com/ecmwf-open-data"
PTS = {"SBGR": (-23.432, -46.469), "FACT": (-33.965, 18.602)}
RUNS = ["2026-07-15T00", "2026-08-20T12", "2026-09-12T00", "2026-09-20T00"]
STEPS = (12, 18, 24, 30, 36, 42, 48)
cl = httpx.Client(timeout=60)
diffs = []
for run in RUNS:
    r0 = datetime.strptime(run, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    om = json.load(open(os.path.join(SP, f"SBGR-FACT_{r0:%Y%m%dT%H}.json")))
    series = {p["station"]: dict(zip(p["time"], p["t2m"])) for p in om["points"]}
    for step in STEPS:
        stem = f"{BASE}/{r0:%Y%m%d}/{r0:%H}z/aifs-single/0p25/oper/{r0:%Y%m%d%H}0000-{step}h-oper-fc"
        idx = [json.loads(l) for l in cl.get(stem + ".index").text.splitlines() if l.strip()]
        e = next(x for x in idx if x.get("param") == "2t")
        msg = cl.get(stem + ".grib2", headers={"Range": f"bytes={e['_offset']}-{e['_offset'] + e['_length'] - 1}"}).content
        vt = (r0 + timedelta(hours=step)).strftime("%Y-%m-%dT%H:%M")
        for st, (la, lo) in PTS.items():
            k, dist = nearest_point_kelvin(msg, latitude=la, longitude=lo)
            o = series[st].get(vt)
            if o is not None:
                diffs.append(dict(run=run, step=step, st=st, official_c=round(k - 273.15, 2), research_c=o,
                                  diff=round(o - (k - 273.15), 2), dist_km=round(dist, 1)))
for d in diffs:
    print(d)
v = [d["diff"] for d in diffs]
print("n", len(v), "mean research-official %.3f  max|diff| %.2f" % (sum(v) / len(v), max(abs(x) for x in v)))
json.dump(diffs, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_aifs_official.json"), "w"), indent=1)
