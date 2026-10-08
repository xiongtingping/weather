import sys, json, sqlite3, time, datetime as dt, collections
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,acts=load()
reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
groups=collections.defaultdict(list)
for i,f in enumerate(buys): groups[(f["station"],f["event_date"])].append(i)
# add current open station-days from positions
pos=json.load(open("positions_current.json"))
open_sd=set()
for p in pos:
    if p["currentValue"]>0:
        c,ed=parse_event(p["eventSlug"]); s=CITY2ST.get(c)
        if s and ed: open_sd.add((s,ed))
t0=time.time(); hist_cache={}; prob_cache={}
def probs(hid):
    if hid not in prob_cache:
        prob_cache[hid]={r[0]:dict(ordinal=r[1],lower_c=r[2],upper_c=r[3],p=r[4]) for r in conn.execute("select bucket,ordinal,lower_c,upper_c,probability from basket_probability_bridge_bucket_probability where history_id=?",(hid,))}
    return prob_cache[hid]
def hist(st,ed):
    k=(st,ed)
    if k not in hist_cache:
        hist_cache[k]=conn.execute("select history_id,stage,decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' order by decision_at_utc",(st,ed)).fetchall()
    return hist_cache[k]
out=[None]*len(buys); nohist=0
for (st,ed),idxs in groups.items():
    H=hist(st,ed)
    for i in idxs:
        f=buys[i]; ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rec=dict(idx=i,ts=f["ts"],station=st,event_date=ed,cid=f["cid"],outcome=f["outcome"],price=f["price"],size=f["size"],taker=f["taker"],bucket=f["bucket"],model={})
        latest={}
        for hid,stage,dec in H:
            if dec<=ts_iso: latest[stage]=(hid,dec)
        if not latest: nohist+=1
        r=bycid.get(f["cid"]); label=r["outcome"] if r else None
        for stage,(hid,dec) in latest.items():
            P=probs(hid)
            pb=P.get(label) if label else None
            if pb is None:
                # fallback by lower/upper in C
                for b,v in P.items():
                    if f["unit"]=="C" and v["lower_c"]==f["lo"] and (v["upper_c"]==f["hi"] or (f["hi"] is None and v["upper_c"] is None)): pb=v; break
            mode=max(P.items(),key=lambda kv:(kv[1]["p"],-kv[1]["ordinal"]))[0] if P else None
            rec["model"][stage]=dict(dec=dec,p=pb["p"] if pb else None,ord=pb["ordinal"] if pb else None,mode=mode,mode_ord=P[mode]["ordinal"] if mode else None,mode_p=P[mode]["p"] if mode else None,age_s=(f["utc"]-dt.datetime.fromisoformat(dec.replace("Z","+00:00"))).total_seconds())
        out[i]=rec
print("fills enriched",len(out),"no history before fill",nohist,"elapsed",round(time.time()-t0,1),"hist rows",sum(len(v) for v in hist_cache.values()),"prob ids",len(prob_cache),file=sys.stderr)
# latest vectors now for open station-days
now={}
for st,ed in sorted(open_sd):
    H=hist(st,ed); latest={}
    for hid,stage,dec in H: latest[stage]=(hid,dec)
    now[f"{st}|{ed}"]={stage:dict(dec=dec,vec=probs(hid)) for stage,(hid,dec) in latest.items()}
conn.close()
json.dump({"fills":out,"open_now":now},open("fills_model.json","w"))
print("open station-days",len(open_sd),"with any history",sum(1 for v in now.values() if v),file=sys.stderr)
