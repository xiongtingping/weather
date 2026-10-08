"""Bounded exact-SHA search across archive dates; never replace parent inputs."""
import collections
import gzip
import json
from rebuild import HERE,ROOT,old,fetch,guard,MAX_BYTES,utc
from bayes_basket_target.contracts import content_sha256


def main():
    guard();missing={};pairs={};used=0
    for path in sorted((HERE/'p0_roots').glob('*.jsonl.gz')):
        stage=0
        for line in gzip.open(path,'rt'):
            stage+=len(line.encode())
            if stage>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('missing_roots_input_limit')
            r=json.loads(line)
            if r.get('reason','').startswith('FileNotFoundError:'):missing[r['original_checkpoint']]=r
    for line in gzip.open(HERE/'raw_pairs.jsonl.gz','rt'):
        used+=len(line.encode())
        if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('missing_pairs_input_limit')
        p=json.loads(line);cp=p['chain_audit']['p0_checkpoint']
        if cp in missing:pairs.setdefault(cp,p)
    if set(missing)!=set(pairs):raise ValueError('missing_parent_pair_not_bound')
    filenames=collections.defaultdict(list)
    for cp,p in pairs.items():filenames[p['station']+'-'+p['chain_audit']['p0_packet_sha'][:16]+'.json.gz'].append(cp)
    archive=ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive';matches=collections.defaultdict(list);profile=collections.Counter();scanbytes=0
    for daydir in sorted(archive.iterdir()):
        if not daydir.is_dir():continue
        profile['date_directories']+=1
        for path in daydir.iterdir():
            scanbytes+=len(str(path).encode());profile['filenames_scanned']+=1
            if scanbytes>MAX_BYTES:raise RuntimeError('archive_filename_scan_limit')
            if path.name in filenames:
                with gzip.open(path,'rb') as inp:body=inp.read(2*1024**2+1)
                if len(body)>2*1024**2:raise RuntimeError('exact_packet_input_limit')
                q=json.loads(body);sha=content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})
                for cp in filenames[path.name]:
                    p=pairs[cp]
                    if sha!=p['chain_audit']['p0_packet_sha']:raise ValueError('archive_prefix_collision_or_packet_corruption')
                    matches[cp].append(dict(path=str(path.relative_to(ROOT)),sha=sha,station=q['station_id'],event=q['event_date'],received_at=q['received_at_utc']))
        guard()
    c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');rows=[]
    for cp,r in sorted(missing.items()):
        es=list(fetch(c,"select created_at,decision_at_utc from basket_probability_envelope where checkpoint_id=? and stage='P0' and state='COMMITTED'",(cp,)))
        if len(es)!=1:raise ValueError('missing_original_parent_not_unique')
        e=es[0];p=pairs[cp];found=matches.get(cp,[])
        for x in found:
            if x['station']!=p['station'] or x['event']!=p['day'] or utc(x['received_at'])>utc(e['created_at']):raise ValueError('relocated_packet_scope_or_clock_failed')
        state='EXACT_SHA_RELOCATED' if found else 'EXACT_SHA_NOT_IN_LOCAL_ARCHIVE';profile[state]+=1
        rows.append(dict(station=r['station'],day=r['day'],checkpoint=cp,packet_sha=p['chain_audit']['p0_packet_sha'],parent_available_at=e['created_at'],state=state,matches=found))
    result=dict(schema='missing_exact_parent_packet_archive_search_v1',counts=dict(profile),scan_path_bytes=scanbytes,rows=rows,production_modified=False,replacement_inputs_used=False)
    out=json.dumps(result,ensure_ascii=False,indent=2)
    if len(out.encode())>2*1024**2:raise RuntimeError('missing_search_output_limit')
    (HERE/'missing_packets.json').write_text(out);print(json.dumps(result['counts']),flush=True)


if __name__=='__main__':main()
