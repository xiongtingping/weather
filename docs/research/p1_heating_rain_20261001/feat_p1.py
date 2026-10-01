import json, sqlite3, sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT="/Users/xiong/weather"; S=__file__.rsplit('/',1)[0]
sys.path.insert(0, ROOT+"/scripts")
from calibration_dashboard import metar_precipitation_reading
LAG=timedelta(minutes=15)
def inst(v): return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
data=json.load(open(S+"/dataset_p1.json"))
ss=sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
zones={r[0]:ZoneInfo(r[1]) for r in ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
ss.close()
st={r["station"] for r in data}
ar=sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro",uri=True,timeout=30)
reps=defaultdict(list)
for s,va,raw in ar.execute("SELECT station_id,valid_at,raw_text FROM reported_trajectory_observations WHERE source_lane='awc' AND valid_at>='2026-08-19'"):
    if s in st and s in zones:
        at=inst(va); loc=at.astimezone(zones[s])
        reps[(s,loc.strftime("%Y-%m-%d"))].append((at,loc.hour,metar_precipitation_reading(raw)["present"]))
ar.close()
out=[]
for r in data:
    s,d=r["station"],r["date"]; z=zones[s]; at=inst(r["at"]); loc=at.astimezone(z)
    start=datetime.fromisoformat(d).replace(tzinfo=z)+timedelta(hours=9)
    seen=[x for x in reps.get((s,d),[]) if start<=x[0]<=at-LAG]
    out.append(dict(local_hour=loc.hour if loc.strftime("%Y-%m-%d")==d else (-1 if loc.date().isoformat()<d else 99),
        rep_hours=len({h for _,h,_ in seen}), wet_hours=len({h for _,h,p in seen if p})))
json.dump(out,open(S+"/feat_p1.json","w"))
print(Counter(min(o["wet_hours"],4) for o in out), Counter(o["local_hour"] for o in out).most_common(8))
