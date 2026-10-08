"""Nested chronological cloud experiments on reconstructed prefix rows.

Results are restricted to the input-authorized population. A partial
population or counterfactual parameter override cannot promote production.
"""
import argparse
import collections
import gzip
import json
import math
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np
from scipy.optimize import minimize
from rebuild import HERE,ROOT,guard,utc,stamp,MAX_BYTES,old
from bayes_basket_target.contracts import ProbabilityEnvelope,content_sha256
from bayes_basket_target.materialization import materialize_p0m_observed
from bayes_basket_target.observed_floor_censoring import p1_root_probabilities
from bayes_basket_target import taf_likelihood as TL,p1_direct_update as DU,p1_observation_likelihood as OL
from scripts import fit_taf_likelihood as TF,fit_p1_observation_likelihood as PF
from clock_offsets import ClosedClocks
from fit_storage import write_fit_report


SAT_STATIONS={'CYYZ','KATL','KAUS','KBKF','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO','MMMX','MPMG','SAEZ','SBGR','NZWN','RCSS','RJTT','RKPK','RKSI','RPLL','WMKK','WSSS','ZBAA','ZGGG','ZGSZ','ZHHH','ZSPD','ZSQD','ZUCK','ZUUU'}


def label_at(r,at=None):
    eligible=[x for x in r['truth_revisions'] if at is None or utc(x['known_at'])<=at]
    if not eligible:return None
    revision=max(x['revision'] for x in eligible);w=[x for x in eligible if x['revision']==revision]
    if len(w)!=1:raise ValueError('ambiguous_truth_revision')
    return w[0]


def bucket(r,value):
    found=[b for b,(lo,hi) in r['sample']['view']['intervals'].items() if (lo is None or value>=lo) and (hi is None or value<hi)]
    if len(found)!=1:raise ValueError('truth_not_uniquely_mapped')
    return found[0]


def served_parent(source,r):
    """Parent-sensitivity arm: the P0 production had actually served at the
    research root's decision instant (newest committed P0 at or before it),
    on the same anchor, ladder and lineage. Only the vector and centre move;
    everything downstream is refitted causally exactly as in the main arm."""
    p=r['p0'];t=utc(p['decision_at_utc'])
    found=[x for x in source.execute("select decision_at_utc,checkpoint_id,posterior_version,probabilities_json,json_extract(lineage_json,'$.model_center_c') from basket_probability_envelope where station_id=? and event_date=? and +stage='P0' and state='COMMITTED' and substr(decision_at_utc,1,19)<=? order by decision_at_utc desc limit 8",(r['station'],r['day'],p['decision_at_utc'][:19])) if utc(x[0])<=t]
    if not found:raise ValueError('served_parent_missing')
    at,checkpoint,version,probs,center=max(found,key=lambda x:utc(x[0]));probs=json.loads(probs)
    if set(probs)!=set(p['probabilities']) or center in (None,''):raise ValueError('served_parent_ladder_or_centre_mismatch')
    lineage=dict(p['lineage'],model_center_c=str(center),served_parent_checkpoint_id=checkpoint,served_parent_decision_at_utc=at,served_parent_posterior_version=version)
    return dict(p,probabilities=probs,posterior_sha256=content_sha256(probs),lineage=lineage)


def satellite_sky(r,sky):
    """Replace the METAR side of each TAF cloud pair with the satellite's own
    fraction (docs/research/sat_cloud_obs_20261008): the newest scan public
    (S3 LastModified) at the anchor and begun within 2 h of it, against the
    same TAF coverage interval.  Heights without a satellite reading become
    unknown; stations without a satellite stay without evidence."""
    rec=sky.at(r['station'],r['as_of'])
    if rec is None:
        r['cloud_pairs']={m:None for m in ('latest','day1','nwp')};return False
    r['cloud_observation_valid_at']=rec['scan_start'];r['cloud_observation_origin_eligible']=True;r['satellite_scan']=rec
    for m,p in list(r['cloud_pairs'].items()):
        if p is None:continue
        inc={}
        for h in ('1500','3000','5000','6500'):
            value=rec.get('low_'+h)
            inc[h]=sky.increment(value,p['forecast_intervals'].get(h)) if value is not None else None
        r['cloud_pairs'][m]=dict(p,coverage_increment=inc,base_lowering_km=None)
    # Like for like: the satellite's total cloud against the NWP's total
    # cloud forecast issued a day before (not the TAF's base-defined cover).
    fc=sky.features.forecast_total(r['station'],rec['scan_start'])
    r['cloud_pairs']['nwp']=None if fc is None else dict(coverage_increment={'total':rec['cloudy']-fc},base_lowering_km=None,
        sha='open-meteo:cloud_cover_previous_day1',known_at=stamp(utc(rec['scan_start'])-timedelta(hours=24)),forecast_total=fc)
    return True


