"""Conditional logit over (bucket, side) alternatives: which features reproduce the cluster's first-taker
choice; lift tables feature by feature; out-of-time test; and the picks judged against truth."""
import json, gzip, math, re, collections
import numpy as np
from scipy.optimize import minimize
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
def centre_c(label, unit):
    m=re.match(r'(-?\d+)-(-?\d+)°F',label)
    if m: return ((int(m.group(1))+int(m.group(2)))/2+0.5-32)*5/9  # F bucket "78-79" covers [78,80)
    m=re.match(r'(-?\d+)°F or (higher|below)',label)
    if m: v=int(m.group(1)); return ((v+1 if m.group(2)=='higher' else v-1)-32)*5/9
    m=re.match(r'(-?\d+)°C or (higher|below)',label)
    if m: v=int(m.group(1)); return float(v+1 if m.group(2)=='higher' else v-1)
    m=re.match(r'(-?\d+)°C',label); return float(m.group(1)) if m else None
FEATS=['log_price','q','is_mode','d_mode','abs_d_mode','log_pq','abs_d_our','d_our','spread','log_depth','mom','c_obs','c_tmax','c_ytruth','c_stmean','is_no','no_x_q','no_x_absd','no_x_logprice','no_x_logpq']
def build(r):
    X=[]; ch=-1; w=1.111 if r['unit']=='F' else 1.0
    for j,a in enumerate(r['alts']):
        c=centre_c(a['bucket'],r['unit']); 
        def rel(x): return 0.0 if (x is None or c is None) else (c-x)/w
        p=max(1e-3,a['p']); q=max(1e-3,a['q']); no=1.0 if a['side']=='No' else 0.0
        f=[math.log(max(0.005,a['price'])), q, 1.0 if a['d_mode']==0 else 0.0, a['d_mode'], abs(a['d_mode']), math.log(p/q), abs(a['d_our']), a['d_our'], a['spread'],
           math.log(1+(a['depth'] or 0.0)), (a['mom'] or 0.0), rel(r['cur_obs']) if r['is_d0'] else 0.0, rel(r['tmax_today']) if r['is_d0'] else 0.0, rel(r['ytruth_c']), rel(r['st_mean_c']), no,
           no*q, no*abs(a['d_mode']), no*math.log(max(0.005,a['price'])), no*math.log(p/q)]
        X.append(f); 
        if a['chosen']: ch=j
    return np.array(X), ch
data=[(r,)+build(r) for r in rows]; data=[d for d in data if d[2]>=0]
print('events',len(data),'| alternatives per event (mean)',np.mean([len(d[1]) for d in data]).round(1))
allX=np.vstack([d[1] for d in data]); mu=allX.mean(axis=0); sd=allX.std(axis=0)+1e-9
def nll(beta, D, lam=1e-3):
    tot=0.0; g=np.zeros_like(beta)
    for r,X,ch in D:
        Z=(X-mu)/sd; u=Z@beta; u-=u.max(); e=np.exp(u); P=e/e.sum()
        tot-=math.log(max(P[ch],1e-12)); g-=Z[ch]-P@Z
    return tot/len(D)+lam*beta@beta, g/len(D)+2*lam*beta
train=[d for d in data if d[0]['event_date']<'2026-10-01']; test=[d for d in data if d[0]['event_date']>='2026-10-01']
print('train',len(train),'test',len(test))
res=minimize(nll,np.zeros(len(FEATS)),args=(train,),jac=True,method='L-BFGS-B'); beta=res.x
def evaluate(D,beta,tag):
    ll=[];top1=[];top3=[];unif=[]
    for r,X,ch in D:
        Z=(X-mu)/sd; u=Z@beta; u-=u.max(); P=np.exp(u); P/=P.sum(); order=np.argsort(-P)
        ll.append(-math.log(max(P[ch],1e-12))); top1.append(order[0]==ch); top3.append(ch in order[:3]); unif.append(math.log(len(P)))
    print(f"  {tag:6s} n={len(D)} | NLL/event {np.mean(ll):.3f} vs uniform {np.mean(unif):.3f} | top-1 {np.mean(top1):.3f} (uniform {np.mean([1/len(d[1]) for d in D]):.3f}) | top-3 {np.mean(top3):.3f}")
