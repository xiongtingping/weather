"""New angles on the cluster: (A) follower flow after their first taker fill and their exits into it;
(B) who is on the other side (maker identity, concentration, the makers' P&L); (C) market age at entry;
(D) merges / complete-set behaviour; (E) size vs per-station edge (self-assessment)."""
import json, collections, math, bisect, sqlite3
import numpy as np
T=json.load(open('tapes.json')); ce=json.load(open('cluster_events.json')); W={f['w'] for f in ce['fills']}
reg={r['cid']:r for r in json.load(open('registry.json'))}
conn=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=30); conn.execute('PRAGMA query_only=1')
truth={}
for st,ed,b in conn.execute("select station_id,event_date,settled_bucket from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"): truth[(st,ed)]=b
def winner(cid, outcome):
    r=reg.get(cid); 
    if not r: return None
    tb=truth.get((r['station_id'],r['event_date']))
    if tb is None: return None
    return (1.0 if r['outcome']==tb else 0.0) if outcome=='Yes' else (0.0 if r['outcome']==tb else 1.0)
def mean(x): return sum(x)/len(x) if x else float('nan')
def eqret(L): return (sum(w for w,p in L)-sum(p for w,p in L))/sum(p for w,p in L) if L else float('nan')
print("== A. follower flow: non-cluster BUYs of the same outcome around the cluster's first taker buy (70 tapes) ==")
before=[];after=[];after_ret=[];exits=[];exit_price_rel=[];n_ev=0; exit_to_follower=0; exit_rows=0
for cid,rows in T.items():
    rows=sorted(rows,key=lambda r:r['timestamp'])
    bytx=collections.defaultdict(list)
    for r in rows: bytx[r['transactionHash']].append(r)
    # cluster taker buys: a cluster BUY row whose tx has another row from a non-cluster wallet... role unknown; use cluster BUY rows as entries, first per (outcome)
    seen=set()
    for r in rows:
        if r['proxyWallet'] in W and r['side']=='BUY' and r['outcome'] not in seen:
            seen.add(r['outcome']); n_ev+=1; t0=r['timestamp']; out=r['outcome']
            b=[x for x in rows if x['proxyWallet'] not in W and x['side']=='BUY' and x['outcome']==out and t0-3600<=x['timestamp']<t0]
            a=[x for x in rows if x['proxyWallet'] not in W and x['side']=='BUY' and x['outcome']==out and t0<x['timestamp']<=t0+3600]
            before.append(sum(x['size']*x['price'] for x in b)); after.append(sum(x['size']*x['price'] for x in a))
            wn=winner(cid,out)
            if wn is not None:
                for x in a: after_ret.append((wn,x['price']))
            # cluster sells of this outcome later
            s=[x for x in rows if x['proxyWallet'] in W and x['side']=='SELL' and x['outcome']==out and x['timestamp']>t0]
            for x in s:
                if x['price']>=0.9: continue
                exits.append(x['size']*x['price']); exit_price_rel.append(x['price']-r['price']); exit_rows+=1
                others=[y for y in bytx[x['transactionHash']] if y['proxyWallet'] not in W]
                exit_to_follower+= bool(others)
print(f"  entries {n_ev} | non-cluster same-side buying: 60 min before ${mean(before):.1f} vs 60 min after ${mean(after):.1f} per entry | followers' equal-dollar return (settled) {eqret(after_ret)*100:+.1f}% on n={len(after_ret)} fills | cluster sells of that outcome later (price <0.9, i.e. before the end): {exit_rows} rows ${sum(exits):.0f}, mean exit − entry price {mean(exit_price_rel):+.3f}, sold to non-cluster {exit_to_follower}/{exit_rows}")
print("\n== B. who is on the other side of the cluster's buys (same tx, non-cluster row) ==")
makers=collections.Counter(); maker_pl=collections.defaultdict(list); n=0
for cid,rows in T.items():
    bytx=collections.defaultdict(list)
    for r in rows: bytx[r['transactionHash']].append(r)
    for tx,L in bytx.items():
        cl=[r for r in L if r['proxyWallet'] in W and r['side']=='BUY']
        if not cl: continue
        for r in cl:
            others=[y for y in L if y['proxyWallet'] not in W]
            for y in others:
                makers[y['proxyWallet']]+=r['size']*r['price']; n+=1
                wn=winner(cid,r['outcome'])
                if wn is not None: maker_pl[y['proxyWallet']].append((r['price']-wn)*r['size'])  # counterparty gains price if the cluster's outcome loses