class SatelliteSky:
    def __init__(self):
        sys.path.insert(0,str(ROOT/'docs/research/sat_cloud_obs_20261008'))
        import sat_features
        self.features=sat_features;self.by=sat_features.load();self.increment=sat_features.increment
    def at(self,station,as_of):
        return self.features.at(self.by[station],as_of) if station in self.by else None


def read_rows(folder,parent='research',sky=None,sat_stations_only=False):
    allrows=[];profile=collections.Counter();slimbytes=0;cache=HERE/'p0_envelope_cache';cache.mkdir(exist_ok=True);origin_audit=json.loads((HERE/'cloud_origin_audit.json').read_text())
    source=old.connect(ROOT/'data/bayes_basket_target_ssot.db') if parent=='served' else None
    for path in sorted(folder.glob('*.jsonl.gz')):
        readbytes=0;station=path.name.split('.')[0];origin=origin_audit['stations'][station]
        if old.sha_file(path)!=origin['input_sha']:raise ValueError('cloud_origin_audit_input_changed')
        with gzip.open(HERE/origin['eligibility_path'],'rb') as f:body=f.read(2*1024**2+1)
        if len(body)>2*1024**2:raise RuntimeError('cloud_origin_eligibility_object_limit')
        eligibility=json.loads(body)
        if content_sha256(eligibility)!=origin['eligibility_sha']:raise ValueError('cloud_origin_eligibility_identity_failed')
        with gzip.open(path,'rt') as f:
            for ln in f:
                readbytes+=len(ln.encode())
                if readbytes>MAX_BYTES:raise RuntimeError('station_input_limit')
                r=json.loads(ln);profile[r['state']]+=1
                if r['state']=='REFUSED':continue
                origin_key=content_sha256([r['station'],r['day'],r['as_of'],r['observation_id']])
                if origin_key not in eligibility:raise ValueError('cloud_origin_eligibility_missing')
                r['cloud_observation_origin_eligible']=eligibility[origin_key]['eligible']
                if sat_stations_only and r['station'] not in SAT_STATIONS:profile['not_a_satellite_station']+=1;continue
                if sky is not None:
                    profile['satellite_scan_at_anchor:'+str(satellite_sky(r,sky))]+=1
                elif not r['cloud_observation_origin_eligible']:
                    r['cloud_pairs']={m:None for m in ('latest','day1')};profile['cloud_increment_refused_origin_not_visible']+=1
                # Keep only the geometry/probabilities and fitted root
                # provenance this stage actually uses; raw manifests remain
                # separately inspectable in the preceding stage.
                r['sample'].pop('prefix_manifest',None);r['sample'].pop('prefix_ids',None)
                if r['forecast']:r['forecast'].pop('raw',None)
                if parent=='served':r['p0']=served_parent(source,r);profile['served_parent:'+r['p0']['lineage']['served_parent_posterior_version']]+=1
                rawroot=r.pop('p0');rootsha=content_sha256(rawroot);cached=cache/(rootsha+'.json.gz')
                if not cached.exists():
                    with gzip.open(cached,'wt') as out:json.dump(rawroot,out,ensure_ascii=False)
                r['p0_receipt_path']=str(cached.relative_to(HERE));r['p0_receipt_sha']=rootsha
                r['p0_probabilities']=rawroot['probabilities'];r['p0_model_center']=rawroot['lineage'].get('model_center_c')
                r.pop('p0_refit_receipts',None);r.pop('p0_parameter_audit',None)
                r['cloud_pairs']={m:(None if p is None else {k:p[k] for k in ('coverage_increment','base_lowering_km','sha','known_at')}) for m,p in r['cloud_pairs'].items()}
                lab=label_at(r)
                if lab['truth_c']<r['sample']['high']-0.05:profile['truth_below_running_high']+=1;continue
                r['evaluation_label']=lab;slimbytes+=len(json.dumps(r).encode())
                if slimbytes>MAX_BYTES:raise RuntimeError('fit_input_projection_limit')
                allrows.append(r)
        guard()
    return allrows,dict(profile)


