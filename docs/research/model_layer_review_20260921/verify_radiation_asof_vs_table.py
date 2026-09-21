import sqlite3,json,sys,time,statistics as st,math
from datetime import date,timedelta
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.materialization import rehydrate_lineage_documents
c=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=5);c.row_factory=sqlite3.Row
c.execute('pragma query_only=on')
stations=[r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding') if len(r[0])==4]
table={(r[0],r[1]):r[2] for r in c.execute("select station_id,local_date,shortwave_radiation_sum from calibration_station_forecast_physics where shortwave_radiation_sum is not null")}
truth={}
for s,d,t,rev in c.execute("select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth where local_date>='2026-08-10' and official_tmax_c is not null"):
    if (s,d) not in truth or (rev or 0)>truth[(s,d)][1]: truth[(s,d)]=(t,rev or 0)
rows=[];t0=time.time()
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
            rad=sc.get('radiation_sum')
            if rad is None: continue
            seen.add(r['event_date'])
            ev=r['event_date']; tb=table.get((s,ev)); tr=truth.get((s,ev))
            lead=(date.fromisoformat(ev)-date.fromisoformat(r['decision_at_utc'][:10])).days
            cen=l.get('model_center_c')
            rows.append(dict(s=s,ev=ev,lead=lead,asof=float(rad),table=tb,truth=tr[0] if tr else None,center=float(cen) if cen is not None else None))
print('rows',len(rows),'secs',round(time.time()-t0,1))
both=[r for r in rows if r['table'] is not None]
diff=[abs(r['asof']-r['table']) for r in both]
print('with table value',len(both),'identical',sum(1 for x in diff if x<1e-9),'mean|asof-table| MJ',round(st.mean(diff),2),'median',round(st.median(diff),2))
# standardise per station by table history
import collections
hist=collections.defaultdict(list)
for (s,d),v in table.items(): hist[s].append(v)
mu={s:st.mean(v) for s,v in hist.items()}; sd={s:st.pstdev(v) for s,v in hist.items()}
sdd=[abs(r['asof']-r['table'])/sd[r['s']] for r in both if sd.get(r['s'])]
print('median |asof-table| in station SD units',round(st.median(sdd),3),'mean',round(st.mean(sdd),3))
res=[r for r in both if r['truth'] is not None and r['center'] is not None and sd.get(r['s'])]
def corr(x,y):
    mx,my=st.mean(x),st.mean(y); sx=math.sqrt(sum((a-mx)**2 for a in x)); sy=math.sqrt(sum((b-my)**2 for b in y))
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/(sx*sy)
# demean residual per station to avoid station bias pooling
by=collections.defaultdict(list)
for r in res: by[r['s']].append(r)
X1=[];X2=[];Y=[]
for s,v in by.items():
    if len(v)<8: continue
    rm=st.mean(r['truth']-r['center'] for r in v)
    for r in v:
        Y.append(r['truth']-r['center']-rm); X1.append((r['asof']-mu[s])/sd[s]); X2.append((r['table']-mu[s])/sd[s])
print('n residual pairs',len(Y),'stations',sum(1 for v in by.values() if len(v)>=8))
print('corr(residual, as-of radiation z)',round(corr(X1,Y),3))
print('corr(residual, table radiation z)',round(corr(X2,Y),3))
# bootstrap by station-day blocks is overkill; report Fisher SE
n=len(Y); print('approx SE',round(1/math.sqrt(n-3),3))
for L in sorted(set(r['lead'] for r in both)):
    v=[abs(r['asof']-r['table'])/sd[r['s']] for r in both if r['lead']==L and sd.get(r['s'])]
    print(' lead',L,'n',len(v),'median |diff| SD',round(st.median(v),3) if v else None)
