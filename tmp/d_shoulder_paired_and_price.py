import sqlite3, json, math
from math import comb
c = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
rows = c.execute(
    "SELECT p.position_id,p.leg_shares_json,p.probabilities_json,p.prices_json,"
    "       p.ladder_center_bucket,s.settled_bucket"
    " FROM paper_trade_position p JOIN paper_trade_settlement s"
    "  ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()

def fisher(a,b,cc,d):  # two-sided-ish: one-tail p for a<=observed
    n=a+b+cc+d; p=0.0
    for k in range(0,a+1):
        bb=a+b-k; ccc=a+cc-k; dd=d-(a-k)
        if bb<0 or ccc<0 or dd<0: continue
        p+= comb(a+b,k)*comb(cc+d,ccc)/comb(n,a+cc)
    return p

D1=[]; rowmap={}
for (pid,legj,probj,pricej,lcb,settled) in rows:
    probs=json.loads(probj or "{}") or {}
    prices=json.loads(pricej or "{}") or {}
    legs={b:float(q) for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    for j,b in enumerate(ladder):
        if abs(j-ci)!=1: continue
        rec=dict(pid=pid,b=b,bought=b in legs,y=1 if b==settled else 0,
                 p=float(probs[b]),px=prices.get(b),
                 side="above" if j>ci else "below")
        D1.append(rec); rowmap.setdefault(pid,[]).append(rec)

B=[r for r in D1 if r["bought"]]; N=[r for r in D1 if not r["bought"]]
a=sum(r["y"] for r in B); b=len(B)-a; cc=sum(r["y"] for r in N); d=len(N)-cc
print("distance-1 rungs: BOUGHT %d/%d = %.3f   NOT-BOUGHT %d/%d = %.3f"%(a,len(B),a/len(B),cc,len(N),cc/len(N)))
print("  mean claim  bought=%.4f  not-bought=%.4f"%(sum(r['p'] for r in B)/len(B),sum(r['p'] for r in N)/len(N)))
pb=[float(r['px']) for r in B if r['px'] is not None]; pn=[float(r['px']) for r in N if r['px'] is not None]
print("  mean price  bought=%.4f (n=%d) not-bought=%.4f (n=%d)"%(sum(pb)/len(pb),len(pb),sum(pn)/len(pn),len(pn)))
print("  Fisher one-tail p(bought hits <= %d) = %.3g"%(a,fisher(a,b,cc,d)))

# paired: rows containing both a bought and a not-bought distance-1 rung
wb=wn=tie=0; nb_pair=0
for pid,recs in rowmap.items():
    bb=[r for r in recs if r["bought"]]; nn=[r for r in recs if not r["bought"]]
    if not bb or not nn: continue
    nb_pair+=1
    hb=any(r["y"] for r in bb); hn=any(r["y"] for r in nn)
    if hb and not hn: wb+=1
    elif hn and not hb: wn+=1
    else: tie+=1
print("\npaired rows with both a bought and an un-bought distance-1 rung: %d"%nb_pair)
print("  bought rung won %d, un-bought rung won %d, neither %d"%(wb,wn,tie))
if wb+wn:
    p=sum(comb(wb+wn,k) for k in range(0,wb+1))/2**(wb+wn)
    print("  sign test one-tail p = %.4g"%p)

# price split of distance-1 rungs
print("\ndistance-1 rungs by price tercile (all, bought or not):")
P=[r for r in D1 if r["px"] is not None]; P.sort(key=lambda r: float(r["px"]))
k=len(P)//3
for q in range(3):
    S=P[q*k:(q+1)*k if q<2 else len(P)]
    print("  price[%.3f,%.3f] n=%3d claim=%.3f realised=%.3f bought%%=%.0f%%"%(
        float(S[0]['px']),float(S[-1]['px']),len(S),sum(r['p'] for r in S)/len(S),
        sum(r['y'] for r in S)/len(S),100*sum(r['bought'] for r in S)/len(S)))
