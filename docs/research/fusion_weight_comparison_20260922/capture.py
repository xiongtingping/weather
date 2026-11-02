"""Recreate the bounded indexed envelope enrichment; no production writes."""
import collections,json
from pathlib import Path
import compare as C
from bayes_basket_target.materialization import rehydrate_lineage_documents

def main():
    g=C.study.Guard();c=C.study.connect(g)
    samples=json.loads((C.HERE.parent/'p0_physical_20260920/samples.json').read_text());out=[];counts=collections.Counter()
    for s in samples:
        rows=c.execute('select lineage_json,content_sha256 from basket_probability_envelope indexed by basket_probability_envelope_checkpoint_idx where checkpoint_id=? and stage=? limit 2',(s['checkpoint'],'P0')).fetchall()
        if len(rows)!=1 or rows[0]['content_sha256']!=s['p0_sha']:raise RuntimeError('frozen_envelope_missing_or_changed')
        l=rehydrate_lineage_documents(c,json.loads(rows[0]['lineage_json']));dec=C.study.decoded
        snap=l.get('native_forward_snapshot_sha256','')
        path=f"data/runtime/bayes_basket_target_nwp/forward_packet_archive/{s['event_date']}/{s['station']}-{snap[:16]}.json.gz"
        out.append(dict(sample_sha=s['sample_sha'],station=s['station'],day=s['event_date'],system_probabilities=dec(l.get('native_system_probabilities_json') or '{}'),packet_path=path,packet_exists=(C.ROOT/path).exists(),families=dec(l.get('system_family_ids_json') or '{}'),caps=dec(l.get('family_weight_caps_json') or '{}'),lead=dec(l.get('hour_lead_bucket_by_system_json') or '{}'),artifacts=dec(l.get('system_artifact_shas_json') or '{}'),provisional=dec(l.get('provisional_uncalibrated_system_ids_json') or '[]')))
    C.study.save('enrichment.json',out)
    print('enriched',len(out))
if __name__=='__main__':main()
