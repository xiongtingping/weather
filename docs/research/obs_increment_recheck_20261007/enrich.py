"""Exact parent versions and raw observation lineage, without guessed joins."""
import collections,gzip,hashlib,json,os
from audit import HERE,ROOT,guard,connection,query,doc

def main():
    c=connection(ROOT/'data/bayes_basket_target_ssot.db');a=connection(ROOT/'data/archive.db');cache={};versions=collections.defaultdict(collections.Counter);lanes=collections.Counter()
    def root(checkpoint):
        if not checkpoint:return None
        if checkpoint not in cache:
            vs=list(query(c,"SELECT DISTINCT stage,posterior_version FROM basket_probability_envelope WHERE checkpoint_id=? AND state='COMMITTED'",(checkpoint,)))
            if len(vs)>1:raise ValueError('ambiguous_checkpoint_version')
            cache[checkpoint]=vs[0] if vs else None
        return cache[checkpoint]
    tmp=HERE/'audited_rows.partial.gz'
    with gzip.open(HERE/'audited_rows.jsonl.gz','rt') as inp,gzip.open(tmp,'wt') as out:
        for line in inp:
            guard();r=json.loads(line)
            r['evidence_role']='observational_candidate_not_oos'
            row=query(c,'SELECT lineage_json FROM basket_probability_envelope WHERE envelope_id=?',(r['envelope_id'],)).fetchone()
            if row is None:raise ValueError('missing_envelope')
            L=doc(c,row[0]);r['root_versions']={}
            for key in ('p0_root_checkpoint_id','prediction_root_checkpoint_id'):
                v=root(L.get(key))
                if v:r['root_versions'][v[0]]=v[1]
            prediction=root(L.get('prediction_root_checkpoint_id'))
            r['prediction_root_stage']=prediction[0] if prediction else None
            for stage,v in r['root_versions'].items():versions[stage][v]+=1
            for stage in ('P0','P0-M'):
                if stage not in r['root_versions']:
                    versions[stage]['NOT_USED_DIRECT_P0_ROOT' if stage=='P0-M' and r['prediction_root_stage']=='P0' else 'MISSING_EXPLICIT_PARENT']+=1
            row=query(a,'SELECT source_lane,report_type FROM reported_trajectory_observations WHERE observation_id=?',(r['observation_id'],)).fetchone()
            if row is None:raise ValueError('missing_observation')
            r['observation_source_lane'],r['observation_report_type']=row;lanes[row[0]]+=1
            out.write(json.dumps(r,ensure_ascii=False)+'\n')
    os.replace(tmp,HERE/'audited_rows.jsonl.gz')
    audit=json.load(open(HERE/'audit.json'));audit['parent_version_rows']={k:dict(v) for k,v in versions.items()};audit['observation_source_lanes']=dict(lanes);audit['audited_rows_sha']=hashlib.sha256((HERE/'audited_rows.jsonl.gz').read_bytes()).hexdigest();(HERE/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
    print(json.dumps(dict(parent_versions=audit['parent_version_rows'],observation_source_lanes=dict(lanes)),indent=2))

if __name__=='__main__':main()
