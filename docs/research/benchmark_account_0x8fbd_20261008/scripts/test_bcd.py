import json, sqlite3, collections, sys, bisect, random, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,acts=load(); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}; bycid={r["cid"]:r for r in reg}
unit={r["station_id"]:r["settlement_unit"] for r in reg}
obs=collections.defaultdict(list)
for o in json.load(open("obs_temps.json")):
    if o["lane"]=="awc": obs[o["station"]].append((dt.datetime.fromisoformat(o["valid"].replace("Z","+00:00")).timestamp(),o["t"],dt.datetime.fromisoformat((o["seen"] or o["committed"]).replace("Z","+00:00")).timestamp()))
for k in obs: obs[k].sort()
B={r["idx"]:r for r in json.load(open("fills_book.json"))}
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
def running_max(st,day_start_local,ts):
    # max of awc temps with local date == event local date and valid<=ts (by receipt time 'seen' <= ts)
    z=ZoneInfo(tz.get(st,"UTC")); mx=None
    for v,t,seen in obs.get(st,[]):
        if seen>ts: continue
        ld=dt.datetime.fromtimestamp(v,dt.timezone.utc).astimezone(z).date()
        if ld==day_start_local: mx=t if mx is None else max(mx,t)
    return mx
def centre(f):
    lo,hi=f["lo"],f["hi"]
    if lo is None: return hi-0.5
    if hi is None: return lo+0.5
    return (lo+hi)/2 if f["unit"]=="F" else lo
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
def model_mean(st,ed,ts_iso):
    r=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,ts_iso)).fetchone()
    if not r: return None
    rows=conn.execute("select lower_c,upper_c,probability from basket_probability_bridge_bucket_probability where history_id=?",(r[0],)).fetchall()
    tot=0;m=0
    for lo,hi,p in rows:
        c=(lo+hi)/2 if (lo is not None and hi is not None) else (lo+0.5 if lo is not None else hi-0.5)
        m+=c*p; tot+=p
    return m/tot if tot else None
# B. D-1 taker YES fills
tab=collections.Counter(); n=0; pairs=[]
for i,f in enumerate(buys):
    if not f["taker"] or f["outcome"]!="Yes": continue
    z=ZoneInfo(tz.get(f["station"],"UTC")); lt=f["utc"].astimezone(z); ed=dt.date.fromisoformat(f["event_date"])
    if (ed-lt.date()).days!=1 or lt.hour<10: continue   # D-1, after local 10:00 so running max is meaningful
    dm1=lt.date().isoformat()
    rm=running_max(f["station"],lt.date(),f["ts"])
    mm=model_mean(f["station"],dm1,f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    bk=B.get(i,{}).get("book")
    if rm is None or mm is None or not bk or bk.get("ord") is None or bk.get("mode_ord") is None: continue
    if unit.get(f["station"])=="F": rm_c=(rm-32)*5/9  # obs temps are °C already? check: reported_temperature_c -> °C
    resid=rm-mm  # both °C
    d=bk["ord"]-bk["mode_ord"]
    if d==0: continue
    tab[("today warmer" if resid>0.5 else "today colder" if resid<-0.5 else "today ~forecast", "YES above mkt mode" if d>0 else "YES below mkt mode")]+=f["size"]; n+=1; pairs.append((resid,d))
print("B. D-1 (after local 10:00) taker YES buys: running D-1 max minus our D-1 model mean  x  their D bucket vs market mode (share-weighted)")
for a in ("today warmer","today ~forecast","today colder"):
    up=tab[(a,"YES above mkt mode")]; dn=tab[(a,"YES below mkt mode")]
    print(f"   {a:16s}: above {up:7.0f}  below {dn:7.0f}  -> above share {up/(up+dn):.2f}" if up+dn else f"   {a}: none")
import statistics as st
if len(pairs)>10:
    xs=[p[0] for p in pairs]; ys=[1 if p[1]>0 else -1 for p in pairs]
    mx=st.mean(xs); my=st.mean(ys); cov=sum((x-mx)*(y-my) for x,y in pairs)/len(pairs); r=cov/(st.pstdev(xs)*st.pstdev(ys)) if st.pstdev(xs)>0 and st.pstdev(ys)>0 else float('nan')
    print(f"   n={len(pairs)} corr(resid, above?)={r:+.3f}")
# C. D0 fills vs running max
cnt=collections.Counter(); sh=collections.Counter()
for i,f in enumerate(buys):
    z=ZoneInfo(tz.get(f["station"],"UTC")); lt=f["utc"].astimezone(z); ed=dt.date.fromisoformat(f["event_date"])
    if (ed-lt.date()).days!=0: continue
    rm=running_max(f["station"],lt.date(),f["ts"])
    if rm is None: cnt[("D0",f["outcome"],"no obs yet")]+=1; continue
    rmv=rm*9/5+32 if f["unit"]=="F" else rm
    lo,hi=f["lo"],f["hi"]
    rel="bucket below running max (dead)" if (hi is not None and rmv>=hi+0.5) else "bucket contains running max" if ((lo is None or rmv>=lo-0.5) and (hi is None or rmv<hi+0.5)) else "bucket above running max"
    k=("D0",f["outcome"],rel,"T" if f["taker"] else "M"); cnt[k]+=1; sh[k]+=f["size"]*f["price"]
print("C. D0 buys by bucket position vs running METAR max at fill time (count, notional):")
for k in sorted(cnt): print("   ",k,cnt[k],round(sh[k]))
# D. minutes since latest METAR receipt (first_seen), taker vs maker vs control
random.seed(9); lo_t=min(f["ts"] for f in buys); hi_t=max(f["ts"] for f in buys)
seen=collections.defaultdict(list)
for stn,L in obs.items(): seen[stn]=sorted(s for _,_,s in L)
def since(stn,t):
    L=seen.get(stn,[]); i=bisect.bisect_right(L,t)-1
    return (t-L[i])/60 if i>=0 else None
bins=[0,2,5,10,15,30,60,10**9]
def dist(getter,fs):
    c=collections.Counter(); n=0
    for f in fs:
        d=getter(f)
        if d is None: continue
        n+=1
        for a,b in zip(bins,bins[1:]):
            if a<=d<b: c[(a,b)]+=1; break
    return c,n
for role in (True,False):
    fs=[f for f in buys if f["taker"]==role]
    cf,nf=dist(lambda f:since(f["station"],f["ts"]),fs); cc,nc=dist(lambda f:since(f["station"],random.uniform(lo_t,hi_t)),fs)
    print(f"D. {'taker' if role else 'maker'} fills: minutes since latest METAR RECEIPT (fills% / control%), n={nf}:"," | ".join(f"{a}-{b if b<10**9 else '∞'}: {100*cf[(a,b)]/nf:.1f}/{100*cc[(a,b)]/nc:.1f}" for a,b in zip(bins,bins[1:])))
conn.close()
