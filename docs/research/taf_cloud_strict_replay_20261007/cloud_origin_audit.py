"""The separate cloud observation must have its own visible official origin."""
import collections
import gzip
import json
from rebuild import HERE,ROOT,old,fetch,utc,guard,MAX_BYTES
from bayes_basket_target.contracts import content_sha256


def main():
    guard();a=old.connect(ROOT/'data/archive.db');counts=collections.Counter();stations={};bad=[];dst=HERE/'cloud_origin_eligibility';dst.mkdir(exist_ok=True)
    for path in sorted((HERE/'prefix_refitted_rows').glob('*.jsonl.gz')):
        used=0;sc=collections.Counter();eligibility={}
        for line in gzip.open(path,'rt'):
            used+=len(line.encode())
            if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('cloud_origin_audit_input_limit')
            r=json.loads(line)
            if r['state']=='REFUSED':continue
            sc['rows']+=1
            origins=list(fetch(a,"select source_lane,station_id,origin_source_id,origin_transport,committed_at from reported_trajectory_observation_origins where observation_id=?",(r['observation_id'],)))
            visible=[x for x in origins if x['source_lane']=='awc' and x['station_id']==r['station'] and x['origin_source_id'] in ('global_noaa_awc_relay','us_noaa_awc','global_noaa_tgftp_relay') and str(x['origin_transport']).startswith(('official_awc_','official_noaa_tgftp_')) and utc(x['committed_at'])<=utc(r['as_of'])]
            if len(visible)!=1:
                sc['not_unique_visible_official_origin']+=1
                if len(bad)<20:bad.append(dict(station=r['station'],day=r['day'],at=r['as_of'],observation=r['observation_id'],origins=[dict(x) for x in origins]))
            else:sc['visible_official_cloud_origin']+=1
            sc['cloud_observation_is_temperature_prefix_member:'+str(r['observation_id'] in r['sample']['prefix_ids'])]+=1
            key=content_sha256([r['station'],r['day'],r['as_of'],r['observation_id']]);eligibility[key]=dict(eligible=len(visible)==1,origins=[dict(x) for x in origins])
        body=json.dumps(eligibility).encode()
        if len(body)>2*1024**2:raise RuntimeError('cloud_origin_eligibility_object_limit')
        station=path.name.split('.')[0];output=dst/(station+'.json.gz')
        with gzip.open(output,'wb') as inp:inp.write(body)
        stations[station]=dict(counts=dict(sc),input_sha=old.sha_file(path),eligibility_path=str(output.relative_to(HERE)),eligibility_sha=content_sha256(eligibility));counts.update(sc);guard()
    report=dict(schema='independent_cloud_observation_official_origin_audit_v1',passed=counts['not_unique_visible_official_origin']==0,counts=dict(counts),stations=stations,exceptions=bad,production_modified=False,definition='The cloud item is a separate actual official report; a non-00 METAR may provide cloud evidence without being admitted as canonical temperature truth.')
    (HERE/'cloud_origin_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report['counts']),flush=True)


if __name__=='__main__':main()
