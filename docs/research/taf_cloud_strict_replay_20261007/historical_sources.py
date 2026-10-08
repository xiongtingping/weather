"""Read-only actual-arrival observation arbitration and regional features.

No current coverage row or inferred publication lag proves past availability.
The existing adapter artifacts are audited, not relabelled across products.
"""
import collections
import json
from urllib.parse import urlparse
from rebuild import HERE,MAX_BYTES,fetch,utc,stamp,guard
from upstream import day_clock
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.regional_features import regional_features_at,p1_reads_pair
from bayes_basket_target.regional_point_lane import retired_cell_row,cell_evidence_held

REGIONAL_ORIGINS={
    'DWD_ICON_EU_POINT':('DWD_OPENDATA_ICON_EU',{'opendata.dwd.de'}),
    'NCEP_HRRR_POINT':('NOAA_HRRR_PDS_S3',{'noaa-hrrr-bdp-pds.s3.amazonaws.com'}),
    'NCEP_NBM_POINT':('NOAA_NBM_PDS_S3',{'noaa-nbm-grib2-pds.s3.amazonaws.com'}),
    'JMA_MSM_POINT':('RISH_KYOTO_JMA_ARCHIVE',{'database.rish.kyoto-u.ac.jp'}),
}


def regional_rows(c,station,day,tz):
    out=[];profile=collections.Counter();used=0
    for rr in fetch(c,'select * from calibration_regional_point_run where station_id=? and target_local_date=? order by run_init_utc desc,archive_query_at_utc desc',(station,day)):
        r=dict(rr);used+=len(json.dumps(r).encode())
        if used>MAX_BYTES:raise RuntimeError('regional_input_limit')
        profile['rows']+=1;sid=r['ensemble_system_id'];origin=REGIONAL_ORIGINS.get(sid)
        if not p1_reads_pair(sid,station):profile['not_p1_pair']+=1;continue
        if origin is None or r['transport_provider_id']!=origin[0] or urlparse(r['source_url']).hostname not in origin[1]:profile['not_admissible_origin']+=1;continue
        if r['settlement_timezone']!=tz:raise ValueError('regional_timezone_mismatch')
        if retired_cell_row(sid,station,r['extraction_version']):profile['retired_cell']+=1;continue
        curve=json.loads(r['hourly_c_json'])
        identity=dict(schema_version=r['schema_version'],ensemble_system_id=sid,station_id=station,target_local_date=day,run_init_utc=r['run_init_utc'],hourly_c=curve,extraction_version=r['extraction_version'])
        if content_sha256(identity)!=r['content_sha256']:raise ValueError('regional_raw_identity_failed')
        arrival=max(utc(r['archive_query_at_utc']),utc(r['run_init_utc']))
        out.append(dict(ensemble_system_id=sid,run_init_utc=r['run_init_utc'],hourly_c=curve,knowable_at_utc=stamp(arrival),actual_fetch_at=r['archive_query_at_utc'],content_sha256=r['content_sha256'],transport_provider_id=r['transport_provider_id'],source_url=r['source_url'],extraction_version=r['extraction_version']))
        profile['source_eligible']+=1
    guard();return out,dict(profile),used


def regional_at(rows,station,at,high,latest):
    visible=[r for r in rows if utc(r['knowable_at_utc'])<=at and not cell_evidence_held(r['ensemble_system_id'],station,stamp(at))]
    features=regional_features_at(visible,as_of_utc=stamp(at),running_high_c=high,latest_observed_c=latest,station_id=station,calibrate=False)
    selected=[r for r in visible if r['ensemble_system_id']==features['regional_system_id'] and r['run_init_utc']==features['regional_run_init_utc']]
    return features,selected[:1]


