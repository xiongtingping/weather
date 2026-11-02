"""Bounded optional source capture for clock-boundary examples; not OOS."""
import json
from review_checks import Guard, connect, OUT, unpack, save


def main():
    guard=Guard();conn=connect(guard)
    artifact=json.loads((OUT/'p1_observation_likelihood_artifact.json').read_text())
    out=[]
    query="""select checkpoint_id,decision_at_utc,
    json_extract(lineage_json,'$.p1_observation_likelihood_json') receipt
    from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx
    where station_id=? and stage=? and state=? order by decision_at_utc desc limit 150"""
    for station in ['KSEA','MMMX','KHOU']:
        candidates=[]
        for r in conn.execute(query,(station,'P1','COMMITTED')):
            if not r['receipt']:continue
            receipt=json.loads(r['receipt'])
            if receipt.get('artifact_sha256')!=artifact['content_sha256'] or 'clock_hours' not in receipt:continue
            h=receipt['clock_hours'];candidates.append((min(abs(h-6),abs(h-10)),r['checkpoint_id'],h))
        if not candidates:continue
        distance,key,h=min(candidates)
        r=conn.execute('select * from basket_probability_envelope where checkpoint_id=?',(key,)).fetchone()
        row=unpack(conn,r);rootid=row['lineage']['prediction_root_checkpoint_id']
        roots=conn.execute('select * from basket_probability_envelope where checkpoint_id=?',(rootid,)).fetchall()
        exact=[r for r in roots if r['content_sha256']==row['lineage']['prediction_root_content_sha256']]
        if len(exact)!=1:raise RuntimeError('root_mismatch')
        out.append({'distance_h':distance,'row':row,'root':unpack(conn,exact[0])})
        guard.check()
    save('near_boundary_rows.json',out);print('captured',len(out))


if __name__=='__main__':main()
