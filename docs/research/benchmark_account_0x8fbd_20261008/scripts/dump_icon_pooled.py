import sqlite3, json, sys, time, collections
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
dates=[f"2026-09-{d:02d}" for d in range(5,31)]+[f"2026-10-{d:02d}" for d in range(1,8)]
t0=time.time(); icon=[]; pooled=[]
for st in sorted(set(CITY2ST.values())):
    for ed in dates:
        for r in conn.execute("select native_run_init_utc,archive_lead_bucket,archive_query_at_utc,member_id,raw_tmax_c from calibration_native_ensemble_historical_member_run where station_id=? and target_local_date=? and ensemble_system_id='DWD_ICON_GLOBAL_EPS'",(st,ed)):
            icon.append(dict(station=st,target=ed,run=r[0],lead=r[1],asof=r[2],member=r[3],t=r[4]))
        for r in conn.execute("select decision_at_utc,lead_bucket,run_cycle,ensemble_system_ids_json,system_weights_json,probabilities_json,bucket_centers_c_json,prediction_oos from calibration_cross_native_ensemble_oos_prediction where station_id=? and event_date=?",(st,ed)):
            pooled.append(dict(station=st,target=ed,dec=r[0],lead=r[1],cycle=r[2],systems=json.loads(r[3]),weights=json.loads(r[4]),p=json.loads(r[5]),centers=json.loads(r[6]),oos=r[7]))
conn.close()
json.dump(icon,open("icon_members.json","w")); json.dump(pooled,open("pooled.json","w"))
print("icon members",len(icon),"| pooled predictions",len(pooled),round(time.time()-t0),"s",file=sys.stderr)
print("pooled sample:",{k:(str(v)[:160]) for k,v in pooled[-1].items()} if pooled else None,file=sys.stderr)
