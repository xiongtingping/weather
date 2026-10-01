"""Bounded read-only capture for the calibration-layer shape audit (2026-10-01).

Per trading station x event day (2026-09-01..2026-09-30, closed days only) take
(a) the FIRST committed P0 and (b) the LAST committed P0 before station-local
midnight, with the served vector, the centre/width receipt, the single-system
centres/probabilities, the system weights and -- for step 5 -- the raw ensemble
members (historical_member_run, latest native run whose archive query precedes
the decision).  Truth = AWC/SETTLEMENT_SOURCE latest revision.

Run with PYTHONPATH=. .venv/bin/python capture.py <chunk> <nchunks>  (then: capture.py merge)
"""
import collections, datetime as dt, json, pathlib, resource, shutil, sqlite3, sys, time
from zoneinfo import ZoneInfo
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.probability_bridge import history_envelope_payload

ROOT = pathlib.Path(__file__).resolve().parents[3]; OUT = pathlib.Path(__file__).parent
START = time.monotonic(); NOW = dt.datetime.now(dt.timezone.utc); USED = 0
resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
resource.setrlimit(resource.RLIMIT_CPU, (540, 540))
DATE0, DATE1 = dt.date(2026, 9, 1), dt.date(2026, 9, 30)
KEEP = ['system_weights_json', 'native_system_probabilities_json', 'native_calibrated_system_centers_c_json',
        'consensus_bridge_json', 'synoptic_cooling_json', 'center_width_json', 'probability_floor_json',
        'hour_lead_bucket_by_system_json', 'archive_query_at_utc_by_system_json', 'bucket_intervals_c_json',
        'system_family_ids_json', 'fusion_blend_json', 'neighbour_direction_json', 'band_reliability_json']

def instant(s): return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
def guard():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if not 0 < rss < 768 * 1024**2 or time.monotonic() - START > 560: raise RuntimeError('memory/time limit')
    if shutil.disk_usage(OUT).free < 4 * 1024**3: raise RuntimeError('disk floor')
    if USED > 2 * 1024**3: raise RuntimeError('read cap')
def connect():
    c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2)
    c.execute('pragma query_only=ON'); c.execute('pragma cache_size=-8192'); c.execute('pragma temp_store=MEMORY')
    c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024**2); c.set_progress_handler(lambda: (guard() or 0), 10000)
    return c
def unpack(l, k):
    v = l.get(k)
    return json.loads(v) if isinstance(v, str) else v

def envelope_row(c, h, st, day, midnight):
    """Return a compact record for one bridge-history row or a rejection string."""
    global USED
    e = c.execute('select lineage_json,created_at,decision_at_utc,state,station_id,event_date from basket_probability_envelope where envelope_id=?', (h[0],)).fetchone()
    if not e or e[3:] != ('COMMITTED', st, day) or instant(e[1]) >= midnight or e[2] != h[3]: return 'envelope_identity_clock'
    USED += len(e[0]) + len(h[2] or '')
    env = history_envelope_payload(c, checkpoint_id=h[0], envelope_content_sha256=h[1], stored=h[2]); lin = env['lineage']
    refs = [v[5:] for v in lin.values() if isinstance(v, str) and v.startswith('@ref/')]
    for sha in refs:
        q = c.execute('select length(document) from basket_probability_lineage_document where document_sha256=?', (sha,)).fetchone()
        if not q: raise RuntimeError('missing lineage document')
        USED += q[0]
    guard()
    inputs = {f: unpack(lin, f) for f in KEEP}
    cw = inputs.get('center_width_json')
    if isinstance(cw, dict):
        cw = {k: v for k, v in cw.items() if k not in ('input_probability',)} | {'input_probability': cw.get('input_probability')}
        inputs['center_width_json'] = cw
    return {'at': h[3], 'created_at': e[1], 'checkpoint': h[0], 'posterior_version': env['posterior_version'],
            'probability': env['probabilities'], 'fresh_until': env.get('fresh_until_utc'),
            'hours_to_midnight': (midnight - instant(h[3])).total_seconds() / 3600.0, 'inputs': inputs}

def members(c, st, day, decision_at, systems):
    """Raw members per system: latest native run whose archive query precedes the decision."""
    out = {}
    for sysid in systems:
        run = c.execute('''select native_run_init_utc,max(archive_query_at_utc) from calibration_native_ensemble_historical_member_run
            where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc is not null
            and archive_query_at_utc<=? and qc_status='ACCEPTED' group by native_run_init_utc order by native_run_init_utc desc limit 1''',
            (sysid, st, day, decision_at)).fetchone()
        if not run: out[sysid] = None; continue
        rows = c.execute('''select member_id,member_role,raw_tmax_c,run_cycle,archive_lead_bucket from calibration_native_ensemble_historical_member_run
            where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc=? and archive_query_at_utc=? and qc_status='ACCEPTED' ''',
            (sysid, st, day, run[0], run[1])).fetchall()
        vals = {}
        for mid, role, v, cyc, lead in rows: vals[mid] = (role, v, cyc, lead)
        out[sysid] = {'run_init': run[0], 'archive_query_at': run[1], 'n': len(vals),
                      'run_cycle': sorted({r[2] for r in vals.values()}), 'lead': sorted({r[3] for r in vals.values()}),
                      'values': [round(r[1], 3) for r in vals.values()]}
    return out

