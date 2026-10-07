"""Bounded, read-only TAF/METAR cloud pairing; never promotes an OOS artifact."""
from __future__ import annotations
import collections
import gzip
import hashlib
import json
import math
import re
import resource
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREVIOUS = HERE.parent / 'obs_increment_recheck_20261007'
MAX_BYTES = 128 * 1024**2
AMOUNT = {'FEW': (1/8, 2/8), 'SCT': (3/8, 4/8),
          'BKN': (5/8, 7/8), 'OVC': (1., 1.)}
LIMITS = (1500, 3000, 5000, 6500)  # ft AGL, exclusive upper bound
TOKEN = re.compile(r'^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?$')


def guard():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin': rss *= 1024
    if rss > 1024**3 or shutil.disk_usage(HERE).free < 2*1024**3:
        raise RuntimeError('resource_probe_or_limit_failed')


def utc(value):
    if isinstance(value, (float, int)):
        if isinstance(value, bool) or not math.isfinite(value): raise ValueError('time_invalid')
        return datetime.fromtimestamp(value, timezone.utc)
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None: raise ValueError('time_zone_missing')
    return result.astimezone(timezone.utc)


def iso(value): return value.isoformat().replace('+00:00', 'Z')


def sha_file(path):
    result = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024**2): result.update(chunk)
    return result.hexdigest()


