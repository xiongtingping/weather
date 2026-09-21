import sqlite3,json,sys,time,statistics as st,math,collections,random
from datetime import date,timedelta
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.materialization import rehydrate_lineage_documents
c=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=5);c.row_factory=sqlite3.Row
c.execute('pragma query_only=on')
stations=[r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding') if len(r[0])==4]
truth={}
for s,d,t,rev in c.execute("select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth where local_date>='2026-08-10' and official_tmax_c is not null"):
    if (s,d) not in truth or (rev or 0)>truth[(s,d)][1]: truth[(s,d)]=(t,rev or 0)
rows=[]
start=date(2026,8,16)
for s in stations:
    seen=set()
    for i in range(0,36):
        d=(start+timedelta(days=i)).isoformat()
        for hh in ('T00:00','T12:00'):
            r=c.execute("select event_date,decision_at_utc,lineage_json from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx where station_id=? and stage='P0' and state='COMMITTED' and decision_at_utc>=? order by decision_at_utc asc limit 1",(s,d+hh)).fetchone()
            if not r or r['event_date'] in seen: continue
            l=rehydrate_lineage_documents(c,json.loads(r['lineage_json']))
            sc=l.get('synoptic_cooling_json'); sc=(json.loads(sc) if isinstance(sc,str) else sc) or {}
            if sc.get('state')!='RESOLVED' or not sc.get('shift_sigma') or sc.get('standardised') is None: continue
            tr=truth.get((s,r['event_date']))
            if tr is None or l.get('model_center_c') is None: continue
            seen.add(r['event_date'])
            sigma=sc['shift_c']/sc['shift_sigma']
            rows.append(dict(s=s,ev=r['event_date'],z=float(sc['standardised']),sig=sigma,k=sc['shift_c']/ (sc['standardised']*sigma) if sc['standardised'] else None,
                             res=tr[0]-float(l['model_center_c']),tag=sc.get('method_tag')))
by=collections.defaultdict(list)
for r in rows: by[r['s']].append(r)
X=[];Y=[];S=[];D=[]
for s,v in by.items():
    if len(v)<8: continue
    m=st.mean(r['res'] for r in v)
    for r in v: X.append(r['z']*r['sig']); Y.append(r['res']-m); S.append(s); D.append(r['ev'])
def fit(ix):
    sxy=sum(X[i]*Y[i] for i in ix); sxx=sum(X[i]*X[i] for i in ix); return sxy/sxx
ix=list(range(len(X))); k=fit(ix)
days=sorted(set(D)); random.seed(1); boots=[]
byday=collections.defaultdict(list)
for i,d in enumerate(D): byday[d].append(i)
for _ in range(400):
    samp=[i for d in random.choices(days,k=len(days)) for i in byday[d]]; boots.append(fit(samp))
boots.sort()
served=[r['k'] for r in rows if r['k']]
print('n',len(X),'days',len(days),'tags',collections.Counter(r['tag'] for r in rows))
print('as-of fitted coefficient (sigma per standardised unit) =',round(k,3),'95% day-block CI',[round(boots[10],3),round(boots[389],3)])
print('served coefficient median',round(st.median(served),3),'range',round(min(served),3),round(max(served),3))
