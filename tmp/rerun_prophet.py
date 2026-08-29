"""Does stage-awareness change the pre-peak comparison at all?

At 12-18h before local midnight the day's observations have barely started,
so P1 may not exist yet and P0 is legitimately the only belief.  If so the
earlier pre-peak numbers stand; this checks rather than assumes.
"""
import json, sqlite3, statistics, sys
from datetime import timedelta
sys.path.insert(0, "tmp")
from stage_pick import DB, RANK, load_chains, local_close, parse
from bisect import bisect_right

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
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
chains = load_chains(conn, keys=set(truth))

print("提前量  站日   模型argmax(纯时间)  模型argmax(stage优先)  取到的stage  市场最贵格")
for lead in (18, 12, 6, 3, 0):
    n = a_old = a_new = mkt = 0
    stages = {}
    for k, tb in truth.items():
        if k not in tz or k not in chains or k not in books:
            continue
        try:
            cut = (local_close(k[1], tz[k]) - timedelta(hours=lead))
        except Exception:
            continue
        cs = cut.isoformat().replace("+00:00", "Z")
        cand = [(s, at, b) for s, at, b in chains[k] if at <= cs]
        if not cand:
            continue
        old = json.loads(cand[-1][2])["probabilities"]
        best = max(cand, key=lambda x: (RANK.get(x[0], 0), x[1]))
        stages[best[0]] = stages.get(best[0], 0) + 1
        new = json.loads(best[2])["probabilities"]
        h = books[k]
        i = bisect_right([x[0] for x in h], cs)
        if not i:
            continue
        priced = {b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
                  if b.get("best_ask")}
        if not priced:
            continue
        n += 1
        a_old += max(old, key=old.get) == tb
        a_new += max(new, key=new.get) == tb
        mkt += max(priced, key=priced.get) == tb
    if n:
        print("%3dh   %4d        %4.0f%%             %4.0f%%           %-22s %4.0f%%" % (
            lead, n, 100 * a_old / n, 100 * a_new / n, stages, 100 * mkt / n))
