"""Round 61 (熊先生 10-13: 「探讨合适的TTL + clip 模板 + burst 密度…持续滚动报价、持续补腿、持续撤改单」).

On the market-mode bucket of formed station-days (round 60's cache), the public trade tape
(data-api ``trades?market=<condition>``) over the 12 h after formation and the official WS
book (best bid/ask, ~85 s snapshots) give:
  flow that would hit a resting YES bid  = taker SELL of YES or taker BUY of NO (mint),
  flow that would hit a resting NO bid   = taker BUY of YES or taker SELL of NO;
  trade sizes (shares) -> the clip; inter-arrival times and clustering -> burst density;
  how long the best bid stands before it moves -> the TTL;
  a simulated clip of c shares resting at bid + 1 tick, re-posted every T seconds: the shares
  it fills per hour on each side (first in queue at its price; a trade fills min(size, clip
  left) when it prints at or through our price), for c in {5, 10, 20, 50} and T in
  {60, 300, 900, 1800}.
Writes the tape to <bench>/pair_tape_r61.json (resume).
"""
import collections
import json
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.proper_bet_lane import market_ids  # noqa: E402

B = Path(sys.argv[1])
N = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 60
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
SER = json.load(open(B / "pair_series_r60.json"))
TAPE_PATH = B / "pair_tape_r61.json"
tape = json.load(open(TAPE_PATH)) if TAPE_PATH.exists() else {}
formed = sorted(k for k, v in SER.items() if v.get("formed"))
step = max(1, len(formed) // N)
sample = formed[::step][:N]


def get(url):
    for i in range(5):
        r = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        try:
            return json.loads(r.stdout)
        except ValueError:
            time.sleep(2 + 2 * i)
    return None


for key in sample:
    if key in tape:
        continue
    st, day = key.split("|")
    s = SER[key]["start"]
    ids = market_ids(conn, st, day, s["mode"])
    cid = ids.get("condition_id")
    if not cid:
        tape[key] = {"error": "no_condition"}
        continue
    t0, t1 = s["ts"], s["ts"] + 12 * 3600
    rows, off = [], 0
    while off <= 3000:
        page = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={off}&takerOnly=true")
        if not isinstance(page, list) or not page:
            break
        rows += page
        if len(page) < 500 or min(int(p["timestamp"]) for p in page) < t0:
            break
        off += 500
    trades = [{"ts": int(p["timestamp"]), "side": p["side"], "outcome": p["outcome"], "price": float(p["price"]),
               "size": float(p["size"])} for p in rows if t0 <= int(p["timestamp"]) <= t1]
    tape[key] = {"cid": cid, "t0": t0, "trades": sorted(trades, key=lambda x: x["ts"])}
    json.dump(tape, open(TAPE_PATH, "w"))
    time.sleep(0.3)


def book_series(st, day, t0, t1, bucket):
    rows = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                        " AND state='COMMITTED' AND observed_at>=? AND observed_at<=? ORDER BY observed_at",
                        (st, day, datetime.fromtimestamp(t0 - 600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                         datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))).fetchall()
    out = []
    for obs, bj in rows:
        for b in json.loads(bj):
            if str(b.get("bucket")) == bucket:
                asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
                bid = float(b["bid_price"]) if b.get("bid_available") and b.get("bid_price") else None
                tick = float(b.get("price_tick_size") or 0.01)
                bid_q = next((float(x["shares"]) for x in (b.get("bid_levels") or []) if bid is not None and abs(float(x["price"]) - bid) < 1e-9), 0.0)
                out.append((datetime.fromisoformat(obs.replace("Z", "+00:00")).timestamp(), bid, asks[0] if asks else None, tick, bid_q))
    return out


def yes_price(tr):
    return tr["price"] if tr["outcome"] == "Yes" else 1 - tr["price"]


