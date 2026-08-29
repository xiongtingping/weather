"""Which scopes lack an artifact, and do they line up with the ceiling failure?

The scope is station x local time bucket, the same partition the artifact ids
carry.  If the missing ones are the evening and night buckets then the promotion
question is really a coverage question, and it is pointed at exactly the hours
where the ceiling is wrong.
"""
import json, sqlite3, sys
from collections import Counter, defaultdict
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}

def bucket_of(hour):
    return ("NIGHT" if hour < 6 else "MORNING" if hour < 12
            else "AFTERNOON" if hour < 18 else "EVENING")

cover = defaultdict(lambda: {"have": 0, "none": 0})
station_gap = Counter()
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,envelope_json"
    " FROM basket_probability_bridge_history WHERE stage='P1' AND state='COMMITTED'"
):
    k = (str(r[0]), str(r[1]))
    if k not in tz:
        continue
    try:
        close = local_close(k[1], tz[k])
    except Exception:
        continue
    hour = parse(str(r[2])).astimezone(close.tzinfo).hour
    lin = json.loads(r[3]).get("lineage") or {}
    act = str(lin.get("metar_trajectory_activation_id") or "NONE")
    slot = bucket_of(hour)
    if act == "NONE":
        cover[slot]["none"] += 1
        station_gap[(k[0], slot)] += 1
    else:
        cover[slot]["have"] += 1

print("按当地时段：P1 行有没有可用轨迹产物")
print("  时段        有产物     无产物     无产物占比")
for slot in ("NIGHT", "MORNING", "AFTERNOON", "EVENING"):
    d = cover[slot]
    tot = d["have"] + d["none"]
    if tot:
        print("  %-10s %7d   %7d      %4.0f%%" % (
            slot, d["have"], d["none"], 100 * d["none"] / tot))
print()
print("缺口最大的 站x时段（前 12）:")
for (st, slot), n in station_gap.most_common(12):
    print("   %-6s %-10s %6d 行" % (st, slot, n))
print("   ... 共 %d 个 站x时段 完全没有产物" % len(station_gap))
