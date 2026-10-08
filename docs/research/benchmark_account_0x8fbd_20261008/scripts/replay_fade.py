import json, sqlite3, collections, datetime as dt, sys
from zoneinfo import ZoneInfo
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
STAKE=30.0
TIMES=[("D-1 12:00",1,12),("D-1 18:00",1,18),("D0 02:00",0,2),("D0 08:00",0,8)]
res=collections.defaultdict(lambda: collections.defaultdict(lambda:[0,0.0,0.0,0.0]))  # [n, cost, payout, shares]
modecal=collections.defaultdict(list)
nsnap=0; nmiss=0
def probs_asof(st,ed,ts_iso):
    out={}
    for stage in ("P0","P0-M","P1"):
        r=conn.execute("select history_id from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and stage=? and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,stage,ts_iso)).fetchone()
        if r: out[stage]={b:p for b,p in conn.execute("select bucket,probability from basket_probability_bridge_bucket_probability where history_id=?",(r[0],))}
    return out
for (st,ed),lab in sorted(labels.items()):
    if not ("2026-09-23"<=ed<="2026-10-07"): continue
    z=ZoneInfo(tz.get(st,"UTC")); d=dt.date.fromisoformat(ed)
    for name,dback,hour in TIMES:
        t=dt.datetime.combine(d-dt.timedelta(days=dback),dt.time(hour,0),tzinfo=z).astimezone(dt.timezone.utc)
        ts_iso=t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"); lo_iso=(t-dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",(st,ed,ts_iso,lo_iso)).fetchone()
        if not row: nmiss+=1; continue
        nsnap+=1; B=json.loads(row[1])
        mids=[]
        for b in B:
            bid,ask=b.get("bid_price"),b.get("best_ask")
            if bid is None or ask is None: continue
            mids.append(((bid+ask)/2,-b["ordinal"],b))
        if len(mids)<3: continue
        msum=sum(m[0] for m in mids); mode=max(mids)[2]; mo=mode["ordinal"]
        byord={b["ordinal"]:b for b in B}
        P=probs_asof(st,ed,ts_iso); P0=P.get("P1") or P.get("P0-M") or P.get("P0") or {}
        won=lab["settled_bucket"]
        def leg(kind,b,side):
            if b is None or b.get("bid_price") is None or b.get("best_ask") is None: return
            if side=="YES": price=b["best_ask"]; depth=(b["ask_levels"] or [{}])[0].get("shares",0); win=(b["bucket"]==won)
            else: price=round(1-b["bid_price"],3); depth=(b["bid_levels"] or [{}])[0].get("shares",0); win=(b["bucket"]!=won)
            if price<=0.01 or price>=0.99: return
            sh=min(STAKE/price, depth) if depth else STAKE/price
            if sh*price<5: return
            pm=P0.get(b["bucket"]); 
            for tag in (kind, kind+" |model agrees" if (pm is not None and ((pm>price) if side=="YES" else ((1-pm)>price))) else kind+" |model disagrees"):
                R=res[name][tag]; R[0]+=1; R[1]+=sh*price; R[2]+=sh*(1.0 if win else 0.0); R[3]+=sh
        leg("A: NO on market mode",mode,"NO")
        leg("A: YES on mode-1",byord.get(mo-1),"YES")
        leg("A: YES on mode+1",byord.get(mo+1),"YES")
        leg("B: YES on market mode (Proper-like)",mode,"YES")
        leg("C: YES on mode-2",byord.get(mo-2),"YES"); leg("C: YES on mode+2",byord.get(mo+2),"YES")
        modecal[name].append((((mode["bid_price"]+mode["best_ask"])/2)/msum, mode["bid_price"], mode["best_ask"], mode["bucket"]==won))
conn.close()
print(f"snapshots found {nsnap}, missing {nmiss}, stake/leg {STAKE} U, taker at best ask (NO ask = 1 - YES bid)")
for name,_,_ in TIMES:
    print(f"--- decision time {name} ---")
    for tag in sorted(res[name]):
        n,cost,pay,sh=res[name][tag]
        if n==0: continue
        print(f"  {tag:42s} n={n:4d} avgp={cost/sh:.3f} hit={pay/sh:.3f} ret={(pay/cost-1)*100:+6.1f}%  pnl={pay-cost:+8.1f}")
    mc=modecal[name]
    if mc:
        print(f"  market mode calibration: n={len(mc)} norm-mid {sum(m[0] for m in mc)/len(mc):.3f} bid {sum(m[1] for m in mc)/len(mc):.3f} ask {sum(m[2] for m in mc)/len(mc):.3f} hit {sum(m[3] for m in mc)/len(mc):.3f}")
