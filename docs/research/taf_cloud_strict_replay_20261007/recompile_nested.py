"""Exercise rebuilt certificates in an isolated in-memory compiler store.

Source SSOT is attached read-only. Only artifact/activation tables in this
private process are writable; nothing is published to production.
"""
import gzip
import json
import re
import signal
import sqlite3
import argparse
from rebuild import HERE,ROOT,old,fetch,guard,MAX_BYTES,utc
from nested_oos_probe import ReadScope
from p0_batch import StrictScope,Physics
from bayes_basket_target import native_ensemble_oos as OOS
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target import fusion_blend as FB

OWN={'calibration_native_ensemble_system_artifact','calibration_cross_native_ensemble_selection_evidence','calibration_cross_native_ensemble_artifact','calibration_cross_native_ensemble_oos_evaluation','calibration_cross_native_ensemble_oos_prediction','calibration_native_ensemble_promotion','calibration_native_ensemble_activation','calibration_native_ensemble_certificate_renewal','calibration_seat_weight_rule_journal'}


def read_report(station,day='2026-10-05'):
    guard();folder=HERE/'nested_oos'/station/day;summary=json.loads((folder/'summary.json').read_text())
    if summary['method_version']!='actual_arrival_inner_truth_ladder_v3' or 'report_header' not in summary:raise ValueError('nested_report_method_not_ready')
    report={**summary['report_header'],'readiness':summary['readiness'],'ready_system_evaluations':[],'cross_candidates':[]};used=0
    for ref in summary['objects']:
        if ref['kind'] not in ('system','cross'):continue
        with gzip.open(HERE/ref['path'],'rb') as f:body=f.read(2*1024**2+1)
        used+=len(body)
        if len(body)>2*1024**2 or used>MAX_BYTES:raise RuntimeError('nested_report_input_limit')
        obj=json.loads(body)
        if content_sha256(obj)!=ref['sha256']:raise ValueError('nested_component_identity_failed')
        report['ready_system_evaluations' if ref['kind']=='system' else 'cross_candidates'].append(obj)
    if content_sha256({k:v for k,v in report.items() if k!='content_sha256'})!=report['content_sha256']:raise ValueError('nested_report_identity_failed')
    return summary,report


def isolated_store(source_path):
    """Each reference table is read-only; writable evidence stays in RAM."""
    guard()
    source=old.connect(source_path);c=sqlite3.connect(':memory:',uri=True);c.row_factory=sqlite3.Row
    c.execute('PRAGMA max_page_count=32768');c.execute('PRAGMA cache_size=-16384');c.execute('PRAGMA temp_store=MEMORY')
    c.execute('ATTACH DATABASE ? AS source',('file:'+str(source_path)+'?mode=ro',))
    write_ops={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX}
    c.set_authorizer(lambda op,a,b,db,origin:sqlite3.SQLITE_DENY if db=='source' and op in write_ops else sqlite3.SQLITE_OK)
    schema=list(fetch(source,"select name,sql from sqlite_master where type='table' and name not like 'sqlite_%'"));schema_bytes=sum(len(str(r['sql']).encode()) for r in schema)
    if schema_bytes>2*1024**2:raise RuntimeError('schema_input_limit')
    for r in schema:
        name=r['name']
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',name) or 'VIRTUAL TABLE' in r['sql'].upper():raise ValueError('unsupported_research_schema')
        c.execute(r['sql'])
        if name not in OWN:c.execute('CREATE TEMP VIEW "'+name+'" AS SELECT * FROM source."'+name+'"')
    for r in fetch(source,"select name,tbl_name,sql from sqlite_master where type='index' and sql is not null"):
        if r['tbl_name'] in OWN:c.execute(r['sql'])
    return source,c