tot=sum(makers.values()); top=makers.most_common(8)
print(f"  counterparty rows {n}, distinct wallets {len(makers)}, top-1 share {top[0][1]/tot:.2f}, top-5 share {sum(v for _,v in top[:5])/tot:.2f}")
for w,v in top[:8]: print(f"    {w[:10]} ${v:8.0f} ({v/tot:.2f}) | P&L vs cluster on settled fills {sum(maker_pl[w]):+8.0f} U over {len(maker_pl[w])} fills")
print("\n== C. market age at the cluster's entry (first tape row = listing proxy) ==")
ages=[]
for cid,rows in T.items():
    rows=sorted(rows,key=lambda r:r['timestamp']); t_first=rows[0]['timestamp']
    first_cluster=next((r for r in rows if r['proxyWallet'] in W and r['side']=='BUY'),None)
    if first_cluster:
        wn=winner(cid,first_cluster['outcome'])
        ages.append(((first_cluster['timestamp']-t_first)/3600, rows[0]['proxyWallet'] in W, wn, first_cluster['price']))
print(f"  markets {len(ages)} | cluster is the very first trader in {mean([a[1] for a in ages]):.2f} of them | hours from listing to first cluster buy: median {np.median([a[0] for a in ages]):.1f}, p25 {np.percentile([a[0] for a in ages],25):.1f}, p75 {np.percentile([a[0] for a in ages],75):.1f}")
for lo,hi in ((0,1),(1,6),(6,24),(24,1e9)):
    P=[a for a in ages if lo<=a[0]<hi and a[2] is not None]
    if P: print(f"    age {lo}-{hi}h: n={len(P)} hit {mean([a[2] for a in P]):.2f} price {mean([a[3] for a in P]):.2f} ret {eqret([(a[2],a[3]) for a in P])*100:+.1f}%")
print("\n== D. merges and complete sets (cluster activity) ==")
cl=json.load(open('cluster.json'))
tot_merge=0; tot_trade=0
for w,d in cl.items():
    act=d.get('activity') or []
    m=sum(a.get('usdcSize',0) or 0 for a in act if a['type']=='MERGE'); tr=sum(a.get('usdcSize',0) or 0 for a in act if a['type']=='TRADE')
    tot_merge+=m; tot_trade+=tr; print(f"  {w[:10]} MERGE ${m:8.0f} ({len([a for a in act if a['type']=='MERGE'])} ops) | TRADE ${tr:8.0f} | REWARD ${sum(a.get('usdcSize',0) or 0 for a in act if a['type']=='REWARD'):.0f}")
print(f"  cluster MERGE ${tot_merge:.0f} vs TRADE ${tot_trade:.0f} ({tot_merge/max(tot_trade,1):.1%})")
# both-sides holding per bucket episode: fills of YES and NO by the cluster in the same cid
fills=ce['fills']; by=collections.defaultdict(lambda:{'Yes':0.0,'No':0.0,'yes_cost':0.0,'no_cost':0.0})
for f in fills:
    if f['outcome'] in ('Yes','No'):
        by[f['cid']][f['outcome']]+=f['size']; by[f['cid']][f['outcome'].lower()+'_cost']+=f['size']*f['price']
both=[(k,v) for k,v in by.items() if v['Yes']>0 and v['No']>0]
print(f"  buckets traded {len(by)} | with BOTH yes and no bought {len(both)} ({len(both)/len(by):.2f}) | in those, pair cost per share (yes+no avg) {mean([(v['yes_cost']/v['Yes']+v['no_cost']/v['No']) for k,v in both]):.3f}, min-side shares {sum(min(v['Yes'],v['No']) for k,v in both):.0f} of total {sum(v['Yes']+v['No'] for v in by.values()):.0f}")
print("\n== E. size vs the station's realised edge (their direction right − other side), first-taker neighbour picks ==")
import gzip
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
st=collections.defaultdict(list)
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if a['side']=='Yes' and abs(a['d_mode'])==1: st[r['station']].append((r['usdc'], int(np.sign(r['truth_ord']-r['mode_ord']))==a['d_mode'], int(np.sign(r['truth_ord']-r['mode_ord']))==-a['d_mode'], r['event_date']))
tbl=[(s,len(v),mean([x[0] for x in v]),np.median([x[0] for x in v]),mean([x[1] for x in v])-mean([x[2] for x in v])) for s,v in st.items() if len(v)>=12]
print(f"  stations {len(tbl)} | corr(median size, edge) = {np.corrcoef([t[3] for t in tbl],[t[4] for t in tbl])[0,1]:+.3f} | corr(mean size, edge) = {np.corrcoef([t[2] for t in tbl],[t[4] for t in tbl])[0,1]:+.3f}")
for s,n,m,md,e in sorted(tbl,key=lambda t:-t[4])[:4]+sorted(tbl,key=lambda t:t[4])[:4]: print(f"    {s} n={n} median {md:.0f} U mean {m:.0f} U edge {e:+.2f}")
kd=sorted(st.get('KDAL',[]),key=lambda x:x[3]); print('  KDAL sizes by date:', [(d[5:],round(u)) for u,_,_,d in kd][:19])
