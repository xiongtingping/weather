"""Why does the model's out-of-window argmax fail so completely?

Four candidate mechanisms, measured rather than argued:
  A. direction -- is the claimed bucket above or below the settled one, and
     above or below the running high the chain already knew about;
  B. the missing ceiling -- the hard bound only ever cuts from below, so mass
     above the day's high is never removed by an observation;
  C. clock -- how late these claims are still standing;
  D. winner's curse -- whether taking the max over out-of-window buckets is
     itself the damage, by comparing the argmax against non-argmax buckets
     carrying the same claimed probability.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
from datetime import timedelta
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
pos = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = pos.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
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
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
             if b.get("best_ask")} if i else {})

chains = load_chains(conn, keys=set(truth))
rows, allpairs = [], []
for k, e in pos.items():
    if k not in truth or k not in chains:
        continue
    p1 = [(at, b) for s, at, b in chains[k] if s == "P1" and at >= e["opened"]]
    alive = None
    for at, blob in p1:
        env = json.loads(blob)
        if sum(v for b, v in env["probabilities"].items()
               if b in e["held"]) > 0.05:
            alive = (at, env)
    if alive is None:
        continue
    at, env = alive
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k]
    if tb not in o:
        continue
    lin = env.get("lineage") or {}
    rh = lin.get("reported_high_bucket")
    rh_ord = o.get(rh) if rh else None
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    asks = asks_at(k, at)
    ask = asks.get(target)
    hour = None
    if k in tz:
        try:
            hour = parse(at).astimezone(
                local_close(k[1], tz[k]).tzinfo).hour
        except Exception:
            pass
    for b, v in outside.items():
        if b in o:
            allpairs.append({
                "p": v, "is_argmax": b == target,
                "above_rh": rh_ord is not None and o[b] > rh_ord,
                "won": b == tb, "ask": asks.get(b)})
    if ask is None:
        continue
    rows.append({"k": k, "at": at, "hour": hour, "p_t": outside[target],
                 "ask": ask, "edge": outside[target] - ask,
                 "t_ord": o[target], "truth_ord": o[tb], "rh_ord": rh_ord,
                 "won": target == tb,
                 "mass_above_rh": sum(v for b, v in p.items()
                                      if rh_ord is not None and o.get(b, -1) > rh_ord)})

def block(name, sel):
    if not sel:
        print("  %-20s (无样本)" % name); return
    above_truth = sum(1 for r in sel if r["t_ord"] > r["truth_ord"])
    below_truth = sum(1 for r in sel if r["t_ord"] < r["truth_ord"])
    above_rh = sum(1 for r in sel if r["rh_ord"] is not None
                   and r["t_ord"] > r["rh_ord"])
    hrs = [r["hour"] for r in sel if r["hour"] is not None]
    print("  %-20s n=%-3d  在真值上方 %3.0f%%  在已测高点上方 %3.0f%%  "
          "当地时中位 %s  头顶总质量 %.0f%%" % (
        name, len(sel), 100 * above_truth / len(sel),
        100 * above_rh / len(sel),
        ("%.0f点" % statistics.median(hrs)) if hrs else "—",
        100 * statistics.mean(r["mass_above_rh"] for r in sel)))

print("A/B/C  方向、天花板、时钟")
print("  (在真值上方 = 模型押的格子比实际更热；在已测高点上方 = 需要继续升温才可能)")
block("边际 >+0.25", [r for r in rows if r["edge"] > .25])
block("边际 +0.10~+0.25", [r for r in rows if .10 < r["edge"] <= .25])
block("边际 0~+0.10", [r for r in rows if 0 < r["edge"] <= .10])
block("边际 <=0", [r for r in rows if r["edge"] <= 0])
print()
print("D  是不是'取最大'这个动作本身的锅（同声称概率档内比较）")
print("  声称档      argmax桶 n / 实际发生     非argmax桶 n / 实际发生")
for lo, hi in ((.05, .12), (.12, .25), (.25, .60)):
    a = [x for x in allpairs if x["is_argmax"] and lo <= x["p"] < hi]
    b = [x for x in allpairs if not x["is_argmax"] and lo <= x["p"] < hi]
    if a or b:
        print("  %.2f-%.2f    %4d / %5.1f%%            %4d / %5.1f%%" % (
            lo, hi, len(a), 100 * sum(x["won"] for x in a) / max(len(a), 1),
            len(b), 100 * sum(x["won"] for x in b) / max(len(b), 1)))
print()
print("B  头顶的质量 vs 脚下的质量，兑现率对比（全部窗外桶）")
for name, sel in (("在已测高点上方(无人可砍)", [x for x in allpairs if x["above_rh"]]),
                  ("在已测高点下方(硬下界管得着)", [x for x in allpairs if not x["above_rh"]])):
    if sel:
        print("  %-26s n=%4d  平均声称 %5.1f%%  实际发生 %5.1f%%" % (
            name, len(sel), 100 * statistics.mean(x["p"] for x in sel),
            100 * sum(x["won"] for x in sel) / len(sel)))
