"""Frozen 12-arm ablation; reconstruct actual baseline before scoring."""
import collections, hashlib, itertools, json, math, pathlib, resource, shutil, time
import numpy as np
from bayes_basket_target.cross_consensus import bridged_cross_probability, distribution_sigma_c
from bayes_basket_target.native_ensemble_p0 import _ordered_buckets, _served_axis, _shift_probability_c
from bayes_basket_target.center_width_calibration import apply_center_width_calibration
from bayes_basket_target.center_width_calibration import bucket_rung_width_c
from bayes_basket_target.materialization import floor_probabilities
import historical_width_pre198 as historical_width

OUT=pathlib.Path(__file__).parent;START=time.monotonic()
resource.setrlimit(resource.RLIMIT_FSIZE,(32*1024**2,32*1024**2))
resource.setrlimit(resource.RLIMIT_CPU,(180,180))
BASE='served_b1_w1'
ARMS=[f'{w}_b{b}_w{l}' for w,b,l in itertools.product(['served','seat','equal'],[0,1],[0,1])]
def guard():
    if time.monotonic()-START>240 or not 0<resource.getrusage(resource.RUSAGE_SELF).ru_maxrss<768*1024**2:raise RuntimeError('resources')
    if shutil.disk_usage(OUT).free<4*1024**3:raise RuntimeError('disk')
def vector_ok(p):
    assert all(math.isfinite(v) and v>=0 for v in p.values()) and abs(sum(p.values())-1)<1e-7
def delta(a,b):return max(abs(a[k]-b[k]) for k in a)

def pipeline(r,weight='served',bridge=True,width=True,actual_floor=False):
    x=r['inputs']; geometry=x['bucket_intervals_c_json'];keys=_ordered_buckets(geometry);axis,edges=_served_axis(geometry,keys)
    systems=x['native_system_probabilities_json'];centers=x['native_calibrated_system_centers_c_json']
    weights=x['system_weights_json'] if weight=='served' else r['selection_weights'] if weight=='seat' else {s:1/len(x['system_weights_json']) for s in x['system_weights_json']}
    if set(weights)!=set(x['system_weights_json']) or not set(weights)<=set(systems):raise ValueError('weight_set')
    vector_ok(weights)
    if bridge and x['consensus_bridge_json']:
        p,receipt=bridged_cross_probability(system_probabilities=systems,system_weights=weights,system_centers_c=centers,ordered_keys=keys,key_centers_c=axis)
        if weight=='served':
            assert abs(receipt['lambda']-x['consensus_bridge_json']['lambda'])<1e-7
    else:
        p={k:sum(weights[s]*systems[s][k] for s in weights) for k in keys};total=sum(p.values());p={k:v/total for k,v in p.items()}
    cooling=x['synoptic_cooling_json']
    shift=0
    if cooling and cooling.get('shift_sigma') is not None:
        shift=float(cooling['shift_sigma'])*distribution_sigma_c([p[k] for k in keys],axis)
        if weight=='served' and bridge:assert abs(shift-cooling['shift_c'])<1e-7
        if abs(shift)>1e-12:p=_shift_probability_c(p,ordered=keys,bucket_centers_c=[a+shift for a in axis],intervals=geometry)
    elif cooling and cooling.get('shift_c',0):raise ValueError('cooling_sigma_missing')
    neighbour=x['neighbour_direction_json']
    if neighbour:
        if neighbour['state']!='RETIRED_CHARTER_147' or neighbour.get('applied_factors'):raise ValueError('non_identity_neighbour')
        total=sum(p.values());p={k:v/total for k,v in p.items()}
    cw=x['center_width_json'];pre=dict(p)
    if cw:
        if cw.get('sigma_floor_rungs',0)!=0 or cw.get('width_tier') is not None:raise ValueError('unsupported_width_parameters')
        width_function=apply_center_width_calibration
        if abs(cw['bucket_rung_c']-bucket_rung_width_c(axis))>1e-8:
            assert abs(cw['bucket_rung_c']-historical_width.bucket_rung_width_c(axis))<1e-8
            width_function=historical_width.apply_center_width_calibration
        if width:
            p,_=width_function(p,delta_c=cw['delta_c'],width_lambda=cw['width_lambda'],ordered_keys=keys,key_centers_c=axis,open_edge_bounds_c=edges)
        elif cw['delta_c']!=0:
            p,_=width_function(p,delta_c=cw['delta_c'],width_lambda=1.,ordered_keys=keys,key_centers_c=axis,open_edge_bounds_c=edges)
    band=x['band_reliability_json']
    if band and any(abs(float(v)-1)>1e-12 for v in band.get('applied_factors',{}).values()):raise ValueError('non_identity_retired_band')
    if band:
        total=sum(p.values());p={k:v/total for k,v in p.items()}
    floor=x['probability_floor_json']
    if not actual_floor or floor:
        if floor and floor['epsilon']!=0.001:raise ValueError('different_historical_floor')
        p,_=floor_probabilities(p)
    vector_ok(p)
    return p,pre

