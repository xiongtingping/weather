"""H3: does P1's likelihood ratio fail to move the far tail?

Reads the real proofs (basket_probability_update_proof.proof_json carries
likelihood_ratio_by_bucket verbatim -- no hand-reimplementation of the rule),
joined to the stage-aware belief used for the target-bucket phenomenon.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)

held = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = held.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))

settled = {}
for r in conn.execute("SELECT station_id,event_date,settled_bucket,settled_tmax_c"
                      " FROM paper_trade_settlement"):
    settled[(str(r[0]), str(r[1]))] = {"truth": str(r[2]), "tmax": r[3],
                                       "tz": None}
tzs = {}
for r in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone"
                      " FROM basket_probability_bridge_history"):
    tzs[(str(r[0]), str(r[1]))] = str(r[2])

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in settled:
        books.setdefault(k, []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
             if b.get("best_ask")} if i else {})

def proof_by_sha(sha):
    r = conn.execute("SELECT proof_json FROM basket_probability_update_proof"
                     " WHERE content_sha256=? LIMIT 1", (sha,)).fetchone()
    return r[0] if r else None

_root_cache = {}
def root_by_sha(key, sha):
    if key not in _root_cache:
        _root_cache[key] = {
            str(x[0]): x[1] for x in conn.execute(
                "SELECT envelope_content_sha256,envelope_json FROM"
                " basket_probability_bridge_history WHERE station_id=?"
                " AND event_date=? AND stage='P0'", key)}
    return _root_cache[key].get(sha)

rows = []
for k, e in held.items():
    if k not in settled:
        continue
    chain = [(str(x[0]), str(x[1]), x[2]) for x in conn.execute(
        "SELECT stage,decision_at_utc,envelope_json FROM"
        " basket_probability_bridge_history WHERE state='COMMITTED'"
        " AND station_id=? AND event_date=? AND stage='P1'"
        " ORDER BY decision_at_utc", k)]
    if not chain:
        continue
    tz = tzs.get(k) or settled[k]["tz"]
    close = local_close(k[1], tz) if tz else None
    alive = None
    for stage, at, blob in chain:
        if stage != "P1" or at < e["opened"]:
            continue
        if close is not None and (close - parse(at)).total_seconds() < 0:
            continue          # after the settlement boundary
        env = json.loads(blob)
        inside = sum(v for b, v in env["probabilities"].items()
                     if b in e["held"])
        if inside > 0.05:
            alive = (at, env, inside)
    if alive is None:
        continue
    at, env, inside = alive
    p, o = env["probabilities"], env["bucket_ordinals"]
    lin = env.get("lineage") or {}
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    ask = asks_at(k, at).get(target)
    pj = proof_by_sha(str(lin.get("metar_trajectory_proof_sha256") or ""))
    if pj is None:
        continue
    pr = json.loads(pj)
    prior_blob = root_by_sha(k, str(lin.get("p0_root_content_sha256") or ""))
    prior = json.loads(prior_blob)["probabilities"] if prior_blob else None
    iv = json.loads(lin.get("bucket_intervals_c_json") or "{}")
    centers = {}
    for b, (lo, hi) in iv.items():
        if lo is None and hi is None:
            continue
        centers[b] = (hi - 0.5 if lo is None else lo + 0.5 if hi is None
                      else (lo + hi) / 2.0)
    rows.append({
        "k": k, "at": at, "held": e["held"], "p": p, "o": o,
        "target": target, "p_t": outside[target], "ask": ask,
        "truth": settled[k]["truth"], "prior": prior, "centers": centers,
        "lr": pr["likelihood_ratio_by_bucket"],
        "strength": float(pr["applied_strength"]),
        "rh": float(pr["running_high_c"]),
        "excluded": set(pr["hard_excluded_buckets"]),
        "reasons": pr.get("degradation_reasons") or [],
        "center_c": float(pr.get("model_center_c")
                          or lin.get("model_center_c") or "nan"),
        "center_b": (pr.get("model_center_bucket")
                     or lin.get("model_center_bucket")),
        "argmax_b": pr.get("center_bucket"),
        "mode": lin.get("observation_likelihood_mode"),
        "lead_h": ((close - parse(at)).total_seconds() / 3600.0
                   if close else None),
    })

q = [r for r in rows if r["ask"] is not None]
print("站日 n=%d，其中有盘口定价 n=%d" % (len(rows), len(q)))
def bins(sel_rows):
    out = []
    for lo, hi, name in ((-9, 0, "<=0"), (0, .10, "0~0.10"),
                         (.10, .25, "0.10-0.25"), (.25, 9, ">0.25")):
        sel = [r for r in sel_rows if lo <= r["p_t"] - r["ask"] < hi] if lo < 0 \
            else [r for r in sel_rows if lo < r["p_t"] - r["ask"] <= hi]
        out.append((name, sel))
    return out
print("复现边际分箱：")
for name, sel in bins(q):
    if sel:
        print("  边际%-10s n=%-3d 模型说%3.0f%% 市场%3.0f%% 实际%3.0f%%" % (
            name, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
            100*statistics.mean(r["ask"] for r in sel),
            100*sum(1 for r in sel if r["truth"] == r["target"])/len(sel)))
json.dump([{kk: (list(vv) if isinstance(vv, set) else vv)
            for kk, vv in r.items() if kk != "k"} | {"k": list(r["k"])}
           for r in q], open("tmp/h3_cases.json", "w"))
print("\n=== 强度 applied_strength 分布（这些行）")
st = [r["strength"] for r in q]
print("  中位 %.3f  均值 %.3f  =0 的行 %d/%d (%.0f%%)" % (
    statistics.median(st), statistics.mean(st),
    sum(1 for x in st if x == 0.0), len(st),
    100*sum(1 for x in st if x == 0.0)/len(st)))
from collections import Counter
print("  降级原因:", Counter(tuple(r["reasons"]) for r in q).most_common(6))