def coverage(rows,start,through,step,internal,edge=600):
    by={}
    for r in rows:
        if start<=r['_valid']<=through and r['_arrival']<=through:
            key=r['_valid'];old=by.get(key)
            if old is None or (r['_arrival'],r['id'])>(old['_arrival'],old['id']):by[key]=r
    values=[by[k] for k in sorted(by)];expected=max(1,int((through-start).total_seconds()//step)+1)
    gaps=[(b['_valid']-a['_valid']).total_seconds() for a,b in zip(values,values[1:])]
    first=(values[0]['_valid']-start).total_seconds() if values else None
    last=(through-values[-1]['_valid']).total_seconds() if values else None
    ratio=len(values)/expected
    complete=bool(values) and first<=edge and last<=edge and max(gaps,default=0)<=internal and ratio>=.90
    return values,dict(complete=complete,rows=len(values),expected=expected,ratio=ratio,first_gap_seconds=first,last_gap_seconds=last,max_internal_gap_seconds=max(gaps,default=0))


class HistoricalAsos:
    def __init__(self,a,station,tz):
        self.station=station;self.tz=tz;self.rows={'IEM':[],'MADIS':[]};self.used=0
        report=json.loads((HERE/'asos_audit.json').read_text())['stations'][station]
        # Exact source and measurement binding is mandatory. A correctly
        # bound adapter may later acquire a current probability OOS binding.
        self.artifacts=report['artifacts']
        for lane,sql in (
            ('IEM','select * from reported_trajectory_asos_one_minute_observations where station_id=?'),
            ('MADIS','select o.*,b.source_lane,b.source_class,b.receipt_sha256,b.fetch_started_at_utc,b.fetch_completed_at_utc,b.object_url from reported_trajectory_madis_hfmetar_observations o left join reported_trajectory_madis_hfmetar_objects b on b.compressed_sha256=o.compressed_sha256 where o.station_id=?')):
            for rr in fetch(a,sql,(station,)):
                r=dict(rr);self.used+=len(json.dumps(r).encode())
                if self.used>MAX_BYTES:raise RuntimeError('asos_source_input_limit')
                if lane=='IEM':
                    if r['source_lane']!='asos_1min' or r['provider_id']!='IEM_ASOS1MIN' or r['source_class']!='aggregator_republisher' or r['measurement_semantics']!='ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF':raise ValueError('iem_product_schema_mismatch')
                    v=r['valid_at'];arrival=max(utc(r[k]) for k in ('fetch_completed_at','canonical_committed_at'));identity=r['observation_id'];temp=r['temperature_c'];receipt=r['source_receipt_sha256']
                else:
                    if not r['qc_eligible']:continue
                    if r['source_class']!='official_republisher' or r['measurement_semantics']!='ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES':raise ValueError('madis_product_schema_mismatch')
                    if not r['fetch_completed_at_utc'] or not r['receipt_sha256']:raise ValueError('madis_actual_receipt_missing')
                    v=r['observed_at_utc'];arrival=max(utc(r[k]) for k in ('fetch_completed_at_utc','canonical_committed_at_utc'));identity=r['content_sha256'];temp=r['temperature_c'];receipt=r['receipt_sha256']
                self.rows[lane].append(dict(id=identity,_valid=utc(v),_arrival=max(arrival,utc(v)),temperature_c=temp,receipt_sha=receipt))
        guard()

    def at(self,day,at):
        start=day_clock(day,self.tz);through=min(at,day_clock(day,self.tz,True));proof={};qualified=[]
        for lane,step,gap,priority in [('IEM',60,300,220),('MADIS',300,600,210)]:
            values,cv=coverage(self.rows[lane],start,through,step,gap)
            declared,product=('asos_1min','ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF') if lane=='IEM' else ('MADIS_HFMETAR_ASOS','ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES')
            artifacts=[a for a in self.artifacts if utc(a['committed'])<=at and a['state']=='OOS_READY'
                       and a.get('declared_lane')==declared and a.get('train',0)>=20 and a.get('holdout',0)>=8
                       and set(a.get('matched_measurements',{}))=={product}
                       and sum(a.get('matched_measurements',{}).values())>=a.get('train',0)+a.get('holdout',0)
                       and not [g for g in a['gaps'] if g!='temperature_adapter_not_complete_current_p1_probability_oos']]
            cv['historical_current_p1_artifact_shas']=[a['sha'] for a in artifacts]
            cv['artifact_role']='exact_source_temperature_adapter; complete current probability chain separately required'
            cv['qualification_complete']=bool(artifacts);proof[lane]=cv
            if cv['complete'] and artifacts:qualified.append((priority,lane,values))
        winner=max(qualified,default=(100,'AWC',[]),key=lambda x:x[0])
        return dict(selected_lane=winner[1],priority=winner[0],source_policy='IEM220_MADIS210_AWC100_unique_source',candidates=proof),winner[2]