def taf_case(r,lab):
    f=r['forecast']
    if not f or f['tx_c'] is None:return None
    s=r['sample'];centers=s['view']['centers'];probs=r['p0_probabilities'];c0=r['p0_model_center']
    if c0 in (None,'NONE',''):raise ValueError('p0_model_center_missing')
    mean=sum(float(probs[b])*centers[b] for b in probs)
    return dict(station_id=r['station'],event_date=r['day'],tx_c=float(f['tx_c']),c0=float(c0),truth_c=float(lab['truth_c']),truth_bucket=bucket(r,lab['truth_c']),centers=centers,probabilities=probs,group=TL.disagreement_group(float(f['tx_c'])-mean,f['cloud_cover'],f['precip_prob']),weight=1.0,
                decided_at=utc(r['as_of']).timestamp(),truth_revisions=[(utc(x['known_at']).timestamp(),x['revision'],x['truth_c'],bucket(r,x['truth_c'])) for x in r['truth_revisions']])


def p0m_rebuild(rows):
    days=sorted({r['day'] for r in rows});ready=[];receipts=[];past_taf=[]
    for day in days:
        current=[r for r in rows if r['day']==day];cut=min(utc(r['as_of']) for r in current);train=[]
        for r in rows:
            if r['day']>=day:continue
            lab=label_at(r,cut)
            if lab is not None:
                case=taf_case(r,lab)
                if case:train.append(case)
        # One copy per forecast/raw input pair. Multiple hourly anchors of
        # the same information do not create extra TAF observations.
        unique={}
        for case in train:
            key=(case['station_id'],case['event_date'],case['tx_c'],case['c0'])
            unique.setdefault(key,case)
        train=list(unique.values());tdays={r['event_date'] for r in train}
        artifact=None
        if len(tdays)>=TF.MIN_TRAIN_DAYS:
            artifact=dict(schema=TL.SCHEMA,rule=TL.RULE,gate=dict(state=TL.GATE_SERVE,ruling=TL.GATE_RULING_ID),**TF.fit(train))
            # Tempering is itself causal. Its gate is re-evaluated using
            # only the closed cases available before this outer day.
            counts=collections.Counter((x['station_id'],x['event_date']) for x in train)
            for x in train:x['weight']=1/counts[(x['station_id'],x['event_date'])]
            lambdas,weights=TF.fit_tempering(artifact,train)
            tg=TF.walk_forward_tempering(train,train)
            artifact[TL.TEMPERING_KEY]=dict(rule=TL.TEMPERING_RULE,state=tg['state'],gate=tg,lambda_by_group=lambdas)
            artifact.update(trained_through=max(tdays),fitted_at=stamp(cut));artifact['content_sha256']=content_sha256(artifact)
        receipts.append(dict(day=day,cutoff=stamp(cut),train_days=len(tdays),train_cases=len(train),training_sha=content_sha256(train),artifact=artifact))
        for r in current:
            with gzip.open(HERE/r['p0_receipt_path'],'rt') as inp:rawroot=json.load(inp)
            if content_sha256(rawroot)!=r['p0_receipt_sha']:raise ValueError('cached_p0_identity_failed')
            root=ProbabilityEnvelope.from_mapping(rawroot);f=r['forecast'];s=r['sample'];selected=root
            if artifact and f and f['tx_c'] is not None:
                selected=materialize_p0m_observed(root,p0_root=root,forecast_revision_id=f['revision'],forecast_source_object_sha256=f['raw_sha'],taf_tx_c=f['tx_c'],decision_at_utc=r['as_of'],fresh_until_utc=stamp(utc(r['as_of'])+timedelta(hours=1)),bridge_sha256=root.bridge_sha256,observed_hard_excluded_buckets=s['excluded'],observed_running_high_c=s['high'],taf_likelihood_artifact=artifact,taf_cloud_cover=f['cloud_cover'],taf_precip_prob=f['precip_prob'])
            r['sample']['view']['probabilities']=p1_root_probabilities(selected);r['p0m_stage']=selected.stage
            r['p0m_probability_sha']=selected.posterior_sha256;ready.append(r)
        print('p0m',day,'train_days',len(tdays),'cases',len(train),flush=True);guard()
    return ready,receipts


