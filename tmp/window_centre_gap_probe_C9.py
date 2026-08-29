import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket from paper_trade_settlement")}
gap=collections.Counter(); dm=collections.Counter(); dl=collections.Counter(); n=0
for k,sr in sorted(setl.items()):
    row=conn.execute("select decision_at_utc,ladder_center_bucket,probabilities_json from paper_trade_position where station_id=? and event_date=? order by decision_at_utc limit 1",k).fetchone()
    h=conn.execute("select envelope_json from basket_probability_bridge_history where station_id=? and event_date=? and decision_at_utc=? limit 1",(k[0],k[1],row[0])).fetchone()
    lin=(json.loads(h[0]) or {}).get("lineage") or {}
    mc=str(lin.get("model_center_bucket") or "")
    p=json.loads(row[2]); idx={b:i for i,b in enumerate(p)}
    if mc not in idx or row[1] not in idx or sr[3] not in idx: continue
    n+=1
    gap[idx[row[1]]-idx[mc]]+=1
    dm[max(-3,min(3,idx[sr[3]]-idx[mc]))]+=1
    dl[max(-3,min(3,idx[sr[3]]-idx[row[1]]))]+=1
print("n=%d  ladder centre minus model centre (buckets): %s"%(n,sorted(gap.items())))
print("\n d | truth vs MODEL centre | truth vs LADDER centre")
for d in range(-3,4):
    print("%3d | %4d %6.1f%%          | %4d %6.1f%%"%(d,dm[d],100*dm[d]/n,dl[d],100*dl[d]/n))
print("|d|<=1: model %.1f%%  ladder %.1f%%"%(100*(dm[-1]+dm[0]+dm[1])/n,100*(dl[-1]+dl[0]+dl[1])/n))
