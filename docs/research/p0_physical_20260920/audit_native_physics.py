"""Bounded, read-only inventory; scope matching is not OOS certification."""
import collections
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from study import Guard, ROOT, OUT, FIRST, LAST, connect, decoded, stamp, iso, save
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.materialization import rehydrate_lineage_documents

PREFIX = {'gefs': 'NOAA_GEFS_05', 'ecmwf': 'ECMWF_IFS_ENS_OPEN_PF50',
          'eccc': 'ECCC_GEM_GLOBAL_ENS', 'icon': 'DWD_ICON_GLOBAL_EPS'}
SOURCES = {'NOAA_GEFS_05':'https://noaa-gefs-pds.s3.amazonaws.com/gefs.',
           'ECMWF_IFS_ENS_OPEN_PF50':'https://storage.googleapis.com/ecmwf-open-data/',
           'ECCC_GEM_GLOBAL_ENS':'https://dd.weather.gc.ca/',
           'DWD_ICON_GLOBAL_EPS':'https://opendata.dwd.de/weather/nwp/icon-eps/grib/'}


def require_hash(obj):
    if obj.get('content_sha256') != content_sha256({k:v for k,v in obj.items() if k!='content_sha256'}):
        raise ValueError('content_hash_mismatch')


def check_curve(capture, sample, run):
    """Require exact scope/run/receipt, not just a same-day curve."""
    require_hash(capture)
    curve = capture.get('family_temperature_curve')
    if not curve: raise ValueError('curve_missing')
    require_hash(curve)
    sid = capture['ensemble_system_id']
    if (capture.get('source_class')!='OFFICIAL_NATIVE_ARCHIVE' or sid not in SOURCES
            or not capture.get('source_prefix','').startswith(SOURCES[sid])):
        raise ValueError('non_native_source')
    for key, expected in [('station_id',sample['station']),('event_date',sample['event_date']),
                          ('settlement_timezone',sample['timezone']),('ensemble_system_id',sid)]:
        if capture.get(key)!=expected or curve.get(key)!=expected: raise ValueError('scope_mismatch')
    if stamp(capture['run_init_utc'])!=stamp(run): raise ValueError('run_mismatch')
    arrival=stamp(capture['archive_query_at_utc'])
    if arrival!=stamp(sample['root_receipts'][sid]): raise ValueError('receipt_mismatch')
    if arrival>stamp(sample['p0_nominal_decision_at']) or stamp(curve['available_at_utc'])>stamp(sample['decision_at']):
        raise ValueError('future_receipt')
    if stamp(curve['available_at_utc'])!=arrival: raise ValueError('curve_arrival_mismatch')
    if stamp(capture['run_init_utc'])>arrival: raise ValueError('run_after_receipt')
    if curve['source_field_receipt_set_sha256']!=capture['field_receipt_set_sha256']:
        raise ValueError('receipt_hash_mismatch')
    if 'field_receipts' in capture and content_sha256(capture['field_receipts'])!=capture['field_receipt_set_sha256']:
        raise ValueError('receipt_set_corrupt')
    points=curve['points'];times=[stamp(p['valid_time_utc']) for p in points]
    if not points or len(points)>100 or times!=sorted(set(times)): raise ValueError('curve_time_order')
    if any(t.astimezone(ZoneInfo(sample['timezone'])).date().isoformat()!=sample['event_date'] for t in times):
        raise ValueError('curve_wrong_day')
    if any(not math.isfinite(p['temperature_c']) for p in points): raise ValueError('nonfinite_temperature')
    # Forecast valid times can legitimately be AFTER the decision time.
    return curve