def direct_rows(rows,labels):
    counts=collections.Counter((r['station'],r['day']) for r in rows);out=[]
    for r,lab in zip(rows,labels):
        s=r['sample'];sample=dict(s['features'],station_id=r['station'],event_date=r['day'],as_of_utc=r['as_of'],as_of_epoch=utc(r['as_of']).timestamp(),truth_known_epoch=utc(lab['known_at']).timestamp(),station_day_weight=1/counts[(r['station'],r['day'])])
        out.append(dict(view=s['view'],sample=sample,high=s['high'],running=s['running'],excluded=set(s['excluded']),truth=bucket(r,lab['truth_c'])))
    return out


def cloud_x(rows,width,mode,height,kind):
    xs=np.zeros((len(rows),width));known=np.zeros(len(rows),dtype=bool)
    for i,r in enumerate(rows):
        if r.get('cloud_observation_origin_eligible') is not True:continue
        p=r['cloud_pairs'][mode]
        if p is None:continue
        if not r.get('cloud_observation_valid_at'):continue
        valid=utc(r['cloud_observation_valid_at']);age=(utc(r['as_of'])-valid).total_seconds()
        if not 0<=age<=7200:continue
        fc_cut=valid-timedelta(hours=24) if mode=='day1' else valid
        if utc(p['known_at'])>fc_cut:continue
        value=p['coverage_increment'][str(height)] if kind=='coverage' else p['base_lowering_km']
        # Only the paired observation's own physical time is used. This
        # evidence is separate from the official temperature prefix.
        if value is None:continue
        known[i]=True;s=r['sample'];order=sorted(s['view']['ordinals'],key=lambda b:s['view']['ordinals'][b]);center=s['view']['centers'][s['running']]
        rise=[max(0,s['view']['centers'][b]-center) for b in order]
        xs[i,:len(order)]=-float(value)*np.array(rise)
    return xs,known


def weighted_p(p,live,x,beta):
    # p is the unfloored direct posterior. Both arms receive the same
    # service floor exactly once. Preserve the no-evidence arm bit-for-bit.
    p=np.asarray(p);live=np.asarray(live,dtype=bool);x=np.asarray(x)
    if p.shape!=live.shape or p.shape!=x.shape or not np.isfinite(beta):raise ValueError('increment_shape_or_beta_invalid')
    if not np.isfinite(p).all() or not np.isfinite(x).all() or (p<0).any() or (~live & (p!=0)).any() or not live.any(1).all():raise ValueError('increment_support_invalid')
    if not np.allclose(p.sum(1),1,atol=1e-12,rtol=0):raise ValueError('increment_prior_not_normalized')
    q=p.copy();active=live.any(1) & (float(beta)!=0) & (live & (x!=0)).any(1)
    if active.any():
        z=np.where(live[active],np.log(np.maximum(p[active],1e-300))+float(beta)*x[active],-np.inf);z-=z.max(1,keepdims=True)
        transformed=np.where(live[active],np.exp(z),0);transformed/=transformed.sum(1,keepdims=True);q[active]=transformed
    return PF._served_vectors(q,live)


def fit_increment(p,design,x,idx,kind,objective):
    truth=design['truth'][idx];w=design['weight'][idx];live=design['live'][idx];centres=design['centres'][idx];weight=w/w.sum()
    def loss(b):
        q=weighted_p(p[idx],live,x[idx],b[0])
        if objective=='logscore':scores=-np.log(np.maximum(q[np.arange(len(idx)),truth],1e-300))
        else:scores=PF._guard_scores(q,truth,centres)[:,0 if objective=='brier' else 1]
        return float(weight@scores)+0.0025*b[0]**2
    bounds=[(0,4)] if kind=='coverage' else [(-4,4)]
    result=minimize(loss,[0.0],method='L-BFGS-B',bounds=bounds)
    if not result.success:raise ValueError('increment_fit_not_converged:'+result.message)
    return float(result.x[0])


