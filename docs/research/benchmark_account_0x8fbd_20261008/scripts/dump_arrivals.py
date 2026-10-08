import sqlite3, json, time, sys
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
stations=sorted(set(CITY2ST.values()))
dates=[f"2026-09-{d:02d}" for d in range(21,31)]+[f"2026-10-{d:02d}" for d in range(1,11)]
t0=time.time()
taf=[]; runs=[]; obs=[]
for st in stations:
    for ed in dates:
        taf+=[dict(station=st,event_date=ed,issued=r[0],avail=r[1],received=r[2],tx=r[3],kind=r[4],seq=r[5]) for r in conn.execute("select issued_at_utc,provider_available_at_utc,received_at_utc,tx_c,revision_kind,revision_sequence from calibration_forward_taf_capture where station_id=? and event_date=?",(st,ed))]
        runs+=[dict(station=st,target=ed,system=r[0],run_init=r[1],cycle=r[2],first_recv=r[3],last_recv=r[4],n=r[5],published=r[6]) for r in conn.execute("select ensemble_system_id,run_init_utc,run_cycle,min(received_at_utc),max(received_at_utc),count(*),min(published_at_utc) from calibration_native_ensemble_member_run where station_id=? and target_local_date=? group by 1,2,3",(st,ed))]
        obs+=[dict(station=st,event_date=ed,observed=r[0],committed=r[1],created=r[2],type=r[3]) for r in conn.execute("select observed_at_utc,canonical_committed_at,created_at,report_type from basket_target_observation_facts where station_id=? and event_date=?",(st,ed))]
    print(st,len(taf),len(runs),len(obs),round(time.time()-t0),"s",file=sys.stderr)
conn.close()
json.dump({"taf":taf,"runs":runs,"obs":obs},open("arrivals.json","w"))
print("taf",len(taf),"runs",len(runs),"obs",len(obs),file=sys.stderr)
import collections
print("systems:",collections.Counter(r["system"] for r in runs).most_common(),file=sys.stderr)
print("taf kinds:",collections.Counter(r["kind"] for r in taf),file=sys.stderr)
