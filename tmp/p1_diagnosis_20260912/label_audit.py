from common import *
from collections import Counter,defaultdict
c=connect();s=connect('runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db')
rows=query(s,'select station_id,event_date,as_of_utc,root_checkpoint_id,running_high_c,truth_tmax_c,delta_c from metar_phase_hazard_sample',limit=45000)
roots={};stats=Counter();examples=[]
for r in rows:
    cp=r['root_checkpoint_id']
    if cp not in roots:
        found=query(c,'select lineage_json from basket_probability_envelope where checkpoint_id=? limit 1',(cp,),limit=1)
        roots[cp]=json.loads(json.loads(found[0]['lineage_json']).get('bucket_intervals_c_json','{}')) if found else {}
    g=roots[cp]
    def bucket(v):
        found=[b for b,(lo,hi) in g.items() if (lo is None or v>=lo) and (hi is None or v<hi)]
        return found[0] if len(found)==1 else None
    high=bucket(r['running_high_c']);truth=bucket(r['truth_tmax_c'])
    unit='F' if any('°F' in b for b in g) else 'C'
    if high is None or truth is None:stats['missing_geometry']+=1;continue
    stats[unit+'_n']+=1
    degree_rise=r['delta_c']>=1;bucket_rise=truth!=high
    if degree_rise!=bucket_rise:
        stats[unit+'_mismatch']+=1
        if len(examples)<20:examples.append({**r,'current_bucket':high,'truth_bucket':truth,'degree_rise':degree_rise,'bucket_rise':bucket_rise,'geometry':g})
save('label_audit.json',{'stats':dict(stats),'root_n':len(roots),'examples':examples,'query':'select station_id,event_date,as_of_utc,root_checkpoint_id,running_high_c,truth_tmax_c,delta_c from metar_phase_hazard_sample'})
print(dict(stats),'roots',len(roots));print(json.dumps(examples[:3],ensure_ascii=False))
