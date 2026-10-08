import json, sqlite3, collections, datetime as dt, sys
from zoneinfo import ZoneInfo
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
out={}
TIMES=[("D-1 12:00",1,12),("D-1 18:00",1,18),("D0 02:00",0,2)]
for (st,ed),lab in sorted(labels.items()):
    if not ("2026-09-23"<=ed<="2026-10-07"): continue
    z=ZoneInfo(tz.get(st,"UTC")); d=dt.date.fromisoformat(ed); rec={"settled_bucket":lab["settled_bucket"],"settled_tmax_c":lab["settled_tmax_c"]}
    for name,db,hh in TIMES:
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc)
        ts=t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"); lo=(t-dt.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",(st,ed,ts,lo)).fetchone()
        mode=None; q=None
        if row:
            B=json.loads(row[1]); mids=[]
            for b in B:
                bid,ask=b.get("bid_price"),b.get("best_ask")
                if bid is not None and ask is not None and ask-bid<0.2: mids.append(((bid+ask)/2,-b["ordinal"],b["bucket"]))
            s=sum(m[0] for m in mids)
            if len(mids)>=3 and 0.85<=s<=1.2:
                m=max(mids); mode=m[2]; q={b:round(mm/s,4) for mm,_,b in mids}
        h=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,ts)).fetchone()
        ours=None
        if h:
            P={b:p for b,p in conn.execute("select bucket,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
            if P: ours={"stage":h[1],"mode":max(P.items(),key=lambda kv:kv[1])[0],"p":P}
        rec[name]={"market_mode":mode,"market_q":q,"ours":ours}
    out[f"{st}|{ed}"]=rec
conn.close(); json.dump(out,open("baseline.json","w"))
n=len(out); print("station-days",n,"| market mode available:",{t:sum(1 for r in out.values() if r[t]["market_mode"]) for t,_,_ in TIMES},"| ours available:",{t:sum(1 for r in out.values() if r[t]["ours"]) for t,_,_ in TIMES})
hit=collections.Counter()
for r in out.values():
    for t,_,_ in TIMES:
        if r[t]["market_mode"]: hit[(t,"mkt")]+= r[t]["market_mode"]==r["settled_bucket"]; hit[(t,"mkt n")]+=1
        if r[t]["ours"]: hit[(t,"ours")]+= r[t]["ours"]["mode"]==r["settled_bucket"]; hit[(t,"ours n")]+=1
for t,_,_ in TIMES: print(f"  {t}: market mode hit {hit[(t,'mkt')]/max(hit[(t,'mkt n')],1):.3f} (n={hit[(t,'mkt n')]}) | our mode hit {hit[(t,'ours')]/max(hit[(t,'ours n')],1):.3f} (n={hit[(t,'ours n')]})")
