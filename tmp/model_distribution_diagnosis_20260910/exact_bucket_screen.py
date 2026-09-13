"""Exact single-system member-kernel bucket screen on retained market axes.
Four event dates; preserves native/republisher boundary. Does not claim an
end-to-end fused/served replay or strict historical publication eligibility.
"""
import pathlib,json,sys,math,collections,time,os,threading,shutil
import numpy as np
from scipy.special import ndtr
import psutil
ROOT=pathlib.Path(__file__).resolve().parents[2];OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from bayes_basket_target.native_ensemble_p0 import _interval_probability
proc=psutil.Process();START=time.monotonic()
def guard():
    while True:
        if proc.memory_info().rss>600*1024**2 or time.monotonic()-START>180 or shutil.disk_usage(ROOT).free<5*1024**3:os._exit(73)
        time.sleep(.25)
threading.Thread(target=guard,daemon=True).start()
source=[json.loads(s) for s in (OUT/'cases.jsonl').open()]
case={(r['station'],r['system'],r['event_date'],r['lead'],r['cycle']):r for r in source}
pred=[json.loads(s) for s in (OUT/'predictions.jsonl').open()]
snap=json.loads((OUT/'serving_snapshot.json').read_text())
axes={(r['station'],r['day'],r['lead']):r['intervals'] for r in snap['rows']}
arms=['baseline5','spread15','spread30','rms_denominator5','variance_floor_mix15'];results=[]
def A(x,s):
    z=x/s;return 2*s*np.exp(-z*z/2)/math.sqrt(2*math.pi)+x*(2*ndtr(z)-1)
for p in pred:
    ints=axes.get((p['station'],p['date'],p['lead']))
    if ints is None:continue
    r=case[(p['station'],p['system'],p['date'],p['lead'],p['cycle'])]
    assert r['transport_provider_id']!='OPEN_METEO'
    actual=r['actual_tmax_c'];winner=next(k for k,(lo,hi) in ints.items() if (lo is None or actual>=lo) and (hi is None or actual<hi))
    mean=r['mean'];v=np.array(r['values']);row={k:p[k] for k in ['station','system','date','lead','cycle']};row['results']={}
    for arm in arms:
        width=p['widths_c'][arm];gain=width/max(r['spread'],1e-12)
        vals=mean+p['center_shift_c']+(v-mean)*gain
        prob=_interval_probability(vals.tolist(),ints)
        assert abs(sum(prob.values())-1)<1e-12 and all(x>=0 for x in prob.values())
        nll=-math.log(max(prob[winner],1e-9));peak=max(prob,key=prob.get)==winner
        # Exact continuous CRPS of the same kernel mixture used above.
        if width>.5: vals=vals.mean()+(vals-vals.mean())*math.sqrt(1-(.5/width)**2)
        crps=float(A(vals-actual,.5).mean()-.5*A(vals[:,None]-vals[None,:],math.sqrt(2)*.5).mean())
        row['results'][arm]={'nll':nll,'peak_hit':float(peak),'crps_c':crps}
    results.append(row)
summary=[]
for arm in arms:
    cells=collections.defaultdict(list)
    for r in results:cells[(r['station'],r['date'])].append(r)
    totals=[]
    for key,rs in cells.items():
        totals.append({m:float(np.mean([r['results'][arm][m] for r in rs])) for m in ['nll','peak_hit','crps_c']})
    summary.append({'arm':arm,'scope_day_predictions':len(results),'station_days':len(totals),
                    **{m:float(np.mean([r[m] for r in totals])) for m in ['nll','peak_hit','crps_c']}})
base=summary[0]
for r in summary:
    r['nll_gain']=base['nll']-r['nll'];r['crps_gain_c']=base['crps_c']-r['crps_c'];r['peak_hit_change_pp']=100*(r['peak_hit']-base['peak_hit'])
by_station={}
for arm in arms[1:]:
    by_station[arm]=sum(np.mean([r['results']['baseline5']['nll']-r['results'][arm]['nll'] for r in results if r['station']==st])>0 for st in {r['station'] for r in results})
out={'summary':summary,'stations':len({r['station'] for r in results}),'dates':sorted({r['date'] for r in results}),
     'systems':sorted({r['system'] for r in results}),'stations_better':by_station,
     'scope':'Exact standalone member-kernel probability on retained real market intervals; no fusion or downstream calibration; retrospective truth revisions; not promotion OOS'}
F_STATIONS={'KATL','KAUS','KBKF','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO'}
for r in results:r['unit_group']='F' if r['station'] in F_STATIONS else 'C'
strata=[]
for dimension in ['date','system','lead','unit_group']:
    for value in sorted({r[dimension] for r in results}):
        rr=[r for r in results if r[dimension]==value]
        cells=collections.defaultdict(list)
        for r in rr:cells[(r['station'],r['date'])].append(r)
        for arm in arms[1:]:
            gains=[np.mean([r['results']['baseline5']['nll']-r['results'][arm]['nll'] for r in cell]) for cell in cells.values()]
            strata.append({'dimension':dimension,'value':value,'arm':arm,'n':len(rr),'station_days':len(cells),'nll_gain':float(np.mean(gains))})
out['stratified_checks']=strata
out['candidate_formula']='sigma_target^2 = sd_residual_15^2 * (0.5 + 0.5 * sigma_today^2 / E15[sigma^2]); fixed coefficients, scope retained; kernel follows current code'
(OUT/'exact_bucket_screen.json').write_text(json.dumps(out,indent=2,default=lambda v:v.item()))
print(json.dumps(out,indent=2,default=lambda v:v.item()))
