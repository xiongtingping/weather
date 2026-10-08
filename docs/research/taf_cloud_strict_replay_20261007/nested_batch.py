"""Bounded sequential fleet probes of actual-arrival nested calibration."""
import collections
import argparse
import json
import subprocess
import sys
import time
from rebuild import HERE,guard,MAX_BYTES


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--repair-headers-only',action='store_true');ap.add_argument('--summarize-only',action='store_true');args=ap.parse_args();guard();stations=sorted(json.loads((HERE/'upstream.json').read_text())['stations']);records={};profile=collections.Counter();destination=HERE/('nested_batch.headers.json' if args.repair_headers_only else 'nested_batch.json')
    for station in stations:
        guard();path=HERE/'nested_oos'/station/'2026-10-05'/'summary.json';start=time.monotonic()
        # Resume only this exact source-method version, never old artifacts.
        prior=json.loads(path.read_text()) if path.exists() else {}
        if prior.get('method_version')=='actual_arrival_inner_truth_ladder_v3' and 'report_header' in prior:
            records[station]=dict(state='PROBED',path=str(path.relative_to(HERE)));profile['PROBED']+=1;continue
        if args.summarize_only or (args.repair_headers_only and prior.get('method_version')!='actual_arrival_inner_truth_ladder_v3'):
            records[station]=dict(state='PENDING_CURRENT_REPORT',path=str(path.relative_to(HERE)));profile['PENDING_CURRENT_REPORT']+=1;continue
        try:
            result=subprocess.run([sys.executable,str(HERE/'nested_oos_probe.py'),'--station',station],capture_output=True,text=True,timeout=240)
            if len((result.stdout+result.stderr).encode())>2*1024**2:raise RuntimeError('nested_process_log_limit')
            record=dict(state='PROBED' if result.returncode==0 else 'REFUSED',exit_code=result.returncode,seconds=round(time.monotonic()-start,2),diagnostic=(result.stdout+result.stderr)[-4000:])
            if result.returncode==0:record['path']=str(path.relative_to(HERE))
        except subprocess.TimeoutExpired:record=dict(state='REFUSED',reason='nested_process_wall_limit')
        records[station]=record;profile[record['state']]+=1;print(station,json.dumps(record),flush=True)
        destination.write_text(json.dumps(dict(schema='fleet_actual_arrival_nested_probe_v1',stations=records,counts=dict(profile),whole_current_chain_certified=False,production_modified=False),ensure_ascii=False,indent=2))
    destination.write_text(json.dumps(dict(schema='fleet_actual_arrival_nested_probe_v1',stations=records,counts=dict(profile),whole_current_chain_certified=False,production_modified=False),ensure_ascii=False,indent=2))
    guard()


if __name__=='__main__':main()