def scores(p,hit):
    return {'brier':sum((v-int(k==hit))**2 for k,v in p.items()),'logloss':-math.log(p[hit]),
            'entropy':-sum(v*math.log(v) for v in p.values() if v>0),'top_probability':max(p.values()),
            'top1':int(max(p,key=p.get)==hit)}

BINS=np.array([0,.01,.05,.10,.20,.30,.40,.50,.60,.70,.80,.90,1.000001])
def calibration(rows,arm):
    pairs=[(p,int(k==r['hit'])) for r in rows for k,p in r['arms'][arm].items()]
    pp=np.array([p for p,y in pairs]);yy=np.array([y for p,y in pairs]);ids=np.searchsorted(BINS,pp,side='right')-1
    scale=len(pairs)/len(rows);base=float(yy.mean());rel=res=ece=binned=0.;bins=[]
    for i in range(len(BINS)-1):
        sel=ids==i;n=int(sel.sum())
        if not n:continue
        p=float(pp[sel].mean());o=float(yy[sel].mean());frac=n/len(pairs)
        rel+=scale*frac*(p-o)**2;res+=scale*frac*(o-base)**2;ece+=frac*abs(p-o)
        binned+=scale*float(np.sum((p-yy[sel])**2))/len(pairs)
        bins.append({'lo':float(BINS[i]),'hi':min(1.,float(BINS[i+1])),'n':n,'mean_probability':p,'observed_rate':o})
    unc=scale*base*(1-base)
    assert abs(binned-(rel-res+unc))<1e-10
    raw=float(np.sum((pp-yy)**2)/len(rows));sel=pp>=.4
    return {'reliability':rel,'resolution':res,'uncertainty':unc,'binned_brier':binned,'raw_brier':raw,'binning_remainder':raw-binned,'ece':ece,'bins':bins,
            'high_probability':{'n':int(sel.sum()),'mean_probability':float(pp[sel].mean()) if sel.any() else None,'observed_rate':float(yy[sel].mean()) if sel.any() else None}}

def summary(rows):
    if not rows:return {'n':0}
    dates=sorted({r['date'] for r in rows});means={a:{m:float(np.mean([r['scores'][a][m] for r in rows])) for m in ['brier','logloss','entropy','top_probability','top1']} for a in ARMS}
    comparison={}
    for a in ARMS:
        comparison[a]={}
        for m in ['brier','logloss']:
            diff=np.array([r['scores'][a][m]-r['scores'][BASE][m] for r in rows]);leave=[]
            for day in dates:
                keep=np.array([r['date']!=day for r in rows])
                if keep.any():leave.append(float(diff[keep].mean()))
            comparison[a][m]={'delta':float(diff.mean()),'leave_one_date_out_range':[min(leave),max(leave)] if leave else None}
    units=sorted({r['market_unit'] for r in rows})
    return {'n':len(rows),'stations':len({r['station'] for r in rows}),'days':len(dates),'dates':dates,'means':means,'comparison':comparison,
      'calibration_scope':'single_unit' if len(units)==1 else 'not_pooled_across_C_F',
      'calibration':{a:calibration(rows,a) for a in ['served_b1_w1','seat_b1_w1','equal_b1_w1','served_b0_w1','served_b1_w0']} if len(units)==1 else {},
      'significance':'not_claimed_fewer_than_10_dates'}

