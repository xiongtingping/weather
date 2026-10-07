"""Predefined strong weights, region slices and legacy-number reconciliation."""
import collections,gzip,json
import numpy as np
from audit import HERE,guard
from experiment import satellite_maps,prepare,pack,update,scores,aggregate

def main():
    rows=[json.loads(l) for l in gzip.open(HERE/'audited_rows.jsonl.gz','rt')]
    data,excluded=prepare(rows,satellite_maps(),2);days=sorted({x['r']['day'] for x in data});test=[x for x in data if x['r']['day'] in days[12:]]
    p,s,u,w,k,z=pack(test);out={}
    for b in (0.,1.,2.,4.,8.):
        guard();q=update(p,s,u,z,[b,0.]);delta=scores(q,k,w)-scores(p,k,w);rs=[]
        for x,v in zip(test,delta):
            r=x['r'];rs.append(dict(day=r['day'],station=r['station'],rad=x['rad'],version=r['posterior_version'],delta=v.tolist(),hour=r['local_hour']))
        out[str(b)]=dict(all_mixed_descriptive_only=aggregate(rs),current_v38=aggregate([r for r in rs if r['version']=='p1_joint_likelihood_v38']),dark_current_v38=aggregate([r for r in rs if r['version']=='p1_joint_likelihood_v38' and r['rad']<-.2]),dark_mixed_descriptive_only=aggregate([r for r in rs if r['rad']<-.2]),bright_mixed_descriptive_only=aggregate([r for r in rs if r['rad']>.2]),zuuu_history=aggregate([r for r in rs if r['station']=='ZUUU']),by_version={v:aggregate([r for r in rs if r['version']==v]) for v in sorted({r['version'] for r in rs})})
    legacy=json.load(open(HERE.parent/'obs_increment_p1_20261007/walk_forward.json'))['results'];sd=collections.defaultdict(list)
    for r in legacy:sd[(r['station'],r['day'])].append([r['log_new']-r['log_base'],r['brier_new']-r['brier_base']])
    reconciliation=dict(mean_station_day=np.mean([np.mean(v,0) for v in sd.values()],0).tolist(),station_days=len(sd),days=len({d for st,d in sd}),role='only reproduces arithmetic; does not validate timing or lineage')
    (HERE/'stress.json').write_text(json.dumps(dict(forced=out,legacy_reconciliation=reconciliation,exclusions=excluded,selected_winner=False,activation_eligible=False),ensure_ascii=False,indent=2))
    print('legacy',reconciliation)
    for b,v in out.items():print('forced',b,'current',v['current_v38']['delta']['logscore'],'dark',v['dark_current_v38'].get('delta'))

if __name__=='__main__':main()
