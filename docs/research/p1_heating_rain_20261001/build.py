import json, sqlite3, sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT="/Users/xiong/weather"
sys.path.insert(0, ROOT+"/scripts"); sys.path.insert(0, ROOT)
from calibration_dashboard import metar_precipitation_reading, PRECIP_ASOS_REMARK_SHARE
OUT="docs/research/p1_heating_rain_20261001/rows.json"
def inst(v): return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
data=json.load(open(ROOT+"/docs/research/conditional_belief_20260923/dataset.json"))
ss=sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
zones={r[0]:ZoneInfo(r[1]) for r in ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
truth={}
for s,d,t in ss.execute("""WITH r AS (SELECT station_id,local_date,official_tmax_c, ROW_NUMBER() OVER (PARTITION BY station_id,local_date ORDER BY revision_no DESC,canonical_committed_at_utc DESC,truth_id DESC) k
 FROM calibration_station_daily_high_truth WHERE source_lane IN ('AWC','SETTLEMENT_SOURCE') AND settlement_definition='official_daily_tmax_c' AND qc_status='ACCEPTED')
 SELECT station_id,local_date,official_tmax_c FROM r WHERE k=1 AND local_date BETWEEN '2026-08-14' AND '2026-09-23'"""):
    truth[(s,d)]=t
fc={}
for s,d,cap,mode,js in ss.execute("SELECT station_id,local_date,captured_at_utc,capture_mode,hourly_precip_mm_json FROM calibration_station_forecast_precip_hourly_version WHERE capture_mode='historical' ORDER BY captured_at_utc"):
    fc[(s,d)]=json.loads(js)
ss.close()
ar=sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro",uri=True,timeout=30)
peak={}
for s,d,mv in ar.execute("SELECT station_id,event_date,max_valid_at FROM reported_trajectory_station_day_bounds WHERE source_lane='awc'"):
    if mv: peak[(s,d)]=inst(mv)
stations={r["station"] for r in data}
reps=defaultdict(list); mn=Counter(); an=Counter()
for s,rt,va,raw in ar.execute("SELECT station_id,report_type,valid_at,raw_text FROM reported_trajectory_observations WHERE source_lane='awc' AND valid_at>='2026-08-13'"):
    if s not in stations or s not in zones: continue
    at=inst(va); loc=at.astimezone(zones[s]); rd=metar_precipitation_reading(raw)
    reps[(s,loc.strftime("%Y-%m-%d"))].append((at,loc,rd["present"],rd["hourly_mm"],rt=="METAR"))
    if rt=="METAR":
        mn[s]+=1; an[s]+=bool(rd["asos"])
ar.close()
asos={s for s in mn if an[s]>=PRECIP_ASOS_REMARK_SHARE*mn[s]}
def obs_window(key, end):
    """Rain in the heating window: 09:00 local up to `end` (actual peak, or decision time)."""
    s,d=key; z=zones[s]; start=datetime.fromisoformat(d).replace(tzinfo=z)+timedelta(hours=9)
    rep=reps.get(key,[])
    hrs={l.hour for a,l,p,mm,m in rep if m and start<=a<=end}
    wet={l.hour for a,l,p,mm,m in rep if p and start<=a<=end}
    mm=None
    if s in asos:
        h={}
        for a,l,p,x,m in rep:
            if m and start<a<=end+timedelta(minutes=59): h[l.hour]=max(h.get(l.hour,0),x or 0)
        mm=sum(h.values())
    return len(hrs),len(wet),mm
out=[]
seen=set()
for r in data:
    key=(r["station"],r["date"]); st=r["stage"]
    if key not in truth or key not in peak or r["station"] not in zones: continue
    f=r["features"]; mu=f.get("mu_p")
    m=json.loads(r["m"]) if isinstance(r["m"],str) else r["m"]
    p=json.loads(r["p"]) if isinstance(r["p"],str) else r["p"]
    c=json.loads(r["centers"]) if isinstance(r["centers"],str) else r["centers"]
    mc=sum(a*b for a,b in zip(m,c))/sum(m)
    at=inst(r["at"]); pk=peak[key]
    if st=="P0":
        if (st,key) in seen: continue   # first P0 decision of the station-day
        end=pk
    elif st=="P1":
        loc=at.astimezone(zones[r["station"]])
        if not (at<pk and 11<=loc.hour<=15): continue
        end=at
    else: continue
    seen.add((st,key))
    nh,wet,mm=obs_window(key,end)
    if nh<3: continue
    hf=fc.get(key)
    fmm=None if hf is None else sum(hf[f"{h:02d}"] for h in range(10,16))   # hourly totals ending 10..15 local = 09:00-15:00
    hit=int(r["hit"])
    out.append(dict(stage=st,station=r["station"],date=r["date"],at=r["at"],truth=truth[key],mu=mu,mc=mc,
        sigma=f.get("sigma_p"),p_hit=p[hit],m_hit=m[hit]/sum(m),obs_hours=nh,wet_hours=wet,obs_mm=mm,asos=r["station"] in asos,
        fc_heat_mm=fmm,peak_hour=pk.astimezone(zones[r["station"]]).hour,unit_f=(c[1]-c[0])>1.05 if len(c)>1 else False))
json.dump(out,open(OUT,"w"))
print(len(out),Counter(o["stage"] for o in out),sum(o["fc_heat_mm"] is not None for o in out))
