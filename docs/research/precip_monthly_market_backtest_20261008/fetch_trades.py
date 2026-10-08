"""Taker trades for every NYC/Seattle monthly-precipitation bucket.

Only side/outcome/price/size/time are kept -- no wallet or user fields.
A taker BUY of YES at x is evidence that YES could be bought at x then.
"""
import json, subprocess, time, gzip, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
URL = "https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={off}&takerOnly=true"
KEEP = ("side", "outcome", "outcomeIndex", "price", "size", "timestamp", "asset")

events = json.load(open(HERE / "events.json"))
ONLY = set(sys.argv[1:])  # condition ids to refetch into the existing file
out = {}
if ONLY:
    with gzip.open(HERE / "raw" / "trades.json.gz", "rt") as f:
        out = json.load(f)
for e in events:
    if not e["closed"]:
        continue
    for m in e["markets"]:
        if ONLY and m["condition_id"] not in ONLY:
            continue
        rows, off = [], 0
        while True:
            for attempt in range(6):
                r = subprocess.run(["curl", "-s", "-m", "40", URL.format(cid=m["condition_id"], off=off)],
                                   capture_output=True, text=True)
                try:
                    page = json.loads(r.stdout)
                except Exception:
                    time.sleep(3 + 3 * attempt)
                    continue
                if isinstance(page, dict) and "Too Many" in str(page.get("error")):
                    time.sleep(10 + 10 * attempt)
                    continue
                break
            else:
                raise RuntimeError(m["condition_id"])
            if not isinstance(page, list):
                print("STOP", e["title"], m["label"], off, str(page)[:120], file=sys.stderr)
                break
            rows += [{k: t.get(k) for k in KEEP} for t in page]
            if len(page) < 500:
                break
            off += 500
            time.sleep(0.2)
        out[m["condition_id"]] = rows
        print(e["title"], m["label"], len(rows), file=sys.stderr)
with gzip.open(HERE / "raw" / "trades.json.gz", "wt") as f:
    json.dump(out, f)
