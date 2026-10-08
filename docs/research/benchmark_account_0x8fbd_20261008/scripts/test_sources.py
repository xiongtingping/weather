import json, collections, sys, bisect, datetime as dt
sys.path.insert(0,'.')
from lib import *
fills,acts=load(); reg=json.load(open("registry.json"))
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
def bucket_of(st,ed,tc):
    """ordinal of ladder bucket containing temp tc (°C); registry lower/upper in °C? check unit"""
    L=lad.get((st,ed)); 
    if not L: return None
    unit=L[0]["settlement_unit"]; t=tc*9/5+32 if unit=="F" else tc
    for i,r in enumerate(L):
        lo,hi=r["lower_c"],r["upper_c"]
        if unit=="F":
            lo=lo if lo is None else lo; hi=hi if hi is None else hi
        if (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5): return i
    return None
# check registry lower/upper units for an F station
ex=[r for r in reg if r["station_id"]=="KLGA" and r["event_date"]=="2026-10-05"]
print("KLGA ladder lower/upper sample:",[(r["outcome"],r["lower_c"],r["upper_c"]) for r in sorted(ex,key=lambda r:(r['lower_c'] if r['lower_c'] is not None else -999))][:4])
P=json.load(open("points.json")); pts=collections.defaultdict(list)
for p in P:
    if p["tmax"] is None or not p["knowable"]: continue
    pts[(p["system"],p["station"],p["target"])].append((dt.datetime.fromisoformat(p["knowable"].replace("Z","+00:00")).timestamp(),p["tmax"]))
for k in pts: pts[k].sort()
systems=sorted(set(p["system"] for p in P))
def latest(sys_,st,ed,ts):
    L=pts.get((sys_,st,ed)); 
    if not L: return None
    i=bisect.bisect_right([x[0] for x in L],ts)-1
    return L[i][1] if i>=0 else None
B={r["idx"]:r for r in json.load(open("fills_book.json"))}
M={r["idx"]:r for r in json.load(open("fills_model.json"))["fills"]}
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
def ladder_ord(f):
    L=lad.get((f["station"],f["event_date"]))
    if not L: return None
    for i,r in enumerate(L):
        if r["outcome"]==f["bucket"]: return i
    return None
def truth_ord(st,ed):
    lab=labels.get((st,ed)); 
    return bucket_of(st,ed,lab["settled_tmax_c"]) if lab else None
# F-station registry: lower/upper stored how? decide conversion from sample above
stats=collections.defaultdict(lambda:collections.Counter())
for i,f in enumerate(buys):
    if f["outcome"]!="Yes": continue
    bk=B.get(i,{}).get("book"); 
    if not bk or bk.get("mode_ord") is None: continue
    my=ladder_ord(f); 
    if my is None: continue
    mode=bk["mode_ord"]; tr=truth_ord(f["station"],f["event_date"])
    role="taker" if f["taker"] else "maker"
    for s in systems:
        tc=latest(s,f["station"],f["event_date"],f["ts"])
        if tc is None: continue
        so=bucket_of(f["station"],f["event_date"],tc)
        if so is None: continue
        c=stats[(role,s)]
        c["n"]+=f["size"]; c["their=src"]+= f["size"]*(my==so); c["mode=src"]+=f["size"]*(mode==so); c["their=mode"]+=f["size"]*(my==mode)
        if tr is not None:
            c["nt"]+=f["size"]; c["src hit"]+=f["size"]*(so==tr); c["their hit"]+=f["size"]*(my==tr); c["mode hit"]+=f["size"]*(mode==tr)
            if my==so and my!=mode: c["n_their=src≠mode"]+=f["size"]; c["hit their=src≠mode"]+=f["size"]*(my==tr)
    # our P0 mode
    m=M.get(i,{}).get("model",{}).get("P0")
    if m and m.get("mode_ord") is not None:
        c=stats[(role,"OUR_P0_MODE")]; so=m["mode_ord"]
        c["n"]+=f["size"]; c["their=src"]+=f["size"]*(my==so); c["mode=src"]+=f["size"]*(mode==so); c["their=mode"]+=f["size"]*(my==mode)
        if tr is not None:
            c["nt"]+=f["size"]; c["src hit"]+=f["size"]*(so==tr); c["their hit"]+=f["size"]*(my==tr); c["mode hit"]+=f["size"]*(mode==tr)
            if my==so and my!=mode: c["n_their=src≠mode"]+=f["size"]; c["hit their=src≠mode"]+=f["size"]*(my==tr)
print("YES buys (share-weighted): P(their bucket = source bucket) vs P(market mode = source bucket); hit rates of source/their/mode buckets; edge where they follow source against the mode")
for role in ("taker","maker"):
    print(f"--- {role} ---")
    for s in systems+["OUR_P0_MODE"]:
        c=stats.get((role,s))
        if not c or c["n"]<500: continue
        extra=f" | their=src≠mode: n={c['n_their=src≠mode']:.0f} hit={c['hit their=src≠mode']/c['n_their=src≠mode']:.3f}" if c["n_their=src≠mode"]>0 else ""
        print(f"  {s:30s} n={c['n']:7.0f} their=src {c['their=src']/c['n']:.3f} mode=src {c['mode=src']/c['n']:.3f} their=mode {c['their=mode']/c['n']:.3f} | hit src {c['src hit']/c['nt']:.3f} their {c['their hit']/c['nt']:.3f} mode {c['mode hit']/c['nt']:.3f}{extra}")
