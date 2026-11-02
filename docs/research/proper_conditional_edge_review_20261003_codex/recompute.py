"""Independent, resource-bounded audit of the retained report snapshot; no DB writes."""
import os, time, threading, subprocess, shutil, resource, gzip, json, hashlib, re, bisect
from pathlib import Path
START=time.monotonic()
HERE=Path(__file__).resolve().parent
SRC=HERE.parent/'proper_conditional_edge_20261003'
resource.setrlimit(resource.RLIMIT_FSIZE,(32*1024**2,32*1024**2))
def guard():
    while True:
        try:
            rss=int(subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())],text=True).strip())*1024
            assert rss<1800*1024**2 and shutil.disk_usage(HERE).free>2*1024**3 and time.monotonic()-START<240
        except Exception:
            os._exit(77)
        time.sleep(.5)
threading.Thread(target=guard,daemon=True).start()
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

def ci(s, val='pnl', weights=None):
    if not len(s):return None
    w=np.asarray(weights if weights is not None else 1/s.groupby(['st','d','ord']).p.transform('size'),float)
    a=pd.DataFrame({'d':s.d,'x':s[val].to_numpy()*w,'w':w}).groupby('d').sum().sort_index()
    x=a.x.to_numpy(); ww=a.w.to_numpy(); n=len(a)
    rng=np.random.default_rng(2603); ix=rng.integers(n,size=(5000,n))
    bs=x[ix].sum(1)/ww[ix].sum(1)
    starts=rng.integers(n,size=(5000,(n+2)//3)); ix3=((starts[:,:,None]+np.arange(3))%n).reshape(5000,-1)[:,:n]
    bs3=x[ix3].sum(1)/ww[ix3].sum(1)
    return {'mean':float(x.sum()/ww.sum()),'ci':np.quantile(bs,[.025,.975]).tolist(),'block3_ci':np.quantile(bs3,[.025,.975]).tolist(),'days':n,'positive_days':int((x>0).sum()),'rows':len(s),'legs':s.groupby(['st','d','ord']).ngroups,'stations':s.st.nunique()}

rows=[]; meta=[]; total=0; raw_n=0; invalid=0
with gzip.open(SRC/'decisions.jsonl.gz','rt') as f:
    for line in f:
        total+=len(line);assert total<512*1024**2 and len(line)<2*1024**2
        r=json.loads(line);raw_n+=1
        if r['stage']!='P0' or r['h'] is None or r['h']<=36:continue
        b=r['b'];p=[z[2] for z in b]
        if any(v is None for v in p) or r['y'] not in [z[0] for z in b]:invalid+=1;continue
        p=np.array(p,float);p/=p.sum()
        if (p<.001).any():p=np.maximum(p,.001);p/=p.sum()
        q=r['q'] or {}
        meta.append({k:r[k] for k in ['st','d','t','te','tq','h','first','qr','pv']})
        for i,z in enumerate(b):
            rows.append((raw_n,r['st'],r['d'],r['t'],r['h'],r['first'],z[0],z[1],p[i],q.get(z[0],np.nan),z[3][0][0] if z[3] else np.nan,z[4][0][0] if z[4] else np.nan,z[3][0][1] if z[3] else 0,int(z[0]==r['y']),z[6],z[5]))
A=pd.DataFrame(rows,columns=['dec','st','d','t','h','first','k','ord','p','q','bid','ask','bsz','y','unmat','tick']);del rows
M=pd.DataFrame(meta)
for col in ['te','tq']:
    M[col+'_age_s']=(pd.to_datetime(M.t,format='ISO8601')-pd.to_datetime(M[col],format='ISO8601')).dt.total_seconds()
R=A[A.k.str.contains('°C')].copy();R['p_served']=R.p;R['rp']=np.nan
replay_bytes=0;lags=[]
for st,S in R.groupby('st'):
    by={}
    with gzip.open(HERE.parent/'downstream_after_247_20261002'/'pass1_down'/f'{st}.jsonl.gz','rt') as f:
        for line in f:
            replay_bytes+=len(line);assert replay_bytes<1024*1024**2 and len(line)<2*1024**2
            r=json.loads(line)
            if r.get('base',{}).get('vec'):by.setdefault(r['event_date'],[]).append((r['at'],{int(k):v for k,v in r['base']['vec'].items()}))
    for d in by:by[d].sort()
    for (d,dec),g in S.groupby(['d','dec']):
        if d not in by:continue
        rs=by[d];i=bisect.bisect_right([z[0][:19]+'Z' for z in rs],g.t.iloc[0][:19]+'Z')-1
        if i<0:continue
        at,vec=rs[i];vals=[]
        for k in g.k:
            m=re.fullmatch(r'(-?\d+)°C( or below| or higher)?',k);assert m
            n=int(m[1]);vals.append(sum(v for b,v in vec.items() if b<=n) if m[2]==' or below' else sum(v for b,v in vec.items() if b>=n) if m[2]==' or higher' else vec.get(n,0))
        if sum(vals)<.98:continue
        p=np.array(vals);p/=p.sum()
        if min(p)<.001:p=np.maximum(p,.001);p/=p.sum()
        R.loc[g.index,'rp']=p
        lags.append(float((pd.Timestamp(g.t.iloc[0])-pd.Timestamp(at)).total_seconds()/3600))
R=R[R.rp.notna()].copy();R['p']=R.rp
out={'raw_decisions':raw_n,'invalid_p0_decisions':invalid,'p0_decisions':len(M),'raw_bytes':total,'meta':{'dates':[M.d.min(),M.d.max()],'duplicate_decisions':int(M.duplicated(['st','d','t']).sum()),'versions':M.pv.value_counts().to_dict(),'q_reasons':M.qr.value_counts(dropna=False).to_dict(),'age':M[['te_age_s','tq_age_s']].describe().to_dict(),'future_ws':int((M.tq_age_s<0).sum()),'ws_gt60':int((M.tq_age_s>60).sum())}}

def prep(T):
    T=T.copy()
    T['mid']=np.where(T.bid.notna()&T.ask.notna(),(T.bid+T.ask)/2,np.where(T.ask.notna(),T.ask/2,np.where(T.bid.notna(),(1+T.bid)/2,0)))
    modes=T.loc[T.groupby('dec')['mid'].idxmax()].set_index('dec')['ord']
    T['dist']=T.ord-T.dec.map(modes);T['adist']=T.dist.abs();T['pnl']=T.bid-T.y-.05*T.bid*(1-T.bid)
    T['cost']=1-T.bid+.05*T.bid*(1-T.bid);T['fires']=T.p<T.bid
    return T

for label,T in [('replay',R),('served',A[A.d>='2026-09-12'])]:
    T=prep(T);N=T[(T.adist>=2)&(T.bid>=.02)];S=N[N.fires]
    keys=['st','d','ord'];first=S.sort_values('t').drop_duplicates(keys)
    proper=S[S['first']==1];sign=proper[proper.q.notna()&(proper.p<proper.q)].copy()
    logw=np.log(sign.q/sign.p)
    cross=pd.merge(S[keys].drop_duplicates(),N[~N.fires][keys].drop_duplicates(),on=keys)
    x={'all':ci(N),'fires':ci(S),'not_fires':ci(N[~N.fires]),'first_firing':ci(first,weights=np.ones(len(first))),'first_decision':ci(proper),'first_decision_s_negative_log_weight':ci(sign,weights=logw),'q_complete':ci(S[S.q.notna()]),'tight':ci(S[(S.ask-S.bid<=.02)&(S.ask>=S.bid)]),'both_groups_same_leg':len(cross),'selected_missing_q':int(S.q.isna().sum()),'sign_invalid_given_q':int(((S.p>=S.q)&S.q.notna()).sum()),'selected_crossed':int((S.bid>S.ask+1e-12).sum()),'date_range':[S.d.min(),S.d.max()]}
    x['sides']={str(k):ci(s) for k,s in S.groupby(np.sign(S.dist))}
    market=N[(N.adist>=3)&N.q.notna()&(N.q<=.05)].sort_values('t').drop_duplicates(keys)
    x['market_first_trigger']={'all':ci(market),'fires':ci(market[market.fires]),'not_fires':ci(market[~market.fires])}
    daylist=sorted(market.d.unique());xx=[];nn=[]
    for d in daylist:
        ss=market[market.d==d];xx.append([ss[ss.fires].pnl.sum(),ss[~ss.fires].pnl.sum()]);nn.append([ss.fires.sum(),(~ss.fires).sum()])
    xx=np.array(xx);nn=np.array(nn);ix=np.random.default_rng(2603).integers(len(daylist),size=(5000,len(daylist)))
    bb=xx[ix].sum(1)/nn[ix].sum(1);difference=bb[:,0]-bb[:,1]
    x['market_first_trigger']['difference']={'mean':float(market[market.fires].pnl.mean()-market[~market.fires].pnl.mean()),'ci':np.quantile(difference,[.025,.975]).tolist()}
    x['leave_station_out_range']=[min(ci(S[S.st!=st])['mean'] for st in S.st.unique()),max(ci(S[S.st!=st])['mean'] for st in S.st.unique())]
    x['market_price_bins']={str(b):{'fires':ci(g[g.fires]),'not_fires':ci(g[~g.fires])} for b,g in N.groupby(pd.cut(N.bid,[.0199,.03,.05,.08,.12,.2,1]),observed=True)}
    x['ratio_bins']={str(b):ci(g) for b,g in N.groupby(pd.cut(N.p/N.bid,[0,.25,.5,1,2,1e9]),observed=True)}
    for unit in ['C','F']:
        x[unit]=ci(S[S.k.str.contains('°'+unit)])
    n=S[S.h<=60].sort_values('t').drop_duplicates(keys).copy();n['cash']=n.bsz*n.cost;n['profit']=n.bsz*n.pnl
    cap=n.groupby('d')[['cash','profit']].sum()
    x['capacity']={'median_daily_cash':cap.cash.median(),'roi':cap.profit.sum()/cap.cash.sum(),'worst':cap.profit.min(),'wins':int((cap.profit>0).sum()),'days':len(cap)}
    Q=T[T.q.notna()].copy();Q['dbr']=(Q.p-Q.y)**2-(Q.q-Q.y)**2
    D=Q[Q.y==1].copy();D['dls']=np.log(D.p/D.q);D['dbr']=D.dec.map(Q.groupby('dec').dbr.sum());w=1/D.groupby(['st','d']).p.transform('size')
    x['dls']=ci(D,'dls',w);x['dbr']=ci(D,'dbr',w)
    code,unique=pd.factorize(Q.dec);ym=Q.y.to_numpy()==1;lp=np.log(Q.p.to_numpy());lq=np.log(Q.q.to_numpy());dw=1/Q.groupby(['st','d']).dec.transform('nunique');wy=dw[ym].to_numpy()
    def loss(v):
        z=lq+v*(lp-lq);return -np.sum(wy*(z[ym]-np.log(np.bincount(code,weights=np.exp(z)))))
    x['pool_weight']=minimize_scalar(loss,bounds=(-1,2),method='bounded').x
    out[label]=x

# Exact agreement of served probabilities in the replay cache with raw snapshot.
j=R.merge(A[['st','d','t','k','p','y','bid','ask']],on=['st','d','t','k'],suffixes=('','_raw'),validate='many_to_one')
out['served_vector_mapping_check']={'rows':len(j),'replay_rows':len(R),'max_p_diff':float((j.p_served-j.p_raw).abs().max()),'label_disagreement':int((j.y!=j.y_raw).sum()),'max_bid_diff':float((j.bid-j.bid_raw).abs().max())}
out['replay_reconstruction']={'bytes':replay_bytes,'decisions':R.dec.nunique(),'lag_quantiles_h':np.quantile(lags,[0,.5,.9,1]).tolist()}
out['files']={p.name:{'size':p.stat().st_size,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()} for p in [SRC/'REPORT.md',SRC/'decisions.jsonl.gz']}
out['elapsed_s']=time.monotonic()-START
(HERE/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str))
print(json.dumps({k:v for k,v in out.items() if k not in ['files']},ensure_ascii=False,default=str))
