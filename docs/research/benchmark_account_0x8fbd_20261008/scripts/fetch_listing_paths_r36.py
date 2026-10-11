"""Round 36 data: every bucket's CLOB prices-history (displayed price, 15-minute
fidelity) from listing to 12:00 local on the event day, for the 440 station-days
of round 34 (r34_rows.json).  Writes <bench>/r36_paths.json; resumable.
"""
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

B = Path(sys.argv[1])
rows = json.load(open(B / "r34_rows.json"))
G = json.load(open(B / "first_gamma.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
OUT = B / "r36_paths.json"
have = json.load(open(OUT)) if OUT.exists() else {}
CLIENT = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=20))
jobs = []
for r in rows:
    y, m, d = (int(x) for x in r["ed"].split("-"))
    end = int(datetime(y, m, d, 12, tzinfo=ZoneInfo(tz[r["st"]])).timestamp())
    for b in G[r["key"]]["buckets"]:
        k = f"{r['key']}|{b['label']}"
        if k not in have or "error" in have[k]:
            jobs.append((k, b["yes"], int(r["listed"]), end))


def fetch(job):
    k, tok, s, e = job
    for i in range(4):
        try:
            j = CLIENT.get("https://clob.polymarket.com/prices-history", params={"market": tok, "startTs": s, "endTs": e, "fidelity": 15}).json()
            h = sorted(j["history"], key=lambda x: int(x["t"]))
            return k, {"t": [int(x["t"]) for x in h], "p": [float(x["p"]) for x in h]}
        except Exception:
            time.sleep(1 + 2 * i)
    return k, {"error": "fetch_failed"}


t0 = time.time()
with ThreadPoolExecutor(max_workers=20) as pool:
    for k, v in pool.map(fetch, jobs):
        have[k] = v
json.dump(have, open(OUT, "w"))
print(f"paths {len(have)}; fetched {len(jobs)} in {time.time() - t0:.0f} s; errors {sum(1 for v in have.values() if 'error' in v)}", file=sys.stderr)
