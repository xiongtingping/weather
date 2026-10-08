import sqlite3, json, sys, time, collections
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
SYS=("ECMWF_IFS_ENS_OPEN_PF50","ECMWF_AIFS_ENS_GLOBAL","DWD_ICON_GLOBAL_EPS","NOAA_GEFS_05","ECCC_GEM_GLOBAL_ENS","NOAA_AIGEFS_025")
dates=[f"2026-09-{d:02d}" for d in range(5,31)]+[f"2026-10-{d:02d}" for d in range(1,8)]
t0=time.time(); out=[]
for st in sorted(set(CITY2ST.values())):
    for ed in dates:
        for r in conn.execute("select ensemble_system_id,archive_lead_bucket,archive_query_at_utc,member_count,bucket_intervals_json,raw_bucket_probabilities_json from calibration_native_ensemble_historical_member_bucket where station_id=? and target_local_date=?",(st,ed)):
            if r[0] not in SYS: continue
            out.append(dict(station=st,target=ed,system=r[0],lead=r[1],asof=r[2],n=r[3],intervals=json.loads(r[4]),p=json.loads(r[5])))
conn.close()
json.dump(out,open("sysbuckets.json","w"))
print("rows",len(out),round(time.time()-t0),"s | per system",collections.Counter(r["system"] for r in out).most_common(),file=sys.stderr)
print("sample",{k:(v if k!="p" and k!="intervals" else str(v)[:120]) for k,v in out[-1].items()},file=sys.stderr)
