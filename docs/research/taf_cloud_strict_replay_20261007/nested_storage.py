"""Lossless content-addressed storage for repeated nested evaluations.

Only serialization changes. Expanded objects retain the original canonical
SHA and the original 2 MiB bound. References must resolve exactly or reject.
"""
import gzip
import json
from rebuild import HERE, guard
from bayes_basket_target.contracts import content_sha256

LIMIT=2*1024**2
SCHEMA='nested_compact_evaluation_refs_v1'


def body_of(obj):
    body=json.dumps(obj,ensure_ascii=False).encode()
    if len(body)>LIMIT:raise RuntimeError('nested_storage_expanded_object_limit')
    return body


def physical_read(path):
    guard()
    with gzip.open(path,'rb') as inp:body=inp.read(LIMIT+1)
    if len(body)>LIMIT:raise RuntimeError('nested_storage_physical_object_limit')
    return json.loads(body)


def shared_path(sha):
    if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('nested_storage_reference_identity')
    return HERE/'nested_shared_objects'/(sha+'.json.gz')


def read_object(path,expected_sha):
    record=physical_read(path)
    if 'storage_schema' in record:
        if record['storage_schema']!=SCHEMA or set(record)!={'storage_schema','expanded_sha','payload','evaluation_refs'} or record['expanded_sha']!=expected_sha:
            raise ValueError('nested_storage_schema_or_identity')
        obj=record['payload']
        if not isinstance(obj,dict) or 'system_evaluations' in obj or not isinstance(record['evaluation_refs'],dict):
            raise ValueError('nested_storage_payload_conflict')
        evaluations={}
        for system,sha in record['evaluation_refs'].items():
            evaluation=physical_read(shared_path(sha))
            if content_sha256(evaluation)!=sha or evaluation.get('system_id')!=system:
                raise ValueError('nested_storage_evaluation_identity')
            evaluations[system]=evaluation
        obj['system_evaluations']=evaluations
    else:obj=record
    body=body_of(obj)
    if content_sha256(obj)!=expected_sha:raise ValueError('nested_storage_expanded_identity')
    return obj,len(body)


def write_atomic(path,body):
    guard();path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp.gz')
    with gzip.open(tmp,'wb') as out:out.write(body)
    tmp.replace(path)


def store_object(path,obj):
    expanded=body_of(obj);sha=content_sha256(obj)
    if 'storage_schema' in obj:raise ValueError('nested_storage_reserved_key')
    if 'system_evaluations' not in obj:
        write_atomic(path,expanded);return sha
    refs={}
    for system,evaluation in sorted(obj['system_evaluations'].items()):
        if evaluation.get('system_id')!=system:raise ValueError('nested_storage_evaluation_scope')
        body=body_of(evaluation);identity=content_sha256(evaluation);target=shared_path(identity)
        if target.exists():
            if content_sha256(physical_read(target))!=identity:raise ValueError('nested_storage_shared_corruption')
        else:write_atomic(target,body)
        refs[system]=identity
    record=dict(storage_schema=SCHEMA,expanded_sha=sha,
        payload={k:v for k,v in obj.items() if k!='system_evaluations'},evaluation_refs=refs)
    write_atomic(path,body_of(record))
    restored,_=read_object(path,sha)
    if restored!=obj:raise ValueError('nested_storage_round_trip_failed')
    return sha


def referenced_shared_paths(path):
    record=physical_read(path)
    if 'storage_schema' not in record:return set()
    if record['storage_schema']!=SCHEMA:raise ValueError('nested_storage_unknown_schema')
    return {shared_path(sha).resolve() for sha in record['evaluation_refs'].values()}