def connect(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('pragma query_only=on'); c.execute('pragma cache_size=-8192')
    return c


QUERIES = {}
def query(c, sql, args=()):
    entry=QUERIES.setdefault(sql, {'sql':sql,'calls':0,'parameter_examples':[]})
    entry['calls']+=1
    if len(entry['parameter_examples'])<3:entry['parameter_examples'].append(args)
    deadline = time.monotonic()+60
    c.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
    return c.execute(sql, args)


def metar_layers(raw):
    # Restrict to the observation; trend and RMK cloud groups are not observations.
    raw = re.split(r'\b(?:RMK|TEMPO|BECMG|NOSIG)\b', raw, maxsplit=1)[0]
    layers = []
    for t in raw.replace('=', '').split():
        m = TOKEN.fullmatch(t)
        if m:
            cover, height, kind = m.groups()
            layers.append({'cover': cover, 'base': None if height=='///' else int(height)*100, 'type': kind})
        elif t in ('CAVOK', 'NSC', 'SKC', 'CLR', 'NCD'):
            layers.append({'cover': t, 'base': None})
    return layers


def cloud_interval(layers, height):
    """Maximum REPORTED layer amount below height, NOT union cloud fraction.

    Cloud base is not cloud top. CAVOK/NSC/NCD constrain only significant
    low clouds below 5000ft (or higher MSA), not an entirely cloudless sky.
    Unknown bases and obscured sky are never converted to zero cloud.
    """
    if not layers: return None
    intervals = []
    for layer in layers:
        cover = layer.get('cover'); base = layer.get('base')
        if cover in ('VV', 'OVX'): return None
        if cover in ('CAVOK', 'NSC', 'NCD'):
            if height > 5000: return None
            intervals.append((0., 0.)); continue
        if cover in ('CLR', 'SKC'):
            if cover=='CLR' and height>12000: return None
            intervals.append((0., 0.)); continue
        if cover not in AMOUNT or base is None or isinstance(base, bool): return None
        try: base = float(base)
        except (ValueError, TypeError): return None
        if not math.isfinite(base) or base<0: return None
        intervals.append(AMOUNT[cover] if base < height else (0., 0.))
    return (max(x[0] for x in intervals), max(x[1] for x in intervals))


def direction(obs, fc):
    if obs is None or fc is None: return 'unknown'
    if obs[0]>fc[1]: return 'more'
    if obs[1]<fc[0]: return 'less'
    return 'overlap'


def forecast_scenarios(taf, at):
    """Return prevailing and all possible cloud change scenarios.

    BECMG transition retains old and new states; TEMPO/PROB are possible
    scenarios, not an invented occurrence probability or weighted amount.
    Omitted cloud in a change group inherits; omitted initial sky is unknown.
    """
    w = taf['awc_row']
    if not utc(w['validTimeFrom'])<=at<utc(w['validTimeTo']): return None
    prevailing = []; overlays = []
    for g in w.get('fcsts', []):
        k = g.get('fcstChange') or ''
        if k not in ('', 'FM', 'BECMG', 'TEMPO', 'PROB'): return None
        try: start, end = utc(g['timeFrom']), utc(g['timeTo'])
        except (KeyError, TypeError, ValueError): return None
        if end<=start: return None
        if k in ('TEMPO', 'PROB'): overlays.append((start, end, g))
        else: prevailing.append((start, end, g))
    prevailing.sort(key=lambda x: x[0])
    last = None; active = None
    for start, end, g in prevailing:
        if start>at: break
        old = last
        current = g.get('clouds') or (old if g.get('fcstChange')=='BECMG' else None)
        if current is None: return None
        last = current
        if start<=at<end:
            if active is not None: return None  # overlapping prevailing decoder spans
            active = [current]
            if g.get('fcstChange')=='BECMG':
                if g.get('timeBec') is None: return None
                if at<utc(g['timeBec']):
                    if old is None: return None
                    active.append(old)
    if active is None: return None
    for start, end, g in overlays:
        if start<=at<end and g.get('clouds'): active.append(g['clouds'])
    return active


def scenario_interval(scenarios, height):
    if scenarios is None: return None
    vals = [cloud_interval(layers, height) for layers in scenarios]
    if not vals or any(x is None for x in vals): return None
    return min(x[0] for x in vals), max(x[1] for x in vals)


def native_sources(c):
    # Record categorical domains before filtering. These are local forward
    # receipts, not the quarantined date-addressed historical replay table.
    domains = {k: [x[0] for x in query(c, 'select distinct '+k+' from calibration_forward_taf_capture')]
               for k in ('station_id', 'source_class', 'authority_scope')}
    bindings = collections.defaultdict(list)
    for r in query(c, 'select * from calibration_forward_taf_capture'):
        if r['source_class']!='official_forward_native_receipt': continue
        bindings[(r['station_id'], r['source_object_sha256'])].append(dict(r))
    return domains, bindings


def load_tafs(c):
    domains, bindings = native_sources(c)
    counts = collections.Counter(); by_key = {}; fingerprints = []
    consumed = 0
    for f in sorted((ROOT/'data/runtime/taf_raw_archive').glob('*.jsonl')):
        # Freeze append-only files at byte length observed at this run.
        size = f.stat().st_size; digest = hashlib.sha256(); n = 0
        with f.open('rb') as inp:
            while n<size:
                line = inp.readline(min(2*1024**2, size-n)); n+=len(line); consumed+=len(line)
                if not line or len(line)>=2*1024**2 or consumed>MAX_BYTES: raise RuntimeError('taf_input_cap')
                if not line.endswith(b'\n'): raise RuntimeError('torn_source_row')
                digest.update(line); r=json.loads(line); counts['raw_rows']+=1
                if r['schema']!='taf_raw_archive_row_v1': raise ValueError('schema_mismatch')
                raw=r['raw_taf']; w=r['awc_row']; st=r['station_id']; sha=r['raw_taf_sha256']
                # taf_raw_archive.archive_rows explicitly strips only outer
                # whitespace before computing the canonical raw hash.
                if raw!=w['rawTAF']: counts['canonical_outer_whitespace_rows']+=1
                if hashlib.sha256(raw.encode()).hexdigest()!=sha or raw!=w['rawTAF'].strip() or st!=w['icaoId'] or utc(r['issue_time_utc'])!=utc(w['issueTime']):
                    raise ValueError('raw_identity_mismatch')
                backfill = '#backfill' in r['source_url'] or 'date' in parse_qs(urlparse(r['source_url']).query)
                counts['backfill_rows' if backfill else 'forward_rows']+=1
                # TGFTP stores the same bulletin without decoded fcsts.
                # Missing decoding is not a contradictory AWC cloud decode.
                # Retain its local raw-text receipt only via the explicit
                # SSOT hash binding; never fabricate decoded groups for it.
                if not w.get('fcsts'):
                    counts['missing_decoded_forecast_rows']+=1;continue
                key=(st,sha)
                # Match semantic decoded fields only; mostRecent/prior can
                # change as an identical bulletin is queried at a later time.
                semantic={k:w.get(k) for k in ('issueTime','validTimeFrom','validTimeTo','fcsts')}
                if key not in by_key:
                    by_key[key]=dict(r, receipts=[], decode_conflict=False, restored_only=backfill)
                else:
                    counts['duplicate_bulletin_rows']+=1
                    prev=by_key[key]
                    if semantic!={k:prev['awc_row'].get(k) for k in semantic}:
                        # Temperature decoder availability changed in Oct.
                        # Compare the cloud/weather/time semantic subset.
                        def slim(d):
                            d=dict(d); d['fcsts']=[{k:g.get(k) for k in ('timeFrom','timeTo','timeBec','fcstChange','probability','clouds')} for g in d['fcsts'] or []]; return d
                        if slim(semantic)!=slim({k:prev['awc_row'].get(k) for k in semantic}): prev['decode_conflict']=True
                    prev['restored_only'] &= backfill
                # Synthetic backfill receipt clocks must never prove arrival.
                if not backfill and r.get('response_sha256') and urlparse(r['source_url']).hostname=='aviationweather.gov':
                    seen=max(utc(r['received_at_utc']), utc(w['issueTime']), utc(w['dbPopTime']))
                    by_key[key]['receipts'].append((seen, 'raw_archive_native', r['response_sha256']))
        fingerprints.append(dict(path=str(f.relative_to(ROOT)),bytes=size,sha256=digest.hexdigest()))
        guard()
    by_station=collections.defaultdict(list)
    for key,t in by_key.items():
        w=t['awc_row']; st,sha=key
        for r in bindings.get(key,[]):
            if utc(r['issued_at_utc'])!=utc(w['issueTime']) or utc(r['valid_from_utc'])!=utc(w['validTimeFrom']) or utc(r['valid_until_utc'])!=utc(w['validTimeTo']):
                counts['receipt_validity_mismatch']+=1; continue
            # Binding proves arrival of the exact raw text; it does not prove
            # that retrospectively restored decoded groups existed then.
            seen=max(utc(r['received_at_utc']),utc(r['provider_available_at_utc']),utc(r['issued_at_utc']))
            t['receipts'].append((seen,'ssot_raw_sha_bound_receipt',r['taf_capture_id']))
        if t['decode_conflict']: counts['decode_conflict_bulletins']+=1; continue
        if ' COR ' in ' '+t['raw_taf']+' ':
            counts['corrected_bulletins_excluded']+=1; continue
        if not t['awc_row'].get('fcsts'):
            counts['missing_decoded_forecast']+=1; continue
        if t['receipts']:
            seen,kind,evidence=min(t['receipts'])
            t.update(known_at=seen, receipt_kind=kind,receipt_id=evidence)
            counts['native_bound_unique_bulletins']+=1
            if t['restored_only']: counts['restored_bound_unique_bulletins']+=1
        else: counts['no_native_receipt_unique_bulletins']+=1
        by_station[st].append(t)
    for ls in by_station.values(): ls.sort(key=lambda t:utc(t['issue_time_utc']))
    return by_station,dict(counts),domains,fingerprints


def select_taf(tafs, valid, decision, mode):
    cutoff=min(decision, valid)
    if mode=='day1':
        from datetime import timedelta
        cutoff=valid-timedelta(hours=24)
    candidates=[]
    for t in tafs:
        w=t['awc_row']
        if utc(t['issue_time_utc'])>cutoff or not utc(w['validTimeFrom'])<=valid<utc(w['validTimeTo']): continue
        if 'known_at' not in t or t['known_at']>cutoff: continue
        candidates.append(t)
    if not candidates: return None
    latest=max(utc(t['issue_time_utc']) for t in candidates)
    latests=[t for t in candidates if utc(t['issue_time_utc'])==latest]
    # A same-issue AMD supersedes the original; unresolved other collisions
    # fail closed rather than selecting by hash/file order.
    if len(latests)>1:
        amended=[t for t in latests if ' AMD ' in ' '+t['raw_taf']+' ']
        if len(amended)!=1: return None
        return amended[0]
    return latests[0]


def summarize(rows):
    summaries={}
    for mode in ('latest','day1'):
        summary=dict(rows=len(rows),matched=0,stations=set(),days=set(),station_days=set(),by_station={},by_day={},by_version={},thresholds={})
        for dimension in ('station','day','posterior_version'):
            grouped=collections.defaultdict(lambda:collections.Counter())
            for r in rows:
                x=grouped[r[dimension]];x['rows']+=1
                if r[mode] is not None:
                    x['matched']+=1
                    for h in ('5000','6500'):x[h+'_'+r[mode]['directions'][h]]+=1
            summary[{'station':'by_station','day':'by_day','posterior_version':'by_version'}[dimension]]={k:dict(v) for k,v in sorted(grouped.items())}
        for r in rows:
            if r[mode] is not None:
                summary['matched']+=1;summary['stations'].add(r['station']);summary['days'].add(r['day']);summary['station_days'].add((r['station'],r['day']))
        for k in ('stations','days','station_days'):summary[k]=len(summary[k])
        summary['receipt_kinds']=dict(collections.Counter(r[mode]['receipt_kind'] for r in rows if r[mode]))
        summary['restored_only_pairs']=sum(bool(r[mode] and r[mode]['restored_only']) for r in rows)
        summary['obs_stale_over_2h']=sum((utc(r['decided_at'])-utc(r['observation_valid_at'])).total_seconds()>7200 for r in rows)
        for h in LIMITS:
            summary['thresholds'][h]=dict(collections.Counter(r[mode]['directions'][str(h)] for r in rows if r[mode]))
        summary['usable_nonstale_by_threshold']={str(h):sum(bool(r[mode] and r[mode]['directions'][str(h)]!='unknown' and (utc(r['decided_at'])-utc(r['observation_valid_at'])).total_seconds()<=7200) for r in rows) for h in LIMITS}
        summaries[mode]=summary
    return summaries


def pair(row, tafs, observation):
    valid=utc(observation['valid_at']);decision=utc(row['decided_at'])
    if any(not observation[k] or utc(observation[k])>decision for k in ('valid_at','first_seen_at','canonical_committed_at')) or observation['correction_suspected']:
        raise ValueError('observation_not_known')
    if observation['source_lane']!='awc' or observation['station_id']!=row['station']: raise ValueError('observation_lineage_mismatch')
    if utc(row['observation_valid_at'])!=valid:raise ValueError('observation_validity_mismatch')
    obs=metar_layers(observation['raw_text']);intervals={str(h):cloud_interval(obs,h) for h in LIMITS}
    result={k:row[k] for k in ('station','day','decided_at','posterior_version','observation_id','observation_valid_at')}
    result.update(obs_layers=obs,obs_intervals=intervals,raw_metar=observation['raw_text'],
                  evidence_role='observational_candidate_not_oos',strict_oos=False)
    for mode in ('latest','day1'):
        t=select_taf(tafs,valid,decision,mode);result[mode]=None
        if t is None:continue
        scenarios=forecast_scenarios(t,valid)
        fc={str(h):scenario_interval(scenarios,h) for h in LIMITS}
        result[mode]=dict(taf_sha=t['raw_taf_sha256'],issue= t['issue_time_utc'],known_at=iso(t['known_at']),receipt_kind=t['receipt_kind'],receipt_id=t['receipt_id'],
                          restored_only=t['restored_only'],valid_from=iso(utc(t['awc_row']['validTimeFrom'])),valid_to=iso(utc(t['awc_row']['validTimeTo'])),
                          raw_taf=t['raw_taf'],scenarios=scenarios,intervals=fc,directions={str(h):direction(intervals[str(h)],fc[str(h)]) for h in LIMITS},
                          issue_lead_hours=(valid-utc(t['issue_time_utc'])).total_seconds()/3600)
    return result


def main():
    guard();c=connect(ROOT/'data/bayes_basket_target_ssot.db');a=connect(ROOT/'data/archive.db')
    obs_domains={k:[x[0] for x in query(a,'select distinct '+k+' from reported_trajectory_observations')] for k in ('station_id','source_lane','report_type')}
    tafs,profile,domains,fingerprints=load_tafs(c)
    timezone_bindings={}
    for st,tz in query(c,'select distinct station_id,settlement_timezone from calibration_station_market_timezone_binding'):
        if st in timezone_bindings and timezone_bindings[st]!=tz:raise ValueError('ambiguous_timezone_binding')
        timezone_bindings[st]=tz
    allrows=[]; byte_count=0; cache={}; output_bytes=0
    with gzip.open(PREVIOUS/'audited_rows.jsonl.gz','rt') as inp,gzip.open(HERE/'paired_rows.jsonl.gz','wt') as out:
        for line in inp:
            byte_count+=len(line.encode())
            if byte_count>MAX_BYTES: raise RuntimeError('research_input_cap')
            r=json.loads(line)
            if r['tz']!=timezone_bindings[r['station']]: raise ValueError('timezone_mismatch')
            oid=r['observation_id']
            if oid not in cache:
                cache[oid]=dict(query(a,'select * from reported_trajectory_observations where observation_id=?',(oid,)).fetchone())
            paired=pair(r,tafs[r['station']],cache[oid]);allrows.append(paired)
            s=json.dumps(paired,ensure_ascii=False)+'\n';output_bytes+=len(s.encode())
            if output_bytes>MAX_BYTES:raise RuntimeError('pair_output_cap')
            out.write(s)
            if len(allrows)%500==0:guard()
    # Today's case is only an arrival/physics example, never a closed-day OOS.
    old=json.loads((PREVIOUS/'audit.json').read_text());case=[]
    for r in old['case']:
        if not r['observation']:continue
        obs=r['observation'];ob=dict(station_id='ZUUU',source_lane=obs['lane'],valid_at=obs['valid'],first_seen_at=obs['seen'],canonical_committed_at=obs['committed'],correction_suspected=obs['correction'],raw_text=obs['raw'])
        row=dict(station='ZUUU',day='2026-10-07',decided_at=r['at'],posterior_version=r['version'],observation_id=obs['id'],observation_valid_at=obs['valid'])
        paired=pair(row,tafs['ZUUU'],ob);paired['local_hour']=r['hour'];case.append(paired)
    result=dict(schema='taf_metar_cloud_pairing_audit_v1',captured_at=iso(datetime.now(timezone.utc)),evidence_role='observational_candidate_not_oos',production_modified=False,
                definition='maximum reported layer coverage interval with cloud base < cutoff ft AGL, not total cloud fraction or low cloud top',
                mode_definitions={'latest':'latest issued and locally received TAF before observation time, valid at that observation', 'day1':'latest TAF issued AND locally received at least 24 hours before observation, valid at that observation'},
                profile=profile,taf_domains=domains,obs_domains=obs_domains,summaries=summarize(allrows),case=case,
                current_chain={'P1':'p1_joint_likelihood_v38','closed_days':3,'not_a_current_chain_36day_oos':True},
                blockers=['stored P1 sample mixes old production chains; current segment only 3 closed days',
                          'backfilled raw text may bind to old raw hash receipts but decoded groups have no historical decoding/QA receipt',
                          'METAR significant-cloud reporting is not a full-sky cloud fraction; broad threshold CAVOK/NSC is unknown',
                          'cloud base lowering is distinct from increased coverage and has no automatic temperature sign'],
                input_fingerprints=fingerprints,research_input_sha256=sha_file(PREVIOUS/'audited_rows.jsonl.gz'),paired_rows_sha256=sha_file(HERE/'paired_rows.jsonl.gz'),
                resource_limits=dict(rss_bytes=1024**3,per_line_bytes=2*1024**2,input_bytes=MAX_BYTES,output_bytes=MAX_BYTES,min_free_disk_bytes=2*1024**3),executed_queries=list(QUERIES.values()))
    (HERE/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(profile=profile,summaries={m:{k:v for k,v in s.items() if not k.startswith('by_')} for m,s in result['summaries'].items()},case=[dict(hour=x['local_hour'],obs=x['obs_layers'],latest=x['latest'] and {k:x['latest'][k] for k in ('raw_taf','directions')},day1=x['day1'] and {k:x['day1'][k] for k in ('raw_taf','directions')}) for x in case]),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
