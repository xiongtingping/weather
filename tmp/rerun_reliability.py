"""Re-run the unconditional reliability table with stage-aware selection.

The first pass took each station-day's chronologically-last committed row,
which was a P0 night row 120 times out of 150 -- a forecast written after the
day's high was measured, scored against a market quote that had ingested it.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, RANK, load_chains, pick

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in truth:
        books.setdefault(k, []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return {}
    return {b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
            if b.get("best_ask")}

chains = load_chains(conn, keys=set(truth))
def build(stage_aware):
    pairs, stages = [], {}
    for k, tb in truth.items():
        ch = chains.get(k)
        if not ch:
            continue
        if stage_aware:
            got = pick(ch)
            if got is None:
                continue
            _, at, stage, env = got
        else:
            stage, at, blob = ch[-1]
            env = json.loads(blob)
        stages[stage] = stages.get(stage, 0) + 1
        asks = asks_at(k, at)
        for b, p in env["probabilities"].items():
            pairs.append((p, asks.get(b), 1 if b == tb else 0))
    return pairs, stages

def table(name, idx, sample):
    print(name)
    print("  声称区间        n     平均声称   实际发生")
    for lo, hi in ((0, .02), (.02, .05), (.05, .12), (.12, .25), (.25, .5), (.5, 1.01)):
        g = [x for x in sample if x[idx] is not None and lo <= x[idx] < hi]
        if not g:
            continue
        print("  %.2f-%.2f  %6d      %5.1f%%     %5.1f%%" % (
            lo, hi, len(g), 100 * statistics.mean(x[idx] for x in g),
            100 * sum(x[2] for x in g) / len(g)))
    ok = [x for x in sample if x[idx] is not None]
    print("  Brier %.4f  (n=%d)\n" % (
        statistics.mean((x[idx] - x[2]) ** 2 for x in ok), len(ok)))

for aware in (False, True):
    pairs, stages = build(aware)
    print("=" * 62)
    print("%s  取到的 stage: %s" % (
        "【修后】stage 优先" if aware else "【修前】纯时间最新", stages))
    table("模型（全部桶，无条件）", 0, pairs)
    both = [x for x in pairs if x[1] is not None]
    table("市场 best_ask（同批桶同时刻）", 1, both)
