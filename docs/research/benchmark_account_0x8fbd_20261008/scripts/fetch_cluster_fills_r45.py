"""Round 45 data: the cluster's BUY fills since the last pull, merged.

Reads the public Polymarket data API (``/trades?user=``, newest first, both
all fills and taker-only to mark the taker side) for the eight cluster
wallets back to ``--since`` (unix seconds), maps each fill to station x event
day x bucket with the bench ``lib`` parsers, and writes
``<bench>/cluster_fills_r45.json`` = ``cluster_fills_all.json`` + the new
fills (deduplicated on wallet, time, condition, outcome, price, size).
"""
import collections
import json
import subprocess
import sys
import time
from pathlib import Path

B = Path(sys.argv[1])
SINCE = int(sys.argv[2])
sys.path.insert(0, str(B))
from lib import CITY2ST, parse_bucket, parse_event  # noqa: E402

WALLETS = list(json.load(open(B / "cluster.json")).keys()) + ["0x8fbd7cf5f806f563080864694415829f7229a959"]


def get(url):
    for i in range(7):
        r = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            time.sleep(2 + i)
            continue
        if isinstance(j, dict) and "Too Many" in str(j.get("error")):
            time.sleep(8 + 8 * i)
            continue
        return j
    return None


def pull(user, taker_only):
    rows, off = [], 0
    while off <= 9500:
        page = get(f"https://data-api.polymarket.com/trades?user={user}&takerOnly={'true' if taker_only else 'false'}&limit=500&offset={off}")
        if not isinstance(page, list) or not page:
            break
        rows += page
        if min(t["timestamp"] for t in page) < SINCE or len(page) < 500:
            break
        off += 500
        time.sleep(0.4)
    return [t for t in rows if t["timestamp"] >= SINCE]


old = json.load(open(B / "cluster_fills_all.json"))
seen = {(f["w"], f["ts"], f["cid"], f["outcome"], round(float(f["price"]), 6), round(float(f["size"]), 6)) for f in old}
new = []
for w in WALLETS:
    allf, tk = pull(w, False), pull(w, True)
    taker = collections.Counter((t["transactionHash"], t["asset"], t["side"], str(t["price"]), str(t["size"]), t["timestamp"]) for t in tk)
    n0 = len(new)
    for t in allf:
        if t["side"] != "BUY":
            continue
        k = (t["transactionHash"], t["asset"], t["side"], str(t["price"]), str(t["size"]), t["timestamp"])
        is_t = taker.get(k, 0) > 0
        if is_t:
            taker[k] -= 1
        city, ed = parse_event(t.get("eventSlug") or "")
        label, _lo, _hi, _unit, _kind = parse_bucket(t.get("title") or "")
        st = CITY2ST.get(city)
        if not st or not ed or not label:
            continue
        rec = {"w": w, "ts": t["timestamp"], "cid": t["conditionId"], "outcome": t["outcome"], "price": float(t["price"]),
               "size": float(t["size"]), "taker": is_t, "station": st, "event_date": ed, "bucket": label}
        key = (w, rec["ts"], rec["cid"], rec["outcome"], round(rec["price"], 6), round(rec["size"], 6))
        if key in seen:
            continue
        seen.add(key)
        new.append(rec)
    print(f"{w[:10]}: fills since {SINCE} {len(allf)}, new buys {len(new) - n0}", file=sys.stderr)
merged = sorted(old + new, key=lambda f: f["ts"])
json.dump(merged, open(B / "cluster_fills_r45.json", "w"))
print(f"merged {len(old)} + {len(new)} = {len(merged)}; newest {max(f['ts'] for f in merged)}", file=sys.stderr)
