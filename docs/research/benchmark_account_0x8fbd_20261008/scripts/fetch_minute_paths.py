"""Round 27 (熊先生 10-10 「继续打，拉分钟级价格路径」): the minute price path of
every bucket on every station-day the replication covers.

One CLOB ``prices-history`` request (public, read) per YES token, fidelity 1
minute, from 75 minutes before the station-day's first first-taker event to
one minute after its last.  Tokens from the SSOT registry (read-only).
Resumable: <bench>/minute_paths.json keeps what came back; rerun until done.
"""
import gzip
import json
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

B = Path(sys.argv[1])
BUDGET = float(sys.argv[2]) if len(sys.argv) > 2 else 520.0
OUT = B / "minute_paths.json"
E = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))
sd = defaultdict(list)
for e in E:
    sd[(e["st"], e["ed"])].append(int(e["ts"]))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA query_only=1")
# Only the buckets within three rungs of some event's market mode: the cluster
# never buys further out, so a tail bucket's path would only be a feature of a
# candidate it never takes.
needed = defaultdict(set)
for e in E:
    for o, label in e["labels"].items():
        if abs(int(o) - int(e["mode_ord"])) <= 3:
            needed[(e["st"], e["ed"])].add(label)
jobs = []
for (st, ed), ts in sd.items():
    start, end = min(ts) - 75 * 60, max(ts) + 60
    seen = set()
    for outcome, tok in conn.execute(
            "SELECT outcome, yes_token FROM basket_target_market_registry WHERE station_id=? AND event_date=?"
            " AND yes_token IS NOT NULL AND yes_token<>'' ORDER BY rowid DESC", (st, ed)):
        if outcome in seen or outcome not in needed[(st, ed)]:
            continue
        seen.add(outcome)
        jobs.append((f"{st}|{ed}|{outcome}", tok, start, end))
conn.close()
have = json.load(open(OUT)) if OUT.exists() else {}
todo = [j for j in jobs if j[0] not in have or "error" in have[j[0]]]
print(f"tokens {len(jobs)}, in hand {len(jobs) - len(todo)}, to fetch {len(todo)}", file=sys.stderr)


def fetch(job):
    key, tok, start, end = job
    for attempt in range(3):
        r = subprocess.run(["curl", "-s", "-m", "20",
                            f"https://clob.polymarket.com/prices-history?market={tok}&startTs={start}&endTs={end}&fidelity=1"],
                           capture_output=True, text=True)
        try:
            data = json.loads(r.stdout)
            hist = data["history"]
            return key, {"token": tok, "t": [int(h["t"]) for h in hist], "p": [float(h["p"]) for h in hist]}
        except Exception:
            time.sleep(2 + 3 * attempt)
    return key, {"token": tok, "error": "fetch_failed"}


def save(obj):
    """Atomic: a run cut off mid-write leaves the previous file whole."""
    tmp = OUT.with_suffix(".tmp")
    json.dump(obj, open(tmp, "w"))
    import os
    os.replace(tmp, OUT)


t0 = time.time()
done = 0
with ThreadPoolExecutor(max_workers=12) as pool:
    futures = {}
    it = iter(todo)
    for job in it:
        futures[pool.submit(fetch, job)] = job
        if len(futures) >= 24:
            break
    while futures:
        for fut in as_completed(list(futures)):
            futures.pop(fut)
            key, val = fut.result()
            have[key] = val
            done += 1
            if time.time() - t0 < BUDGET:
                nxt = next(it, None)
                if nxt is not None:
                    futures[pool.submit(fetch, nxt)] = nxt
            break
        if done % 100 == 0:
            save(have)
save(have)
ok = sum(1 for v in have.values() if "error" not in v)
print(f"fetched {done} this run in {round(time.time() - t0)} s; in hand {ok} ok of {len(jobs)}", file=sys.stderr)