def merge():
    import glob
    rows = []; counts = collections.Counter(); excluded = []
    for f in sorted(glob.glob(str(OUT / 'capture_part_*.json'))):
        d = json.load(open(f)); rows += d['rows']; counts.update(d['counts']); excluded += d['excluded']
    (OUT / 'capture.json').write_text(json.dumps({'rows': rows, 'counts': dict(counts), 'excluded': excluded}))
    print(dict(counts), len(rows))

def main():
    if sys.argv[1] == 'merge': return merge()
    chunk, nchunks = int(sys.argv[1]), int(sys.argv[2])
    guard(); c = connect()
    stations = sorted(r['station'] for r in json.loads((ROOT / 'tmp/model_weight_verify_20260928/evidence.json').read_text())['latest_station_rows'])
    stations = [s for i, s in enumerate(stations) if i % nchunks == chunk]
    zones = dict(c.execute('select station_id,settlement_timezone from calibration_station_market_timezone_binding'))
    truth = {}; counts = collections.Counter(); excluded = []; rows = []
    for r in c.execute(f'''select station_id,local_date,official_tmax_c,revision_no,truth_id,source_lane,qc_status,
      settlement_timezone,authority_scope,canonical_committed_at_utc,settlement_definition from calibration_station_daily_high_truth
      where local_date>=? and local_date<=? and source_lane in {FORMAL_TRUTH_LANES_SQL} order by revision_no''', (DATE0.isoformat(), DATE1.isoformat())):
        truth[r[:2]] = r
    for st in stations:
        for offset in range((DATE1 - DATE0).days + 1):
            guard(); day = (DATE0 + dt.timedelta(days=offset)).isoformat(); counts['planned'] += 1
            def reject(reason): counts[reason] += 1; excluded.append({'station': st, 'date': day, 'reason': reason})
            tr = truth.get((st, day))
            if not tr: reject('missing_truth'); continue
            if tr[6] != 'ACCEPTED' or tr[7] != zones[st] or not tr[9] or instant(tr[9]) > NOW: reject('truth_qc_clock_timezone'); continue
            midnight = dt.datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zones[st])).astimezone(dt.timezone.utc)
            if midnight + dt.timedelta(days=1) > NOW: reject('not_closed'); continue
            cutoff = midnight.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            base = '''select checkpoint_id,envelope_content_sha256,envelope_json,decision_at_utc,created_at_utc
              from basket_probability_bridge_history where station_id=? and event_date=? and stage='P0' and state='COMMITTED'
              and decision_at_utc<? and created_at_utc<? order by decision_at_utc {o},created_at_utc {o},checkpoint_id {o} limit 1'''
            last = c.execute(base.format(o='desc'), (st, day, cutoff, cutoff)).fetchone()
            if not last: reject('no_pre_midnight_p0'); continue
            first = c.execute(base.format(o='asc'), (st, day, cutoff, cutoff)).fetchone()
            rec = {'station': st, 'date': day, 'tz': zones[st], 'truth_c': tr[2], 'truth_revision': tr[3], 'truth_lane': tr[5],
                   'settlement_definition': tr[10], 'midnight_utc': cutoff}
            for tag, h in (('last', last), ('first', first)):
                er = envelope_row(c, h, st, day, midnight)
                if isinstance(er, str): rec[tag] = {'rejected': er}; counts[f'{tag}_{er}'] += 1; continue
                w = er['inputs'].get('system_weights_json') or {}
                er['members'] = members(c, st, day, h[3], sorted(w))
                rec[tag] = er
            rows.append(rec); counts['captured'] += 1
        print(st, counts['captured'], round(time.monotonic() - START), flush=True)
    c.close(); guard()
    result = {'captured_at': NOW.isoformat(), 'chunk': [chunk, nchunks], 'stations': stations, 'counts': dict(counts),
              'excluded': excluded, 'rows': rows, 'peak_rss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, 'read_bytes': USED}
    (OUT / f'capture_part_{chunk}.json').write_text(json.dumps(result, ensure_ascii=False))
    print(json.dumps({k: v for k, v in result.items() if k not in ['rows', 'excluded']}, ensure_ascii=False))
if __name__ == '__main__': main()
