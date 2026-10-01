"""P1 sunshine: forecast clearness/cloud (as-of forward physics version) vs observed METAR cloud in the heating window."""
import json, sqlite3, sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT="/Users/xiong/weather"; S=__file__.rsplit('/',1)[0]
sys.path.insert(0, ROOT)
from bayes_basket_target.metar_path_state import _CLOUD
from bayes_basket_target.cross_consensus import clearness_index
LAG=timedelta(minutes=15)
FR={"FEW":0.25,"SCT":0.5,"BKN":0.875,"OVC":1.0,"VV":1.0}
def inst(v): return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
def cloud(raw):
    body=str(raw or "").upper().split(" RMK ",1)[0]
    t=_CLOUD.findall(body)
    if t: return max(FR[x] for x in t)
    if any(x in body.split() for x in ("CAVOK","NCD","NSC","SKC","CLR")): return 0.0
    return None
D=json.load(open(S+"/dataset_p1.json"))
ss=sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
zones={r[0]:ZoneInfo(r[1]) for r in ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
ver=defaultdict(list)
for s,d,sw,cc,lat,cap in ss.execute("SELECT station_id,local_date,shortwave_radiation_sum,cloudcover_mean,latitude,captured_at_utc FROM calibration_station_forecast_physics_version WHERE capture_mode IN ('forward','p0_envelope_receipt') AND shortwave_radiation_sum IS NOT NULL AND local_date>='2026-08-20' AND source_id LIKE '%'"):
    ver[(s,d)].append((inst(cap),sw,cc,lat))
ss.close()
for k in ver: ver[k].sort()
st={r["station"] for r in D}
ar=sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro",uri=True,timeout=30)
reps=defaultdict(list)
for s,rt,va,raw in ar.execute("SELECT station_id,report_type,valid_at,raw_text FROM reported_trajectory_observations WHERE source_lane='awc' AND valid_at>='2026-08-19'"):
    if s in st and rt=="METAR":
        c=cloud(raw)
        if c is not None: reps[s].append((inst(va),c))
ar.close()
for s in reps: reps[s].sort()
out=[]
for r in D:
    s,d=r["station"],r["date"]; z=zones[s]; at=inst(r["at"]); loc=at.astimezone(z)
    lh=loc.hour if loc.strftime("%Y-%m-%d")==d else 99
    v=[x for x in ver.get((s,d),[]) if x[0]<=at]
    fkt=fcc=None
    if v:
        _,sw,cc,lat=v[-1]; fkt=clearness_index(sw,lat,d); fcc=None if cc is None else cc/100.0
    start=datetime.fromisoformat(d).replace(tzinfo=z)+timedelta(hours=9); end16=datetime.fromisoformat(d).replace(tzinfo=z)+timedelta(hours=16)
    seen=[c for a,c in reps.get(s,[]) if start<=a<=at-LAG]
    rest=[c for a,c in reps.get(s,[]) if at-LAG<a<=end16]
    out.append(dict(lh=lh,fkt=fkt,fcc=fcc,obs_mean=sum(seen)/len(seen) if seen else None,obs_now=seen[-1] if seen else None,
                    n_seen=len(seen),rest_mean=sum(rest)/len(rest) if rest else None))
json.dump(out,open(S+"/sun_feat.json","w"))
E=[o for o in out if 9<=o["lh"]<=16]
print(len(E),"fcc",sum(o["fcc"] is not None for o in E),"obs",sum(o["obs_mean"] is not None for o in E),"rest",sum(o["rest_mean"] is not None for o in E))
