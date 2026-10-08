import json, sqlite3, collections, datetime as dt, sys, time, random, statistics as st
sys.path.insert(0,'.')
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
D=json.load(open("cluster_events.json")); fills=D["fills"]
reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
tk=[f for f in fills if f["taker"] and f["cid"] in bycid and f["event_date"]<="2026-10-07" and f["event_date"]>="2026-09-28"]
random.seed(17); sample=random.sample(tk,min(700,len(tk)))
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
def iso(ts): return dt.datetime.fromtimestamp(ts,dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
def series(st_,ed,t0,t1):
    rows=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at>=? and observed_at<=? order by observed_at",(st_,ed,iso(t0),iso(t1))).fetchall()
    out=[]
    for obs,bj in rows:
        B=json.loads(bj); ts=dt.datetime.fromisoformat(obs.replace("Z","+00:00")).timestamp()
        d={}
        for b in B:
            if b.get("bid_price") is not None and b.get("best_ask") is not None: d[b["token_id"]]=(b["bid_price"],b["best_ask"],(b.get("ask_levels") or [{}])[0].get("shares"),(b.get("bid_levels") or [{}])[0].get("shares"))
        out.append((ts,d))
    return out
def stats_at(ser,tok,t,outcome):
    """quote the account would hit: YES buy hits best_ask; NO buy hits (1-bid) i.e. the YES bid. age = time since that level last changed; others_move = mean |Δmid| of other tokens over last 3h"""
    pre=[(ts,d) for ts,d in ser if ts<=t and tok in d]
    if len(pre)<3: return None
    key=lambda d: d[tok][1] if outcome=="Yes" else d[tok][0]
    cur=key(pre[-1][1]); age=0; last=pre[-1][0]
    for ts,d in reversed(pre[:-1]):
        if abs(key(d)-cur)<1e-9: last=ts
        else: break
    age=(pre[-1][0]-last)/60
    # how many distinct levels in last 3h
    levels=len(set(round(key(d),3) for ts,d in pre))
    # other buckets' mid movement over the window
    first=pre[0][1]; lastd=pre[-1][1]; moves=[abs((lastd[k][0]+lastd[k][1])/2-(first[k][0]+first[k][1])/2) for k in lastd if k!=tok and k in first]
    depth=pre[-1][1][tok][2] if outcome=="Yes" else pre[-1][1][tok][3]
    # direction of the last change of the hit level, and minutes since ANY bucket's bid/ask changed in the ladder
    prev_level=None
    for ts,d in reversed(pre[:-1]):
        if abs(key(d)-cur)>=1e-9: prev_level=key(d); break
    delta=(cur-prev_level) if prev_level is not None else None
    any_change=None
    for (ts1,d1),(ts2,d2) in zip(reversed(pre[:-1]),reversed(pre[1:])):
        ch=any(k in d1 and (abs(d1[k][0]-d2[k][0])>1e-9 or abs(d1[k][1]-d2[k][1])>1e-9) for k in d2)
        if ch: any_change=(pre[-1][0]-ts2)/60; break
    return dict(level_delta=delta,any_change_min=any_change,age_min=age,levels=levels,others_move=st.mean(moves) if moves else None,depth=depth,snap_gap=(t-pre[-1][0])/60,span=(pre[-1][0]-pre[0][0])/60)
t0=time.time(); hit_rows=[]; ctl_rows=[]
cache={}
for i,f in enumerate(sample):
    st_,ed=f["station"],f["event_date"]; tok=bycid[f["cid"]]["yes_token"]
    ser=series(st_,ed,f["ts"]-3*3600,f["ts"]+60)
    s=stats_at(ser,tok,f["ts"],f["outcome"])
    if s: s.update(lead=f["lead_h"] if "lead_h" in f else None,outcome=f["outcome"],price=f["price"]); hit_rows.append(s)
    # control: same station-day & token, a random time 2-5 hours BEFORE the fill (not within 30 min of any cluster fill on this bucket)
    cl_ts=sorted(x["ts"] for x in fills if x["cid"]==f["cid"] and x["outcome"]==f["outcome"])
    for a in range(6):
        tc=f["ts"]-random.uniform(2*3600,5*3600)
        if all(abs(tc-x)>1800 for x in cl_ts): break
    else: continue
    ser2=series(st_,ed,tc-3*3600,tc+60); s2=stats_at(ser2,tok,tc,f["outcome"])
    if s2: s2.update(outcome=f["outcome"]); ctl_rows.append(s2)
    if i%100==0: print(i,round(time.time()-t0),"s",file=sys.stderr)
conn.close(); json.dump({"hit":hit_rows,"ctl":ctl_rows},open("staleness.json","w"))
def q(v,k): v=sorted(v); return v[int(k*(len(v)-1))]
print(f"taker fills with quote history: {len(hit_rows)} | controls: {len(ctl_rows)} | elapsed {round(time.time()-t0)}s")
for name,rows in (("HIT (quote they took)",hit_rows),("CONTROL (same bucket, 2-5h earlier)",ctl_rows)):
    ages=[r["age_min"] for r in rows]; lv=[r["levels"] for r in rows]; om=[r["others_move"] for r in rows if r["others_move"] is not None]
    print(f"  {name:36s} n={len(rows)} quote age (min) q25/50/75 {q(ages,.25):.0f}/{q(ages,.5):.0f}/{q(ages,.75):.0f} | age>60min {st.mean(a>60 for a in ages):.2f} | distinct levels in 3h median {st.median(lv):.0f} | others' mid moved (abs, 3h) median {st.median(om):.3f} | snap gap before median {st.median(r['snap_gap'] for r in rows):.1f} min")
for name,rows in (("HIT",hit_rows),("CONTROL",ctl_rows)):
    dl=[r["level_delta"] for r in rows if r["level_delta"] is not None]; ac=[r["any_change_min"] for r in rows if r["any_change_min"] is not None]
    print(f"  {name:8s} last change of hit level: up {st.mean(x>0 for x in dl):.2f} down {st.mean(x<0 for x in dl):.2f} (n={len(dl)}) mean Δ {st.mean(dl):+.3f} | minutes since ANY ladder quote changed: q25/50/75 {q(ac,.25):.0f}/{q(ac,.5):.0f}/{q(ac,.75):.0f} | <=10min {st.mean(x<=10 for x in ac):.2f}")
    for oc in ("Yes","No"):
        dl2=[r["level_delta"] for r in rows if r["level_delta"] is not None and r["outcome"]==oc]
        if dl2: print(f"     {oc}: last change up {st.mean(x>0 for x in dl2):.2f} down {st.mean(x<0 for x in dl2):.2f} mean Δ {st.mean(dl2):+.3f}")
for oc in ("Yes","No"):
    h=[r for r in hit_rows if r["outcome"]==oc]; c=[r for r in ctl_rows if r["outcome"]==oc]
    if h and c: print(f"  {oc}: hit age median {st.median(r['age_min'] for r in h):.0f} vs control {st.median(r['age_min'] for r in c):.0f} | hit age>60 {st.mean(r['age_min']>60 for r in h):.2f} vs {st.mean(r['age_min']>60 for r in c):.2f}")
