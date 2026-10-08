"""Remove only this study's content-addressed objects with no surviving refs."""
import gzip
import json
from rebuild import HERE,guard,MAX_BYTES
from bayes_basket_target.contracts import content_sha256
from nested_storage import referenced_shared_paths


def remove_unreferenced(folder,keep):
    removed=0;size=0
    for path in folder.rglob('*.json.gz'):
        guard()
        if path.resolve() in keep:continue
        if path.is_symlink():raise ValueError('research_cleanup_symlink_refused')
        size+=path.stat().st_size;path.unlink();removed+=1
    return dict(removed_objects=removed,removed_compressed_bytes=size)


def main():
    guard();keep=set();shared_keep=set()
    for path in (HERE/'nested_oos').glob('*/*/summary.json'):
        s=json.loads(path.read_text())
        if s.get('method_version')!='actual_arrival_inner_truth_ladder_v3' or 'report_header' not in s:raise ValueError('cleanup_requires_complete_current_nested_reports')
        for ref in s['objects']:
            target=HERE/ref['path'];keep.add(target.resolve());shared_keep.update(referenced_shared_paths(target))
    result=dict(nested=remove_unreferenced(HERE/'nested_oos',keep),nested_shared=remove_unreferenced(HERE/'nested_shared_objects',shared_keep));keep=set()
    for path in (HERE/'p0_refitted_roots').glob('*.jsonl.gz'):
        used=0
        for line in gzip.open(path,'rt'):
            used+=len(line.encode())
            if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('research_cleanup_reference_limit')
            r=json.loads(line)
            for ref in r.get('current_parameter_refits',{}).values():keep.add((HERE/ref['path']).resolve())
    result['refit_manifests']=remove_unreferenced(HERE/'p0_refit_manifests',keep);keep=set()
    for path in (HERE/'prefix_refitted_rows').glob('*.jsonl.gz'):
        used=0
        for line in gzip.open(path,'rt'):
            used+=len(line.encode())
            if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('research_cleanup_prefix_limit')
            r=json.loads(line)
            if 'p0' in r:keep.add((HERE/'p0_envelope_cache'/(content_sha256(r['p0'])+'.json.gz')).resolve())
    result['p0_envelope_cache']=remove_unreferenced(HERE/'p0_envelope_cache',keep);removed=[]
    for name in ('restricted_baseline_fit.json','nested_compiler_probe.json'):
        p=HERE/name
        if p.exists():p.unlink();removed.append(name)
    result.update(removed_superseded_debug_outputs=removed,production_modified=False)
    (HERE/'cleanup.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result),flush=True)


if __name__=='__main__':main()
