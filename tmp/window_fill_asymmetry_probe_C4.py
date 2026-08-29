import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
pos=collections.defaultdict(list)
for r in conn.execute("select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,posted_buckets_json,window_buckets_json,probabilities_json,leg_shares_json from paper_trade_position"):
    pos[(r[0],r[1])].append(r)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket,hit,pnl_usdc,stake_usdc from paper_trade_settlement")}

drop=collections.Counter(); dropped_holds_truth=0; days=0
side_skew=collections.Counter()
legband=collections.defaultdict(lambda:[0,0,0.0])   # dist -> [legs, hits, shares]
for k,sr in sorted(setl.items()):
    cands=sorted(pos.get(k) or [],key=lambda r:r[3])
    if not cands: continue
    r=cands[0]; p=json.loads(r[7]); order=list(p); idx={b:i for i,b in enumerate(order)}
    posted=[b for b in json.loads(r[5]) if b in idx]; filled=[b for b in json.loads(r[6]) if b in idx]
    if not posted or r[4] not in idx: continue
    c=idx[r[4]]; s=idx.get(sr[3])
    days+=1
    d=[b for b in posted if b not in filled]
    for b in d:
        drop["below" if idx[b]<c else "above" if idx[b]>c else "centre"]+=1
    if s is not None and any(idx[b]==s for b in d): dropped_holds_truth+=1
    if s is not None: side_skew[r[0]] += (1 if s>c else -1 if s<c else 0)
    # leg bands over the whole chain, keyed on each stage's own posted window
    for cr in cands:
        pp=json.loads(cr[7]); oo=list(pp); ii={b:i for i,b in enumerate(oo)}
        cc=ii.get(cr[4]); w=[b for b in json.loads(cr[5]) if b in ii]
        if cc is None or not w: continue
        for b,q in (json.loads(cr[8] or "{}") or {}).items():
            if b not in ii or float(q)<=0: continue
            dist=abs(ii[b]-cc) if b in w and cr[4] in w else abs(ii[b]-cc)
            e=legband[min(dist,3)]; e[0]+=1; e[1]+= (b==sr[3]); e[2]+=float(q)
print("station-days:",days)
print("posted legs dropped before fill, by side of centre:",drop.most_common())
print("days where a DROPPED bucket held the truth: %d/%d = %.1f%%"%(dropped_holds_truth,days,100*dropped_holds_truth/days))
print("\nleg bands (all stages, own posted window, distance from ladder centre):")
for d in sorted(legband):
    n,h,sh=legband[d]
    print("  dist %s : %4d legs  hit %5.1f%%  shares %.0f"%(("%d"%d) if d<3 else ">=3",n,100*h/n,sh))
print("\nper-station net (truth above centre = +1):",
      sorted(side_skew.items(), key=lambda kv:-kv[1])[:8], "...", sorted(side_skew.items(), key=lambda kv:kv[1])[:5])
print("stations with net>0:",sum(1 for v in side_skew.values() if v>0),
      " net<0:",sum(1 for v in side_skew.values() if v<0),
      " net=0:",sum(1 for v in side_skew.values() if v==0))
