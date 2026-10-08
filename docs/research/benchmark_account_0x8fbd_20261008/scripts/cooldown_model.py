import json, sqlite3, collections, statistics as st, datetime as dt, sys, re
from zoneinfo import ZoneInfo
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
labels={}
for st_,ed,rev,b,t in conn.execute("select station_id,event_date,truth_revision_no,settled_bucket,settled_tmax_c from basket_station_day_settlement_label where event_date between '2026-07-25' and '2026-10-07'"):
    k=(st_,ed)
    if k not in labels or rev>labels[k][0]: labels[k]=(rev,b,t)
tz={r[0]:r[1] for r in conn.execute("select station_id, settlement_timezone from basket_target_market_registry where station_id<>'' group by station_id")}
def centre(lo,hi):
    if lo is None: return hi-0.5
    if hi is None: return lo+0.5
    return (lo+hi)/2
rows=[]
for (st_,ed),(rev,sb,tC) in sorted(labels.items()):
    prev=(dt.date.fromisoformat(ed)-dt.timedelta(days=1)).isoformat()
    if (st_,prev) not in labels: continue
    yday=labels[(st_,prev)][2]; z=ZoneInfo(tz.get(st_,"UTC")); d=dt.date.fromisoformat(ed)
    for name,db,hh in (("D-1 12:00",1,12),("D0 02:00",0,2)):
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        h=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st_,ed,t)).fetchone()
        if not h: continue
        P=conn.execute("select bucket,ordinal,lower_c,upper_c,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],)).fetchall()
        if not P: continue
        tot=sum(p for *_,p in P); mean=sum(centre(lo,hi)*p for b,o,lo,hi,p in P)/tot
        mode=max(P,key=lambda r:r[4]); step=1.111 if st_[0]=="K" else 1.0
        # truth ordinal
        tro=next((o for b,o,lo,hi,p in P if b==sb),None)
        if tro is None: continue
        pm1=next((p for b,o,lo,hi,p in P if o==mode[1]-1),0.0)/tot
        rows.append(dict(st=st_,ed=ed,month=ed[:7],t=name,stage=h[1],d=(yday-mean)/step,resid=(tC-mean)/step,below_mode=tro<mode[1],at_mode=tro==mode[1],hit_m1=tro==mode[1]-1,p_m1=pm1,p_mode=mode[4]/tot,hit_mode=tro==mode[1]))
conn.close()
print("rows",len(rows),"| months",collections.Counter(r["month"] for r in rows if r["t"]=="D0 02:00"))
for t in ("D-1 12:00","D0 02:00"):
    print(f"\n=== {t}: OUR SERVED model, by expected change d = (yesterday truth - model mean)/bucket ===")
    for name,lo,hi in (("d<=-2.5 (big warm-up)",-99,-2.5),("-2.5..-1.5",-2.5,-1.5),("-1.5..1.5",-1.5,1.5),("1.5..2.5",1.5,2.5),("d>=2.5 (big cool-down)",2.5,99)):
        rs=[r for r in rows if r["t"]==t and lo<=r["d"]<hi]
        if len(rs)<20: continue
        print(f"   {name:24s} n={len(rs):4d} mean resid(truth-model mean) {st.mean(r['resid'] for r in rs):+.2f} bkt | truth below/at/above mode {st.mean(r['below_mode'] for r in rs):.2f}/{st.mean(r['at_mode'] for r in rs):.2f}/{1-st.mean(r['below_mode'] for r in rs)-st.mean(r['at_mode'] for r in rs):.2f} | mode-1: our p {st.mean(r['p_m1'] for r in rs):.3f} hit {st.mean(r['hit_m1'] for r in rs):.3f} | mode: p {st.mean(r['p_mode'] for r in rs):.3f} hit {st.mean(r['hit_mode'] for r in rs):.3f}")
    big=[r for r in rows if r["t"]==t and r["d"]>=2.5]
    print("   big cool-down by month:",{m:f"n={len(v)} resid {st.mean(x['resid'] for x in v):+.2f} mode-1 p {st.mean(x['p_m1'] for x in v):.2f} hit {st.mean(x['hit_m1'] for x in v):.2f}" for m,v in sorted(collections.defaultdict(list,{m:[x for x in big if x['month']==m] for m in set(x['month'] for x in big)}).items()) if len(v)>=10})
    allr=[r for r in rows if r["t"]==t]
    print("   all cases by month: mean resid",{m:f"{st.mean(x['resid'] for x in allr if x['month']==m):+.2f} (n={sum(1 for x in allr if x['month']==m)})" for m in sorted(set(x['month'] for x in allr))})
    # continuous: correlation of d with resid
    xs=[r["d"] for r in allr]; ys=[r["resid"] for r in allr]; mx,my=st.mean(xs),st.mean(ys)
    cov=sum((x-mx)*(y-my) for x,y in zip(xs,ys))/len(xs); print(f"   corr(d, resid) = {cov/(st.pstdev(xs)*st.pstdev(ys)):+.3f} | slope resid per bucket of d = {cov/st.pvariance(xs):+.3f}")
