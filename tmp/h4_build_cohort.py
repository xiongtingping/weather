"""Build the stage-aware out-of-window-target cohort once, cache to JSON.

Same rule as tmp/rerun_verdict.py (belief = last P1 row at/after the open
whose held window still carries >5%), plus the settlement-boundary discard:
lead = local_close(event_date, tz) - decision_at_utc must be >= 0.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

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

rows, skipped = [], {"no_truth": 0, "no_alive_p1": 0, "no_ask": 0,
                     "no_tz": 0, "truth_off_map": 0}
for k, e in pos.items():
    if k not in truth:
        skipped["no_truth"] += 1; continue
    tzr = conn.execute("SELECT settlement_timezone FROM"
                       " basket_official_ws_snapshot WHERE station_id=? AND"
                       " event_date=? LIMIT 1", k).fetchone()
    if not tzr:
        skipped["no_tz"] += 1; continue
    close = local_close(k[1], str(tzr[0]))
    alive = None
    for stage, at, blob in conn.execute(
            "SELECT stage,decision_at_utc,envelope_json FROM"
            " basket_probability_bridge_history WHERE station_id=? AND"
            " event_date=? AND state='COMMITTED' ORDER BY decision_at_utc", k):
        at = str(at)
        if str(stage) != "P1" or at < e["opened"]:
            continue
        if (close - parse(at)).total_seconds() < 0:
            continue
        env = json.loads(blob)
        if sum(v for b, v in env["probabilities"].items()
               if b in e["held"]) > 0.05:
            alive = (at, env)
    if alive is None:
        skipped["no_alive_p1"] += 1; continue
    at, env = alive
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k]
    if tb not in o:
        skipped["truth_off_map"] += 1; continue
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    snaps = conn.execute(
        "SELECT observed_at,buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
        (k[0], k[1], at)).fetchone()
    ask = None
    if snaps:
        ask = {b["bucket"]: float(b["best_ask"]) for b in json.loads(snaps[1])
               if b.get("best_ask")}.get(target)
    if ask is None:
        skipped["no_ask"] += 1; continue
    lin = env.get("lineage") or {}
    rows.append({"station": k[0], "date": k[1], "at": at,
                 "p": outside[target], "ask": ask,
                 "edge": outside[target] - ask, "won": int(target == tb),
                 "lead_h": (close - parse(at)).total_seconds() / 3600.0,
                 "target": target, "truth": tb,
                 "t_ord": o[target], "truth_ord": o[tb],
                 "held": sorted(e["held"]),
                 "running_high_c": lin.get("metar_trajectory_running_high_c"),
                 "center_c": lin.get("model_center_c")})

json.dump(rows, open("tmp/h4_cohort.json", "w"))
print("skipped:", skipped, "kept:", len(rows))
BANDS = ((-9, 0, "edge<=0"), (0, .10, "0..0.10"),
         (.10, .25, "0.10..0.25"), (.25, 9, ">0.25"))
for lo, hi, name in BANDS:
    s = [r for r in rows if (r["edge"] <= hi if lo == -9
                             else lo < r["edge"] <= hi)]
    if s:
        print("  %-11s n=%-3d model %3.0f%% market %3.0f%% happened %3.0f%%"
              % (name, len(s), 100*statistics.mean(r["p"] for r in s),
                 100*statistics.mean(r["ask"] for r in s),
                 100*sum(r["won"] for r in s)/len(s)))
