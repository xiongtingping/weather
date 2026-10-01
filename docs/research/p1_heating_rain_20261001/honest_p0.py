import json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, __file__.rsplit('/',1)[0]); sys.path.insert(0,"/Users/xiong/weather")
import fut_wf as W
from bayes_basket_target.forecast_precip_asof import resolve_precip_hourly_as_of
S=W.S
rows=W.load("p0")
ss=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
D=json.load(open(f"{S}/dataset_p0.json")); hist={}
for r,row in zip(D,rows):
    f=row["f"]
    if f is None: continue
    k=(r["station"],r["date"])
    if k not in hist:
        h,_=resolve_precip_hourly_as_of(ss,station_id=k[0],local_date=k[1],cutoff_utc="2099-01-01T00:00:00Z",modes=("historical",)); hist[k]=h
    a,_=resolve_precip_hourly_as_of(ss,station_id=k[0],local_date=k[1],cutoff_utc=r["at"])
    f["hist_pre"]=None if hist[k] is None else hist[k]["precip_before_peak_mm"]
    f["asof_pre"]=None if a is None else a["precip_before_peak_mm"]
ss.close()
print("hist",sum(1 for r in rows if r["f"] and r["f"].get("hist_pre") is not None),"asof",sum(1 for r in rows if r["f"] and r["f"].get("asof_pre") is not None))
print("== P0 诚实臂（出价时可得）")
arms=[("预报Kt:阴→冷/晴→暖",lambda f:0 if f["kt"] is None else (-1 if f["kt"]<=W.T1 else (1 if f["kt"]>=W.T2 else 0))),
      ("预报Kt阴(≤0.50)→加宽/冷",lambda f:-1 if f["kt"] is not None and f["kt"]<=W.T1 else 0),
      ("预报Kt很阴(≤0.40)→加宽/冷",lambda f:-1 if f["kt"] is not None and f["kt"]<=0.40 else 0),
      ("as-of逐小时降水峰前≥1mm",lambda f:-1 if (f.get("asof_pre") or 0)>=1 else 0),
      ("[天花板]历史短时效峰前≥1mm",lambda f:-1 if (f.get("hist_pre") or 0)>=1 else 0),
      ("[天花板]历史短时效峰前≥5mm",lambda f:-1 if (f.get("hist_pre") or 0)>=5 else 0)]
for nm,sg in arms:
    for fam in ("shift","widen"): W.run(rows,nm,sg,fam)
