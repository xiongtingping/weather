"""Model vs market, scored only before the day's peak can have happened.

Lead is measured to local settlement midnight, so anything under 12 hours is
local afternoon and the observed high may already be in -- confirmation, not
prediction.  At 12 hours and beyond both callers are still guessing, which is
the only place a comparison means anything.
"""
import json, sqlite3, statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60.0)
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))

tzmap = {}
for r in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone"
                      " FROM basket_official_ws_snapshot"):
    tzmap[(str(r[0]), str(r[1]))] = str(r[2])

def close_of(key):
    tz = tzmap.get(key)
    if not tz:
        return None
    try:
        return datetime.fromisoformat(key[1]).replace(tzinfo=ZoneInfo(tz)) + timedelta(days=1)
    except Exception:
        return None

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    key = (str(r[0]), str(r[1]))
    if key in truth:
        books.setdefault(key, []).append((parse(str(r[2])), r[3]))

envs = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,envelope_json"
                      " FROM basket_probability_bridge_history WHERE state='COMMITTED'"
                      " ORDER BY decision_at_utc"):
    key = (str(r[0]), str(r[1]))
    if key in truth:
        envs.setdefault(key, []).append((parse(str(r[2])), json.loads(r[3])["probabilities"]))

def latest_before(seq, cutoff):
    pick = None
    for stamp, payload in seq:
        if stamp <= cutoff:
            pick = payload
    return pick

print("在提前量 X 小时那一刻（当地午夜前，>12h 表示峰值尚未出现）")
print("提前量   站日   模型 argmax   市场最贵那格   均价    两者一致时")
for lead in (36, 24, 18, 12, 6):
    hitm = hitk = agree = agree_hit = n = 0
    prices = []
    for key, tb in truth.items():
        close = close_of(key)
        if close is None or key not in books or key not in envs:
            continue
        cutoff = close - timedelta(hours=lead)
        blob = latest_before(books[key], cutoff)
        probs = latest_before(envs[key], cutoff)
        if blob is None or probs is None:
            continue
        asks = {b["bucket"]: b.get("best_ask") for b in json.loads(blob)}
        priced = {b: v for b, v in asks.items() if v}
        if not priced:
            continue
        n += 1
        mpick = max(probs, key=probs.get)
        kpick = max(priced, key=priced.get)
        prices.append(priced[kpick])
        hitm += mpick == tb
        hitk += kpick == tb
        if mpick == kpick:
            agree += 1
            agree_hit += mpick == tb
    if n:
        print("%3dh    %4d     %4.0f%%          %4.0f%%       %.3f    %3d 个 · 命中 %3.0f%%" % (
            lead, n, 100 * hitm / n, 100 * hitk / n,
            statistics.mean(prices), agree, 100 * agree_hit / agree if agree else 0))
