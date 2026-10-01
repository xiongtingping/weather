"""Capture closed-day P1 cells (one per station-day-3h block, median decision) whose root is a P0-M,
with that P0-M's root P0 vector, TAF likelihood and observed exclusion (read-only, bounded)."""
import json, sqlite3, time, pathlib, sys, shutil, collections
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT = pathlib.Path(__file__).resolve().parents[3]; OUT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.station_registry import _COORDINATES
START, END = '2026-09-19', '2026-09-30'
def connect():
    if shutil.disk_usage(ROOT).free < 3 * 1024**3: raise RuntimeError('free disk below 3GiB')
    c = sqlite3.connect('file:' + str(ROOT / 'data/bayes_basket_target_ssot.db') + '?mode=ro', uri=True, timeout=3)
    c.execute('pragma query_only=on'); t0 = time.monotonic()
    c.set_progress_handler(lambda: 1 if time.monotonic() - t0 > 120 else 0, 50000)
    return c
def env(c, st, day, stage, ck):
    r = c.execute("SELECT probabilities_json,lineage_json,posterior_version,bucket_ordinals_json,decision_at_utc FROM basket_probability_envelope "
                  "WHERE station_id=? AND event_date=? AND stage=? AND checkpoint_id=?", (st, day, stage, ck)).fetchone()
    return None if r is None else dict(p=json.loads(r[0]), lin=json.loads(r[1]), ver=r[2], ords=json.loads(r[3]), dec=r[4])
rows = []; stats = {}
for station in sorted(_COORDINATES):
    c = connect(); t0 = time.monotonic()
    truth = {d: v for d, v in c.execute(
        "SELECT t.local_date,t.official_tmax_c FROM calibration_station_daily_high_truth t WHERE t.station_id=? "
        "AND t.source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND t.qc_status='ACCEPTED' AND t.local_date BETWEEN ? AND ? "
        "AND t.revision_no=(SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth t2 WHERE t2.station_id=t.station_id "
        "AND t2.local_date=t.local_date AND t2.settlement_definition=t.settlement_definition AND t2.source_lane IN " + FORMAL_TRUTH_LANES_SQL +
        " AND t2.qc_status='ACCEPTED')", (station, START, END))}
    hist = c.execute("SELECT event_date,checkpoint_id,decision_at_utc,settlement_timezone FROM basket_probability_bridge_history "
                     "WHERE stage='P1' AND station_id=? AND event_date BETWEEN ? AND ? AND decision_at_utc>=?", (station, START, END, START)).fetchall()
    blocks = collections.defaultdict(list)
    for day, ck, dec, tz in hist:
        if day not in truth or not tz: continue
        z = ZoneInfo(tz); h = (datetime.fromisoformat(dec.replace('Z', '+00:00')).astimezone(z) - datetime.fromisoformat(day + 'T00:00:00').replace(tzinfo=z)).total_seconds() / 3600
        blocks[(day, int(h // 3))].append((h, ck, dec, tz))
    cache = {}; n = 0
    for (day, blk), entries in sorted(blocks.items()):
        entries.sort()
        for h, ck, dec, tz in [entries[len(entries) // 2]] + entries:   # median first; walk on if unusable
            p1 = env(c, station, day, 'P1', ck)
            if p1 is None: continue
            lin = p1['lin']; rec = json.loads(lin.get('p1_observation_likelihood_json') or '{}')
            if lin.get('prediction_root_stage') != 'P0-M' or rec.get('state') != 'APPLIED' or rec.get('update') != 'DIRECT_LOG_UPDATE_V1': continue
            rk = ('P0-M', lin['prediction_root_checkpoint_id'])
            if rk not in cache: cache[rk] = env(c, station, day, 'P0-M', rk[1])
            m = cache[rk]
            if m is None: continue
            pk = ('P0', m['lin']['p0_root_checkpoint_id'])
            if pk not in cache: cache[pk] = env(c, station, day, 'P0', pk[1])
            p0 = cache[pk]
            if p0 is None: continue
            mrec = json.loads(m['lin'].get('p0m_taf_likelihood_json') or '{}')
            keys = sorted(p1['ords'], key=lambda k: p1['ords'][k])
            rows.append(dict(station=station, date=day, tz=tz, hour=h, decision=dec, p1_version=p1['ver'], keys=keys, ords=p1['ords'],
                             intervals=json.loads(lin['bucket_intervals_c_json']), p1=p1['p'], p1_excluded=[x for x in (lin.get('hard_excluded_buckets') or '').split(',') if x and x != 'NONE'],
                             p1_receipt=rec, p0m=m['p'], p0m_decision=m['dec'], p0m_excluded=[] if m['lin'].get('observed_hard_excluded_buckets', 'NONE') == 'NONE' else m['lin']['observed_hard_excluded_buckets'].split(','),
                             p0m_running_high=m['lin'].get('observed_running_high_c'), taf_L=mrec.get('likelihood_by_bucket') if mrec.get('state') == 'APPLIED' else None,
                             p0=p0['p'], truth=truth[day]))
            n += 1; break
    c.close(); stats[station] = dict(history=len(hist), blocks=len(blocks), rows=n, seconds=round(time.monotonic() - t0, 1))
    print(station, stats[station], flush=True)
(OUT / 'capture_p1.json').write_text(json.dumps(dict(window=[START, END], stats=stats, rows=rows), separators=(',', ':')))
print('DONE rows', len(rows), 'root P0-M with exclusion', sum(1 for r in rows if r['p0m_excluded']))
