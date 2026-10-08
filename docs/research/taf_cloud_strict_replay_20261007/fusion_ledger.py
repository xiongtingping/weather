"""Chronological, counterfactual fusion serving and closed-day veto ledger.

Original seating remains explicitly unqualified for the complete current
chain. This stage closes only the missing chronological fusion component.
Daily theta/tau manifests are reused after their full causal validation.
No serving database is read or written by overlay_system_weights.
"""
import argparse
import collections
import gzip
import json
from rebuild import HERE, ROOT, MAX_BYTES, old, guard, utc, stamp
from fusion_refit import visible_fact
from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.contracts import content_sha256

LIMIT = 2 * 1024**2


def bounded_json(path, compressed=False):
    guard()
    opener = gzip.open if compressed else open
    with opener(path, 'rb') as inp:
        body = inp.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise RuntimeError('fusion_ledger_object_limit')
    return json.loads(body)


def read_inputs():
    summary = bounded_json(HERE/'fusion_facts.json')
    index = bounded_json(HERE/'fusion_refit.json')
    source = HERE/'fusion_facts.jsonl.gz'
    if old.sha_file(source) != summary['output_sha'] or index['source_facts_sha'] != summary['output_sha']:
        raise ValueError('fusion_ledger_input_changed')
    facts = []; used = 0
    with gzip.open(source, 'rt') as inp:
        for line in inp:
            size = len(line.encode()); used += size
            if size > LIMIT or used > MAX_BYTES:
                raise RuntimeError('fusion_ledger_fact_limit')
            p = json.loads(line)
            if content_sha256({k:v for k,v in p.items() if k!='content_sha256'}) != p['content_sha256']:
                raise ValueError('fusion_ledger_fact_identity_failed')
            facts.append(p)
    return summary, index, sorted(facts, key=lambda p:(utc(p['decision_at_utc']), p['station_id'], p['checkpoint']))


def controls_for(facts):
    required = {p['checkpoint']:p for p in facts}; controls = {}
    for station in sorted({p['station_id'] for p in facts}):
        guard(); used = 0
        with gzip.open(HERE/'p0_refitted_roots'/(station+'.jsonl.gz'), 'rt') as inp:
            for line in inp:
                size = len(line.encode()); used += size
                if size > LIMIT or used > MAX_BYTES:
                    raise RuntimeError('fusion_ledger_control_input_limit')
                r = json.loads(line); cp = r['original_checkpoint']
                if cp not in required:
                    continue
                p = required[cp]; env = r['output']['packet']['native_probability_envelope']; lin = env['lineage']
                if cp in controls or content_sha256(env)!=p['root_probability_sha'] or r['parent_committed_at']!=p['decision_at_utc']:
                    raise ValueError('fusion_ledger_control_root_mismatch')
                receipt = json.loads(lin['fusion_blend_json'])
                if receipt['seat_weights'] != p['seat_weights']:
                    raise ValueError('fusion_ledger_seating_mismatch')
                controls[cp] = dict(family_by_system=json.loads(lin['system_family_ids_json']),
                    family_caps=json.loads(lin['family_weight_caps_json']),
                    lead_by_system=json.loads(lin['hour_lead_bucket_by_system_json']),
                    source_root_sha=p['root_probability_sha'], source_packet_sha=p['parent_packet_sha'])
    if set(controls)!=set(required):
        raise ValueError('fusion_ledger_control_missing')
    return controls


def parameter_tables(daily, facts_sha):
    params = {}; used = 0
    for station, ref in sorted(daily['parameters'].items()):
        p = bounded_json(HERE/ref['path'], True); used += len(json.dumps(p).encode())
        if used > MAX_BYTES:
            raise RuntimeError('fusion_ledger_daily_parameter_limit')
        if content_sha256(p)!=ref['sha'] or p['station']!=station or p['cutoff']!=daily['cutoff'] or p['source_facts_sha']!=facts_sha or content_sha256(p['train_manifest'])!=p['train_manifest_sha']:
            raise ValueError('fusion_ledger_parameter_identity_failed')
        for row in p['train_manifest']:
            if max(utc(row['truth_known']), utc(row['decision_at'])) > utc(p['cutoff']):
                raise ValueError('fusion_ledger_parameter_time_travel')
        params[station] = [dict(params_id='offline-fusion-'+ref['sha'][:24],
            computed_at=utc(p['cutoff']), computed_at_utc=p['cutoff'], tau=p['tau'],
            theta=p['theta'], days=p['days'])]
    return params


def served_fact(p, serving, at):
    if utc(p['decision_at_utc']) > at:
        raise ValueError('fusion_ledger_future_serving')
    pair = visible_fact(p, at)
    if pair is None:
        return None
    row, proof = pair
    row.served = dict(serving['weights'])
    row.rule_version = FB.FUSION_BLEND_RULE_VERSION
    row.state = serving['receipt']['state']
    return row, {**proof, 'serving_sha':serving['content_sha256'],
        'state':row.state, 'served_weights':row.served, 'seat_weights':row.seat}


def calculate_veto(served, at):
    rows = []; refs = []
    for p, serving in served:
        pair = served_fact(p, serving, at)
        if pair is not None:
            row, proof = pair; rows.append(row); refs.append(proof)
    evidence = FB.veto_evidence(rows, at)
    return evidence, refs