print("\n== conditional logit: fit on <10-01, test on ≥10-01 ==")
evaluate(train,beta,'train'); evaluate(test,beta,'test')
print("  standardised coefficients (|β| large = matters; sign = direction of preference):")
for f,b in sorted(zip(FEATS,beta),key=lambda t:-abs(t[1])): print(f"    {f:14s} {b:+.3f}")
# baselines on test: rule picks
def rule_eval(D, pick, tag):
    hit=[];ret=[];acc=[]
    for r,X,ch in D:
        j=pick(r); a=r['alts'][j]; acc.append(j==ch)
        win=(a['ord']==r['truth_ord']) if a['side']=='Yes' else (a['ord']!=r['truth_ord']); hit.append(win); ret.append(win/a['price']-1)
    print(f"  {tag:44s} agree-with-them {np.mean(acc):.3f} | hit {np.mean(hit):.3f} | return at their-time price {np.mean(ret)*100:+.1f}%")
def their(r): return next(j for j,a in enumerate(r['alts']) if a['chosen'])
def model_pick(r):
    X,_=build(r); Z=(X-mu)/sd; return int(np.argmax(Z@beta))
print("\n== picks judged against settled truth (test ≥10-01) ==")
rule_eval(test,their,'THEIR actual pick'); rule_eval(test,model_pick,'conditional-logit top pick')
rule_eval(test,lambda r: next(j for j,a in enumerate(r['alts']) if a['d_mode']==0 and a['side']=='Yes'),'market mode YES')
rule_eval(test,lambda r: max(range(len(r['alts'])),key=lambda j:(math.log(max(1e-3,r['alts'][j]['p'])/max(1e-3,r['alts'][j]['q'])) if r['alts'][j]['side']=='Yes' else math.log(max(1e-3,1-r['alts'][j]['p'])/max(1e-3,1-r['alts'][j]['q'])))),'our max log(p/q) (side-aware)')
rule_eval(test,lambda r: min((j for j,a in enumerate(r['alts']) if a['side']=='Yes' and a['d_mode']!=0),key=lambda j:r['alts'][j]['price']),'cheapest non-mode YES')
# lift tables: chosen share / available share
print("\n== lift tables (all events): share of choices vs share of available alternatives ==")
def lift(name, keyf, D=data):
    ch=collections.Counter(); av=collections.Counter()
    for r,X,c in D:
        for j,a in enumerate(r['alts']):
            k=keyf(r,a); av[k]+=1
            if j==c: ch[k]+=1
    tot_c=sum(ch.values()); tot_a=sum(av.values())
    print(f"  {name}:"); 
    for k in sorted(av, key=lambda x:(str(type(x)),x)):
        if av[k]<40: continue
        print(f"     {str(k):18s} chosen {ch[k]:4d} ({ch[k]/tot_c:.3f}) available {av[k]:5d} ({av[k]/tot_a:.3f}) lift {((ch[k]/tot_c)/(av[k]/tot_a)) if av[k] else 0:.2f}")
lift('side × d_mode', lambda r,a:(a['side'],max(-3,min(3,a['d_mode']))))
lift('side × price decile', lambda r,a:(a['side'],round(min(0.95,a['price'])//0.1*0.1,1)))
lift('YES: our log(p/q) band', lambda r,a:('Yes' if a['side']=='Yes' else 'No', 'lpq<-0.5' if math.log(max(1e-3,a['p'])/max(1e-3,a['q']))<-0.5 else 'lpq<0' if math.log(max(1e-3,a['p'])/max(1e-3,a['q']))<0 else 'lpq<0.5' if math.log(max(1e-3,a['p'])/max(1e-3,a['q']))<0.5 else 'lpq>=0.5'))
lift('D0 only: centre − current obs (buckets)', lambda r,a:(a['side'],max(-3,min(3,round((centre_c(a['bucket'],r['unit'])-r['cur_obs'])/(1.111 if r['unit']=='F' else 1.0))))) if (r['is_d0'] and r['cur_obs'] is not None) else ('skip',0), [d for d in data if d[0]['is_d0'] and d[0]['cur_obs'] is not None])
lift('centre − yesterday truth (buckets)', lambda r,a:(a['side'],max(-3,min(3,round((centre_c(a['bucket'],r['unit'])-r['ytruth_c'])/(1.111 if r['unit']=='F' else 1.0))))) if r['ytruth_c'] is not None else ('skip',0), [d for d in data if d[0]['ytruth_c'] is not None])
lift('60-min momentum of the bucket mid', lambda r,a:(a['side'], 'down' if (a['mom'] or 0)<-0.01 else 'up' if (a['mom'] or 0)>0.01 else 'flat'))
