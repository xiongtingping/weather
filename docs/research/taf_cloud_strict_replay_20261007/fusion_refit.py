"""Causal daily current-rule fusion fits on reconstructed factual inputs.

No serving DB is opened by this stage. Counterfactual fits and complete
training references are content-addressed research objects.
"""
import collections
import gzip
import json
from datetime import date,timedelta
from rebuild import HERE,guard,utc,stamp,old,MAX_BYTES
from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.contracts import content_sha256


def visible_fact(p,at):
    if utc(p['decision_at_utc'])>at:raise ValueError('fusion_fact_parent_after_fit')
    known=[x for x in p['truth_revisions'] if utc(x['truth_known_at_utc'])<=at]
    if not known:return None
    revision=max(x['truth_revision'] for x in known);win=[x for x in known if x['truth_revision']==revision]
    if len(win)!=1:raise ValueError('fusion_fit_truth_not_unique')
    t=win[0]
    r=dict(station_id=p['station_id'],event_date=p['event_date'],horizon_bin=p['horizon_bin'],decision_at_utc=p['decision_at_utc'],truth_revision=t['truth_revision'],truth_known_at_utc=t['truth_known_at_utc'],systems_json=json.dumps(sorted(p['system_probabilities'])),truth_probability_json=json.dumps(t['truth_probability']),seat_weights_json=json.dumps(p['seat_weights']),served_weights_json=json.dumps(p['seat_weights']),fusion_rule_version='RESEARCH_CURRENT_SINGLE_FIT_OLD_SEATING',fusion_state='OFFLINE_COUNTERFACTUAL')
    proof=dict(fact_sha=p['content_sha256'],truth_id=t['truth_id'],truth_revision=t['truth_revision'],truth_known=t['truth_known_at_utc'],truth_lane=t['truth_lane'],decision_at=p['decision_at_utc'])
    return FB.WeightFact(r),proof


def main():
    guard();summary=json.loads((HERE/'fusion_facts.json').read_text());path=HERE/'fusion_facts.jsonl.gz'
    if old.sha_file(path)!=summary['output_sha']:raise ValueError('frozen_fusion_facts_changed')
    facts=[];used=0
    for line in gzip.open(path,'rt'):
        used+=len(line.encode())
        if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('fusion_refit_input_limit')
        p=json.loads(line)
        if content_sha256({k:v for k,v in p.items() if k!='content_sha256'})!=p['content_sha256']:raise ValueError('fusion_fact_identity_invalid')
        facts.append(p)
    systems=sorted({s for p in facts for s in p['system_probabilities']});dst=HERE/'fusion_refit_manifests';dst.mkdir(exist_ok=True);records={};outbytes=0
    start=date(2026,8,31);end=date(2026,10,7)
    for delta in range((end-start).days+1):
        day=(start+timedelta(days=delta)).isoformat();at=utc(day+'T00:00:00Z');visible=[];proofs=collections.defaultdict(list);by=collections.defaultdict(list)
        for p in facts:
            if utc(p['decision_at_utc'])>at:continue
            f=visible_fact(p,at)
            if f:
                row,proof=f;visible.append(row);by[row.station].append(row);proofs[row.station].append(proof)
        tau,evidence=FB.choose_tau(visible,systems,at);refs={}
        for station,rows in sorted(by.items()):
            known=FB.knowable_facts(rows,at,strict=False);theta=FB.fit_theta(known,systems);days=FB.days_by_system(known)
            payload=dict(schema='offline_current_fusion_fit_v1',station=station,cutoff=stamp(at),rule=FB.FUSION_BLEND_RULE_VERSION,tau=tau,theta=theta,days=days,train_manifest=proofs[station],train_manifest_sha=content_sha256(proofs[station]),source_facts_sha=summary['output_sha'],role='counterfactual_fit_not_historical_activation',parent_chain_certified=False)
            sha=content_sha256(payload);body=json.dumps(payload).encode();outbytes+=len(body)
            if len(body)>2*1024**2 or outbytes>MAX_BYTES:raise RuntimeError('fusion_refit_output_limit')
            path=dst/(sha+'.json.gz')
            if not path.exists():
                with gzip.open(path,'wb') as out:out.write(body)
            refs[station]=dict(path=str(path.relative_to(HERE)),sha=sha,training_facts=len(known),training_station_days=len({f.event_date for f in known}))
        records[day]=dict(cutoff=stamp(at),known_facts=len(visible),tau=tau,tau_evidence=evidence,parameters=refs)
        print(day,'facts',len(visible),'stations',len(refs),'tau',tau,flush=True);guard()
    result=dict(schema='fleet_causal_daily_fusion_refit_v1',days=records,source_facts_sha=summary['output_sha'],output_uncompressed_bytes=outbytes,production_modified=False,parent_chain_certified=False,limitations=['current probabilities and actual ladders, original seating qualifications','no predecessor fusion served weights: veto requires rerooted chronological serving ledger','fixed UTC-midnight research fit schedule, no claim of historical parameter publication'])
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if len(body.encode())>2*1024**2:raise RuntimeError('fusion_refit_summary_limit')
    (HERE/'fusion_refit.json').write_text(body)


if __name__=='__main__':main()
