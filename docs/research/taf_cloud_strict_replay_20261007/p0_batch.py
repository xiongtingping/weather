"""Resumeable read-only P0 arithmetic replay with restored radiation clocks.

This stage does not certify current-method nested OOS. Historical parameter
forms are audited explicitly; a version tag cannot promote an old fit.
"""
import argparse
import collections
import gzip
import json
import signal
import time
from pathlib import Path
from rebuild import ROOT,HERE,ORIGINAL,old,fetch,guard,utc,MAX_BYTES
from probe_replay import ArchivedScope
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.materialization import rehydrate_lineage_documents
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from bayes_basket_target.forecast_physics_asof import AS_OF_CAPTURE_MODES
from p0_refit import CurrentFits


class Rows:
    def __init__(self, rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def __iter__(self):return iter(self.rows)


class Physics:
    def __init__(self,c,station):
        self.station=station;self.rows=collections.defaultdict(list);self.bytes=0
        for r in fetch(c,'select local_date,shortwave_radiation_sum,captured_at_utc,source_id,capture_mode,content_sha256 from calibration_station_forecast_physics_version where station_id=?',(station,)):
            day,value,at,source,mode,sha=r
            self.bytes+=len(json.dumps(list(r)))
            if self.bytes>MAX_BYTES:raise RuntimeError('physics_input_limit')
            if mode not in AS_OF_CAPTURE_MODES or value is None:continue
            self.rows[day].append(dict(day=day,value=float(value),at=utc(at),source=source,mode=mode,sha=sha))
        for rs in self.rows.values():rs.sort(key=lambda r:r['at'])

    def pick(self,day,at):
        rs=[r for r in self.rows.get(day,()) if r['at']<=at]
        if not rs:return None
        newest=max(r['at'] for r in rs);w=[r for r in rs if r['at']==newest]
        if len({r['value'] for r in w})!=1:raise ValueError('ambiguous_physics_same_clock')
        return sorted(w,key=lambda r:(r['source'],r['sha']))[-1]


class StrictScope(ArchivedScope):
    def __init__(self,c,q,proof,physics,cutoff):
        super().__init__(c,q,proof);self.physics=physics;self.cutoff=utc(cutoff);self.receipts={}
    def execute(self,sql,args=()):
        s=' '.join(sql.split())
        if 'FROM calibration_station_forecast_physics ' in s:
            if args[0]!=self.packet['station_id']:raise ValueError('physics_station_scope')
            day=args[1]
            if 'WHERE station_id=? AND local_date=? AND reference_only=1' in s:
                r=self.physics.pick(day,self.cutoff)
                if r:self.receipts[day]=r
                return Rows([(r['value'],)] if r else [])
            if ('local_date<?' in s or 'local_date<>?' in s) and len(args)==3:
                # Both production standardizers must use strictly earlier
                # event days. A future forecast is never historical context.
                rows=[]
                for d in sorted((d for d in self.physics.rows if d<day),reverse=True):
                    r=self.physics.pick(d,self.cutoff)
                    if r:rows.append(r)
                    if len(rows)>=int(args[2]):break
                self.receipts.update({r['day']:r for r in rows})
                return Rows([(r['day'],r['value']) if s.startswith('SELECT local_date,') else (r['value'],) for r in rows])
            raise ValueError('unhandled_physics_sql')
        return super().execute(sql,args)


def clock_audit(c,z,at):
    l=z['packet']['native_probability_envelope']['lineage'];fp=z['forward_prediction'];issues=[];forms={}
    sha_to_system={v:k for k,v in json.loads(l.get('system_artifact_shas_json','{}')).items()}
    # The lineage currently stores individual SHAs in the availability
    # document; the compiler's system evidence is authoritative if omitted.
    rows=list(fetch(c,'select ensemble_system_id,bias_parameters_json,spread_parameters_json,reliability_parameters_json,content_sha256,train_end_date,valid_from_utc from calibration_native_ensemble_system_artifact where content_sha256 IN ('+','.join('?' for _ in sha_to_system)+')',tuple(sha_to_system))) if sha_to_system else []
    for r in rows:
        sid,b,s,w,sha,end,valid_from=r;b=json.loads(b);s=json.loads(s);w=json.loads(w)
        fit_through=w.get('fit_through_date')
        forms[sid]=dict(bias=b.get('correction_form'),kernel=s.get('kernel_rule'),fit=w.get('fit_rule',b.get('fit_rule')),artifact_train_end_date=end,serving_fit_through_date=fit_through,valid_from_utc=valid_from,sha=sha)
        if not fit_through:issues.append('system_serving_fit_cutoff_unproved:'+sid)
        if (fit_through or end)>=fp['event_date'] or utc(valid_from)>utc(at):issues.append('system_parameter_clock:'+sid)
        if s.get('kernel_rule')!='dress_v1':issues.append('system_kernel_requires_current_refit:'+sid)
    fusion=json.loads(l.get('fusion_blend_json','{}'))
    if fusion.get('params_computed_at_utc') and utc(fusion['params_computed_at_utc'])>utc(at):issues.append('fusion_parameter_after_cutoff')
    return dict(issues=issues,system_forms=forms,fusion=fusion,trained_through=fp['trained_through_event_date'],
                selection_uses_historical_oos_artifacts=True,current_method_nested_oos_certified=False)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--stations',nargs='*');ap.add_argument('--max-roots',type=int,default=0);ap.add_argument('--resume',action='store_true');ap.add_argument('--current-refit',action='store_true');ap.add_argument('--retry-provisional',action='store_true');args=ap.parse_args()
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');by=collections.defaultdict(dict);nbytes=0;available={}
    with gzip.open(ORIGINAL/'audited_rows.jsonl.gz','rt') as f:
        for ln in f:
            nbytes+=len(ln.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('original_rows_input_limit')
            r=json.loads(ln);e=fetch(c,'select created_at from basket_probability_envelope where envelope_id=?',(r['envelope_id'],)).fetchone()
            if e is None:raise ValueError('original_p1_receipt_missing')
            available[(r['station'],r['day'],r['decided_at'],r['observation_id'])]=max(utc(r['decided_at']),utc(e[0])).isoformat()
    nbytes=0
    with gzip.open(HERE/'raw_pairs.jsonl.gz','rt') as f:
        for ln in f:
            nbytes+=len(ln.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('pairs_input_limit')
            p=json.loads(ln);cp=p['chain_audit']['p0_checkpoint'];p['p1_available_at']=available[(p['station'],p['day'],p['decided_at'],p['observation_id'])]
            if cp and (not args.stations or p['station'] in args.stations):by[p['station']].setdefault(cp,p)
    outdir=HERE/('p0_refitted_roots' if args.current_refit else 'p0_roots');outdir.mkdir(exist_ok=True);total=collections.Counter();station_results={}
    for station,roots in sorted(by.items()):
        dst=outdir/(station+'.jsonl.gz');done={};profile=collections.Counter()
        if args.resume and dst.exists():
            consumed=0
            with gzip.open(dst,'rt') as f:
                for ln in f:
                    consumed+=len(ln.encode())
                    if consumed>MAX_BYTES:raise RuntimeError('resume_limit')
                    r=json.loads(ln)
                    if r.get('reason')!='ValueError:original_parent_clock':done[r['original_checkpoint']]=r
        if args.retry_provisional:
            consumed=0
            with gzip.open(HERE/'p0_roots'/(station+'.jsonl.gz'),'rt') as inp:
                for ln in inp:
                    consumed+=len(ln.encode())
                    if consumed>MAX_BYTES:raise RuntimeError('baseline_provisional_audit_limit')
                    baseline=json.loads(ln);cp=baseline['original_checkpoint'];r=done.get(cp)
                    if not r or 'current_parameter_train_days_below_20:' not in r.get('reason','') or 'output' not in baseline:continue
                    sid=r['reason'].rsplit(':',1)[-1];provisional=json.loads(baseline['output']['packet']['native_probability_envelope']['lineage'].get('provisional_uncalibrated_system_ids_json','[]'))
                    if sid in provisional:del done[cp]
        physics=Physics(c,station);written=0;nt=0;start=time.monotonic();tmp=dst.with_suffix('.tmp.gz');fits=None;fitbytes=0;fitpaths=set()
        with gzip.open(tmp,'wt') as out:
            for cp,p in sorted(roots.items(),key=lambda x:x[1]['decided_at']):
                if cp in done:r=done[cp]
                elif args.max_roots and nt>=args.max_roots:continue
                else:
                    nt+=1;r=dict(station=station,day=p['day'],original_checkpoint=cp,downstream_decided_at=p['decided_at'],downstream_available_at=p['p1_available_at'])
                    try:
                        es=list(fetch(c,'select decision_at_utc,created_at,lineage_json from basket_probability_envelope where checkpoint_id=? and stage=? and state=?',(cp,'P0','COMMITTED')))
                        if len(es)!=1:raise ValueError('original_p0_envelope_missing_or_ambiguous')
                        e=es[0];at=e['created_at'];L=rehydrate_lineage_documents(c,json.loads(e['lineage_json']))
                        if utc(e['decision_at_utc'])>utc(at) or utc(at)>utc(p['p1_available_at']):raise ValueError('original_parent_clock')
                        sha=p['chain_audit']['p0_packet_sha'];path=ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'/p['day']/f'{station}-{sha[:16]}.json.gz'
                        with gzip.open(path,'rb') as f:b=f.read(2*1024**2+1)
                        if len(b)>2*1024**2:raise RuntimeError('packet_limit')
                        q=json.loads(b)
                        if content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})!=sha:raise ValueError('packet_sha_mismatch')
                        if utc(q['received_at_utc'])>utc(at):raise ValueError('packet_after_parent')
                        ps=[dict(x) for x in fetch(c,'select * from basket_probability_bridge_history where checkpoint_id=? and state=?',(cp,'COMMITTED'))]
                        ps=[x for x in ps if json.loads(x['bucket_intervals_json'])==q['bucket_intervals'] and x['settlement_timezone']==q['settlement_timezone'] and utc(x['created_at_utc'])<=utc(at)]
                        if len(ps)!=1:raise ValueError('asof_geometry_receipt_missing_or_ambiguous')
                        scope=StrictScope(c,q,ps[0],physics,at)
                        def timeout(sig,frame):raise TimeoutError('compiler_wall_limit')
                        signal.signal(signal.SIGALRM,timeout);signal.alarm(45)
                        try:
                            z=compile_native_ensemble_p0(scope,forward_packet=q,decision_at_utc=at,fresh_seconds=86400)
                            if args.current_refit:
                                if fits is None:fits=CurrentFits(c,station,q['settlement_timezone'])
                                overrides,fitproof=fits.at(q,z,at)
                                z=compile_native_ensemble_p0(scope,forward_packet=q,decision_at_utc=at,fresh_seconds=86400,artifact_parameter_override=overrides)
                                refdir=HERE/'p0_refit_manifests'/station;refdir.mkdir(parents=True,exist_ok=True);refs={}
                                for sid,proof in fitproof.items():
                                    # Identical training facts and fitted
                                    # parameters share one manifest. The
                                    # consuming root's own cutoff remains
                                    # explicit in its reference below.
                                    stored={k:v for k,v in proof.items() if k!='as_of'}
                                    identity=content_sha256(stored);path=refdir/(identity+'.json.gz')
                                    if path not in fitpaths:
                                        body=json.dumps(stored).encode();fitbytes+=len(body)
                                        if len(body)>2*1024**2 or fitbytes>MAX_BYTES:raise RuntimeError('station_fit_manifest_output_limit')
                                        if not path.exists():
                                            with gzip.open(path,'wb') as mf:mf.write(body)
                                        fitpaths.add(path)
                                    refs[sid]=dict(content_sha256=identity,path=str(path.relative_to(HERE)),train_manifest_sha256=proof['train_manifest_sha256'],as_of=at,scope=proof['scope'],parameters=proof['parameters'])
                                r['current_parameter_refits']=refs
                        finally:signal.alarm(0)
                        r.update(state='ASOF_INPUT_ARITHMETIC_NOT_CURRENT_CHAIN_OOS',parent_decision_at=e['decision_at_utc'],parent_committed_at=at,packet_sha=sha,output=z,
                                 parameter_audit=clock_audit(c,z,at),physics_receipts=[{**x,'at':x['at'].isoformat()} for x in scope.receipts.values()])
                    except (FileNotFoundError,ValueError,RuntimeError,TimeoutError) as exc:r.update(state='REFUSED',reason=type(exc).__name__+':'+str(exc))
                    except Exception as exc:
                        if type(exc).__name__!='ContractError':raise
                        r.update(state='REFUSED',reason=type(exc).__name__+':'+str(exc))
                b=json.dumps(r,ensure_ascii=False)+'\n';written+=len(b.encode())
                if len(b.encode())>2*1024**2 or written>MAX_BYTES:raise RuntimeError('station_output_limit')
                out.write(b);profile[r['state']]+=1
                if r.get('reason'):profile['FileNotFoundError:exact_root_packet_missing' if r['reason'].startswith('FileNotFoundError:') else r['reason']]+=1
                if (sum(profile[k] for k in ('REFUSED','ASOF_INPUT_ARITHMETIC_NOT_CURRENT_CHAIN_OOS')))%50==0:guard()
        tmp.replace(dst);total.update(profile);station_results[station]=dict(profile=dict(profile),roots=len(roots),output_bytes=written,physics_input_bytes=physics.bytes,fit_manifest_bytes=fitbytes,sha256=old.sha_file(dst),seconds=round(time.monotonic()-start,2));print(station,json.dumps(station_results[station]),flush=True)
    report=dict(schema='p0_asof_input_arithmetic_rebuild_v1',counts=dict(total),stations=station_results,current_chain_oos_complete=False,production_modified=False,
                compiler_sha256=old.sha_file(ROOT/'bayes_basket_target/native_ensemble_p0.py'),native_oos_sha256=old.sha_file(ROOT/'bayes_basket_target/native_ensemble_oos.py'))
    (HERE/('p0_refitted_batch.json' if args.current_refit else 'p0_batch.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
