"""Remove repeated serialization after exact expanded-object verification."""
import collections
import json
from rebuild import HERE,MAX_BYTES,old,guard
from nested_storage import read_object,store_object


def main():
    guard();records={};counts=collections.Counter();before=after=0
    shared=HERE/'nested_shared_objects';shared_before=sum(p.stat().st_size for p in shared.glob('*.json.gz')) if shared.exists() else 0
    for path in sorted((HERE/'nested_oos').glob('*/*/summary.json')):
        summary=json.loads(path.read_text());used=0;sc=collections.Counter();oldbytes=newbytes=0
        for ref in summary['objects']:
            target=HERE/ref['path'];obj,size=read_object(target,ref['sha256']);used+=size
            if used>MAX_BYTES:raise RuntimeError('nested_compaction_station_phase_limit')
            oldbytes+=target.stat().st_size
            if ref['kind']=='cross':store_object(target,obj);sc['cross_objects']+=1
            else:sc['other_objects']+=1
            restored,_=read_object(target,ref['sha256'])
            if restored!=obj:raise ValueError('nested_compaction_identity_failed')
            newbytes+=target.stat().st_size
        key=summary['station']+'/'+summary['event'];records[key]=dict(counts=dict(sc),summary_sha=old.sha_file(path),expanded_verified_bytes=used,physical_before=oldbytes,physical_after=newbytes)
        before+=oldbytes;after+=newbytes;counts.update(sc);print(key,'before',oldbytes,'after',newbytes,flush=True);guard()
    shared_after=sum(p.stat().st_size for p in shared.glob('*.json.gz'))
    result=dict(schema='lossless_nested_storage_compaction_v1',passed=True,counts=dict(counts),records=records,
        physical_before=before+shared_before,physical_after=after+shared_after,
        physical_bytes_saved=before+shared_before-after-shared_after,
        original_expanded_identities_preserved=True,production_modified=False)
    (HERE/'nested_compaction.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='records'}),flush=True)


if __name__=='__main__':main()
