import json, collections, sys, bisect, datetime as dt
sys.path.insert(0,'.')
from lib import *
fills,_=load(); reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
def ord_label(st,ed,label):
    for i,r in enumerate(lad.get((st,ed),[])):
        if r["outcome"]==label: return i
def bucket_of(st,ed,tc):
    L=lad.get((st,ed))
    if not L: return None
    t=tc*9/5+32 if L[0]["settlement_unit"]=="F" else tc
    for i,r in enumerate(L):
        lo,hi=r["lower_c"],r["upper_c"]
        if (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5): return i
P=json.load(open("points.json")); pts=collections.defaultdict(list)
for p in P:
    if p["tmax"] is None or not p["knowable"]: continue
    pts[(p["system"],p["station"],p["target"])].append((dt.datetime.fromisoformat(p["knowable"].replace("Z","+00:00")).timestamp(),p["tmax"]))
for k in pts: pts[k].sort()
def latest(s,st,ed,ts):
    L=pts.get((s,st,ed))
    if not L: return None
    i=bisect.bisect_right([x[0] for x in L],ts)-1
    return L[i][1] if i>=0 else None
systems=["ECMWF_IFS_9KM_RELAY_POINT","UKMO_GLOBAL_10KM_RELAY_POINT","ECMWF_AIFS_SINGLE_POINT","CMA_GRAPES_GLOBAL_POINT","NCEP_NBM_POINT","NCEP_HRRR_POINT","DWD_ICON_EU_POINT","JMA_MSM_POINT"]
M2={r["idx"]:r for r in json.load(open("fills_mode2.json"))}; MM={r["idx"]:r for r in json.load(open("fills_model.json"))["fills"]}
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
HI={"ZHHH","LIMC","EGLC","RCSS","RKSI","LTAC","KLAX","OEJN"}; LO={"KMIA","KATL","OPKC","KORD","EFHK","EPWA"}
# reference time per station-day: their first taker YES fill with healthy ladder (any lead>=12h before event 12Z)
ref={}
for i,f in enumerate(buys):
    if f["outcome"]!="Yes" or not f["taker"]: continue
    r=M2[i]
    if not r["healthy"] or (r["stale"] or 1e9)>1800: continue
    k=(f["station"],f["event_date"])
    if k not in ref: ref[k]=i
grp=collections.defaultdict(lambda:collections.defaultdict(lambda:[0,0]))
their_src=collections.defaultdict(lambda:collections.defaultdict(lambda:[0,0]))
for k,i in ref.items():
    st,ed=k; f=buys[i]; lab=labels.get(k)
    if not lab: continue
    tr=ord_label(st,ed,lab["settled_bucket"]); mk=M2[i]["mode"]; my=M2[i]["my_ord"]
    if tr is None or mk is None or my is None: continue
    g="HI" if st in HI else "LO" if st in LO else "MID"
    for gg in (g,"ALL"):
        grp[gg]["market mode"][0]+=mk==tr; grp[gg]["market mode"][1]+=1
        grp[gg]["their first YES"][0]+=my==tr; grp[gg]["their first YES"][1]+=1
        m=MM.get(i,{}).get("model",{}).get("P0")
        if m and m.get("mode_ord") is not None: grp[gg]["our P0 mode"][0]+=m["mode_ord"]==tr; grp[gg]["our P0 mode"][1]+=1
        for s in systems:
            tc=latest(s,st,ed,f["ts"])
            if tc is None: continue
            so=bucket_of(st,ed,tc)
            if so is None: continue
            grp[gg][s][0]+=so==tr; grp[gg][s][1]+=1
            their_src[gg][s][0]+=my==so; their_src[gg][s][1]+=1
            their_src[gg][s+" (mode=src)"][0]+=mk==so; their_src[gg][s+" (mode=src)"][1]+=1
for g in ("ALL","HI","LO","MID"):
    print(f"--- {g} stations (their-info group), station-days n={grp[g]['market mode'][1]} ---")
    for name,(h,n) in sorted(grp[g].items(),key=lambda x:-(x[1][0]/x[1][1] if x[1][1] else 0)):
        if n>=15: print(f"   {name:30s} hit {h/n:.3f} (n={n})")
    print("   their YES = source bucket? / market mode = source bucket?:",{s.replace('_POINT','').replace('_RELAY',''):f"{v[0]/v[1]:.2f}" for s,v in their_src[g].items() if v[1]>=15 and '(mode' not in s},"|",{s.replace('_POINT','').replace('_RELAY','').replace(' (mode=src)',''):f"{v[0]/v[1]:.2f}" for s,v in their_src[g].items() if v[1]>=15 and '(mode' in s})
