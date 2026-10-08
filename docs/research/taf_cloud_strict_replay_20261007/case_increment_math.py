"""Frozen Chengdu case: incremental odds effect, not a full P1 replay."""
import json
import math
from rebuild import HERE,ROOT,old,utc,fetch
from fit_storage import load_fit_report


def main():
    source=json.loads((HERE/'results.json').read_text());fit=load_fit_report(HERE/'restricted_refitted_fit.json');rec=fit['p1_fit_receipts'][-1]
    beta={x['variant']:x['beta'] for x in rec['increments']};out=[];archive=old.connect(ROOT/'data/archive.db')
    for r in source['case']:
        if utc(rec['cutoff'])>utc(r['decided_at']) or rec['trained_through']>=r['day']:raise ValueError('case_coefficient_time_travel')
        origins=[dict(x) for x in fetch(archive,'select source_lane,station_id,origin_source_id,origin_transport,committed_at from reported_trajectory_observation_origins where observation_id=?',(r['observation_id'],))]
        visible=[x for x in origins if x['source_lane']=='awc' and x['station_id']==r['station'] and x['origin_source_id'] in ('global_noaa_awc_relay','us_noaa_awc','global_noaa_tgftp_relay') and str(x['origin_transport']).startswith(('official_awc_','official_noaa_tgftp_')) and utc(x['committed_at'])<=utc(r['decided_at'])]
        terms={};p=r['latest'] if len(visible)==1 else None
        for kind,height in [('coverage',6500),('coverage',5000),('base',6500)]:
            value=(p['coverage_increment'][str(height)] if kind=='coverage' else p['base_lowering_km']) if p else None
            key=f'latest_{height}_{kind}_logscore';b=beta[key]
            terms[key]=dict(increment=value,beta=b,one_c_rise_odds_multiplier=1.0 if value is None else math.exp(-b*value),evidence_known=value is not None)
        out.append(dict(local_hour=r['local_hour'],observation_id=r['observation_id'],as_of=r['decided_at'],running_high_bucket=r['running_high'],cloud_origin_eligible=len(visible)==1,cloud_origin=visible[0] if len(visible)==1 else None,terms=terms))
    result=dict(schema='chengdu_frozen_increment_odds_diagnostic_v1',coefficient_cutoff=rec['cutoff'],trained_through=rec['trained_through'],case=out,production_modified=False,evidence_role='restricted_model_sensitivity_not_full_current_p1_replay',limitations=['odds multipliers are independent likelihood terms only; no absolute P(25) has been reconstructed','coefficients come from the restricted parent reconstruction and cannot authorize production','cloud-base direction is fitted and not assumed to imply less radiation'],source_sha=old.sha_file(HERE/'results.json'),score_sha=old.sha_file(HERE/'restricted_refitted_fit.json'))
    (HERE/'case_increment_math.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(dict(rows=len(out),trained_through=rec['trained_through'])),flush=True)


if __name__=='__main__':main()
