import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
cols=[r[1] for r in conn.execute("pragma table_info(basket_probability_bridge_history)")]
print("bridge_history cols:",cols)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket from paper_trade_settlement")}
same=diff=miss=0; examples=[]; ep_model=0; tot=0; abandoned=0; onesided=0
for k,sr in sorted(setl.items()):
    row=conn.execute("select decision_at_utc,ladder_center_bucket,posted_buckets_json,probabilities_json from paper_trade_position where station_id=? and event_date=? order by decision_at_utc limit 1",k).fetchone()
    if not row: continue
    h=conn.execute("select envelope_json from basket_probability_bridge_history where station_id=? and event_date=? and decision_at_utc=? limit 1",(k[0],k[1],row[0])).fetchone()
    if not h: miss+=1; continue
    lin=(json.loads(h[0]) or {}).get("lineage") or {}
    mc=str(lin.get("model_center_bucket") or "")
    tot+=1
    if mc==row[1]: same+=1
    else:
        diff+=1
        if len(examples)<5: examples.append((k,row[1],mc))
    p=json.loads(row[3]); idx={b:i for i,b in enumerate(p)}
    w=[b for b in json.loads(row[2]) if b in idx]
    if mc in idx and w:
        lo,hi=min(idx[b] for b in w),max(idx[b] for b in w); c=idx[mc]; s=idx.get(sr[3])
        if c==lo or c==hi: ep_model+=1
        nb=sum(1 for b in w if idx[b]<c); na=sum(1 for b in w if idx[b]>c)
        if (nb==0)!=(na==0):
            onesided+=1
            if s is not None and ((s<c) if na>0 else (s>c)): abandoned+=1
print("model_center_bucket vs ladder_center_bucket on the entry row: same %d / diff %d / no-bridge-row %d"%(same,diff,miss))
print("examples (key, ladder, model):",examples)
print("MODEL centre at posted-window endpoint: %d/%d = %.1f%%"%(ep_model,tot,100*ep_model/tot if tot else 0))
print("one-sided about MODEL centre: %d ; truth on abandoned side %d = %.1f%%"%(onesided,abandoned,100*abandoned/onesided if onesided else 0))
