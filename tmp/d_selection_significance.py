"""Is the bought subset worse-calibrated than not-bought at the SAME claim? Stratified + paired."""
import sqlite3, json, math
from collections import defaultdict

c = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
rows = c.execute(
    "SELECT p.position_id,p.station_id,p.event_date,p.stage,p.leg_shares_json,"
    "       p.probabilities_json,p.prices_json,p.ladder_center_bucket,"
    "       p.window_buckets_json,p.posted_buckets_json,s.settled_bucket"
    " FROM paper_trade_position p JOIN paper_trade_settlement s"
    "  ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()

obs = []  # p, price, y, bought, pid, bucket
for (pid, st, ed, stage, legj, probj, pricej, lcb, winj, postj, settled) in rows:
    probs = json.loads(probj or "{}") or {}
    prices = json.loads(pricej or "{}") or {}
    legs = {b: float(q) for b, q in (json.loads(legj or "{}") or {}).items() if float(q) > 0}
    if not probs: continue
    for b, p in probs.items():
        obs.append(dict(p=float(p), price=prices.get(b), y=1 if b == settled else 0,
                        bought=b in legs, pid=pid, b=b, stage=stage, st=st, ed=ed,
                        lcb=lcb, win=json.loads(winj or "[]"), post=json.loads(postj or "[]")))

EDGES = [0.0,0.02,0.05,0.10,0.20,0.35,1.01]
def binof(p):
    for i in range(len(EDGES)-1):
        if EDGES[i] <= p < EDGES[i+1]: return i
    return len(EDGES)-2

# Mantel-Haenszel over claim bins: bought vs not-bought, outcome = settled
print("stratum        nB  hitB    nN  hitN      OR")
a_sum=b_sum=c_sum=d_sum=0.0; num=den=0.0
Ea=Va=A=0.0
for i in range(len(EDGES)-1):
    B=[o for o in obs if binof(o['p'])==i and o['bought']]
    N=[o for o in obs if binof(o['p'])==i and not o['bought']]
    if not B or not N: continue
    a=sum(o['y'] for o in B); b=len(B)-a
    cc=sum(o['y'] for o in N); d=len(N)-cc
    n=a+b+cc+d
    num += a*d/n; den += b*cc/n
    A += a; Ea += (a+b)*(a+cc)/n
    Va += (a+b)*(cc+d)*(a+cc)*(b+d)/(n*n*(n-1))
    print("[%.2f,%.2f)  %4d %.3f  %4d %.3f  %6s" % (
        EDGES[i],EDGES[i+1],len(B),a/len(B),len(N),cc/len(N),
        ("%.3f"%((a*d)/(b*cc))) if b*cc else "inf"))
print("MH odds ratio = %.3f   MH chi2 = %.2f  z = %.2f" % (
    num/den if den else float('nan'), (abs(A-Ea)-0.5)**2/Va, (A-Ea)/math.sqrt(Va)))

# Paired within-row: rows where a bought and a not-bought bucket share a claim bin
wins_b=wins_n=ties=0
for pid in {o['pid'] for o in obs}:
    R=[o for o in obs if o['pid']==pid]
    for i in range(len(EDGES)-1):
        B=[o for o in R if binof(o['p'])==i and o['bought']]
        N=[o for o in R if binof(o['p'])==i and not o['bought']]
        if not B or not N: continue
        hb=any(o['y'] for o in B); hn=any(o['y'] for o in N)
        if hb and not hn: wins_b+=1
        elif hn and not hb: wins_n+=1
        else: ties+=1
print("\npaired within-row, same claim bin: bought-won %d, not-bought-won %d, neither/both %d" % (wins_b,wins_n,ties))

# Brier / log loss
def scores(sel,lab):
    S=[o for o in obs if sel(o)]
    br=sum((o['p']-o['y'])**2 for o in S)/len(S)
    ll=-sum(math.log(max(min(o['p'] if o['y'] else 1-o['p'],1-1e-9),1e-9)) for o in S)/len(S)
    print("%-12s n=%4d Brier=%.4f logloss=%.4f" % (lab,len(S),br,ll))
print()
scores(lambda o: True,"ALL"); scores(lambda o: o['bought'],"BOUGHT"); scores(lambda o: not o['bought'],"NOT-BOUGHT")

# market price as rival forecast on the bought legs
Bp=[o for o in obs if o['bought'] and o['price'] is not None]
if Bp:
    print("\nbought legs with a price: n=%d  mean claim=%.3f mean price=%.3f realised=%.3f"%(
        len(Bp),sum(o['p'] for o in Bp)/len(Bp),sum(float(o['price']) for o in Bp)/len(Bp),
        sum(o['y'] for o in Bp)/len(Bp)))
    br_m=sum((float(o['price'])-o['y'])**2 for o in Bp)/len(Bp)
    br_c=sum((o['p']-o['y'])**2 for o in Bp)/len(Bp)
    print("  Brier price=%.4f  Brier claim=%.4f"%(br_m,br_c))
Np=[o for o in obs if not o['bought'] and o['price'] is not None]
if Np:
    print("not-bought legs with a price: n=%d mean claim=%.3f mean price=%.3f realised=%.3f"%(
        len(Np),sum(o['p'] for o in Np)/len(Np),sum(float(o['price']) for o in Np)/len(Np),
        sum(o['y'] for o in Np)/len(Np)))
    print("  Brier price=%.4f  Brier claim=%.4f"%(
        sum((float(o['price'])-o['y'])**2 for o in Np)/len(Np),
        sum((o['p']-o['y'])**2 for o in Np)/len(Np)))

# edge (claim - price) decile on ALL priced buckets
P=[o for o in obs if o['price'] is not None]
P.sort(key=lambda o: o['p']-float(o['price']))
print("\nedge=claim-price quintiles over ALL priced buckets (n=%d):"%len(P))
k=len(P)//5
for q in range(5):
    S=P[q*k:(q+1)*k if q<4 else len(P)]
    print("  q%d edge[%+.3f,%+.3f] n=%3d claim=%.3f price=%.3f realised=%.3f bought%%=%.0f%%"%(
        q+1,S[0]['p']-float(S[0]['price']),S[-1]['p']-float(S[-1]['price']),len(S),
        sum(o['p'] for o in S)/len(S),sum(float(o['price']) for o in S)/len(S),
        sum(o['y'] for o in S)/len(S),100*sum(o['bought'] for o in S)/len(S)))
