"""Verify full raw-cloud receipts, intervals, fingerprints and case boundaries."""
import collections
import gzip
import json
import unittest
from datetime import timedelta
from pathlib import Path
from rebuild import HERE,ROOT,HEIGHTS,MAX_BYTES,guard,old
from raw_cloud import decode,interval,utc,scenarios,base_increment,cloud_increment
from test_raw_cloud import RawCloudBoundaries
from upstream import day_clock


def main():
    guard();result=json.loads((HERE/'results.json').read_text());counts=collections.Counter();nbytes=0;profile={m:collections.Counter() for m in ('latest','day1')};tafs={}
    if result['fingerprints']['raw_decoder']!=old.sha_file(HERE/'raw_cloud.py'):raise ValueError('decoder_changed_after_run')
    if result['fingerprints']['raw_pairs']!=old.sha_file(HERE/'raw_pairs.jsonl.gz'):raise ValueError('pairs_fingerprint_failed')
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(RawCloudBoundaries)
    run=unittest.TextTestRunner(verbosity=1).run(suite)
    if not run.wasSuccessful():raise ValueError('boundaries_failed')
    assert (day_clock('2026-09-27','Pacific/Auckland',True)-day_clock('2026-09-27','Pacific/Auckland')).total_seconds()==23*3600
    assert (day_clock('2026-11-01','America/New_York',True)-day_clock('2026-11-01','America/New_York')).total_seconds()==25*3600
    with gzip.open(HERE/'raw_pairs.jsonl.gz','rt') as f:
        for line in f:
            nbytes+=len(line.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('input_limit')
            r=json.loads(line);at=utc(r['decided_at']);valid=utc(r['observation_valid_at']);counts['rows']+=1
            if max(valid,utc(r['observation_first_seen']),utc(r['observation_committed']))>at:raise ValueError('observation_lookahead')
            for mode in profile:
                p=r[mode];s=profile[mode];s['rows']+=1
                if p is None:s['no_taf']+=1;continue
                counts['pairs']+=1;s['taf_paired']+=1
                if max(utc(p['issue']),utc(p['known_at']))>(min(valid,at) if mode=='latest' else valid-timedelta(hours=24)):raise ValueError('forecast_lookahead')
                if not utc(p['valid_from'])<=valid<utc(p['valid_to']):raise ValueError('forecast_invalid_time')
                if p['sha'] not in tafs:tafs[p['sha']]=decode(p['raw'],station=r['station'],issue_time=p['issue'])
                d=tafs[p['sha']]
                if scenarios(d,valid)!=p['scenarios']:raise ValueError('scenarios_drift')
                for h in HEIGHTS:
                    k=str(h);new=interval(d,valid,h)
                    if (list(new) if new is not None else None)!=p['forecast_intervals'][k]:raise ValueError('forecast_interval_drift')
                    oi=p['obs_intervals'][k];fc=p['forecast_intervals'][k];inc=p['coverage_increment'][k]
                    if cloud_increment(oi,fc)!=inc:raise ValueError('amount_difference_failed')
                    if inc is not None and not -1<=inc<=1:raise ValueError('amount_range_failed')
                if base_increment(r['obs_layers'],scenarios(d,valid))!=p['base_lowering_km']:raise ValueError('base_difference_failed')
                if (at-valid).total_seconds()>7200:s['stale_observation']+=1;continue
                for h in HEIGHTS:
                    inc=p['coverage_increment'][str(h)]
                    s[str(h)+'_'+('unknown' if inc is None else 'more' if inc>0 else 'less' if inc<0 else 'overlap')]+=1
                if p['base_lowering_km'] is not None:s['unique_base_comparison']+=1
                if not r['chain_audit']['blockers'] and p['coverage_increment']['6500'] is not None:s['current_chain_cloud_rows']+=1
            if counts['rows']%500==0:guard()
    for mode in profile:
        if dict(profile[mode])!=result['summaries'][mode]:raise ValueError('summary_not_reproducible')
    qa=json.loads((HERE/'decode_qa.json').read_text());qa_reasons={}
    # Inspectable raw syntax plus the independent fourth-layer boundary test
    # explains the official decoder's three-cloud limit. COR remains excluded.
    for x in qa['disagreements']:
        if x['raw'].startswith('TAF COR '):qa_reasons[x['sha']]='COR_excluded'
        elif x['station'] in ('RCSS','ZUCK') and any(len(g['clouds'])==3 for g in x['awc_groups']):qa_reasons[x['sha']]='AWC_missing_fourth_cloud_layer_raw_retained'
        else:raise ValueError('unresolved_decode_disagreement')
    case=result['case'];noon=[x for x in case if x['local_hour'] in (12,13,14)]
    assert len(noon)==3 and all(x['latest']['coverage_increment']['6500']==0 for x in noon)
    assert all(abs(x['latest']['base_lowering_km']-.6096)<1e-12 for x in noon)
    assert next(x for x in case if x['local_hour']==15)['latest']['coverage_increment']['6500']==.125
    assert next(x for x in case if x['local_hour']==12)['day1'] is None
    assert next(x for x in case if x['local_hour']==13)['day1'] is not None
    assert next(x for x in case if x['local_hour']==14)['day1'] is None
    proof=dict(schema='raw_cloud_rebuild_verification_v1',passed=True,counts=dict(counts),independent_boundary_tests=run.testsRun,dst_boundaries=2,qa_reasons=qa_reasons,
               source_sha256=old.sha_file(HERE/'results.json'),raw_pairs_sha256=old.sha_file(HERE/'raw_pairs.jsonl.gz'),scores_computed=False,production_modified=False)
    (HERE/'verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2));print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