def serve_one(p, controls, tables):
    original = FB.load_serving_tables
    FB.load_serving_tables = lambda *a, **k: tables
    try:
        weights, receipt = FB.overlay_system_weights(station_id=p['station_id'],
            event_date=p['event_date'], decision_at_utc=p['decision_at_utc'],
            timezone_name=p['settlement_timezone'], system_weights=p['seat_weights'],
            family_by_system=controls['family_by_system'], family_caps=controls['family_caps'],
            lead_by_system=controls['lead_by_system'], mode=FB.FUSION_BLEND_MODE_BLEND)
    finally:
        FB.load_serving_tables = original
    if set(weights)!=set(p['seat_weights']) or abs(sum(weights.values())-1)>1e-6:
        raise ValueError('fusion_ledger_served_scope_failed')
    out = dict(schema='offline_chronological_fusion_serving_v1',
        checkpoint=p['checkpoint'], station=p['station_id'], event=p['event_date'],
        decision_at=p['decision_at_utc'], fact_sha=p['content_sha256'],
        controls=controls, weights=weights, receipt=receipt,
        role='counterfactual_current_formula_original_seating_not_activation',
        parent_chain_certified=False)
    out['content_sha256'] = content_sha256(out)
    return out


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    guard(); summary, index, facts = read_inputs(); controls = controls_for(facts)
    production = ROOT/'bayes_basket_target/fusion_blend.py'; code_sha = old.sha_file(production)
    daily_items = sorted(index['days'].values(), key=lambda r:utc(r['cutoff']))
    served = []; days = {}; counts = collections.Counter(); cursor = 0; written = 0
    folder = HERE/'fusion_ledger_veto_manifests'; folder.mkdir(exist_ok=True)
    destination = HERE/'fusion_served_ledger.jsonl.gz'; tmp = destination.with_suffix('.tmp.gz')
    tables = dict(params={}, vetoes=[]); active_cutoff = None
    with gzip.open(tmp, 'wt') as out:
        for p in facts:
            at = utc(p['decision_at_utc'])
            while cursor<len(daily_items) and utc(daily_items[cursor]['cutoff'])<=at:
                daily = daily_items[cursor]; cutoff = utc(daily['cutoff'])
                evidence, refs = calculate_veto(served, cutoff)
                payload = dict(schema='offline_chronological_fusion_veto_v1',
                    cutoff=daily['cutoff'], evidence=evidence, training_refs=refs,
                    references_sha=content_sha256(refs), source_facts_sha=summary['output_sha'],
                    role='counterfactual_current_formula_original_seating_not_activation')
                identity = content_sha256(payload); body = json.dumps(payload).encode(); written += len(body)
                if len(body)>LIMIT or written>MAX_BYTES:
                    raise RuntimeError('fusion_ledger_output_limit')
                path = folder/(identity+'.json.gz')
                with gzip.open(path, 'wb') as target:
                    target.write(body)
                veto = dict(veto_id='offline-veto-'+identity[:24], computed_at=cutoff,
                    vetoed=evidence['vetoed'])
                tables = dict(params=parameter_tables(daily, summary['output_sha']), vetoes=[veto])
                active_cutoff = daily['cutoff']
                days[daily['cutoff']] = dict(evidence=evidence,
                    manifest=dict(path=str(path.relative_to(HERE)), sha=identity),
                    prior_serving_rows=len(served), parameter_cutoff=active_cutoff)
                print('cutoff', active_cutoff, 'served', len(served), 'veto', evidence['vetoed'], flush=True)
                cursor += 1; guard()
            record = serve_one(p, controls[p['checkpoint']], tables)
            record['parameter_cutoff'] = active_cutoff
            record['content_sha256'] = content_sha256({k:v for k,v in record.items() if k!='content_sha256'})
            body = json.dumps(record)+'\n'; written += len(body.encode())
            if len(body.encode())>LIMIT or written>MAX_BYTES:
                raise RuntimeError('fusion_ledger_output_limit')
            out.write(body); served.append((p,record)); counts[record['receipt']['state']] += 1
        # Evaluate final declared fit cutoffs even if no decision follows.
        while cursor<len(daily_items):
            daily=daily_items[cursor]; evidence,refs=calculate_veto(served,utc(daily['cutoff']))
            payload=dict(schema='offline_chronological_fusion_veto_v1',cutoff=daily['cutoff'],
                evidence=evidence,training_refs=refs,references_sha=content_sha256(refs),
                source_facts_sha=summary['output_sha'],
                role='counterfactual_current_formula_original_seating_not_activation')
            identity=content_sha256(payload);body=json.dumps(payload).encode();written+=len(body)
            if len(body)>LIMIT or written>MAX_BYTES:raise RuntimeError('fusion_ledger_output_limit')
            path=folder/(identity+'.json.gz')
            with gzip.open(path,'wb') as target:target.write(body)
            days[daily['cutoff']]=dict(evidence=evidence,manifest=dict(path=str(path.relative_to(HERE)),sha=identity),prior_serving_rows=len(served),parameter_cutoff=daily['cutoff'])
            cursor+=1
    if old.sha_file(production)!=code_sha:
        raise ValueError('fusion_formula_changed_during_replay')
    tmp.replace(destination)
    result=dict(schema='causal_fusion_served_veto_ledger_v1', rows=len(served), counts=dict(counts),
        days=days, source_facts_sha=summary['output_sha'], source_parameters_sha=old.sha_file(HERE/'fusion_refit.json'),
        source_formula_sha=code_sha, output_sha=old.sha_file(destination), output_uncompressed_bytes=written,
        production_modified=False, full_parent_chain_certified=False,
        limitations=['current formula, original seating qualifications',
            'representative original hourly anchors only; not continuous full-root serving',
            'fixed UTC-midnight counterfactual parameter and veto schedule'])
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if len(body.encode())>LIMIT:raise RuntimeError('fusion_ledger_summary_limit')
    (HERE/'fusion_served_ledger.json').write_text(body)
    print(json.dumps(dict(rows=len(served),states=dict(counts),vetoed_cutoffs=sum(x['evidence']['vetoed'] for x in days.values()))),flush=True)


if __name__=='__main__':main()
