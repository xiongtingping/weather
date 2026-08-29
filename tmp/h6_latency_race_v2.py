"""H6 v2: separate 'the chain lacked an observation the market had' from
'the chain had everything and was still wrong'.

For each phenomenon row, rebuild the observation record for the LOCAL day from
archive.reported_trajectory_observations and answer:
  Q1 chain_seen_max  vs  available_max (all obs with valid_at <= decision_at)
  Q2 would the unconsumed-but-available obs have hard-excluded the target?
  Q3 among fully caught-up rows, what did the model still claim, and did it hit?
Caches the row set so later cuts do not rescan the 189k-row bridge table.
"""
import json, os, pickle, sqlite3, statistics, sys
from bisect import bisect_right
from datetime import timedelta
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

CACHE = os.environ.get("H6_CACHE", "/private/tmp/claude-501/-Users-xiong-weather/"
                       "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h6_rows.pkl")

def build():
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
    truth, tmax = {}, {}
    for r in conn.execute("SELECT station_id,event_date,settled_bucket,settled_tmax_c"
                          " FROM paper_trade_settlement"):
        truth[(str(r[0]), str(r[1]))] = str(r[2]); tmax[(str(r[0]), str(r[1]))] = r[3]
    tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
        "SELECT DISTINCT station_id,event_date,settlement_timezone"
        " FROM basket_official_ws_snapshot")}
    keys = sorted(truth)
    ph = ",".join(["(?,?)"] * len(keys)); flat = [x for k in keys for x in k]
    books = {}
    for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                          " FROM basket_official_ws_snapshot"
                          " WHERE (station_id,event_date) IN (VALUES %s)"
                          " ORDER BY observed_at" % ph, flat):
        books.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), r[3]))
    chains = {}
    for r in conn.execute(
            "SELECT station_id,event_date,stage,decision_at_utc,envelope_json"
            " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
            " AND (station_id,event_date) IN (VALUES %s)"
            " ORDER BY decision_at_utc" % ph, flat):
        chains.setdefault((str(r[0]), str(r[1])), []).append(
            (str(r[2]), str(r[3]), r[4]))

    def book_at(k, when):
        h = books.get(k)
        if not h: return None, {}
        i = bisect_right([x[0] for x in h], when)
        if not i: return None, {}
        return h[i - 1][0], {b["bucket"]: b for b in json.loads(h[i - 1][1])}

    out = []
    for k, e in pos.items():
        if k not in truth or k not in chains or k not in tz: continue
        close = local_close(k[1], tz[k])
        alive = None
        for s, at, blob in chains[k]:
            if s != "P1" or at < e["opened"] or parse(at) > close: continue
            env = json.loads(blob)
            if sum(v for b, v in env["probabilities"].items()
                   if b in e["held"]) > 0.05:
                alive = (at, env)
        if alive is None: continue
        at, env = alive
        p, o = env["probabilities"], env["bucket_ordinals"]
        tb = truth[k]
        if tb not in o: continue
        outside = {b: v for b, v in p.items() if b not in e["held"]}
        if not outside: continue
        target = max(outside, key=outside.get)
        obs_at, bk = book_at(k, at)
        ask = bk.get(target, {}).get("best_ask")
        if ask is None: continue
        lin = env.get("lineage") or {}
        out.append({
            "k": k, "at": at, "tz": tz[k], "close": close.isoformat(),
            "open_local": (close - timedelta(days=1)).isoformat(),
            "held": sorted(e["held"]), "probs": p, "ords": o,
            "target": target, "p_t": outside[target], "ask": float(ask),
            "edge": outside[target] - float(ask),
            "truth_bucket": tb, "settled_c": tmax.get(k), "won": target == tb,
            "book_obs_at": obs_at,
            "ids": [x for x in str(lin.get("canonical_observation_ids", "")).split(",") if x],
            "committed_at": str(lin.get("canonical_observation_committed_at") or ""),
            "run_high_c": lin.get("metar_trajectory_running_high_c"),
            "hard_ex": str(lin.get("hard_excluded_buckets") or ""),
            "iv": json.loads(lin.get("bucket_intervals_c_json", "{}")),
            "mode": lin.get("observation_likelihood_mode"),
            "strength": lin.get("metar_trajectory_applied_strength"),
            "degrade": lin.get("metar_trajectory_degradation_reasons"),
        })
    return out

if os.path.exists(CACHE):
    rows = pickle.load(open(CACHE, "rb"))
else:
    rows = build()
    pickle.dump(rows, open(CACHE, "wb"))
print("cached rows:", len(rows))
