"""Capture closed-day P0-M envelopes with their P0 roots and TAF likelihoods (read-only, bounded).

Every P0-M history row decided 2026-09-19 onward (charter 150 arithmetic: P0 x L_TAF x observed
mask) on event days 09-19..09-30 with a formal truth (charter 225 lanes, newest revision).
Per row: root P0 vector, the TAF likelihood by bucket (receipt; identity when not APPLIED), the
observed hard exclusion, the served vector and the truth.  Reconstruction is asserted later.
"""
import json, sqlite3, time, pathlib, sys, shutil, resource
ROOT = pathlib.Path(__file__).resolve().parents[3]; OUT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.station_registry import _COORDINATES
resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3)) if sys.platform != 'darwin' else None
START, END = '2026-09-19', '2026-09-30'
def connect():
    if shutil.disk_usage(ROOT).free < 3 * 1024**3: raise RuntimeError('free disk below 3GiB')
    c = sqlite3.connect('file:' + str(ROOT / 'data/bayes_basket_target_ssot.db') + '?mode=ro', uri=True, timeout=3)
    c.execute('pragma query_only=on'); t0 = time.monotonic()
    c.set_progress_handler(lambda: 1 if time.monotonic() - t0 > 90 else 0, 50000)   # 90 s per station connection
    return c
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
                     "WHERE stage='P0-M' AND station_id=? AND event_date BETWEEN ? AND ? AND decision_at_utc>=?", (station, START, END, START)).fetchall()
    roots = {}; seen = set(); n = 0; miss = 0
    for day, ck, dec, tz in hist:
        if day not in truth or (day, ck) in seen: continue
        seen.add((day, ck))
        e = c.execute("SELECT probabilities_json,lineage_json,posterior_version,bucket_ordinals_json,decision_at_utc FROM basket_probability_envelope "
                      "WHERE station_id=? AND event_date=? AND stage='P0-M' AND checkpoint_id=?", (station, day, ck)).fetchone()
        if e is None: miss += 1; continue
        lin = json.loads(e[1]); rk = lin.get('p0_root_checkpoint_id')
        if (day, rk) not in roots:
            r = c.execute("SELECT probabilities_json,lineage_json,posterior_version FROM basket_probability_envelope "
                          "WHERE station_id=? AND event_date=? AND stage='P0' AND checkpoint_id=?", (station, day, rk)).fetchone()
            roots[(day, rk)] = None if r is None else (json.loads(r[0]), json.loads(r[1]).get('bucket_intervals_c_json'), r[2])
        root = roots[(day, rk)]
        if root is None: miss += 1; continue
        receipt = json.loads(lin.get('p0m_taf_likelihood_json') or '{}')
        ords = json.loads(e[3]); keys = sorted(ords, key=lambda k: ords[k])
        rows.append(dict(station=station, date=day, tz=tz, decision=e[4], version=e[2], root_version=root[2], keys=keys,
                         intervals=json.loads(root[1]) if isinstance(root[1], str) else root[1], root=root[0], served=json.loads(e[0]),
                         taf_state=receipt.get('state', lin.get('taf_revision_state')), likelihood=receipt.get('likelihood_by_bucket') if receipt.get('state') == 'APPLIED' else None,
                         excluded=[] if lin.get('observed_hard_excluded_buckets', 'NONE') == 'NONE' else lin['observed_hard_excluded_buckets'].split(','),
                         running_high=None if lin.get('observed_running_high_c', 'NONE') == 'NONE' else float(lin['observed_running_high_c']), truth=truth[day]))
        n += 1
    c.close(); stats[station] = dict(history=len(hist), rows=n, missing=miss, truth_days=len(truth), seconds=round(time.monotonic() - t0, 1))
    print(station, stats[station], flush=True)
(OUT / 'capture.json').write_text(json.dumps(dict(window=[START, END], stats=stats, rows=rows), separators=(',', ':')))
print('DONE rows', len(rows), 'with exclusion', sum(1 for r in rows if r['excluded']))
