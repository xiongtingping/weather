"""Unconditional reliability: every bucket, not just the ones we bet on.

The earlier split deliberately picked the bucket where the model most
disagreed with the book, which is a selected sample.  Here every bucket in
the last committed recompute of every settled station-day gets one
(claimed probability, did it happen) pair, and the book's best ask at that
same instant gets the same treatment, so model and market are scored on
identical events.
"""
import json, sqlite3, statistics
from bisect import bisect_right

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60.0)

truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    books.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), r[3]))

def asks_at(key, when):
    hist = books.get(key)
    if not hist:
        return None
    i = bisect_right([h[0] for h in hist], when)
    if i == 0:
        return None
    return {b["bucket"]: float(b["best_ask"]) for b in json.loads(hist[i - 1][1])
            if b.get("best_ask")}

pairs = []
days = 0
for (st, day), tb in truth.items():
    last = conn.execute(
        "SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY decision_at_utc DESC LIMIT 1", (st, day)).fetchone()
    if not last:
        continue
    days += 1
    probs = json.loads(last[1])["probabilities"]
    asks = asks_at((st, day), str(last[0])) or {}
    for bucket, p in probs.items():
        pairs.append((p, asks.get(bucket), 1 if bucket == tb else 0))

print("站日 %d，桶级样本 %d 个" % (days, len(pairs)))
both = [x for x in pairs if x[1] is not None]
print("模型与市场都有报价的样本 %d 个\n" % len(both))

def reliability(name, idx, sample):
    print("%s" % name)
    print("  声称区间        n     平均声称   实际发生")
    for lo, hi in ((0, .02), (.02, .05), (.05, .12), (.12, .25), (.25, .5), (.5, 1.01)):
        g = [x for x in sample if lo <= x[idx] < hi]
        if not g:
            continue
        print("  %.2f-%.2f  %6d      %5.1f%%     %5.1f%%" % (
            lo, hi, len(g), 100 * statistics.mean(x[idx] for x in g),
            100 * sum(x[2] for x in g) / len(g)))
    brier = statistics.mean((x[idx] - x[2]) ** 2 for x in sample)
    print("  Brier %.4f   总声称 %.3f  总实际 %.3f\n" % (
        brier, sum(x[idx] for x in sample) / days, sum(x[2] for x in sample) / days))

reliability("模型（全部桶，无条件）", 0, pairs)
reliability("模型（限于市场也有报价的桶）", 0, both)
reliability("市场 best_ask（同一批桶、同一时刻）", 1, both)
