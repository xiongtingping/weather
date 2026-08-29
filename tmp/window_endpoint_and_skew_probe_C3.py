import json, sqlite3, collections, math, sys, time
DB="file:data/bayes_basket_target_ssot.db?mode=ro"
conn=sqlite3.connect(DB,uri=True,timeout=30)
pos=collections.defaultdict(list)
for r in conn.execute("select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,posted_buckets_json,window_buckets_json,probabilities_json from paper_trade_position"):
    pos[(r[0],r[1])].append(r)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket,hit from paper_trade_settlement")}
print("rows with empty posted_buckets_json:",
      conn.execute("select count(*) from paper_trade_position where posted_buckets_json=''").fetchone()[0],
      "of", conn.execute("select count(*) from paper_trade_position").fetchone()[0])
print("posted != filled on how many rows:",
      conn.execute("select count(*) from paper_trade_position where posted_buckets_json<>window_buckets_json").fetchone()[0])

for lane,f in (("posted",5),("filled",6)):
    ep=one=onemiss=onemiss_ab=0; tot=0; ext_ct=collections.Counter(); outside=collections.Counter()
    pred_ab=real_ab=pred_be=real_be=pred_eq=real_eq=0.0
    for k,sr in sorted(setl.items()):
        cands=sorted(pos.get(k) or [],key=lambda r:r[3])
        if not cands: continue
        r=cands[0]; p=json.loads(r[7]); order=list(p); idx={b:i for i,b in enumerate(order)}
        w=[b for b in json.loads(r[f] or "[]") if b in idx]
        if not w or r[4] not in idx: continue
        s=idx.get(sr[3]);  c=idx[r[4]]
        if s is None: continue
        lo,hi=min(idx[b] for b in w),max(idx[b] for b in w)
        tot+=1
        if c==lo or c==hi: ep+=1
        nb=sum(1 for b in w if idx[b]<c); na=sum(1 for b in w if idx[b]>c)
        ext = "up" if na>nb else "down" if nb>na else "even"
        ext_ct[ext]+=1
        if (nb==0)!=(na==0) or (nb==0 and na==0):
            one+=1
            ab = (s<c) if na>0 or (nb==0 and na==0) else (s>c)
            if not sr[4]:
                onemiss+=1; onemiss_ab+= bool(ab)
        if s<lo: outside["below"]+=1
        elif s>hi: outside["above"]+=1
        pred_ab+=sum(p[b] for b in order[c+1:]); real_ab+= (s>c)
        pred_be+=sum(p[b] for b in order[:c]);   real_be+= (s<c)
        pred_eq+=p[order[c]];                    real_eq+= (s==c)
    print("\n--- %s window, entry stage, n=%d"%(lane,tot))
    print(" centre at endpoint: %.1f%% (%d/%d)"%(100*ep/tot,ep,tot))
    print(" one-sided: %d ; among ONE-SIDED MISSES truth on abandoned side: %s"
          %(one, "%.1f%% (%d/%d)"%(100*onemiss_ab/onemiss,onemiss_ab,onemiss) if onemiss else "n/a"))
    print(" extension direction:",ext_ct.most_common(), " outside:",outside.most_common())
    print(" P(truth ABOVE ladder centre): model %.3f  actual %.3f (%d/%d)"%(pred_ab/tot,real_ab/tot,real_ab,tot))
    print(" P(truth  = ladder centre)   : model %.3f  actual %.3f (%d/%d)"%(pred_eq/tot,real_eq/tot,real_eq,tot))
    print(" P(truth BELOW ladder centre): model %.3f  actual %.3f (%d/%d)"%(pred_be/tot,real_be/tot,real_be,tot))
