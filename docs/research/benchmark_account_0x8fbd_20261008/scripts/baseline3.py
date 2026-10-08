import json, sqlite3, collections, datetime as dt, sys, time
from zoneinfo import ZoneInfo
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
labels={}
for st,ed,rev,b,t in conn.execute("select station_id,event_date,truth_revision_no,settled_bucket,settled_tmax_c from basket_station_day_settlement_label where event_date between '2026-09-05' and '2026-10-07'"):
    k=(st,ed)
    if k not in labels or rev>labels[k][0]: labels[k]=(rev,b,t)
tz={r[0]:r[1] for r in conn.execute("select station_id, settlement_timezone from basket_target_market_registry where station_id<>'' group by station_id")}
TIMES=[("D-2 12:00",2,12),("D-1 06:00",1,6),("D-1 12:00",1,12),("D-1 18:00",1,18),("D0 02:00",0,2),("D0 08:00",0,8)]
out={}; t0=time.time(); n=0
for (st,ed),(rev,sb,stc) in sorted(labels.items()):
    z=ZoneInfo(tz.get(st,"UTC")); d=dt.date.fromisoformat(ed); rec={"settled_bucket":sb,"settled_tmax_c":stc}
    for name,db,hh in TIMES:
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc)
        ts=t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"); lo=(t-dt.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",(st,ed,ts,lo)).fetchone()
        mode=None; q=None; quotes=None
        if row:
            B=json.loads(row[1]); mids=[]
            for b in B:
                bid,ask=b.get("bid_price"),b.get("best_ask")
                if bid is not None and ask is not None and ask-bid<0.2: mids.append(((bid+ask)/2,-b["ordinal"],b["bucket"]))
            s=sum(m[0] for m in mids)
            if len(mids)>=3 and 0.85<=s<=1.2:
                m=max(mids); mode=m[2]; q={b:round(mm/s,4) for mm,_,b in mids}
                quotes={b["bucket"]:{"bid":b.get("bid_price"),"ask":b.get("best_ask"),"ord":b["ordinal"],"bid_sh":(b.get("bid_levels") or [{}])[0].get("shares"),"ask_sh":(b.get("ask_levels") or [{}])[0].get("shares")} for b in B}
        h=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,ts)).fetchone()
        ours=None
        if h:
            rows=conn.execute("select bucket,ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],)).fetchall()
            if rows: ours={"stage":h[1],"mode":max(rows,key=lambda r:r[2])[0],"p":{b:p for b,o,p in rows},"ord":{b:o for b,o,p in rows}}
        rec[name]={"market_mode":mode,"market_q":q,"quotes":quotes,"ours":ours}
    out[f"{st}|{ed}"]=rec; n+=1
    if n%200==0: print(n,round(time.time()-t0),"s",file=sys.stderr)
conn.close(); json.dump(out,open("baseline3.json","w"))
print("station-days",len(out),"| market available:",{t:sum(1 for r in out.values() if r[t]["market_mode"]) for t,_,_ in TIMES},"| elapsed",round(time.time()-t0),"s")
