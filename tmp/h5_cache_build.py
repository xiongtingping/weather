"""H5: cache only the settled station-days' bridge rows + books, once."""
import json, sqlite3, pickle, sys, time
sys.path.insert(0, "tmp")
from stage_pick import DB

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
t0 = time.time()
pos = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = pos.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))
truth = {(str(r[0]), str(r[1])): (str(r[2]), r[3]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket,settled_tmax_c"
    " FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
keys = set(truth) & set(pos)
print("keys", len(keys), time.time()-t0)

chains = {}
for r in conn.execute(
        "SELECT station_id,event_date,stage,decision_at_utc,envelope_json"
        " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
        " ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    if k not in keys:
        continue
    chains.setdefault(k, []).append((str(r[2]), str(r[3]), json.loads(r[4])))
print("chain rows", sum(len(v) for v in chains.values()), time.time()-t0)

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k not in keys:
        continue
    asks = {}
    for b in json.loads(r[3]):
        if b.get("best_ask"):
            asks[b["bucket"]] = float(b["best_ask"])
    books.setdefault(k, []).append((str(r[2]), asks))
print("book rows", sum(len(v) for v in books.values()), time.time()-t0)

pickle.dump({"pos": pos, "truth": truth, "tz": tz, "chains": chains,
             "books": books}, open("tmp/h5_cache.pkl", "wb"))
print("done", time.time()-t0)
