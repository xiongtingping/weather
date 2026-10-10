"""Round 21: ICON / ECMWF IFS hourly shortwave from the Open-Meteo previous-runs
API (previous_day1/2) per fleet station.  Run inside the bench dir (needs lib.py
and registry.json there).  Usage: fetch_prev_sw.py icon_global,ecmwf_ifs025 prev_sw.json 2026-09-22 2026-10-08
"""
import json, subprocess, time, sys, sqlite3, pathlib
sys.path.insert(0,'.')
from lib import CITY2ST
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
models=sys.argv[1].split(","); outp=pathlib.Path(sys.argv[2]); START, END = sys.argv[3], sys.argv[4]
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30)
coords={r[0]:(r[1],r[2]) for r in conn.execute("select station_id,latitude,longitude from calibration_station_forecast_physics_version where latitude is not null group by station_id")}
conn.close()
tz={r["station_id"]:r["settlement_timezone"] for r in json.load(open("registry.json"))}
out=json.load(open(outp)) if outp.exists() else {}
t0=time.time(); n=0
for st in sorted(set(CITY2ST.values())):
    if st not in coords: print("no coords",st,file=sys.stderr); continue
    lat,lon=coords[st]
    for m in models:
        key=f"{st}|{m}"
        if key in out: continue
        url=f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&start_date={START}&end_date={END}&hourly=shortwave_radiation_previous_day1,shortwave_radiation_previous_day2&models={m}&timezone={tz.get(st,'GMT').replace('/','%2F')}"
        for a in range(4):
            r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
            try: j=json.loads(r.stdout); break
            except Exception: j={"error":True,"reason":"nonjson "+r.stdout[:60]}; time.sleep(2+2*a)
        if "error" in j: out[key]={"error":j.get("reason","")[:100]}
        else:
            h=j["hourly"]; k1=[k for k in h if "previous_day1" in k][0]; k2=[k for k in h if "previous_day2" in k][0]
            out[key]={"time":h["time"],"d1":h[k1],"d2":h[k2],"tz":j.get("timezone")}
        n+=1; time.sleep(0.25)
    json.dump(out,open(outp,"w"))
ok=sum(1 for v in out.values() if "error" not in v and any(x is not None for x in v["d1"]))
print("calls",n,"keys",len(out),"with data",ok,round(time.time()-t0),"s",file=sys.stderr)
