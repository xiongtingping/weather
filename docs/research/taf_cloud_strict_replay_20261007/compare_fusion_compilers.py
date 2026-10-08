"""All-station integration comparison after adding the causal veto ledger."""
import json
from rebuild import HERE,MAX_BYTES,guard,old
from fusion_ledger import bounded_json


def main():
    guard();before=bounded_json(HERE/'compiler_batch.fusion.json');after=bounded_json(HERE/'compiler_batch.ledger.json')
    if set(before['stations'])!=set(after['stations']):raise ValueError('fusion_compiler_comparison_population')
    records={};used=0
    for station,record in sorted(after['stations'].items()):
        prior=before['stations'][station];a_path=HERE/record['output'];b_path=HERE/prior['output']
        used+=a_path.stat().st_size+b_path.stat().st_size
        if used>MAX_BYTES:raise RuntimeError('fusion_compiler_comparison_input_limit')
        if old.sha_file(a_path)!=record['output_sha'] or old.sha_file(b_path)!=prior['output_sha']:
            raise ValueError('fusion_compiler_comparison_output_changed')
        a=bounded_json(a_path);b=bounded_json(b_path)
        if (a['state'],a.get('reason'),a['report_sha'],a['as_of'])!=(b['state'],b.get('reason'),b['report_sha'],b['as_of']):
            raise ValueError('fusion_compiler_comparison_scope_or_refusal_changed')
        if a['state']=='OFFLINE_NESTED_CERTIFICATE_P0_COMPILED':
            p=a['output']['packet']['native_probability_envelope'];q=b['output']['packet']['native_probability_envelope']
            if p['probabilities']!=q['probabilities']:raise ValueError('fusion_compiler_comparison_probability_changed')
            for field in ('system_weights_json','native_system_probabilities_json','system_family_ids_json','family_weight_caps_json'):
                if p['lineage'][field]!=q['lineage'][field]:raise ValueError('fusion_compiler_comparison_lineage_changed:'+field)
            blend=json.loads(p['lineage']['fusion_blend_json'])
            if 'veto_id' not in blend:raise ValueError('fusion_compiler_comparison_missing_veto')
            records[station]=dict(state='PROBABILITY_AND_WEIGHTS_EXACTLY_EQUAL',report_sha=a['report_sha'])
        else:records[station]=dict(state='REFUSAL_EXACTLY_EQUAL',reason=a['reason'])
        guard()
    result=dict(schema='fleet_causal_fusion_ledger_integration_comparison_v1',passed=True,
        stations=records,exact_probability_stations=sum(r['state']=='PROBABILITY_AND_WEIGHTS_EXACTLY_EQUAL' for r in records.values()),
        exact_refusal_stations=sum(r['state']=='REFUSAL_EXACTLY_EQUAL' for r in records.values()),
        compiler_before_sha=old.sha_file(HERE/'compiler_batch.fusion.json'),compiler_after_sha=old.sha_file(HERE/'compiler_batch.ledger.json'),
        production_modified=False,whole_current_chain_certified=False)
    (HERE/'fusion_compiler_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='stations'}),flush=True)


if __name__=='__main__':main()
