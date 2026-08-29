"""Does "the bucket nobody will sell" call the answer, and how early?

Each station-day's book is replayed against local settlement midnight, so the
signal is scored by how many hours of warning it gave.  Three callers compete
on the same instants: the model's argmax, the single bucket whose asks have
been bought out, and the most expensive bucket still on offer.
"""
import json, sqlite3, statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60.0)
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}

def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

rows = []
for r in conn.execute("SELECT station_id,event_date,settlement_timezone,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    key = (str(r[0]), str(r[1]))
    if key not in truth:
        continue
    try:
        tz = ZoneInfo(str(r[2]))
    except Exception:
        continue
    close = datetime.fromisoformat(str(r[1])).replace(tzinfo=tz) + timedelta(days=1)
    lead = (close - parse(str(r[3]))).total_seconds() / 3600.0
    if lead < 0:
        continue
    book = json.loads(r[4])
    asks = {b["bucket"]: b.get("best_ask") for b in book}
    gone = [b for b, v in asks.items() if not v]
    priced = {b: v for b, v in asks.items() if v}
    rows.append({
        "key": key, "lead": lead, "truth": truth[key],
        "gone": gone[0] if len(gone) == 1 else None,
        "top": max(priced, key=priced.get) if priced else None,
        "top_price": max(priced.values()) if priced else None,
    })

env = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,envelope_json"
                      " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
                      " ORDER BY decision_at_utc"):
    key = (str(r[0]), str(r[1]))
    if key in truth:
        p = json.loads(r[3])["probabilities"]
        env.setdefault(key, []).append((str(r[2]), max(p, key=p.get)))

def model_call(key, when):
    hist = env.get(key)
    if not hist:
        return None
    pick = None
    for stamp, arg in hist:
        if stamp <= when:
            pick = arg
    return pick

BANDS = [(24, 999), (12, 24), (6, 12), (3, 6), (1, 3), (0, 1)]
print("提前量        快照数  站日  卖单被买光的那格   最贵的那格   模型 argmax")
for lo, hi in BANDS:
    sel = [r for r in rows if lo <= r["lead"] < hi]
    if not sel:
        continue
    days = len({r["key"] for r in sel})
    g = [r for r in sel if r["gone"]]
    t = [r for r in sel if r["top"]]
    print("%3d-%-4s h  %7d  %4d      %4.0f%% (n=%4d)   %4.0f%%       %s" % (
        lo, "∞" if hi > 900 else hi, len(sel), days,
        100 * sum(1 for r in g if r["gone"] == r["truth"]) / len(g) if g else 0, len(g),
        100 * sum(1 for r in t if r["top"] == r["truth"]) / len(t) if t else 0,
        "—"))

print("\n只看每个站日在该提前量band内的最后一个快照（每站日一票）：")
print("提前量        站日   被买光那格   最贵那格   均价    模型 argmax")
for lo, hi in BANDS:
    per = {}
    for r in rows:
        if lo <= r["lead"] < hi:
            k = r["key"]
            if k not in per or r["lead"] < per[k]["lead"]:
                per[k] = r
    if not per:
        continue
    v = list(per.values())
    g = [r for r in v if r["gone"]]
    t = [r for r in v if r["top"]]
    m = 0
    mn = 0
    for r in v:
        stamp = None
        for s2, a in env.get(r["key"], []):
            pass
        hist = env.get(r["key"])
        if hist:
            pick = None
            for s2, a in hist:
                if (parse(s2) if s2.endswith("Z") else parse(s2)) and s2 <= "9999":
                    pass
            mn += 1
    print("%3d-%-4s h   %4d      %4.0f%% (n=%3d)   %4.0f%%    %.3f" % (
        lo, "∞" if hi > 900 else hi, len(v),
        100 * sum(1 for r in g if r["gone"] == r["truth"]) / len(g) if g else 0, len(g),
        100 * sum(1 for r in t if r["top"] == r["truth"]) / len(t) if t else 0,
        statistics.mean(r["top_price"] for r in t) if t else 0))