def main():
    guard=Guard();samples=json.loads((OUT/'samples.json').read_text());conn=connect(guard)
    directory=ROOT/'data/runtime/bayes_basket_target_nwp/official_native_capture'
    requested=collections.defaultdict(list); roots={};counts=collections.Counter();issues=[]
    for s in samples:
        rows=conn.execute('select content_sha256,lineage_json from basket_probability_envelope where checkpoint_id=? and stage=?',
                          (s['checkpoint'],'P0')).fetchall()
        matches=[r for r in rows if r['content_sha256']==s['p0_sha']]
        if len(matches)!=1: raise ValueError('frozen_root_missing_or_ambiguous')
        lineage=rehydrate_lineage_documents(conn,decoded(matches[0]['lineage_json']))
        runs=decoded(lineage.get('run_init_utc_by_system_json') or '{}')
        curve=decoded(lineage.get('conditional_future_temperature_curve_json') or 'null')
        roots[s['sample_sha']]={'runs':runs,'matches':[]}
        counts['root_curve_present' if curve else 'root_curve_missing']+=1
        for sid in s['weights']:
            counts['system_scopes']+=1
            if sid not in runs: counts['root_native_run_missing']+=1;continue
            requested[(s['station'],s['event_date'],sid)].append((s,runs[sid]))
        guard.check()
    inventory=[];total=0; raw_examples={}; curves=[]
    # Bounded canonical directory and four known packet naming patterns only.
    for prefix,sid in PREFIX.items():
        paths=sorted(directory.glob(prefix+'_????-??-??_d?.json'))
        if len(paths)>1000: raise RuntimeError('packet_count_limit')
        for path in paths:
            day=path.name[len(prefix)+1:len(prefix)+11]
            if not FIRST<=day<=LAST: continue
            size=path.stat().st_size;total+=size
            if size>24*1024**2 or total>4*1024**3: raise RuntimeError('input_byte_limit')
            raw=path.read_bytes();packet=json.loads(raw);file_sha=hashlib.sha256(raw).hexdigest();del raw
            if packet.get('ensemble_system_id')!=sid: raise ValueError('packet_model_mismatch')
            require_hash(packet)
            info={'path':str(path.relative_to(ROOT)),'file_sha256':file_sha,'bytes':size,'system':sid,
                  'run':packet['run_init_utc'],'event_date':day,'captures':len(packet['captures']),
                  'curves':0,'matched_scopes':0,'source_prefixes':[]}
            prefixes=set()
            for cap in packet['captures']:
                prefixes.add(cap.get('source_prefix',''))
                if cap.get('family_temperature_curve'): info['curves']+=1
                for r in cap.get('field_receipts',[])[:1]:
                    raw_examples.setdefault(prefix,[])
                    if len(raw_examples[prefix])<3: raw_examples[prefix].append(r)
                key=(cap['station_id'],cap['event_date'],sid)
                for sample,run in requested.get(key,[]):
                    try: curve=check_curve(cap,sample,run)
                    except (ValueError,KeyError) as e:
                        counts['candidate_rejection:'+str(e)]+=1
                        continue
                    row={'sample_sha':sample['sample_sha'],'system':sid,'run':run,'packet':info['path'],
                         'packet_file_sha256':file_sha,'capture_sha256':cap['content_sha256'],
                         'curve_sha256':curve['content_sha256'],'receipt_set_sha256':cap['field_receipt_set_sha256'],
                         'available_at':curve['available_at_utc'],'points':curve['points'],
                         'semantics':'ensemble_mean_of_3h_maxima' if prefix=='ecmwf' else 'ensemble_mean_instantaneous_t2m',
                         'status':'SCOPE_MATCHED_NOT_OOS_CERTIFIED'}
                    roots[sample['sample_sha']]['matches'].append(sid);curves.append(row);info['matched_scopes']+=1
            info['source_prefixes']=sorted(prefixes);inventory.append(info);guard.check()
    for s in samples:
        found=set(roots[s['sample_sha']]['matches'])
        counts['matched_unique_system_scopes']+=len(found)
        counts['samples_all_systems_matched']+=int(found==set(s['weights']))
        counts['samples_any_system_matched']+=bool(found)
    counts['samples']=len(samples);counts['packets']=len(inventory)
    counts['packet_captures']=sum(i['captures'] for i in inventory)
    counts['packet_curves']=sum(i['curves'] for i in inventory)
    out={'schema':'native_physics_local_inventory_v1','checked_at':iso(datetime.now(timezone.utc)),
         'window':[FIRST,LAST],'counts':dict(counts),'bytes_read':total,'packets':inventory,
         'raw_receipt_examples':raw_examples,'source_scope':str(directory),'network_requests':0,'production_writes':0,
         'qualification':'Scope/run/receipt/hash audit only. Does not recertify training artifacts, raw decoding, publication time, member paths, or formal OOS.',
         'aifs':'No AIFS ensemble packet family audited here; deterministic AIFS excluded.',
         'limitation':'Known retained capture directory only; absence here is not proof that the provider never offered a field.'}
    save('native_physics_inventory.json',out);save('native_temperature_curve_matches.json',curves)
    print(json.dumps(dict(counts),ensure_ascii=False,indent=2));print('input_MiB',round(total/1024**2,2))


if __name__=='__main__': main()
