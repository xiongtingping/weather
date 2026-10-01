"""Oracle 'advance' features: weather that will happen after the decision, inside the heating window (09:00 local -> actual peak)."""
import json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT="/Users/xiong/weather"; S=__file__.rsplit('/',1)[0]
sys.path.insert(0, ROOT+"/scripts"); sys.path.insert(0, ROOT)
from calibration_dashboard import metar_precipitation_reading
from bayes_basket_target.metar_path_state import _CLOUD
from bayes_basket_target.cross_consensus import clearness_index
FR={"FEW":0.25,"SCT":0.5,"BKN":0.875,"OVC":1.0,"VV":1.0}
def inst(v): return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
def cloud(raw):
    body=str(raw or "").upper().split(" RMK ",1)[0]; t=_CLOUD.findall(body)
    if t: return max(FR[x] for x in t)
    if any(x in body.split() for x in ("CAVOK","NCD","NSC","SKC","CLR")): return 0.0
    return None
ss=sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
zones={r[0]:ZoneInfo(r[1]) for r in ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
ver=defaultdict(list)
for s,d,sw,lat,cap in ss.execute("SELECT station_id,local_date,shortwave_radiation_sum,latitude,captured_at_utc FROM calibration_station_forecast_physics_version WHERE capture_mode IN ('forward','p0_envelope_receipt') AND shortwave_radiation_sum IS NOT NULL AND local_date>='2026-08-01'"):
    ver[(s,d)].append((inst(cap),clearness_index(sw,lat,d)))
ss.close()
for k in ver: ver[k].sort(key=lambda x: x[0])
ar=sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro",uri=True,timeout=30)
peak={}
for s,d,mv in ar.execute("SELECT station_id,event_date,max_valid_at FROM reported_trajectory_station_day_bounds WHERE source_lane='awc'"):
    if mv: peak[(s,d)]=inst(mv)
reps=defaultdict(list)
for s,rt,va,raw in ar.execute("SELECT station_id,report_type,valid_at,raw_text FROM reported_trajectory_observations WHERE source_lane='awc' AND valid_at>='2026-08-01'"):
    if s in zones: reps[s].append((inst(va),zones[s] and inst(va).astimezone(zones[s]).hour, metar_precipitation_reading(raw)["present"], cloud(raw) if rt=="METAR" else None))
ar.close()
for s in reps: reps[s].sort(key=lambda x: x[0])
for stage in ("p0","p1"):
    D=json.load(open(f"{S}/dataset_{stage}.json")); out=[]
    for r in D:
        s,d=r["station"],r["date"]; z=zones[s]; at=inst(r["at"]); pk=peak.get((s,d))
        loc=at.astimezone(z); lh=loc.hour if loc.strftime("%Y-%m-%d")==d else (-1 if loc.strftime("%Y-%m-%d")<d else 99)
        v=[k for c,k in ver.get((s,d),[]) if c<=at]
        if pk is None: out.append(None); continue
        start=max(datetime.fromisoformat(d).replace(tzinfo=z)+timedelta(hours=9), at)
        fut=[x for x in reps.get(s,[]) if start<x[0]<=pk]
        cl=[x[3] for x in fut if x[3] is not None]
        out.append(dict(lh=lh, kt=v[-1] if v else None, before_peak=at<pk,
            rain_h=len({x[1] for x in fut if x[2]}), cloud=sum(cl)/len(cl) if len(cl)>=2 else None, n=len(cl)))
    json.dump(out,open(f"{S}/fut_{stage}.json","w"))
    E=[o for o in out if o]
    print(stage,len(D),"with peak",len(E),"kt",sum(o["kt"] is not None for o in E),"rain>=2h",sum(o["rain_h"]>=2 for o in E),"cloud",sum(o["cloud"] is not None for o in E))
