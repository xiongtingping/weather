"""Retrospective chronological sensitivity, NOT strict-as-of promotion OOS.

Retained historical revisions are read at the audit cutoff, not recovered at
each old decision. Gaussian scores isolate center and variance; production
member mixtures and downstream shaping are not replaced by this screen.
"""
import collections,json,math,pathlib,sys,time,os,threading,shutil
import numpy as np
from scipy.special import ndtr
import psutil
ROOT=pathlib.Path(__file__).resolve().parents[2];OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos
proc=psutil.Process();START=time.monotonic()
def guard():
    while True:
        if proc.memory_info().rss>700*1024**2 or time.monotonic()-START>300 or shutil.disk_usage(ROOT).free<5*1024**3:os._exit(73)
        time.sleep(.25)
threading.Thread(target=guard,daemon=True).start()
rows=[json.loads(s) for s in (OUT/'cases.jsonl').read_text().splitlines()]
scopes=collections.defaultdict(list)
for r in rows:scopes[(r['station'],r['system'],r['lead'],r['cycle'])].append(r)
profiles=[]
for key,rr in sorted(scopes.items()):
    e=np.array([r['actual_tmax_c']-r['mean'] for r in rr]);s=np.array([r['spread'] for r in rr])
    profiles.append(dict(zip(['station','system','lead','cycle'],key),n=len(rr),error_bias_c=float(e.mean()),error_sd_c=float(e.std()),raw_sigma_c=float(s.mean()),ratio=float(e.std()/max(s.mean(),1e-6))))
mapping={(r['station'],r['system'],r['event_date'],r['cycle'],r['lead']):r for r in rows}
pairs=[]
for key,r in mapping.items():
    if key[-1]!='h24_48':continue
    short=mapping.get(key[:-1]+('h00_12',))
    if short:pairs.append({'system':r['system'],'station':r['station'],'date':r['event_date'],'cycle':r['cycle'],
        'long_s':r['spread'],'short_s':short['spread'],'long_e':r['actual_tmax_c']-r['mean'],'short_e':short['actual_tmax_c']-short['mean']})
pair_stats=[]
for system in sorted({p['system'] for p in pairs}):
    rr=[p for p in pairs if p['system']==system]
    station_ratios=[]
    for st in sorted({r['station'] for r in rr}):
        ss=[r for r in rr if r['station']==st];station_ratios.append(np.mean([r['short_s'] for r in ss])/np.mean([r['long_s'] for r in ss])-1)
    pair_stats.append({'system':system,'n':len(rr),'stations':len(station_ratios),'mean_long_sigma_c':float(np.mean([r['long_s'] for r in rr])),
        'mean_short_sigma_c':float(np.mean([r['short_s'] for r in rr])),'station_mean_relative_change':float(np.mean(station_ratios)),
        'median_abs_center_error_long_c':float(np.median([abs(r['long_e']) for r in rr])),
        'median_abs_center_error_short_c':float(np.median([abs(r['short_e']) for r in rr]))})
