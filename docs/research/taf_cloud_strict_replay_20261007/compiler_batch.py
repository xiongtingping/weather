"""Sequential isolated P0 compiler probes, with explicit incomplete reports."""
import collections
import argparse
import json
import subprocess
import sys
import time
from rebuild import HERE,guard,old


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--fusion-refit',action='store_true');args=ap.parse_args();suffix='.fusion' if args.fusion_refit else '';guard();stations=sorted(json.loads((HERE/'upstream.json').read_text())['stations']);records={};profile=collections.Counter()
    for station in stations:
        guard();summary=json.loads((HERE/'nested_oos'/station/'2026-10-05'/'summary.json').read_text());out=HERE/'nested_compiler_probes'/(station+suffix+'.json')
        if summary.get('method_version')!='actual_arrival_inner_truth_ladder_v3' or 'report_header' not in summary:
            record=dict(state='PENDING_CURRENT_REPORT')
        elif out.exists() and json.loads(out.read_text())['report_sha']==summary['report_header']['content_sha256']:
            prior=json.loads(out.read_text());record=dict(state=prior['state'],reason=prior.get('reason'),certificate_counts=prior['certificate_persistence']['counts'],output=str(out.relative_to(HERE)))
        else:
            start=time.monotonic()
            try:
                r=subprocess.run([sys.executable,str(HERE/'recompile_nested.py'),'--station',station]+(['--fusion-refit'] if args.fusion_refit else []),capture_output=True,text=True,timeout=150)
                if len((r.stdout+r.stderr).encode())>2*1024**2:raise RuntimeError('compiler_batch_log_limit')
                if r.returncode:record=dict(state='PROBE_PROCESS_REFUSED',reason=(r.stdout+r.stderr)[-2000:])
                else:
                    prior=json.loads(out.read_text());record=dict(state=prior['state'],reason=prior.get('reason'),certificate_counts=prior['certificate_persistence']['counts'],output=str(out.relative_to(HERE)))
            except subprocess.TimeoutExpired:record=dict(state='PROBE_PROCESS_REFUSED',reason='compiler_process_wall_limit')
            record['seconds']=round(time.monotonic()-start,2)
        if out.exists() and record['state'] in ('OFFLINE_NESTED_CERTIFICATE_P0_COMPILED','REFUSED'):record['output_sha']=old.sha_file(out)
        records[station]=record;profile[record['state']]+=1;print(station,json.dumps(record),flush=True)
        (HERE/('compiler_batch'+suffix+'.json')).write_text(json.dumps(dict(schema='fleet_isolated_nested_certificate_compiler_v1',counts=dict(profile),stations=records,production_modified=False,whole_current_chain_certified=False,fusion_refit=bool(args.fusion_refit)),ensure_ascii=False,indent=2))
    guard()


if __name__=='__main__':main()
