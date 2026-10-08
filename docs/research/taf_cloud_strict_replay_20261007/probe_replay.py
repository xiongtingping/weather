"""Read-only current P0 compiler probe using exact archived bucket contracts.

Online registry expiry cannot erase a historical raw packet's bound geometry.
Only its timezone lookup is supplied from a committed, matching bridge; all
model activations and compiler gates are unaltered. No scores are produced.
"""
import gzip
import hashlib
import json
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from rebuild import HERE,ROOT,guard,fetch,old,utc,MAX_BYTES
sys.path.insert(0,str(ROOT))
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0


class ArchivedScope:
    def __init__(self,c,packet,proof):self.c=c;self.packet=packet;self.proof=proof
    def __getattr__(self,k):return getattr(self.c,k)
    def execute(self,sql,args=()):
        normalized=' '.join(sql.split())
        if normalized=='SELECT DISTINCT settlement_timezone FROM basket_target_market_registry WHERE station_id=? AND event_date=? AND settlement_timezone<>\'\'':
            if tuple(args)!=(self.packet['station_id'],self.packet['event_date']):raise ValueError('archived_scope_mismatch')
            return self.c.execute('select ? as settlement_timezone',(self.proof['settlement_timezone'],))
        return self.c.execute(sql,args)


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');selected={};nbytes=0
    with gzip.open(HERE/'raw_pairs.jsonl.gz','rt') as f:
        for ln in f:
            nbytes+=len(ln.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('input_limit')
            p=json.loads(ln)
            if p['station'] in ('ZUUU','KLGA','EGLC') and p['day'] in ('2026-09-02','2026-09-22','2026-10-05'):
                # First packet decision of the local afternoon, fixed before
                # invoking the compiler rather than picking on outcomes.
                at=utc(p['observation_valid_at']);local=at.astimezone(__import__('zoneinfo').ZoneInfo({'ZUUU':'Asia/Shanghai','KLGA':'America/New_York','EGLC':'Europe/London'}[p['station']]))
                if 12<=local.hour<=14:selected.setdefault((p['station'],p['day']),p)
    results=[];profile=Counter();phasebytes=0
    for key,p in sorted(selected.items()):
        start=time.monotonic();r=dict(station=p['station'],day=p['day'],decided_at=p['decided_at'])
        sha=p['chain_audit']['p0_packet_sha'];paths=list((ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'/p['day']).glob(f'{p["station"]}-{sha[:16]}.json.gz')) if sha else []
        if len(paths)!=1:r['state']='MISSING_PACKET';results.append(r);continue
        with gzip.open(paths[0],'rb') as f:b=f.read(2*1024**2+1)
        phasebytes+=len(b)
        if len(b)>2*1024**2 or phasebytes>MAX_BYTES:raise RuntimeError('packet_limit')
        q=json.loads(b)
        if content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})!=sha:raise ValueError('packet_sha_failed')
        proofs=[dict(x) for x in fetch(c,'select * from basket_probability_bridge_history where checkpoint_id=? and state=?',(p['chain_audit']['p0_checkpoint'],'COMMITTED'))]
        proofs=[x for x in proofs if x['station_id']==p['station'] and x['event_date']==p['day'] and utc(x['created_at_utc'])<=utc(p['decided_at']) and json.loads(x['bucket_intervals_json'])==q['bucket_intervals'] and x['settlement_timezone']==q['settlement_timezone']]
        if len(proofs)!=1:r['state']='MISSING_ASOF_GEOMETRY_PROOF';results.append(r);continue
        proof=proofs[0];r.update(packet_sha=sha,geometry_history_id=proof['history_id'],geometry_content_sha=proof['content_sha256'])
        deadline=time.monotonic()+40;c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
        def timeout(signum,frame):raise TimeoutError('compiler_wall_limit')
        signal.signal(signal.SIGALRM,timeout);signal.alarm(45)
        try:
            z=compile_native_ensemble_p0(ArchivedScope(c,q,proof),forward_packet=q,decision_at_utc=p['decided_at'],fresh_seconds=86400)
            r.update(state='COMPILED_ARITHMETIC_ONLY_NOT_OOS',output_keys=sorted(z),output_sha=content_sha256(z))
            # A current arithmetic tag is not proof of refit or nested OOS.
            (HERE/f'probe_{p["station"]}_{p["day"]}.json').write_text(json.dumps(z,ensure_ascii=False,indent=2))
        except Exception as e:r.update(state='REFUSED',reason=type(e).__name__+':'+str(e))
        finally:signal.alarm(0)
        r['elapsed_seconds']=round(time.monotonic()-start,3);results.append(r);profile[r['state']]+=1;print(json.dumps(r),flush=True);guard()
    report=dict(schema='current_p0_archived_scope_compiler_probe_v1',results=results,profile=dict(profile),scores_computed=False,production_modified=False,
                compiler_code_sha256=old.sha_file(ROOT/'bayes_basket_target/native_ensemble_p0.py'),native_oos_code_sha256=old.sha_file(ROOT/'bayes_basket_target/native_ensemble_oos.py'),not_a_complete_p1_replay=True)
    (HERE/'probe_replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
