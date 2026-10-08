"""Audit ASOS evidence clocks and exact artifact lineage without promotion."""
import collections
import gzip
import json
from rebuild import HERE,ROOT,old,fetch,utc,guard,MAX_BYTES
from replay_rows import US_ASOS
from bayes_basket_target.contracts import content_sha256


def main():
    a=old.connect(ROOT/'data/archive.db');result={};total=collections.Counter()
    for st in sorted(US_ASOS):
        guard();artifacts=[];profile=collections.Counter();used=0
        for rr in fetch(a,'select * from reported_trajectory_asos_p1_oos_artifacts where station_id=?',(st,)):
            r=dict(rr);body=r['artifact_json'];used+=len(body.encode())
            if len(body.encode())>2*1024**2 or used>MAX_BYTES:raise RuntimeError('artifact_input_limit')
            x=json.loads(body)
            if content_sha256({k:v for k,v in x.items() if k!='content_sha256'})!=r['content_sha256']:raise ValueError('artifact_sha_invalid')
            facts=[];missing=[];matched_sources=collections.Counter();times=[]
            for sha in x.get('train_match_sha256s',[])+x.get('holdout_match_sha256s',[]):
                f=fetch(a,'select * from reported_trajectory_asos_metar_match_facts where content_sha256=?',(sha,)).fetchone()
                if f is None:missing.append(sha);continue
                f=dict(f);y=json.loads(f['fact_json']);used+=len(f['fact_json'].encode())
                if used>MAX_BYTES:raise RuntimeError('match_fact_input_limit')
                if content_sha256({k:v for k,v in y.items() if k!='content_sha256'})!=sha:raise ValueError('match_sha_invalid')
                product=y.get('exact_asos_temperature_product') or {};matched_sources[product.get('measurement_semantics','MISSING')]+=1;times.append(y['metar_observed_at_utc'])
                facts.append(dict(sha=sha,match_state=y['match_state'],metar_id=y['metar_observation_id'],valid_at=y['metar_observed_at_utc'],committed=f['canonical_committed_at_utc'],product=product,prior_madis_hf_cycle=y.get('prior_madis_hf_cycle')))
            # These artifacts certify a temperature adapter's MAE. They do
            # not contain the complete current P1/OOS scope or probability
            # scores, and their declared MADIS lane must not be silently
            # repurposed as an IEM one-minute-source artifact.
            gaps=[]
            if missing:gaps.append('referenced_match_fact_missing')
            if x.get('state')!='OOS_READY':gaps.append('artifact_not_oos_ready')
            if x.get('source_lane')=='MADIS_HFMETAR_ASOS' and any(f['prior_madis_hf_cycle'] is None for f in facts):gaps.append('declared_madis_lane_not_bound_to_madis_match_input')
            if not any(k in x for k in ('p1_oos_artifact_sha256','p1_probability_oos','p1_replay_oos')):gaps.append('temperature_adapter_not_complete_current_p1_probability_oos')
            if any(utc(f['committed'])>utc(r['canonical_committed_at_utc']) for f in facts):gaps.append('match_fact_after_artifact')
            artifacts.append(dict(sha=r['content_sha256'],state=x['state'],committed=r['canonical_committed_at_utc'],declared_lane=x.get('source_lane'),measurement=x.get('measurement_semantics'),train=x['train_count'],holdout=x['holdout_count'],matched_measurements=dict(matched_sources),valid_range=[min(times),max(times)] if times else None,gaps=gaps,referenced_facts=facts))
            profile.update(gaps)
        periods={}
        for lane,t,valid,commit in [('IEM','reported_trajectory_asos_one_minute_observations','valid_at','canonical_committed_at'),('MADIS','reported_trajectory_madis_hfmetar_observations','observed_at_utc','canonical_committed_at_utc')]:
            rr=fetch(a,f'select count(*) as n,min({valid}) as first_valid,max({valid}) as last_valid,min({commit}) as first_commit from {t} where station_id=?',(st,)).fetchone();periods[lane]=dict(rr)
            # Survey domains once before interpreting the source vocabulary.
            fields='source_lane,provider_id,source_class,measurement_semantics' if lane=='IEM' else 'measurement_semantics,temperature_qc_summary,temperature_sensor_status,qc_eligible'
            periods[lane]['domains']=[dict(x) for x in fetch(a,'select distinct '+fields+' from '+t+' where station_id=?',(st,))]
        result[st]=dict(artifacts=artifacts,periods=periods,gap_counts=dict(profile));total.update(profile);print(st,json.dumps(dict(profile)),flush=True)
    report=dict(schema='asos_current_p1_historical_lineage_audit_v1',stations=result,counts=dict(total),production_modified=False,
                conclusion='historical source objects and temperature adapter artifacts exist; full current P1 authority is not proved by those adapter labels alone',source_policy='IEM 220 / MADIS 210 / AWC 100, one canonical winner, no mixed maximum')
    (HERE/'asos_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
