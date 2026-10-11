"""Round 38 (熊先生 10-11 「我如果要实现簇对标挂牌即建仓可以实现吗？」): round 34's
resting-bid test with OUR belief instead of the Open-Meteo deterministic F --
the bucket is the mode of our first committed served vector (P0, six global
ensemble members), posted at T = max(listing + 5 min, that P0's decision time),
resting 2 h; same fill rule (a trade strictly below X), same accounting.  Post-
hoc on the round-34 universe (440 station-days, event days 09-28..10-10); reads
round 34's F legs on the same station-days for comparison.  Polymarket data-api
only (no Open-Meteo).
"""
import collections
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

B = Path(sys.argv[1])
rows = json.load(open(B / "r34_rows.json"))
tapes = json.load(open(B / "r34_tapes.json"))
cond = json.load(open(B / "r34_gamma.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ts_ = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
CLIENT = httpx.Client(timeout=30.0)

legs = []
for r in rows:
    row = conn.execute("SELECT history_id, decision_at_utc FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND stage='P0'"
                       " AND state='COMMITTED' ORDER BY decision_at_utc LIMIT 1", (r["st"], r["ed"])).fetchone()
    if not row:
        continue
    vec = dict(conn.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (row[0],)).fetchall())
    if not vec:
        continue
    mode = max(vec, key=lambda k: vec[k])
    if mode not in r["labels"]:
        continue
    T = max(r["listed"] + 300, ts_(row[1]))
    cid = cond.get(r["key"], {}).get(mode)
    legs.append({"r": r, "mode": mode, "p_mode": vec[mode] / sum(vec.values()), "T": T, "cid": cid, "p0_lag_min": (ts_(row[1]) - r["listed"]) / 60})
print(f"station-days with a first P0: {len(legs)}; P0 after listing (min): median {np.median([l['p0_lag_min'] for l in legs]):.1f}")


def tape(cid, lo, hi):
    out, end = [], hi
    for _ in range(20):
        for i in range(5):
            try:
                page = CLIENT.get("https://data-api.polymarket.com/trades", params={"market": cid, "limit": 500, "takerOnly": "false", "end": end}).json()
                break
            except Exception:
                time.sleep(2 + 2 * i)
                page = None
        if not isinstance(page, list) or not page:
            break
        out += [{"t": int(x["timestamp"]), "outcome": x.get("outcome"), "price": float(x["price"]), "size": float(x["size"]), "w": str(x.get("proxyWallet", ""))[:10]} for x in page]
        oldest = min(int(x["timestamp"]) for x in page)
        if oldest <= lo or len(page) < 500:
            break
        end = oldest
    return [x for x in out if lo <= x["t"] <= hi]


need = [l for l in legs if l["cid"] and (l["cid"] not in tapes or not tapes[l["cid"]] or max((x["t"] for x in tapes[l["cid"]]), default=0) < l["T"] + 7200 - 3600)]
need_c = {}
for l in need:
    need_c[l["cid"]] = (int(l["r"]["listed"]), int(l["T"]) + 7200 + 300)
with ThreadPoolExecutor(max_workers=4) as pool:
    for cid, v in zip(need_c, pool.map(lambda kv: tape(kv[0], *kv[1]), need_c.items())):
        tapes[cid] = v
json.dump(tapes, open(B / "r34_tapes.json", "w"))
print(f"tapes fetched for our mode buckets: {len(need_c)}")


def run(sel, X, min_shares=0.0):
    book = collections.defaultdict(list)
    n = fill = hit_all = hit_fill = 0
    for l in sel:
        if not l["cid"]:
            continue
        r = l["r"]
        hit = r["truth"] == l["mode"]
        n += 1
        hit_all += hit
        below = [x for x in tapes.get(l["cid"], []) if l["T"] < x["t"] <= l["T"] + 7200 and (x["price"] if x["outcome"] == "Yes" else 1 - x["price"]) < X]
        if not below or sum(x["size"] for x in below) < min_shares:
            continue
        fill += 1
        hit_fill += hit
        book[r["ed"]].append(((1.0 if hit else 0.0) - X, X))
    keys = list(book)
    S = np.array([[sum(p for p, c in book[k]), sum(c for p, c in book[k])] for k in keys])
    rng = np.random.default_rng(38)
    idx = rng.integers(0, len(keys), size=(2000, len(keys)))
    T = S[idx].sum(axis=1)
    bs = np.sort(T[:, 0] / T[:, 1])
    return n, fill / n, hit_all / n, hit_fill / max(1, fill), 100 * S[:, 0].sum() / S[:, 1].sum(), 100 * bs[50], 100 * bs[1949], len(keys)


print("\n== YES bid on OUR first P0 mode bucket, at max(listing+5 min, first P0), 2 h, trade strictly below X")
for X in (0.25, 0.30, 0.35, 0.40):
    for ms in (0.0, 5.0):
        n, fr, ha, hf, roi, lo, hi, nd = run(legs, X, ms)
        print(f"   X={X:.2f}{' (>=5 shares below X)' if ms else ''}: legs {n}  fill {fr:.2f}  hit all {ha:.3f}  hit filled {hf:.3f}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}] ({nd} days)")
print("   by our P0's own probability on that bucket (X=0.30):")
for lo_p, hi_p in ((0, 0.30), (0.30, 0.40), (0.40, 1.01)):
    sel = [l for l in legs if lo_p <= l["p_mode"] < hi_p]
    if len(sel) < 30:
        continue
    n, fr, ha, hf, roi, lo, hi, nd = run(sel, 0.30)
    print(f"     p_mode {lo_p:.2f}-{hi_p:.2f}: legs {n}  fill {fr:.2f}  hit filled {hf:.3f}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]")

# separating timing from bucket: (a) F posted at OUR start time; (b) our mode posted at listing + 5 min (as if P0 were instant)
print("\n== timing vs bucket (X=0.30 / 0.35, 2 h, strict)")
byk = {l["r"]["key"]: l for l in legs}
variants = {}
alt = []
for r in rows:
    l = byk.get(r["key"])
    if not l:
        continue
    F = r["legs"]["F"]
    alt.append({"r": r, "mode": F, "T": l["T"], "cid": r["cid"].get(F) or cond.get(r["key"], {}).get(F), "p_mode": 0})
variants["F bucket, posted at our start time"] = alt
variants["our mode, posted at listing + 5 min"] = [dict(l, T=l["r"]["listed"] + 300) for l in legs]
variants["our mode, posted at our start time (as above)"] = legs
variants["F bucket, posted at listing + 5 min (round 34)"] = [{"r": r, "mode": r["legs"]["F"], "T": r["listed"] + 300, "cid": r["cid"].get(r["legs"]["F"]), "p_mode": 0} for r in rows if r["key"] in byk]
for name, sel in variants.items():
    for X in (0.30, 0.35):
        n, fr, ha, hf, roi, lo, hi, nd = run(sel, X)
        print(f"   {name:46s} X={X:.2f}: fill {fr:.2f}  hit all {ha:.3f}  hit filled {hf:.3f}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]")
same = sum(1 for r in rows if r["key"] in byk and byk[r["key"]]["mode"] == r["legs"]["F"])
print(f"   our first-P0 mode = F on {same} of {len(byk)} station-days")
