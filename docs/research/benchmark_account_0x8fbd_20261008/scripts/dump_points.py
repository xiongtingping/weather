import sqlite3, json, sys, time, collections
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
dates=[f"2026-09-{d:02d}" for d in range(21,31)]+[f"2026-10-{d:02d}" for d in range(1,11)]
t0=time.time(); out=[]
for st in sorted(set(CITY2ST.values())):
    for ed in dates:
        out+=[dict(station=st,target=ed,system=r[0],run_init=r[1],tmax=r[2],knowable=r[3],captured=r[4],complete=r[5]) for r in conn.execute("select ensemble_system_id,run_init_utc,forecast_tmax_c,knowable_at_utc,archive_query_at_utc,remaining_peak_complete from calibration_regional_point_run where station_id=? and target_local_date=?",(st,ed))]
conn.close()
json.dump(out,open("points.json","w"))
print("point runs",len(out),round(time.time()-t0),"s",file=sys.stderr)
print("systems:",collections.Counter(r["system"] for r in out).most_common(),file=sys.stderr)
print("stations per system:",{s:len(set(r["station"] for r in out if r["system"]==s)) for s in set(r["system"] for r in out)},file=sys.stderr)
