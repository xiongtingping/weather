import json, collections, sys, bisect, datetime as dt, random
from zoneinfo import ZoneInfo
sys.path.insert(0,'.')
from lib import *
fills,acts=load()
A=json.load(open("arrivals.json")); R=json.load(open("runs.json"))
reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
def ts(s): return dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
tk=[f for f in buys if f["taker"]]; mk=[f for f in buys if not f["taker"]]
# 1. UTC time-of-day 30-min bins, taker vs maker (share-weighted), and minute-of-hour
def tod(fs,binm=30):
    c=collections.Counter()
    for f in fs: c[(f["utc"].hour*60+f["utc"].minute)//binm]+=f["size"]*f["price"]
    t=sum(c.values()); return {k:100*v/t for k,v in c.items()}
T=tod(tk); M=tod(mk)
print("1. taker BUY notional % by UTC 30-min bin (top 12):",[(f"{k//2:02d}:{(k%2)*30:02d}",round(v,1)) for k,v in sorted(T.items(),key=lambda x:-x[1])[:12]])
print("   maker top 12:",[(f"{k//2:02d}:{(k%2)*30:02d}",round(v,1)) for k,v in sorted(M.items(),key=lambda x:-x[1])[:12]])
moh=collections.Counter(f["utc"].minute//5*5 for f in tk); t=sum(moh.values())
print("   taker fills by minute-of-hour (5-min) %:",[(m,round(100*moh[m]/t)) for m in range(0,60,5)])
# 7. D-1 local-noon peak: by region in UTC
reg_of=lambda st:"US" if st[0]=="K" or st in("CYYZ","MMMX","MPMG") else "EU/ME" if st[0] in "ELU" or st in ("OEJN","LLBG","LTAC","LTFM","FACT","OPKC") else "ASIA/OC"
for region in ("ASIA/OC","EU/ME","US"):
    fs=[f for f in tk if reg_of(f["station"])==region]
    c=collections.Counter()
    for f in fs:
        z=ZoneInfo(tz.get(f["station"],"UTC")); lt=f["utc"].astimezone(z)
        if (dt.date.fromisoformat(f["event_date"])-lt.date()).days==1: c[f["utc"].hour]+=f["size"]*f["price"]
    t=sum(c.values()); print(f"7. D-1 taker buys {region}: UTC hour % top6:",[(h,round(100*v/t)) for h,v in sorted(c.items(),key=lambda x:-x[1])[:6]])
# 2/3/4 alignment: time since latest event of each type at that station, fills vs control
taf=collections.defaultdict(list); obs=collections.defaultdict(list); runs=collections.defaultdict(list); run_by_sys=collections.defaultdict(lambda:collections.defaultdict(list))
for r in A["taf"]: taf[r["station"]].append(ts(r["avail"] or r["received"]))
for r in A["obs"]: obs[r["station"]].append(ts(r["observed"]))
for r in R: runs[r["station"]].append(ts(r["first_arr"])); run_by_sys[r["system"]][r["station"]].append(ts(r["first_arr"]))
for d in (taf,obs,runs): 
    for k in d: d[k]=sorted(set(d[k]))
for s in run_by_sys:
    for k in run_by_sys[s]: run_by_sys[s][k]=sorted(set(run_by_sys[s][k]))
def since(evlist,t):
    i=bisect.bisect_right(evlist,t)-1
    return (t-evlist[i]) if i>=0 else None
random.seed(3)
# control: for each taker fill, a random time uniformly within the account's active window (first..last fill) on same station, same UTC day span
lo=min(f["ts"] for f in tk); hi=max(f["ts"] for f in tk)
def hist(name,evs,fs,bins=(0,300,600,900,1800,3600,7200,14400,10**9)):
    def dist(getter):
        c=collections.Counter(); n=0
        for f in fs:
            d=getter(f)
            if d is None: continue
            n+=1
            for a,b in zip(bins,bins[1:]):
                if a<=d<b: c[(a,b)]+=1; break
        return c,n
    cf,nf=dist(lambda f:since(evs.get(f["station"],[]),f["ts"]))
    cc,nc=dist(lambda f:since(evs.get(f["station"],[]),random.uniform(lo,hi)))
    print(f"{name}: minutes since latest event -> fills% vs control%  (n={nf})")
    print("   "+" | ".join(f"{a//60}-{b//60 if b<10**9 else '∞'}m: {100*cf[(a,b)]/nf:.1f}/{100*cc[(a,b)]/max(nc,1):.1f}" for a,b in zip(bins,bins[1:])))
hist("2. TAF issuance (provider_available)",taf,tk)
hist("3. METAR observation time",obs,tk)
hist("4. any native run first captured by us",runs,tk)
for s in ("ECMWF_IFS_ENS_OPEN_PF50","ECMWF_AIFS_ENS_GLOBAL","NOAA_GEFS_05","NOAA_AIGEFS_025","DWD_ICON_GLOBAL_EPS","ECCC_GEM_GLOBAL_ENS"):
    hist(f"   run {s}",run_by_sys[s],tk,bins=(0,900,1800,3600,7200,14400,28800,10**9))
# maker fills for contrast on TAF/METAR
hist("2b. TAF issuance, MAKER fills",taf,mk)
hist("3b. METAR observation, MAKER fills",obs,mk)
# 5. D0 fills: is the latest METAR a new daily high? need temps -> not in facts; skip. Instead: D0 taker fills by minutes since latest METAR, local hour