stats = collections.defaultdict(list)
sim = collections.defaultdict(lambda: [0.0, 0.0])  # (side, clip, T) -> [shares filled, hours]
bid_life = []
n_used = 0
for key in sample:
    tp = tape.get(key) or {}
    if "trades" not in tp:
        continue
    st, day = key.split("|")
    s = SER[key]["start"]
    t0, t1 = tp["t0"], tp["t0"] + 12 * 3600
    bs = book_series(st, day, t0, t1, s["mode"])
    if len(bs) < 10:
        continue
    n_used += 1
    trades = tp["trades"]
    hit_yes = [t for t in trades if (t["outcome"] == "Yes" and t["side"] == "SELL") or (t["outcome"] == "No" and t["side"] == "BUY")]
    hit_no = [t for t in trades if (t["outcome"] == "Yes" and t["side"] == "BUY") or (t["outcome"] == "No" and t["side"] == "SELL")]
    stats["trades_per_h"].append(len(trades) / 12)
    stats["hit_yes_per_h"].append(len(hit_yes) / 12)
    stats["hit_no_per_h"].append(len(hit_no) / 12)
    stats["size"] += [t["size"] for t in trades]
    gaps = [b["ts"] - a["ts"] for a, b in zip(trades, trades[1:])]
    stats["gap"] += gaps
    # best-bid lifetime: consecutive snapshots with the same bid
    run_start, cur = bs[0][0], bs[0][1]
    for ts, bid, _a, _t, _q in bs[1:]:
        if bid != cur:
            bid_life.append(ts - run_start)
            run_start, cur = ts, bid
    stats["bid_queue"] += [q for *_x, q in bs if q > 0]

    def book_at(ts):
        prev = bs[0]
        for row in bs:
            if row[0] > ts:
                break
            prev = row
        return prev

    # simulate a clip at bid + 1 on each side, re-posted every T seconds
    for T in (60, 300, 900, 1800):
        for clip in (5, 10, 20, 50):
            for side, flow in (("YES", hit_yes), ("NO", hit_no)):
                filled = 0.0
                t = t0
                fi = 0
                while t < t1:
                    _ts, bid, ask, tick, _q = book_at(t)
                    if bid is None or ask is None:
                        t += T
                        continue
                    if side == "YES":
                        price = min(bid + tick, ask - tick)
                    else:
                        price = min((1 - ask) + tick, (1 - bid) - tick)
                    left = clip
                    while fi < len(flow) and flow[fi]["ts"] < t:
                        fi += 1
                    j = fi
                    while j < len(flow) and flow[j]["ts"] < t + T and left > 0:
                        yp = yes_price(flow[j])
                        through = yp <= price + 1e-9 if side == "YES" else (1 - yp) <= price + 1e-9
                        if through:
                            take = min(left, flow[j]["size"])
                            left -= take
                            filled += take
                        j += 1
                    t += T
                sim[(side, clip, T)][0] += filled
                sim[(side, clip, T)][1] += 12

print(f"{n_used} formed mode buckets with a tape and a book (of {len(sample)} sampled)")
q = lambda a, p: float(np.percentile(a, p)) if a else float("nan")  # noqa: E731
print(f"trades per hour (12 h after formation): median {q(stats['trades_per_h'], 50):.1f}, p25 {q(stats['trades_per_h'], 25):.1f}, p75 {q(stats['trades_per_h'], 75):.1f};"
      f" flow hitting a YES bid {q(stats['hit_yes_per_h'], 50):.1f}/h, a NO bid {q(stats['hit_no_per_h'], 50):.1f}/h (medians)")
print(f"trade size (shares): p25 {q(stats['size'], 25):.1f}, median {q(stats['size'], 50):.1f}, p75 {q(stats['size'], 75):.1f}, p90 {q(stats['size'], 90):.1f}")
print(f"inter-trade gap (s): p10 {q(stats['gap'], 10):.0f}, median {q(stats['gap'], 50):.0f}, p90 {q(stats['gap'], 90):.0f};"
      f" share of gaps under 10 s {np.mean(np.array(stats['gap']) < 10) if stats['gap'] else float('nan'):.0%} (bursts)")
print(f"best-bid lifetime (s, at ~85 s snapshot resolution): p25 {q(bid_life, 25):.0f}, median {q(bid_life, 50):.0f}, p75 {q(bid_life, 75):.0f}")
print(f"queue at the best bid (shares): median {q(stats['bid_queue'], 50):.0f}, p25 {q(stats['bid_queue'], 25):.0f}")
print("\nclip at bid + 1, re-posted every T s: shares filled per hour per bucket (YES side / NO side)")
print(f"{'T s':>6s} " + " ".join(f"{'clip ' + str(c):>14s}" for c in (5, 10, 20, 50)))
for T in (60, 300, 900, 1800):
    cells = []
    for clip in (5, 10, 20, 50):
        y = sim[("YES", clip, T)]
        nn = sim[("NO", clip, T)]
        cells.append(f"{y[0] / max(1, y[1]):5.1f} / {nn[0] / max(1, nn[1]):5.1f}")
    print(f"{T:6d} " + " ".join(f"{c:>14s}" for c in cells))
