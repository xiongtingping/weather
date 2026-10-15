"""Post-hoc diagnostic (not pre-registered): test 2's shift with a walk-forward strength gamma.

The pre-registered shift moved the P1 mean by the full predicted residual
under P1's own variance; late in the day that variance is tiny and the tilt
concentrates the whole vector.  Here the shift is gamma x e_hat, gamma chosen
on prior days (fleet) from {0, .1, .25, .5, 1}, and per station from the same
grid on the station's own prior days (>= 8 station-days, else fleet gamma).
"""
import json, numpy as np, analyze as A
GRID=[0.0,0.1,0.25,0.5,1.0]
recs=A.load(); n=len(recs)
st=np.array([r['station'] for r in recs]); days=np.array([r['day'] for r in recs]); X=np.array([r['x'] for r in recs],float)
T=np.array([r['T'] for r in recs]); p1m=np.array([(r['p']*r['c']).sum() for r in recs]); e=T-p1m
day_ids=np.unique(days); test_days=[d for d in day_ids if (day_ids<d).sum()>=10]
ehat=np.full(n,np.nan)
for d in test_days:
    tr=days<d; te=np.where(days==d)[0]
    Z=A.standardise(X,np.where(tr)[0]); ehat[te]=A.shrunk_station_predict(Z,e,st,days,tr,te)
# NOTE: e_hat for training-day rows used in gamma selection must itself be out-of-sample: it is (each day's e_hat was predicted from earlier days)
base=np.full(n,np.nan); sc={g:np.full(n,np.nan) for g in GRID}
for i in np.where(~np.isnan(ehat))[0]:
    r=recs[i]; base[i]=A.score(r['p'],r)[0]
    for g in GRID: sc[g][i]=A.score(A.tilt_shift(r['p'],r['c'],g*ehat[i]),r)[0]
dd={g:sc[g]-base for g in GRID}
sd_key=np.array([r['station']+'|'+r['day'] for r in recs]); sd_ids,sd_idx=np.unique(sd_key,return_inverse=True)
day_ids2,day_idx=np.unique(days,return_inverse=True); sd_day=np.zeros(len(sd_ids),int); sd_day[sd_idx]=day_idx
draws=np.random.default_rng(7).multinomial(len(day_ids2),np.full(len(day_ids2),1/len(day_ids2)),size=2000)
fleet=np.full(n,np.nan); stat=np.full(n,np.nan); chosen_f={}; chosen_s={}
for d in test_days:
    prior=(days<d)&~np.isnan(ehat)
    if len(set(days[prior]))<5: continue
    gf=min(GRID,key=lambda g:np.nanmean(dd[g][prior])); chosen_f[d]=gf
    te=(days==d)&~np.isnan(ehat); fleet[te]=dd[gf][te]
    for s in set(st[te]):
        ps=prior&(st==s)
        gs=min(GRID,key=lambda g:np.nanmean(dd[g][ps])) if len(set(days[ps]))>=8 else gf
        chosen_s[(s,d)]=gs; m=te&(st==s); stat[m]=dd[gs][m]
out={'fixed':{str(g):A.boot_sd_day(dd[g],sd_idx,sd_day,len(day_ids2),draws) for g in GRID},
     'fleet_wf':A.boot_sd_day(fleet,sd_idx,sd_day,len(day_ids2),draws),'fleet_gamma_by_day':chosen_f,
     'station_wf':A.boot_sd_day(stat,sd_idx,sd_day,len(day_ids2),draws),'stations':{}}
pv={}
for s in sorted(set(st)):
    b=A.boot_sd_day(stat,sd_idx,sd_day,len(day_ids2),draws,st==s)
    if b: out['stations'][s]=b; pv[s]=b['p']
ps=A.bh({s:p for s,p in pv.items() if out['stations'][s]['station_days']>=10})
for s in out['stations']: out['stations'][s]['bh_q10_min10sd']=s in ps
json.dump(out,open('readings_gamma_diag.json','w'),indent=1,default=str)
for g in GRID: v=out['fixed'][str(g)]; print('gamma',g,round(v['point'],4),[round(v['lo'],4),round(v['hi'],4)])
print('fleet wf',out['fleet_wf'],'gammas',sorted(set(chosen_f.values())))
print('station wf',out['station_wf'])
for s,v in sorted(out['stations'].items(),key=lambda kv:kv[1]['point']):
    print(f"  {s} sd{v['station_days']:3d} {v['point']:+.4f} [{v['lo']:+.4f},{v['hi']:+.4f}] {'BH*' if v['bh_q10_min10sd'] else ''}")
