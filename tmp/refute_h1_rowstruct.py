import json, sqlite3, sys
from collections import defaultdict, Counter
sys.path.insert(0,"tmp")
from stage_pick import DB, local_close, parse
conn=sqlite3.connect(DB,uri=True,timeout=120)
settle={}
for st,ed,sb,stmax in conn.execute("SELECT s.station_id,s.event_date,s.settled_bucket,s.settled_tmax_c FROM paper_trade_settlement s JOIN paper_trade_position p ON p.position_id=s.position_id"):
    settle[(str(st),str(ed))]=(sb,stmax)
keys=sorted(settle)
k=keys[0]
print("example key",k, settle[k])
rows=list(conn.execute("SELECT rowid,stage,decision_at_utc,envelope_json,bucket_intervals_json,settlement_timezone FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",k))
print("total rows",len(rows))
z=rows[0][5]; close=local_close(k[1],z)
# structure: per decision_at_utc, how many P1 rows, what running_high values
byat=defaultdict(list)
for rid,stage,at,env,bi,_ in rows:
    if stage!='P1': continue
    e=json.loads(env); lin=e.get("lineage") or {}
    byat[at].append((rid,lin.get("metar_trajectory_running_high_c"),lin.get("hard_excluded_buckets"),lin.get("metar_trajectory_applied_strength")))
print("distinct P1 decision instants:",len(byat))
c=Counter(len(v) for v in byat.values())
print("rows-per-instant histogram:",dict(c))
ats=sorted(byat)
for at in ats[:3]+ats[-3:]:
    d=parse(at); lh=d.astimezone(close.tzinfo)
    v=byat[at]
    print(at,"local",lh.strftime("%H:%M"),"n=",len(v),"rh seq:",[x[1] for x in v][:12],"rowid range",v[0][0],v[-1][0])