def main():
    guard();cap=json.loads((OUT/'capture.json').read_text());rows=[];rejections=[];checks=[]
    for original in cap['rows']:
        guard();x=original['inputs'];r={k:original[k] for k in ['station','date','at','checkpoint','envelope_sha','posterior_version','truth_c','truth_revision','truth_id','truth_lane']}
        try:
            actual,pre=pipeline(original,actual_floor=True)
        except ValueError as error:
            rejections.append({'station':r['station'],'date':r['date'],'reason':str(error)});continue
        error=delta(actual,original['stored_probability'])
        cw=x['center_width_json'];pre_error=delta(pre,cw['input_probability']) if cw and cw.get('input_probability') else None
        checks.append({'station':r['station'],'date':r['date'],'error':error,'pre_width_error':pre_error})
        if error>1e-7:continue
        intervals=x['bucket_intervals_c_json'];hit=[k for k,(lo,hi) in intervals.items() if (lo is None or r['truth_c']>=lo) and (hi is None or r['truth_c']<hi)]
        assert len(hit)==1;r['hit']=hit[0];r['pool']=len(x['system_weights_json']);r['market_unit']='F' if any('°F' in k for k in intervals) else 'C'
        r['fusion_state']=(x['fusion_blend_json'] or {}).get('state','NOT_PRESENT');r['bridge_lambda']=(x['consensus_bridge_json'] or {}).get('lambda',0)
        r['hours_to_settlement']=(__import__('datetime').datetime.fromisoformat(r['date']).replace(tzinfo=__import__('zoneinfo').ZoneInfo(original['tz']))+__import__('datetime').timedelta(days=1)-__import__('datetime').datetime.fromisoformat(r['at'].replace('Z','+00:00'))).total_seconds()/3600
        r['arms']={};r['scores']={}
        for w,b,l in itertools.product(['served','seat','equal'],[0,1],[0,1]):
            name=f'{w}_b{b}_w{l}';p,_=pipeline(original,weight=w,bridge=bool(b),width=bool(l));r['arms'][name]=p;r['scores'][name]=scores(p,r['hit'])
        r['historical_floor_was_present']=bool(x['probability_floor_json']);r['floor_changed_probability']=delta(r['arms'][BASE],original['stored_probability'])>1e-7
        rows.append(r)
    validation={'input_n':len(cap['rows']),'explicit_rejections':rejections,'baseline_checks':checks,'mismatches':[x for x in checks if x['error']>1e-7],
      'max_baseline_error':max(x['error'] for x in checks),'max_pre_width_error':max(x['pre_width_error'] for x in checks if x['pre_width_error'] is not None)}
    (OUT/'reconstruction.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2))
    if validation['mismatches']:raise RuntimeError(f"baseline mismatch: {len(validation['mismatches'])}; inspect reconstruction.json")
    groups={'all':rows,'six_models':[r for r in rows if r['pool']==6],'fusion_applied':[r for r in rows if r['fusion_state']=='APPLIED'],
      'bridge_active':[r for r in rows if r['bridge_lambda']>0], 'bridge_full':[r for r in rows if r['bridge_lambda']==.4],
      'C':[r for r in rows if r['market_unit']=='C'],'F':[r for r in rows if r['market_unit']=='F'],
      'h_gt36':[r for r in rows if r['hours_to_settlement']>36],
      'current_v19':[r for r in rows if r['posterior_version']=='calibrated_native_cross_ensemble_p0_v19']}
    for unit in ['C','F']:
        groups['six_'+unit]=[r for r in groups['six_models'] if r['market_unit']==unit]
        groups['current_'+unit]=[r for r in groups['current_v19'] if r['market_unit']==unit]
    result={'protocol_sha256':hashlib.sha256((OUT/'PROTOCOL.md').read_bytes()).hexdigest(),'capture_sha256':hashlib.sha256((OUT/'capture.json').read_bytes()).hexdigest(),
      'n':len(rows),'days':dict(collections.Counter(r['date'] for r in rows)),'model_counts':dict(collections.Counter(r['pool'] for r in rows)),
      'fusion_states':dict(collections.Counter(r['fusion_state'] for r in rows)), 'uniform_current_floor_changed_rows':sum(r['floor_changed_probability'] for r in rows),
      'groups':{g:summary(rr) for g,rr in groups.items()},'by_date':{d:summary([r for r in rows if r['date']==d]) for d in sorted({r['date'] for r in rows})},
      'by_station':{s:summary([r for r in rows if r['station']==s]) for s in sorted({r['station'] for r in rows})}}
    (OUT/'scored_rows.json').write_text(json.dumps(rows,ensure_ascii=False,separators=(',',':')))
    (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));guard()
    print('accepted',len(rows),'rejected',len(rejections),'parity',validation['max_baseline_error'])
    for group in ['all','six_models','fusion_applied','current_v19']:
        g=result['groups'][group];print(group,g['n'])
        if not g['n']:continue
        for arm in [BASE,'seat_b1_w1','equal_b1_w1','served_b0_w1','served_b1_w0','served_b0_w0']:print(arm,g['means'][arm],g['comparison'][arm])
if __name__=='__main__':main()
