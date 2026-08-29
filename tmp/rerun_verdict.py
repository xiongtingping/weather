"""Re-derive the "should the book follow the model" verdict, stage-aware.

The first pass walked the chain ascending and kept the last row whose held
buckets still carried 5% -- with no stage predicate, and with that 5% test
applied ACROSS stages.  Both halves pushed the same way: P0 recomputes late,
so last-row-wins hands back a P0 row; and when P1 correctly zeroed the held
window because the day's observation excluded it, the 5% test skipped that P1
row and fell back to the P0 row that had not seen the observation.  The
"positive edge" the verdict was built on was largely that gap.

Here the belief is P1's, and a P1 row that zeroed the held window is recorded
as what it is -- the observation ruling the position out -- not skipped.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, RANK, load_chains

conn = sqlite3.connect(DB, uri=True, timeout=120.0)

chains_pos = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = chains_pos.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))

settled = {}
for r in conn.execute("SELECT station_id,event_date,hit,pnl_usdc,stake_usdc,"
                      "settled_bucket FROM paper_trade_settlement"):
    k = (str(r[0]), str(r[1]))
    c = settled.setdefault(k, {"hit": 0, "pnl": 0.0, "stake": 0.0,
                               "truth": str(r[5])})
    c["hit"] = max(c["hit"], int(r[2]))
    c["pnl"] += float(r[3]); c["stake"] += float(r[4] or 0.0)

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in settled:
        books.setdefault(k, []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return None
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return None
    return {b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
            if b.get("best_ask")}

chains = load_chains(conn, keys=set(settled))
rows, excluded_by_obs, no_p1 = [], 0, 0
for k, e in chains_pos.items():
    out = settled.get(k)
    ch = chains.get(k)
    if out is None or not ch:
        continue
    p1 = [(at, blob) for stage, at, blob in ch
          if stage == "P1" and at >= e["opened"]]
    if not p1:
        no_p1 += 1
        continue
    alive = None
    for at, blob in p1:
        env = json.loads(blob)
        inside = sum(v for b, v in env["probabilities"].items()
                     if b in e["held"])
        if inside > 0.05:
            alive = (at, env, inside)
    if alive is None:
        excluded_by_obs += 1
        continue
    at, env, inside = alive
    p, o = env["probabilities"], env["bucket_ordinals"]
    if not [o[b] for b in e["held"] if b in o]:
        continue
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    asks = asks_at(k, at) or {}
    ask = asks.get(target)
    rows.append({"k": k, "out": 1 - inside, "target": target,
                 "p_target": outside[target], "ask": ask,
                 "truth": out["truth"], "hit": out["hit"],
                 "ret": out["pnl"] / out["stake"] if out["stake"] else 0.0,
                 "nheld": len(e["held"]), "p_all": p, "ord": o,
                 "asks": asks, "held": e["held"]})

print("站日归类：可评估 %d ｜ P1 已把持仓窗判出局 %d ｜ 建仓后无 P1 %d"
      % (len(rows), excluded_by_obs, no_p1))
q = [r for r in rows if r["ask"] is not None]
print("其中有盘口可定价的 %d\n" % len(q))

hot = [r for r in q if r["out"] > .5]
pos = [r for r in hot if r["p_target"] - r["ask"] > 0]
print("=== 分家 >50%% 的判决（n=%d）" % len(hot))
print("  当时算出正边际(该买没买): %d (%.0f%%)"
      % (len(pos), 100 * len(pos) / max(len(hot), 1)))

def cf1(r):
    return (1.0 / r["ask"] - 1.0) if r["truth"] == r["target"] else -1.0

def window_cf(r):
    best = max(r["p_all"], key=r["p_all"].get)
    lo = r["ord"][best] - (r["nheld"] - 1) // 2
    picks = [b for b, x in r["ord"].items() if lo <= x < lo + r["nheld"]]
    if not picks or not all(b in r["asks"] for b in picks):
        return None
    cost = sum(r["asks"][b] for b in picks)
    if cost >= 1.0:
        return None
    return (1.0 / cost - 1.0) if r["truth"] in picks else -1.0

if hot:
    print("\n  跟着换到模型最看好那一格: 命中 %.0f%%  平均回报 %+.2f/u"
          % (100 * sum(1 for r in hot if r["truth"] == r["target"]) / len(hot),
             statistics.mean(cf1(r) for r in hot)))
    w = [(r, window_cf(r)) for r in hot]
    w = [(r, v) for r, v in w if v is not None]
    if w:
        print("  同宽度窗重挂在模型新中心:   命中 %.0f%%  平均回报 %+.2f/u  (n=%d)"
              % (100 * sum(1 for _, v in w if v > 0) / len(w),
                 statistics.mean(v for _, v in w), len(w)))
    print("  实际站着不动:               命中 %.0f%%  平均回报 %+.2f/u"
          % (100 * sum(r["hit"] for r in hot) / len(hot),
             statistics.mean(r["ret"] for r in hot)))

print("\n=== 模型在'它最看好的窗外格'上的校准")
print("  分组                      n    模型说   市场价   实际发生")
for name, sel in (("边际>0（模型比市场乐观）", [r for r in q if r["p_target"] - r["ask"] > 0]),
                  ("边际<=0（市场比模型贵）", [r for r in q if r["p_target"] - r["ask"] <= 0])):
    if sel:
        print("  %-22s %4d   %4.0f%%   %5.0f%%   %6.0f%%" % (
            name, len(sel), 100 * statistics.mean(r["p_target"] for r in sel),
            100 * statistics.mean(r["ask"] for r in sel),
            100 * sum(1 for r in sel if r["truth"] == r["target"]) / len(sel)))
for lo, hi in ((-1, 0), (0, .1), (.1, .25), (.25, 1.1)):
    sel = [r for r in q if lo <= r["p_target"] - r["ask"] < hi]
    if sel:
        print("  边际 %+.2f~%+.2f  n=%3d  模型说 %3.0f%%  市场价 %3.0f%%  实际 %3.0f%%"
              % (lo, hi, len(sel),
                 100 * statistics.mean(r["p_target"] for r in sel),
                 100 * statistics.mean(r["ask"] for r in sel),
                 100 * sum(1 for r in sel if r["truth"] == r["target"]) / len(sel)))
