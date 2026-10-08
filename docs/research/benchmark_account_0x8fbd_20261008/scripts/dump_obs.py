import sqlite3, json, sys, time
sys.path.insert(0,'.')
from lib import CITY2ST
A="/Users/xiong/weather/data/archive.db"
conn=sqlite3.connect(f"file:{A}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
t0=time.time(); out=[]
for st in sorted(set(CITY2ST.values())):
    rows=conn.execute("select valid_at,reported_temperature_c,canonical_committed_at,first_seen_at,source_lane,report_type from reported_trajectory_observations where station_id=? and valid_at>='2026-09-21' and valid_at<'2026-10-09' and reported_temperature_c is not null",(st,)).fetchall()
    out+=[dict(station=st,valid=r[0],t=r[1],committed=r[2],seen=r[3],lane=r[4],type=r[5]) for r in rows]
conn.close()
json.dump(out,open("obs_temps.json","w"))
import collections
print("obs",len(out),round(time.time()-t0),"s | lanes",collections.Counter(o["lane"] for o in out).most_common(4),"| sample",out[-1],file=sys.stderr)
