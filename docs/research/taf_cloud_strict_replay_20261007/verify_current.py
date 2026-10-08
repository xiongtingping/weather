"""Full reconstructed-row/fit-manifest clocks and served formula audit."""
import collections
import gzip
import json
from datetime import timedelta
import numpy as np
from rebuild import HERE,ROOT,old,guard,utc,stamp,MAX_BYTES
from upstream import day_clock
from bayes_basket_target.contracts import content_sha256,ProbabilityEnvelope
from bayes_basket_target.materialization import materialize_p0m_observed
from bayes_basket_target.observed_floor_censoring import p1_root_probabilities
from bayes_basket_target import p1_direct_update as DU,p1_observation_likelihood as OL
from scripts import fit_p1_observation_likelihood as PF
from strict_fit import direct_rows,label_at,weighted_p,read_rows,cloud_x
from fit_storage import load_fit_report


def main():
    guard();counts=collections.Counter();fit=load_fit_report(HERE/'restricted_refitted_fit.json');by_day={r['day']:r for r in fit['p1_fit_receipts']};taf_by_day={r['day']:r for r in fit['taf_fit_receipts']};sampled={};prefix=json.loads((HERE/'prefix_refitted_rows.json').read_text())
    for path in sorted((HERE/'prefix_refitted_rows').glob('*.jsonl.gz')):
        if old.sha_file(path)!=prefix['stations'][path.stem.split('.')[0]]['sha256']:raise ValueError('prefix_file_changed')
        used=0
        for ln in gzip.open(path,'rt'):
            used+=len(ln.encode())
            if used>MAX_BYTES or len(ln.encode())>2*1024**2:raise RuntimeError('verification_prefix_limit')
            r=json.loads(ln);counts['prefix_rows']+=1
            if r['state']=='REFUSED':continue
            counts['authorized_rows']+=1;at=utc(r['as_of'])
            if utc(r['p0']['decision_at_utc'])>at:raise ValueError('p0_not_visible')
            for p in r['sample']['prefix_manifest']:
                if max(utc(p[k]) for k in ('valid_at','first_seen','committed','origin_committed'))>at:raise ValueError('prefix_time_travel')
            for p in r['sample']['regional_receipt']:
                if max(utc(p['actual_fetch_at']),utc(p['run_init_utc']))>at:raise ValueError('regional_time_travel')
                counts['regional_present']+=1
            if r['forecast'] and utc(r['forecast']['received_at'])>at:raise ValueError('taf_time_travel')
            for mode,p in r['cloud_pairs'].items():
                if p is None:continue
                valid=utc(r['cloud_observation_valid_at']);cut=valid-timedelta(hours=24) if mode=='day1' else valid
                if valid>at or utc(p['known_at'])>cut:raise ValueError('defensive_cloud_time_checks_change_dataset')
                counts['cloud_time_pairs']+=1
            if r['day'] in by_day and label_at(r)['truth_c']>=r['sample']['high']-.05:
                prev=sampled.get(r['day']);has_tx=bool(r['forecast'] and r['forecast']['tx_c'] is not None)
                if prev is None or (has_tx and not (prev['forecast'] and prev['forecast']['tx_c'] is not None)):sampled[r['day']]=r
        guard()
    for path in sorted((HERE/'p0_refitted_roots').glob('*.jsonl.gz')):
        seen={};used=0;proofbytes=0
        for ln in gzip.open(path,'rt'):
            used+=len(ln.encode())
            if used>MAX_BYTES:raise RuntimeError('verification_root_limit')
            r=json.loads(ln);counts['root_rows']+=1
            if r['state']=='REFUSED':continue
            at=utc(r['parent_committed_at']);tz=r['output']['packet']['settlement_timezone']
            for sid,ref in r['current_parameter_refits'].items():
                name=ref['path']
                if name not in seen:
                    with gzip.open(HERE/name,'rb') as f:body=f.read(2*1024**2+1)
                    proofbytes+=len(body)
                    if len(body)>2*1024**2 or proofbytes>MAX_BYTES:raise RuntimeError('verification_manifest_limit')
                    proof=json.loads(body)
                    if content_sha256(proof)!=ref['content_sha256'] or content_sha256(proof['train_manifest'])!=ref['train_manifest_sha256']:raise ValueError('fit_manifest_identity_failed')
                    seen[name]=proof;counts['unique_fit_manifests']+=1
                proof=seen[name]
                if proof['parameters']!=ref['parameters'] or proof['scope']!=ref['scope'] or utc(ref['as_of'])!=at:raise ValueError('refit_parameters_not_bound')
                for p in proof['train_manifest']:
                    if p['day']>=r['day'] or max(utc(p['arrival']),utc(p['truth_committed_at']),day_clock(p['day'],tz,True))>at:raise ValueError('fit_parameter_time_travel')
                counts['refit_references']+=1
        guard()
    for day,r in sampled.items():
        rec=by_day[day];lab=label_at(r);f=r['forecast'];artifact=taf_by_day[day]['artifact'];s=r['sample']
        root=ProbabilityEnvelope.from_mapping(r['p0'])
        if artifact and f and f['tx_c'] is not None:
            selected=materialize_p0m_observed(root,p0_root=root,forecast_revision_id=f['revision'],forecast_source_object_sha256=f['raw_sha'],taf_tx_c=f['tx_c'],decision_at_utc=r['as_of'],fresh_until_utc=stamp(utc(r['as_of'])+timedelta(hours=1)),bridge_sha256=root.bridge_sha256,observed_hard_excluded_buckets=s['excluded'],observed_running_high_c=s['high'],taf_likelihood_artifact=artifact,taf_cloud_cover=f['cloud_cover'],taf_precip_prob=f['precip_prob'])
            s['view']['probabilities']=p1_root_probabilities(selected);counts['p0m_materializer_sample_days']+=1
        sr=direct_rows([r],[lab]);design=PF.direct_design(sr,{OL.STATION_CLOCK_OFFSET_KEY:rec['clock_offsets']});theta=np.array(rec['theta']);scale=rec['scaling'];x=PF.direct_x(design,np.array([0]),scale)
        sample=sr[0];view=sample['view'];bases=DU.bucket_bases(bucket_ordinals=view['ordinals'],bucket_centers_c=view['centers'],bucket_upper_bounds_c=view['uppers'],running_high_c=sample['high'],running_high_bucket=sample['running'],hard_excluded_buckets=sample['excluded'])
        width=x.shape[1];block={**scale,'theta_rise':theta[:width].tolist(),'theta_slope':theta[width:2*width].tolist(),'theta_slope_sq':float(theta[-2]),'theta_open_top':float(theta[-1])}
        # The numpy design and canonical scalar serving formula receive
        # identical vectors. Compare logits directly, including open edges.
        fs=DU.log_update(block,x[0],bases);fn=PF._direct_f(design,np.array([0]),x,theta)[0]
        order=sorted(view['ordinals'],key=view['ordinals'].get)
        for i,b in enumerate(order):
            if bases[b][0]>=0 and abs(fs[b]-fn[i])>1e-10:raise ValueError('production_direct_formula_mismatch')
        raw=PF._direct_posterior(design,np.array([0]),x,theta);base=PF._served_vectors(raw,design['live'])
        for beta in (0,1,4):
            if not np.array_equal(weighted_p(raw,design['live'],np.zeros_like(raw),beta),base):raise ValueError('single_floor_identity_failed')
        counts['production_formula_sample_days']+=1
    for r in fit['p1_fit_receipts']:
        if r['trained_through']>=r['day'] or r['clock_receipt']['cutoff']!=r['cutoff']:raise ValueError('outer_fit_clock_failed')
    if fit['cloud_origin_audit_sha']!=old.sha_file(HERE/'cloud_origin_audit.json'):raise ValueError('score_cloud_origin_audit_not_bound')
    projected,profile=read_rows(HERE/'prefix_refitted_rows')
    if profile['cloud_increment_refused_origin_not_visible']!=fit['source_profile']['cloud_increment_refused_origin_not_visible']:raise ValueError('score_cloud_origin_mask_changed')
    width=max(len(r['sample']['view']['ordinals']) for r in projected)
    for mode in ('latest','day1'):
        values,known=cloud_x(projected,width,mode,6500,'coverage')
        for i,r in enumerate(projected):
            if not r['cloud_observation_origin_eligible'] and (known[i] or np.any(values[i])):raise ValueError('unqualified_cloud_evidence_not_neutral')
    counts['cloud_origin_rejections_neutral']=profile['cloud_increment_refused_origin_not_visible']
    result=dict(schema='reconstructed_current_components_verification_v1',passed=True,counts=dict(counts),prefix_summary_sha=old.sha_file(HERE/'prefix_refitted_rows.json'),score_sha=old.sha_file(HERE/'restricted_refitted_fit.json'),production_modified=False,current_chain_certified=False,scope='row clocks, fit bindings, direct scalar formula and one-floor identities; no cross-activation certification')
    (HERE/'current_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result),flush=True)


if __name__=='__main__':main()
