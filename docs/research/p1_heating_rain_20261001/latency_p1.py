"""Latency: rain the P1 decision could not yet see (next report after decision) vs rain it saw."""
import json, sqlite3, sys
import numpy as np
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT="/Users/xiong/weather"; S=__file__.rsplit('/',1)[0]
sys.path.insert(0, ROOT+"/scripts")
from calibration_dashboard import metar_precipitation_reading
LAG=timedelta(minutes=15)
def inst(v): return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
D=json.load(open(S+"/dataset_p1.json"))
ss=sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
zones={r[0]:ZoneInfo(r[1]) for r in ss.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
ss.close()
st={r["station"] for r in D}
ar=sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro",uri=True,timeout=30)
reps=defaultdict(list)
for s,va,raw in ar.execute("SELECT station_id,valid_at,raw_text FROM reported_trajectory_observations WHERE source_lane='awc' AND valid_at>='2026-08-19'"):
    if s in st: reps[s].append((inst(va),metar_precipitation_reading(raw)["present"]))
ar.close()
for s in reps: reps[s].sort()
out=[]
for r in D:
    s=r["station"]; z=zones[s]; at=inst(r["at"]); lh=at.astimezone(z).hour
    rr=reps.get(s,[]); seen=[x for x in rr if x[0]<=at-LAG]; nxt=[x for x in rr if at-LAG<x[0]<=at+timedelta(hours=2)]
    if not seen: out.append(None); continue
    last=seen[-1]; prev2=[x for x in seen if x[0]>=at-timedelta(hours=2)]
    out.append(dict(lh=lh if at.astimezone(z).strftime("%Y-%m-%d")==r["date"] else 99, age_min=(at-last[0]).total_seconds()/60,
        now=bool(last[1]), any2h=any(p for _,p in prev2), next_rain=any(p for _,p in nxt[:2]), next_gap=(nxt[0][0]-last[0]).total_seconds()/60 if nxt else None))
json.dump(out,open(S+"/latency_p1.json","w"))
E=[o for o in out if o and 9<=o["lh"]<=16]
print("decisions 9-16h",len(E),"report age min p50/p90", np.percentile([o["age_min"] for o in E],[50,90]).round(0),
      "gap to next report p50/p90", np.percentile([o["next_gap"] for o in E if o["next_gap"]],[50,90]).round(0))
print(Counter(("看见在下" if o["now"] else ("刚停" if o["any2h"] else "看见没下"), "下一报有雨" if o["next_rain"] else "下一报无雨") for o in E))
