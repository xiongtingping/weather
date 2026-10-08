"""Audit all original input identities and arrival clocks of built reports."""
import collections
import gzip
import json
from rebuild import HERE,ROOT,old,fetch,guard,MAX_BYTES
from fusion_ledger import bounded_json,LIMIT
from parent_clock import original_parent_clock,validate_original_packet_clock
from bayes_basket_target.materialization import rehydrate_lineage_documents
from bayes_basket_target.contracts import content_sha256


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');records={};used=0;counts=collections.Counter()
    for path in sorted((HERE/'nested_oos').glob('*/*/summary.json')):
        summary=bounded_json(path);station=summary['station'];day=summary['event'];cp=summary['anchor_checkpoint']
        clock=original_parent_clock(c,station,day,cp)
        if clock['outer_cutoff']!=summary['outer_cutoff']:raise ValueError('original_packet_audit_parent_clock_changed')
        r=fetch(c,"select lineage_json,content_sha256 from basket_probability_envelope where checkpoint_id=? and stage='P0' and state='COMMITTED'",(cp,)).fetchone()
        if r['content_sha256']!=clock['original_envelope_content_sha'] or len(r['lineage_json'].encode())>LIMIT:raise ValueError('original_packet_audit_envelope_identity')
        L=rehydrate_lineage_documents(c,json.loads(r['lineage_json']));sha=L['native_forward_snapshot_sha256'];packet_path=ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'/day/(station+'-'+sha[:16]+'.json.gz')
        with gzip.open(packet_path,'rb') as inp:body=inp.read(LIMIT+1)
        used+=len(body)
        if len(body)>LIMIT or used>MAX_BYTES:raise RuntimeError('original_packet_clock_audit_input_limit')
        packet=json.loads(body)
        if content_sha256({k:v for k,v in packet.items() if k!='snapshot_content_sha256'})!=sha:raise ValueError('original_packet_clock_audit_sha_mismatch')
        validate_original_packet_clock(packet,clock)
        records[station+'/'+day]=dict(state='ORIGINAL_PACKET_CLOCK_AND_IDENTITY_PASS',parent_clock=clock,packet_sha=sha,received_at=packet['received_at_utc']);counts['passed']+=1;guard()
    result=dict(schema='original_native_packet_and_parent_clock_audit_v1',passed=True,counts=dict(counts),records=records,production_modified=False,whole_current_chain_certified=False)
    (HERE/'original_packet_clock_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));c.close()
    print(json.dumps(dict(passed=True,counts=dict(counts))),flush=True)


if __name__=='__main__':main()
