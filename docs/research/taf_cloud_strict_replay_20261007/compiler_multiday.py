"""Compile scheduled boundary reports with their own original packet clocks."""
import collections
import json
import subprocess
import sys
import time
from rebuild import HERE,guard,old
from fusion_ledger import bounded_json,LIMIT


def main():
    guard();batch=bounded_json(HERE/'nested_multiday_batch.json');records={};counts=collections.Counter()
    for key,r in sorted(batch['records'].items()):
        if r['state']!='COMPONENT_REPORT_BUILT' or r['event']=='2026-10-05':continue
        target=HERE/'nested_compiler_probes'/r['station']/(r['event']+'.ledger.json');begin=time.monotonic();guard()
        process=subprocess.run([sys.executable,str(HERE/'recompile_nested.py'),'--station',r['station'],'--day',r['event'],'--fusion-ledger'],capture_output=True,text=True,timeout=150)
        if len((process.stdout+process.stderr).encode())>LIMIT:raise RuntimeError('compiler_multiday_process_output_limit')
        if process.returncode:
            record=dict(state='PROCESS_REFUSED',reason=(process.stdout+process.stderr)[-2000:])
        else:
            result=bounded_json(target)
            if result['report_sha']!=bounded_json(HERE/r['path'])['report_header']['content_sha256'] or result['as_of']!=r['outer_cutoff']:
                raise ValueError('compiler_multiday_result_scope_changed')
            record=dict(state=result['state'],reason=result.get('reason'),output=str(target.relative_to(HERE)),output_sha=old.sha_file(target),report_sha=result['report_sha'])
        record['seconds']=round(time.monotonic()-begin,2);records[key]=record;counts[record['state']]+=1
        payload=dict(schema='multiday_boundary_isolated_compiler_v1',counts=dict(counts),records=records,
            source_schedule_sha=old.sha_file(HERE/'nested_multiday_batch.json'),production_modified=False,whole_current_chain_certified=False)
        (HERE/'compiler_multiday.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2));print(key,json.dumps(record),flush=True)
    guard()


if __name__=='__main__':main()