def causal_fusion_tables(station,at):
    index=json.loads((HERE/'fusion_refit.json').read_text());eligible=[r for r in index['days'].values() if utc(r['cutoff'])<=utc(at)]
    if not eligible:raise ValueError('no_causal_fusion_fit_cutoff')
    daily=max(eligible,key=lambda r:utc(r['cutoff']));ref=daily['parameters'].get(station);proof=dict(cutoff=daily['cutoff'],source_facts_sha=index['source_facts_sha'],veto='unavailable_until_full_chronological_serving_ledger')
    if not ref:return dict(params={},vetoes=[]),proof
    with gzip.open(HERE/ref['path'],'rb') as inp:body=inp.read(2*1024**2+1)
    if len(body)>2*1024**2:raise RuntimeError('fusion_compiler_parameter_limit')
    p=json.loads(body)
    if content_sha256(p)!=ref['sha'] or p['station']!=station or p['cutoff']!=daily['cutoff'] or p['source_facts_sha']!=index['source_facts_sha'] or content_sha256(p['train_manifest'])!=p['train_manifest_sha']:raise ValueError('fusion_compiler_parameter_identity_failed')
    for r in p['train_manifest']:
        if max(utc(r['truth_known']),utc(r['decision_at']))>utc(p['cutoff']):raise ValueError('fusion_compiler_parameter_time_travel')
    proof['manifest']=ref
    table=dict(params_id='offline-fusion-'+ref['sha'][:24],computed_at=utc(p['cutoff']),computed_at_utc=p['cutoff'],tau=p['tau'],theta=p['theta'],days=p['days'])
    return dict(params={station:[table]},vetoes=[]),proof


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--station',default='ZUUU');ap.add_argument('--day',default='2026-10-05');ap.add_argument('--fusion-refit',action='store_true');args=ap.parse_args();station=args.station
    summary,report=read_report(station,args.day)
    source,c=isolated_store(ROOT/'data/bayes_basket_target_ssot.db')
    # Attaching mode=ro is enforced by SQLite, and write targets are main's
    # isolated artifact tables. No source DB transaction is started.
    receipt=OOS.persist_native_ensemble_oos(c,report,activated_at_utc=summary['outer_cutoff']);c.commit();guard()
    root=None;readbytes=0
    for ln in gzip.open(HERE/'p0_roots'/(station+'.jsonl.gz'),'rt'):
        readbytes+=len(ln.encode())
        if readbytes>MAX_BYTES:raise RuntimeError('nested_compile_anchor_limit')
        r=json.loads(ln)
        if r['original_checkpoint']==summary['anchor_checkpoint']:root=r;break
    if root is None:raise ValueError('nested_compile_anchor_missing')
    at=root['parent_committed_at'];packet_sha=root['packet_sha'];path=ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'/root['day']/(station+'-'+packet_sha[:16]+'.json.gz')
    with gzip.open(path,'rb') as f:body=f.read(2*1024**2+1)
    if len(body)>2*1024**2:raise RuntimeError('nested_compile_packet_limit')
    packet=json.loads(body)
    if content_sha256({k:v for k,v in packet.items() if k!='snapshot_content_sha256'})!=packet_sha:raise ValueError('nested_compile_packet_identity_failed')
    proofs=[dict(x) for x in fetch(source,"select * from basket_probability_bridge_history where checkpoint_id=? and state='COMMITTED'",(root['original_checkpoint'],))]
    proofs=[x for x in proofs if json.loads(x['bucket_intervals_json'])==packet['bucket_intervals'] and utc(x['created_at_utc'])<=utc(at)]
    if len(proofs)!=1:raise ValueError('nested_compile_geometry_not_unique')
    scope=StrictScope(c,packet,proofs[0],Physics(source,station),at)
    result=dict(schema='nested_certificate_isolated_compiler_probe_v1',station=station,event=root['day'],as_of=at,report_sha=report['content_sha256'],certificate_persistence=receipt,storage='private in-memory DB; source SSOT attached read-only',production_modified=False,current_chain_certified=False)
    original_loader=FB.load_serving_tables
    if args.fusion_refit:
        tables,proof=causal_fusion_tables(station,at);result['fusion_refit_receipt']=proof;FB.load_serving_tables=lambda *a,**k:tables
    try:
        def timeout(*a):raise TimeoutError('nested_compile_wall_limit')
        signal.signal(signal.SIGALRM,timeout);signal.alarm(45)
        compiled=compile_native_ensemble_p0(scope,forward_packet=packet,decision_at_utc=at,fresh_seconds=86400)
        result.update(state='OFFLINE_NESTED_CERTIFICATE_P0_COMPILED',output=compiled)
    except Exception as e:result.update(state='REFUSED',reason=type(e).__name__+':'+str(e))
    finally:signal.alarm(0);FB.load_serving_tables=original_loader
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if len(body.encode())>2*1024**2:raise RuntimeError('nested_compile_output_limit')
    dst=HERE/'nested_compiler_probes';dst.mkdir(exist_ok=True);(dst/(station+('.fusion' if args.fusion_refit else '')+'.json')).write_text(body);c.close();source.close();print(json.dumps(dict(station=station,state=result['state'],reason=result.get('reason'),certificate_counts=receipt.get('counts'),fusion_refit=bool(args.fusion_refit))),flush=True)


if __name__=='__main__':main()
