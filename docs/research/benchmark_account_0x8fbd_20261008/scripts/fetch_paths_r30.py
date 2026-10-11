"""Round 30 data: every bucket's CLOB ``prices-history`` (public read, fidelity
5 minutes) over local 04:00-22:00 of its event day, for every ladder in
<bench>/ladders.json.  Stored reduced to the protocol's market price on a
10-minute grid from local 04:00 (``t0``): at each grid time the last price at
or before it no more than 90 minutes old, else null.  Writes <bench>/paths.json
atomically every 200 tokens; resumable -- rerun until done.  argv[2] = seconds
of fetching this run.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

STEP, STALE = 600, 5400


def reduce_grid(t, p, start, end):
    out, j = [], -1
    for g in range(start, end + 1, STEP):
        while j + 1 < len(t) and t[j + 1] <= g:
            j += 1
        out.append(round(p[j], 4) if j >= 0 and g - t[j] <= STALE else None)
    return {"t0": start, "g": out}


B = Path(sys.argv[1])
BUDGET = float(sys.argv[2]) if len(sys.argv) > 2 else 540.0
OUT = B / "paths.json"
L = json.load(open(B / "ladders.json"))
jobs = []
for key, lad in L.items():
    if "error" in lad:
        continue
    st, ed = key.split("|")
    zone = ZoneInfo(lad["tz"])
    y, m, d = (int(x) for x in ed.split("-"))
    start = int(datetime(y, m, d, 4, tzinfo=zone).timestamp())
    end = int(datetime(y, m, d, 22, tzinfo=zone).timestamp())
    for b in lad["buckets"]:
        jobs.append((f"{key}|{b['label']}", b["yes"], start, end))
have = json.load(open(OUT)) if OUT.exists() else {}
starts = {j[0]: (j[2], j[3]) for j in jobs}
for k, v in have.items():  # entries fetched before the grid reduction
    if "t" in v and k in starts:
        have[k] = reduce_grid(v["t"], v["p"], *starts[k])
todo = [j for j in jobs if j[0] not in have or "error" in have[j[0]]]
print(f"tokens {len(jobs)}, in hand {len(jobs) - len(todo)}, to fetch {len(todo)}", file=sys.stderr)


# One pooled keep-alive client: a fresh curl per token paid a TLS handshake each.
CLIENT = httpx.Client(timeout=20.0, limits=httpx.Limits(max_connections=24, max_keepalive_connections=24))


def fetch(job):
    key, tok, start, end = job
    for attempt in range(3):
        try:
            r = CLIENT.get("https://clob.polymarket.com/prices-history",
                           params={"market": tok, "startTs": start, "endTs": end, "fidelity": 5})
            if len(r.content) > 2_000_000:
                raise ValueError("oversized")
            hist = sorted(r.json()["history"], key=lambda h: int(h["t"]))
            return key, reduce_grid([int(h["t"]) for h in hist], [float(h["p"]) for h in hist], start, end)
        except Exception:
            time.sleep(2 + 3 * attempt)
    return key, {"error": "fetch_failed"}


def save():
    tmp = OUT.with_suffix(".tmp")
    json.dump(have, open(tmp, "w"))
    os.replace(tmp, OUT)


t0 = time.time()
done = 0
with ThreadPoolExecutor(max_workers=24) as pool:
    it = iter(todo)
    futures = {pool.submit(fetch, j): j for j in [next(it, None) for _ in range(48)] if j is not None}
    while futures:
        fut = next(as_completed(list(futures)))
        futures.pop(fut)
        key, val = fut.result()
        have[key] = val
        done += 1
        if time.time() - t0 < BUDGET:
            nxt = next(it, None)
            if nxt is not None:
                futures[pool.submit(fetch, nxt)] = nxt
        if done % 200 == 0:
            save()
save()
ok = sum(1 for v in have.values() if "error" not in v)
print(f"fetched {done} in {round(time.time() - t0)} s ({done / max(1, time.time() - t0):.1f}/s); ok {ok} of {len(jobs)}", file=sys.stderr)
