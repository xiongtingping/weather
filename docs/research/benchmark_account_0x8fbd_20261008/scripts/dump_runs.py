import sqlite3, json, time, sys, collections
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
print("asof_model_run cols:",[r[1] for r in conn.execute("PRAGMA table_info(calibration_historical_asof_model_run)")],file=sys.stderr)
print("asof sample:",conn.execute("select * from calibration_historical_asof_model_run where station_id='EPWA' order by rowid desc limit 1").fetchone(),file=sys.stderr)
stations=sorted(set(CITY2ST.values()))
dates=[f"2026-09-{d:02d}" for d in range(21,31)]+[f"2026-10-{d:02d}" for d in range(1,11)]
t0=time.time(); runs=[]
for st in stations:
    for ed in dates:
        runs+=[dict(station=st,target=ed,system=r[0],run_init=r[1],lead=r[2],first_arr=r[3],last_arr=r[4],n=r[5]) for r in conn.execute("select ensemble_system_id,native_run_init_utc,archive_lead_bucket,min(archive_query_at_utc),max(archive_query_at_utc),count(*) from calibration_native_ensemble_historical_member_run where station_id=? and target_local_date=? group by 1,2,3",(st,ed))]
print("native runs",len(runs),round(time.time()-t0),"s",file=sys.stderr)
print("systems:",collections.Counter(r["system"] for r in runs).most_common(),file=sys.stderr)
print("sample:",runs[-3:],file=sys.stderr)
conn.close()
json.dump(runs,open("runs.json","w"))
