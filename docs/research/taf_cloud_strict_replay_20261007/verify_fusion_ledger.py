"""Audit every serving row and every causal veto reference independently."""
import collections
import gzip
import json
from fusion_ledger import read_inputs, controls_for, bounded_json, parameter_tables, serve_one, LIMIT
from fusion_refit import visible_fact
from rebuild import HERE, ROOT, MAX_BYTES, old, guard, utc
from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.contracts import content_sha256


def main():
    guard(); facts_summary, fit_index, facts = read_inputs(); controls = controls_for(facts)
    index = bounded_json(HERE/'fusion_served_ledger.json'); path = HERE/'fusion_served_ledger.jsonl.gz'
    if old.sha_file(path)!=index['output_sha'] or facts_summary['output_sha']!=index['source_facts_sha'] or old.sha_file(HERE/'fusion_refit.json')!=index['source_parameters_sha'] or old.sha_file(ROOT/'bayes_basket_target/fusion_blend.py')!=index['source_formula_sha']:
        raise ValueError('fusion_ledger_audit_input_changed')
    by_cp={}; used=0
    with gzip.open(path,'rt') as inp:
        for line in inp:
            size=len(line.encode());used+=size
            if size>LIMIT or used>MAX_BYTES:raise RuntimeError('fusion_ledger_audit_limit')
            r=json.loads(line)
            if r['checkpoint'] in by_cp or content_sha256({k:v for k,v in r.items() if k!='content_sha256'})!=r['content_sha256']:
                raise ValueError('fusion_ledger_audit_serving_identity')
            by_cp[r['checkpoint']]=r
    if set(by_cp)!={p['checkpoint'] for p in facts}:raise ValueError('fusion_ledger_audit_population')
    counts=collections.Counter(); tables={}; fact_by_sha={p['content_sha256']:p for p in facts}
    for key,daily in sorted(index['days'].items()):
        cutoff=utc(key);ref=daily['manifest'];p=bounded_json(HERE/ref['path'],True)
        if content_sha256(p)!=ref['sha'] or p['cutoff']!=key or content_sha256(p['training_refs'])!=p['references_sha']:
            raise ValueError('fusion_ledger_audit_veto_identity')
        expected=[];expected_refs=[]
        # At a sweep cutoff, its own coincident decision has not served yet.
        for f in facts:
            if utc(f['decision_at_utc'])>=cutoff:continue
            pair=visible_fact(f,cutoff)
            if pair is None:continue
            row,proof=pair;serving=by_cp[f['checkpoint']]
            row.rule_version=FB.FUSION_BLEND_RULE_VERSION;row.state=serving['receipt']['state'];row.served=serving['weights']
            expected.append(row);expected_refs.append({**proof,'serving_sha':serving['content_sha256'],'state':row.state,'served_weights':row.served,'seat_weights':row.seat})
        if expected_refs!=p['training_refs']:raise ValueError('fusion_ledger_audit_veto_reference_set')
        if FB.veto_evidence(expected,cutoff)!=p['evidence'] or p['evidence']!=daily['evidence']:
            raise ValueError('fusion_ledger_audit_veto_arithmetic')
        fit=fit_index['days'][key[:10]]
        tables[key]=dict(params=parameter_tables(fit,facts_summary['output_sha']),vetoes=[dict(veto_id='offline-veto-'+ref['sha'][:24],computed_at=cutoff,vetoed=p['evidence']['vetoed'])])
        counts['veto_cutoffs']+=1;counts['veto_training_references']+=len(expected_refs)
        counts['vetoed_cutoffs']+=int(p['evidence']['vetoed']);guard()
    for f in facts:
        r=by_cp[f['checkpoint']];eligible=[k for k in tables if utc(k)<=utc(f['decision_at_utc'])]
        key=max(eligible,key=utc) if eligible else None
        expected=serve_one(f,controls[f['checkpoint']],tables[key] if key else dict(params={},vetoes=[]))
        expected['parameter_cutoff']=key;expected['content_sha256']=content_sha256({k:v for k,v in expected.items() if k!='content_sha256'})
        if expected!=r:raise ValueError('fusion_ledger_audit_serving_mismatch')
        counts['serving_rows']+=1;counts['state:'+r['receipt']['state']]+=1
    result=dict(schema='chronological_fusion_ledger_audit_v1',passed=True,counts=dict(counts),
        source_ledger_sha=old.sha_file(HERE/'fusion_served_ledger.json'),
        source_facts_sha=facts_summary['output_sha'],production_modified=False,
        whole_current_chain_certified=False,
        scope='all source roots, actual family caps, parameter cutoff, serving weights, latest knowable truth revisions and all veto references')
    (HERE/'fusion_ledger_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
