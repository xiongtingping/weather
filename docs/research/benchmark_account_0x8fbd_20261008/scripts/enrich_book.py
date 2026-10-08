import sys, json, sqlite3, time, datetime as dt, collections
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,acts=load()
reg=json.load(open("registry.json"))
bycid={r["cid"]:r for r in reg}
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30)
conn.execute("PRAGMA query_only=1")
st=conn.execute("select state from basket_official_ws_snapshot where station_id='ZSPD' and event_date='2026-10-09' limit 1").fetchone()
print("snapshot state value:",st, file=sys.stderr)
STATE=st[0] if st else None
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
buys.sort(key=lambda f:(f["station"],f["event_date"],f["ts"]))
t0=time.time(); out=[]; miss=0; cache_key=None; cache=None
for i,f in enumerate(buys):
    ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row=conn.execute("select observed_at, buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state=? and observed_at<=? order by observed_at desc limit 1",(f["station"],f["event_date"],STATE,ts_iso)).fetchone()
    rec=dict(idx=i,ts=f["ts"],station=f["station"],event_date=f["event_date"],cid=f["cid"],outcome=f["outcome"],price=f["price"],size=f["size"],taker=f["taker"],bucket=f["bucket"])
    if not row: miss+=1; rec["book"]=None; out.append(rec); continue
    obs,bj=row
    try: buckets=json.loads(bj)
    except Exception: rec["book"]=None; out.append(rec); continue
    r=bycid.get(f["cid"])
    tok=r["yes_token"] if r else None
    mine=None
    for b in buckets:
        if (tok and b.get("token_id")==tok) or (r and b.get("bucket")==r["outcome"]):
            mine=b; break
    # mode by normalized mid
    mids=[]
    for b in buckets:
        bid=b.get("bid_price"); ask=b.get("best_ask")
        if bid is not None and ask is not None: mid=(bid+ask)/2
        elif ask is not None: mid=ask/2
        elif bid is not None: mid=bid/2
        else: mid=None
        mids.append((b.get("ordinal"),mid,b.get("bucket")))
    valid=[m for m in mids if m[1] is not None]
    msum=sum(m[1] for m in valid) if valid else None
    mode=max(valid,key=lambda m:(m[1],-m[0]))[0] if valid else None
    obs_dt=dt.datetime.fromisoformat(obs.replace("Z","+00:00"))
    rec["book"]=dict(obs=obs,stale_s=(f["utc"]-obs_dt).total_seconds(),mode_ord=mode,mid_sum=msum,n_buckets=len(buckets))
    if mine:
        rec["book"].update(ord=mine.get("ordinal"),yes_bid=mine.get("bid_price"),yes_ask=mine.get("best_ask"),
            bid_sh=(mine.get("bid_levels") or [{}])[0].get("shares"),ask_sh=(mine.get("ask_levels") or [{}])[0].get("shares"),
            ask_levels=mine.get("ask_levels"),bid_levels=mine.get("bid_levels"),book_obs=mine.get("book_observed_at"))
        # normalized market q for this bucket
        if mine.get("bid_price") is not None and mine.get("best_ask") is not None and msum:
            rec["book"]["q_norm"]=((mine["bid_price"]+mine["best_ask"])/2)/msum
    else:
        rec["book"]["ord"]=None
    out.append(rec)
    if i%2000==0: print(i,len(buys),round(time.time()-t0,1),"s",file=sys.stderr)
conn.close()
print("done",len(out),"no snapshot",miss,"elapsed",round(time.time()-t0,1),file=sys.stderr)
json.dump(out,open("fills_book.json","w"))