arms=['baseline5','spread15','spread30','rms_denominator5','variance_floor_mix15']
preds=[];parity=[]
for key,rr in sorted(scopes.items()):
    rr.sort(key=lambda r:r['event_date']); dates=np.array([np.datetime64(r['event_date'],'D').astype(int) for r in rr])
    means=np.array([r['mean'] for r in rr]);errors=np.array([r['actual_tmax_c']-r['mean'] for r in rr]);spreads=np.array([r['spread'] for r in rr])
    for i,r in enumerate(rr):
        if r['event_date']<'2026-08-20' or r['event_date']>'2026-09-08' or not r['holdout_eligible']:continue
        idx=np.flatnonzero((dates<dates[i])&(dates>=dates[i]-60))
        if len(idx)<20:continue
        e=errors[idx];s=spreads[idx];m=means[idx];ages=dates[idx].max()-dates[idx]
        w5=2.**(-ages/5);w5/=w5.sum();bias=float(w5@e)
        ordered=sorted(m);cuts=[ordered[max(0,len(ordered)*(j+1)//3-1)] for j in range(2)]
        bin_idx=np.searchsorted(cuts,m,side='left');today=int(np.searchsorted(cuts,means[i],side='left'))
        unnorm=2.**(-ages/5);sel=bin_idx==today
        shift=float((unnorm[sel]@e[sel]+10*bias)/(unnorm[sel].sum()+10))
        esd=math.sqrt(float(w5@((e-bias)**2)));avg=float(w5@s);rms=math.sqrt(float(w5@(s*s)))
        k=float(np.clip(esd/max(avg,.1),.5,5));ks=[k];rse=[]
        for half in [15,30]:
            w=2.**(-ages/half);w/=w.sum();b=float(w@e);sd=math.sqrt(float(w@((e-b)**2)))
            ks.append(float(np.clip(sd/max(float(w@s),.1),.5,5)))
            if half==15:sd15=sd;rms15=math.sqrt(float(w@(s*s)))
        widths=[ks[0]*spreads[i],ks[1]*spreads[i],ks[2]*spreads[i],float(np.clip(esd/max(rms,.1),.5,5))*spreads[i],
                math.sqrt(sd15**2*(.5+.5*spreads[i]**2/max(rms15**2,.01)))]
        # Preserve the existing kernel's actual variance for the three k arms.
        sigmas=[math.sqrt(v*v+.25) if v<=.5 else v for v in widths]
        resid=errors[i]-shift
        results={}
        for name,sd in zip(arms,sigmas):
            z=resid/sd
            nll=.5*math.log(2*math.pi)+math.log(sd)+.5*z*z
            crps=sd*(z*(2*float(ndtr(z))-1)+2*math.exp(-.5*z*z)/math.sqrt(2*math.pi)-1/math.sqrt(math.pi))
            results[name]={'sigma_c':sd,'nll':nll,'crps_c':crps,'cover80':float(abs(z)<=1.2815515655),'cover95':float(abs(z)<=1.9599639845)}
        preds.append({'station':key[0],'system':key[1],'lead':key[2],'cycle':key[3],'date':r['event_date'],
            'train_n':len(idx),'neff5':float(1/(w5@w5)),'ratio_mean_vs_rms':rms/max(avg,.1),
            'raw_sigma_c':spreads[i],'calibrated_sigma_c':widths[0],'widths_c':dict(zip(arms,widths)),
            'center_shift_c':shift,'k5':ks[0],'center_error_c':resid,'raw_error_c':float(errors[i]),'results':results})
        if len(parity)<40:
            pp=oos._fit_parameters([rr[j] for j in idx]); resolved=oos._resolved_bias_c(means[i],pp)
            parity.append(max(abs(pp['spread_factor']-k),abs(resolved-shift)))
summaries=[];by_day_station=collections.defaultdict(list)
for p in preds:by_day_station[(p['station'],p['date'])].append(p)
for arm in arms:
    cells=[]
    for (st,day),pr in by_day_station.items():
        cells.append({'station':st,'date':day,**{met:float(np.mean([p['results'][arm][met] for p in pr])) for met in ['nll','crps_c','cover80','cover95']},
          'nll_gain':float(np.mean([p['results']['baseline5']['nll']-p['results'][arm]['nll'] for p in pr])),
          'crps_gain':float(np.mean([p['results']['baseline5']['crps_c']-p['results'][arm]['crps_c'] for p in pr]))})
    station_gains=[np.mean([p['nll_gain'] for p in cells if p['station']==st]) for st in sorted({p['station'] for p in cells})]
    ds=sorted({p['date'] for p in cells}); daily=np.array([np.mean([p['nll_gain'] for p in cells if p['date']==d]) for d in ds])
    rng=np.random.default_rng(631);boots=[]
    for _ in range(1500):
        selected=[]
        while len(selected)<len(daily):
            st=int(rng.integers(len(daily)));selected.extend(daily[(st+np.arange(4))%len(daily)])
        boots.append(np.mean(selected[:len(daily)]))
    summaries.append({'arm':arm,'predictions':len(preds),'station_days':len(cells),'dates':len(ds),
       'stations':len(station_gains),'stations_better_nll':int(sum(v>0 for v in station_gains)),
       **{met:float(np.mean([p[met] for p in cells])) for met in ['nll','crps_c','cover80','cover95','nll_gain','crps_gain']},
       'date_block4_bootstrap_nll_gain_ci95':list(np.quantile(boots,[.025,.975]))})
stats={'paired_raw_convergence':pair_stats,'scope_profiles':profiles,'sensitivity':summaries,
 'prediction_count':len(preds),'parity_max_abs_error':max(parity),'case_count':len(rows),
 'stations':sorted({r['station'] for r in rows}),'systems':sorted({r['system'] for r in rows}),
 'calibrated_sigma_le_05_count':sum(p['calibrated_sigma_c']<=.5 for p in preds),
 'mean_vs_rms_sigma_ratio_quantiles':list(np.quantile([p['ratio_mean_vs_rms'] for p in preds],[.1,.5,.9])),
 'neff5_quantiles':list(np.quantile([p['neff5'] for p in preds],[0,.1,.5,.9,1])),
 'limitations':['Retrospective current revisions; not strict-as-of OOS','Gaussian moment scores; not served bucket scores','Four-day circular date block bootstrap; few event dates','All current loader training truths are AWC; canonical ASOS parity not established']}
(OUT/'analysis.json').write_text(json.dumps(stats,indent=2,default=lambda v:v.item()))
with (OUT/'predictions.jsonl').open('w') as f:
    for p in preds:f.write(json.dumps(p,separators=(',',':'),default=lambda v:v.item())+'\n')
print(json.dumps({k:v for k,v in stats.items() if k!='scope_profiles'},indent=2,default=lambda v:v.item()))
