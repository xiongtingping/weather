"""Bounded chronological station/date qualification expansion, resumable.

Every scheduled cutoff is the first original parent actually committed that
event day. Missing anchors remain explicit; they are not synthesized. This
is component coverage, not full current-chain activation or promotion.
"""
import argparse
import collections
import gzip
import json
import subprocess
import sys
import time
from datetime import date,timedelta
from rebuild import HERE,ROOT,MAX_BYTES,old,guard,utc
from nested_storage import LIMIT
from parent_clock import original_parent_clock


def inventory(stations,start,end,source=None):
    result={};profile=collections.Counter();fingerprints={}
    for station in stations:
        guard();path=HERE/'p0_roots'/(station+'.jsonl.gz');fingerprints[station]=old.sha_file(path)
        anchors={};used=0
        with gzip.open(path,'rt') as inp:
            for line in inp:
                size=len(line.encode());used+=size
                if size>LIMIT or used>MAX_BYTES:raise RuntimeError('nested_multiday_root_limit')
                r=json.loads(line)
                if not start<=r['day']<=end:continue
                if 'parent_committed_at' not in r:
                    profile['root_without_committed_parent_clock']+=1
                    reason=r.get('reason','unknown');profile['root_refusal:'+('FileNotFoundError' if reason.startswith('FileNotFoundError:') else reason)]+=1
                    if source is None:continue
                    restored=original_parent_clock(source,station,r['day'],r['original_checkpoint'])
                    profile['original_parent_clock_restored_independent_of_old_replay_gate']+=1
                    r={**r,'parent_committed_at':restored['outer_cutoff'],'original_clock_receipt':restored}
                held=anchors.get(r['day'])
                if held is None or utc(r['parent_committed_at'])<utc(held['outer_cutoff']):
                    anchors[r['day']]=dict(station=station,event=r['day'],
                        outer_cutoff=r['parent_committed_at'],anchor_checkpoint=r['original_checkpoint'],
                        clock_origin='ORIGINAL_P0_SOURCE_RECEIPT' if 'original_clock_receipt' in r else 'PREVIOUSLY_AUDITED_ORIGINAL_P0_SOURCE_RECEIPT')
        for offset in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1):
            day=(date.fromisoformat(start)+timedelta(days=offset)).isoformat();key=station+'/'+day
            if day not in anchors:
                result[key]=dict(state='NO_RESEARCH_PARENT_ANCHOR',station=station,event=day)
                profile['station_dates_without_anchor']+=1
            else:
                result[key]=dict(state='SCHEDULED',**anchors[day]);profile['scheduled_station_dates']+=1
    return result,dict(profile),fingerprints


def current_report(record):
    path=HERE/'nested_oos'/record['station']/record['event']/'summary.json'
    if not path.exists():return None
    if path.stat().st_size>LIMIT:raise RuntimeError('nested_multiday_summary_limit')
    summary=json.loads(path.read_text())
    if summary.get('method_version')!='actual_arrival_inner_truth_ladder_v3' or 'report_header' not in summary:return None
    if (summary['station'],summary['event'],summary['outer_cutoff'],summary['anchor_checkpoint'])!=(record['station'],record['event'],record['outer_cutoff'],record['anchor_checkpoint']):
        return None
    return dict(state='COMPONENT_REPORT_BUILT',path=str(path.relative_to(HERE)),
        summary_sha=old.sha_file(path),objects=len(summary['objects']),cross_states=summary['cross_states'])


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--stations',nargs='*');ap.add_argument('--start',default='2026-09-01');ap.add_argument('--end',default='2026-10-06');ap.add_argument('--probe-days',nargs='*');ap.add_argument('--inventory-only',action='store_true');ap.add_argument('--max-new-jobs',type=int,default=0);args=ap.parse_args()
    if args.max_new_jobs<0 or args.start>args.end:raise ValueError('nested_multiday_invalid_bounds')
    guard();available=sorted(json.loads((HERE/'upstream.json').read_text())['stations']);stations=sorted(args.stations or available)
    if not set(stations)<=set(available):raise ValueError('nested_multiday_unknown_station')
    source=old.connect(ROOT/'data/bayes_basket_target_ssot.db')
    try:schedule,census,fingerprints=inventory(stations,args.start,args.end,source)
    finally:source.close()
    sources=[HERE/'nested_oos_probe.py',HERE/'nested_storage.py',HERE/'parent_clock.py',ROOT/'bayes_basket_target/native_ensemble_oos.py',ROOT/'bayes_basket_target/center_width_store.py']
    code_fingerprints={str(path.relative_to(ROOT)):old.sha_file(path) for path in sources}
    records={};counts=collections.Counter();jobs=0;destination=HERE/('nested_multiday_inventory.json' if args.inventory_only else 'nested_multiday_batch.json')
    def save():
        payload=dict(schema='chronological_station_date_nested_component_batch_v1',
            period=[args.start,args.end],stations=stations,census=census,counts=dict(counts),
            records=records,source_roots=fingerprints,source_code=code_fingerprints,
            complete_component_schedule=len(records)==len(schedule) and not any(x['state'] in ('PENDING','REFUSED') for x in records.values()),
            production_modified=False,whole_current_chain_certified=False,
            limitations=['first committed parent per station event-day only',
                'center/width tables still historical; full causal table refit and downstream reroot pending',
                'alternative native candidates are not substituted for absent original anchors'])
        body=json.dumps(payload,ensure_ascii=False,indent=2)
        if len(body.encode())>LIMIT:raise RuntimeError('nested_multiday_batch_output_limit')
        destination.write_text(body)
    for key,record in sorted(schedule.items(),key=lambda pair:(pair[1]['event'],pair[1]['station'])):
        guard()
        if record['state']=='NO_RESEARCH_PARENT_ANCHOR':out=record
        else:
            held=current_report(record)
            if held:out={**record,**held,'reused_existing':True}
            elif args.inventory_only or (args.probe_days and record['event'] not in args.probe_days) or (args.max_new_jobs and jobs>=args.max_new_jobs):out={**record,'state':'PENDING'}
            else:
                jobs+=1;begin=time.monotonic()
                try:
                    process=subprocess.run([sys.executable,str(HERE/'nested_oos_probe.py'),'--station',record['station'],'--day',record['event']],capture_output=True,text=True,timeout=240)
                    if len((process.stdout+process.stderr).encode())>LIMIT:raise RuntimeError('nested_multiday_process_output_limit')
                    if process.returncode:out={**record,'state':'REFUSED','reason':(process.stdout+process.stderr)[-2000:]}
                    else:
                        built=current_report(record)
                        if built is None:raise ValueError('nested_multiday_missing_built_report')
                        out={**record,**built,'seconds':round(time.monotonic()-begin,2)}
                except subprocess.TimeoutExpired:out={**record,'state':'REFUSED','reason':'nested_multiday_child_wall_limit'}
                print(key,json.dumps(out),flush=True)
        records[key]=out;counts[out['state']]+=1
        if 'seconds' in out or out['state']=='REFUSED':save()
        if any(old.sha_file(path)!=code_fingerprints[str(path.relative_to(ROOT))] for path in sources):
            raise ValueError('nested_multiday_source_code_changed')
    save();print(json.dumps(dict(counts=dict(counts),new_jobs=jobs,scheduled_station_dates=census.get('scheduled_station_dates',0),restored_parent_clocks=census.get('original_parent_clock_restored_independent_of_old_replay_gate',0))),flush=True)


if __name__=='__main__':main()
