"""H6: is the tail-edge failure a latency race -- market has an observation the
model's chain has not yet consumed?

Reconstructs the phenomenon's sample (stage-aware, lead>=0), then for every row
measures how far behind the physical truth the chain's belief actually was:
  * newest METAR the chain consumed (valid_at of lineage canonical_observation_ids)
  * lineage.canonical_observation_committed_at
  * the book snapshot's observed_at that priced the target
  * archive max_valid_at / observed_max_c = when/what the day's high really was
A chain that has already consumed the observation carrying the day's final high
is CAUGHT UP; if such rows still claim 31% the hypothesis dies.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
arc = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=120.0)

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
tmax = {(str(r[0]), str(r[1])): r[2] for r in conn.execute(
    "SELECT station_id,event_date,settled_tmax_c FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
books = {}
_keys = sorted(truth)
_ph = ",".join(["(?,?)"] * len(_keys))
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot"
                      " WHERE (station_id,event_date) IN (VALUES %s)"
                      " ORDER BY observed_at" % _ph,
                      [x for k in _keys for x in k]):
    books.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), r[3]))

def book_at(k, when):
    h = books.get(k)
    if not h:
        return None, {}
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return None, {}
    obs_at, blob = h[i - 1]
    return obs_at, {b["bucket"]: b for b in json.loads(blob)}

# archive: when the day's high was physically reached
bounds = {}
for r in arc.execute("SELECT station_id,event_date,observed_max_c,max_valid_at"
                     " FROM reported_trajectory_canonical_station_day_bounds"):
    bounds[(str(r[0]), str(r[1]))] = (r[2], str(r[3]))
# archive: rtobs id -> valid_at / committed_at
obsmeta = {}
for r in arc.execute("SELECT observation_id,valid_at,canonical_committed_at,"
                     "reported_temperature_c,first_seen_at"
                     " FROM reported_trajectory_observations"):
    obsmeta[str(r[0])] = (str(r[1]), str(r[2] or ""), r[3], str(r[4] or ""))

keys = sorted(truth)
ph = ",".join(["(?,?)"] * len(keys))
flat = [x for k in keys for x in k]
chains = {}
for r in conn.execute(
        "SELECT station_id,event_date,stage,decision_at_utc,envelope_json"
        " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
        " AND (station_id,event_date) IN (VALUES %s) ORDER BY decision_at_utc" % ph, flat):
    chains.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), str(r[3]), r[4]))
rows = []
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz:
        continue
    close = local_close(k[1], tz[k])
    p1 = [(at, b) for s, at, b in chains[k]
          if s == "P1" and at >= e["opened"] and parse(at) <= close]
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
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    obs_at, bk = book_at(k, at)
    ask = bk.get(target, {}).get("best_ask")
    if ask is None:
        continue
    lin = env.get("lineage") or {}
    ids = [x for x in str(lin.get("canonical_observation_ids", "")).split(",") if x]
    vts = [obsmeta[i][0] for i in ids if i in obsmeta]
    newest_obs_valid = max(vts) if vts else None
    known = [obsmeta[i][2] for i in ids if i in obsmeta and obsmeta[i][2] is not None]
    omax_c, omax_at = bounds.get(k, (None, None))
    ivs = json.loads(lin.get("bucket_intervals_c_json", "{}"))
    rows.append({
        "k": k, "at": at, "p_t": outside[target], "ask": float(ask),
        "edge": outside[target] - float(ask), "target": target,
        "t_ord": o[target], "truth_ord": o[tb], "won": target == tb,
        "n_obs": len(ids), "newest_obs_valid": newest_obs_valid,
        "committed_at": str(lin.get("canonical_observation_committed_at") or ""),
        "book_obs_at": obs_at,
        "run_high_c": (float(lin["metar_trajectory_running_high_c"])
                       if lin.get("metar_trajectory_running_high_c") not in (None, "") else None),
        "chain_max_seen_c": max(known) if known else None,
        "obs_max_c": omax_c, "obs_max_at": omax_at,
        "settled_c": tmax.get(k),
        "hard_ex": str(lin.get("hard_excluded_buckets") or ""),
        "iv": ivs.get(target),
        "close": close,
    })

def mins(a, b):
    if not a or not b:
        return None
    return (parse(a) - parse(b)).total_seconds() / 60.0

BANDS = (("edge>0.25", lambda r: r["edge"] > .25),
         ("edge0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         ("edge0-0.10", lambda r: 0 < r["edge"] <= .10),
         ("edge<=0", lambda r: r["edge"] <= 0))

print("sample n=%d" % len(rows))
for nm, f in BANDS:
    s = [r for r in rows if f(r)]
    print("  %-15s n=%-3d claim %.0f%% ask %.0f%% hit %.0f%%" % (
        nm, len(s), 100*statistics.mean(r["p_t"] for r in s),
        100*statistics.mean(r["ask"] for r in s),
        100*sum(r["won"] for r in s)/len(s)))
print()

print("H6-1  chain 落后物理高点多久 (分钟, 负=chain 已看过高点之后的观测)")
print("  band            n   已消费最新METAR比高点晚(中位)  决策时刻比高点晚  已追平比例  chain已知最高≥当日最高比例")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["obs_max_at"]]
    if not s: continue
    d1 = [mins(r["newest_obs_valid"], r["obs_max_at"]) for r in s if r["newest_obs_valid"]]
    d2 = [mins(r["at"], r["obs_max_at"]) for r in s]
    caught = [r for r in s if r["newest_obs_valid"] and r["newest_obs_valid"] >= r["obs_max_at"]]
    knew = [r for r in s if r["chain_max_seen_c"] is not None and r["obs_max_c"] is not None
            and r["chain_max_seen_c"] >= r["obs_max_c"] - 1e-9]
    print("  %-15s %-3d  %+8.0f  %+8.0f  %5.0f%%  %5.0f%%" % (
        nm, len(s), statistics.median(d1) if d1 else float("nan"),
        statistics.median(d2), 100*len(caught)/len(s), 100*len(knew)/len(s)))
print()

print("H6-2  只看'完全追平'的行 (chain 已消费当日最高那条观测之后的观测)")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["obs_max_at"] and r["newest_obs_valid"]
         and r["newest_obs_valid"] >= r["obs_max_at"]
         and r["chain_max_seen_c"] is not None and r["obs_max_c"] is not None
         and r["chain_max_seen_c"] >= r["obs_max_c"] - 1e-9]
    if not s:
        print("  %-15s n=0" % nm); continue
    print("  %-15s n=%-3d claim %.0f%% ask %.0f%% hit %.0f%%  目标桶在真值上方 %.0f%%" % (
        nm, len(s), 100*statistics.mean(r["p_t"] for r in s),
        100*statistics.mean(r["ask"] for r in s),
        100*sum(r["won"] for r in s)/len(s),
        100*sum(r["t_ord"] > r["truth_ord"] for r in s)/len(s)))
print()

print("H6-3  书本是否比 chain 新 (book_observed_at 与 chain 消费的最新观测 valid_at / committed_at 之差, 分钟)")
for nm, f in BANDS:
    s = [r for r in rows if f(r)]
    d = [mins(r["book_obs_at"], r["newest_obs_valid"]) for r in s if r["book_obs_at"] and r["newest_obs_valid"]]
    d2 = [mins(r["book_obs_at"], r["committed_at"]) for r in s if r["book_obs_at"] and r["committed_at"]]
    if d:
        print("  %-15s n=%-3d book-最新观测valid 中位 %+7.0f  book-committed 中位 %+7.0f" % (
            nm, len(s), statistics.median(d), statistics.median(d2) if d2 else float("nan")))
print()

print("H6-4  tail (edge>0.25) 逐行")
print("  %-14s %-10s %-8s %5s %5s  %-6s %-6s %-6s  %-9s %-9s %-9s" % (
    "station-day", "target", "iv", "p", "ask", "runHi", "chainHi", "dayHi", "hi时刻", "最新obs", "决策"))
for r in sorted([r for r in rows if r["edge"] > .25], key=lambda x: -x["edge"]):
    print("  %-14s %-10s %-8s %5.2f %5.2f  %-6s %-6s %-6s  %-9s %-9s %-9s  won=%s" % (
        r["k"][0]+" "+r["k"][1][5:], r["target"],
        str(r["iv"]), r["p_t"], r["ask"],
        r["run_high_c"], r["chain_max_seen_c"], r["obs_max_c"],
        (r["obs_max_at"] or "")[5:16], (r["newest_obs_valid"] or "")[5:16], r["at"][5:16],
        r["won"]))