def row_scores(p,design,idx,truth_c):
    y=design['truth'][idx];centers=design['centres'][idx];guards=PF._guard_scores(p,y,centers);mean=(p*centers).sum(1)
    return np.column_stack([-np.log(np.maximum(p[np.arange(len(idx)),y],1e-300)),guards,np.abs(mean-truth_c)])


def ci_by_date(records):
    by_sd=collections.defaultdict(list)
    for r in records:by_sd[(r['station'],r['day'])].append(r['delta'])
    by_day=collections.defaultdict(list)
    for (_st,day),xs in by_sd.items():by_day[day].append(np.mean(xs,axis=0))
    days=sorted(by_day);values=np.array([np.mean(by_day[d],axis=0) for d in days]);rng=np.random.default_rng(20261007)
    if len(days)<8:return dict(state='TOO_FEW_OOS_DAYS',days=len(days),station_days=len(by_sd))
    # Dates are the independent blocks, station-days equally weighted
    # within each date; no report-level bootstrap is used.
    boot=values[rng.integers(0,len(days),(5000,len(days)))].mean(1)
    return dict(state='RESTRICTED_SCOPE_OOS',days=len(days),station_days=len(by_sd),rows=len(records),mean=values.mean(0).tolist(),ci95=np.quantile(boot,[.025,.975],axis=0).T.tolist(),day_deltas={d:values[i].tolist() for i,d in enumerate(days)})


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',action='store_true');ap.add_argument('--parent',choices=('research','served'),default='research');ap.add_argument('--variants',nargs='*');ap.add_argument('--sky',choices=('metar','satellite'),default='metar');ap.add_argument('--sat-stations-only',action='store_true');args=ap.parse_args();guard()
    folder=HERE/('prefix_baseline_rows' if args.baseline else 'prefix_refitted_rows');rows,profile=read_rows(folder,args.parent,SatelliteSky() if args.sky=='satellite' else None,args.sat_stations_only)
    if not rows:raise ValueError('no_authorized_prefix_rows')
    rows,taf_receipts=p0m_rebuild(rows);days=sorted({r['day'] for r in rows});scores=collections.defaultdict(list);fit_receipts=[];clocks=ClosedClocks()
    # Models, objectives, height sensitivity and comparison modes are
    # fixed before any outer test is scored. They are reported separately,
    # never selected on the outer test's outcome.
    variants=[(mode,height,kind,obj) for mode in ('latest','day1') for height,kind in ((6500,'coverage'),(5000,'coverage'),(3000,'coverage'),(6500,'base')) for obj in ('logscore','brier','crps')]
    # Strong effects are a fixed sensitivity experiment, not chosen after
    # seeing the test labels and not candidates for automatic promotion.
    variants += [(mode,6500,'coverage','fixed_'+str(b)) for mode in ('latest','day1') for b in (1,2,4)]
    if args.sky=='satellite':variants += [('nwp','total','coverage',obj) for obj in ('logscore','brier','fixed_1','fixed_2')]
    if args.variants:variants=[v for v in variants if f'{v[0]}_{v[1]}_{v[2]}_{v[3]}' in args.variants]
    for day in days:
        test=[r for r in rows if r['day']==day];cut=min(utc(r['as_of']) for r in test);train=[];trainlabs=[]
        for r in rows:
            if r['day']>=day:continue
            lab=label_at(r,cut)
            if lab is not None and lab['truth_c']>=r['sample']['high']-0.05:train.append(r);trainlabs.append(lab)
        if len({r['day'] for r in train})<20:continue
        offsets,clockreceipt=clocks.fit(train,trainlabs,day,cut)
        combined=train+test;labels=trainlabs+[r['evaluation_label'] for r in test];sr=direct_rows(combined,labels);design=PF.direct_design(sr,{OL.STATION_CLOCK_OFFSET_KEY:offsets})
        tr=np.arange(len(train));te=np.arange(len(train),len(combined));tr=tr[design['usable'][tr]];te=te[design['usable'][te]]
        if len(tr)==0 or len(te)==0:continue
        lam,scaling,theta=PF.direct_choose(design,tr)
        raw=PF._direct_posterior(design,np.arange(len(combined)),PF.direct_x(design,np.arange(len(combined)),scaling),theta)
        base=PF._served_vectors(raw,design['live'])
        truevalues=np.array([lab['truth_c'] for lab in labels]);base_scores=row_scores(base[te],design,te,truevalues[te]);parameters=[]
        for mode,height,kind,obj in variants:
            x,known=cloud_x(combined,base.shape[1],mode,height,kind);eligible=tr[known[tr]]
            if obj.startswith('fixed_'):beta=float(obj.split('_')[1])
            elif not len(eligible):beta=0.0
            else:beta=fit_increment(raw,design,x,eligible,kind,obj)
            q=weighted_p(raw[te],design['live'][te],x[te],beta);candidate=row_scores(q,design,te,truevalues[te]);key=f'{mode}_{height}_{kind}_{obj}'
            for j,i in enumerate(te):
                r=combined[i];value=(r['cloud_pairs'][mode] or {}).get('coverage_increment',{}).get(str(height)) if kind=='coverage' else (r['cloud_pairs'][mode] or {}).get('base_lowering_km')
                record=dict(station=r['station'],day=day,as_of=r['as_of'],local_hour=utc(r['as_of']).astimezone(__import__('zoneinfo').ZoneInfo(r['tz'])).hour,known=bool(known[i]),increment=value,delta=(candidate[j]-base_scores[j]).tolist())
                if (not known[i] or value==0 or beta==0) and not np.array_equal(q[j],base[te[j]]):raise ValueError('zero_evidence_identity_failed')
                scores[key].append(record)
            parameters.append(dict(variant=key,beta=beta,known_train_rows=len(eligible)))
        fit_receipts.append(dict(day=day,cutoff=stamp(cut),train_days=len({r['day'] for r in train}),train_rows=len(tr),test_rows=len(te),lambda_=lam,scaling=scaling,theta=theta.tolist(),clock_offsets=offsets,clock_receipt=clockreceipt,trained_through=max(r['day'] for r in train),training_row_ids_sha=content_sha256([(r['station'],r['day'],r['as_of'],lab['id'],lab['revision']) for r,lab in zip(train,trainlabs)]),increments=parameters));print('p1_oos',day,'train',len(tr),'test',len(te),flush=True);guard()
    result=dict(schema='restricted_prefix_cloud_walk_forward_v3_official_cloud_origin_single_floor',source_profile=profile,metrics=['negative_logscore','bucket_brier','ladder_crps_c','probability_mean_mae_c'],variants={k:dict(all=ci_by_date(v),known_only=ci_by_date([r for r in v if r['known']]),more=ci_by_date([r for r in v if r['known'] and r['increment']>0]),less=ci_by_date([r for r in v if r['known'] and r['increment']<0]),afternoon=ci_by_date([r for r in v if 12<=r['local_hour']<=18])) for k,v in scores.items()},taf_fit_receipts=taf_receipts,p1_fit_receipts=fit_receipts,
                production_modified=False,production_gate=dict(state='NOT_ELIGIBLE_INCOMPLETE_CURRENT_CHAIN',scores_are_full_fleet_oos=False,reasons=['station clocks rebuilt on authorized prefixes only, not the complete fleet','P0 system refits are offline overrides, not regenerated exact-scope nested activation artifacts','fusion and center/width parameters still use original history, not a full current-method causal refit'],source_note='ASOS coverage and exact-source qualification checked; unqualified high-frequency sources legitimately fall back to official AWC'),interpretation='restricted research evidence only; neither full P1 efficacy verdict nor promotion evidence',score_formula='unfloored direct posterior -> independent increment likelihood -> same service floor once',zero_evidence_identity='verified on every scored row')
    result['cloud_origin_audit_sha']=old.sha_file(HERE/'cloud_origin_audit.json')
    if args.parent=='served':result['parent_arm']='served_p0_at_research_root_instant'
    result['sky']=args.sky;result['sat_stations_only']=args.sat_stations_only
    if args.sky=='satellite' or args.sat_stations_only:
        dst=HERE/f"restricted_{args.sky}_sky_32st_fit.json"
    else:
        dst=HERE/('restricted_baseline_fit.json' if args.baseline else 'restricted_served_parent_fit.json' if args.parent=='served' else 'restricted_refitted_fit.json')
    write_fit_report(dst,result)


if __name__=='__main__':main()
