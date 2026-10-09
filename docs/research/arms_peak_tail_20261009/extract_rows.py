"""Served vectors (bit-exact, as served) for every labelled station-day since 2026-09-05:
one representative decision per (station, day, stage, hours-to-settlement band) -- the band's
last decision -- with its ladder intervals and the standing truth label."""
import sys, json, gzip, sqlite3, time, collections
sys.path.insert(0, '/Users/xiong/weather')
from bayes_basket_target.label_knowability import knowable_labels
from bayes_basket_target.settlement_time import hours_to_settlement_if_known
DB = '/Users/xiong/weather/data/bayes_basket_target_ssot.db'
OUT = '/private/tmp/claude-501/-Users-xiong-weather/8e8e0e0f-2636-45a0-a0af-fe34e79a733b/scratchpad/arms_259/rows.jsonl.gz'
BANDS = ((48, 1e9, 'h48+'), (36, 48, 'h36_48'), (24, 36, 'h24_36'), (12, 24, 'h12_24'), (0, 12, 'h0_12'))
t0 = time.time()
conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=60); conn.execute('PRAGMA query_only=1')
labels = knowable_labels(conn)
print('labels', len(labels))
hist = conn.execute("SELECT history_id, station_id, event_date, settlement_timezone, stage, decision_at_utc FROM basket_probability_bridge_history WHERE state='COMMITTED' AND event_date>='2026-09-05'").fetchall()
print('history rows', len(hist), round(time.time()-t0,1), 's')
best = {}
for hid, st, ed, tz, stage, dec in hist:
    if (st, ed) not in labels: continue
    h = hours_to_settlement_if_known(ed, as_of_utc=dec, settlement_timezone=tz)
    if h is None: continue
    band = next((name for lo, hi, name in BANDS if lo <= h < hi), None)
    if band is None: continue
    key = (st, ed, stage, band)
    if key not in best or dec > best[key][1]:
        best[key] = (hid, dec, h, tz)
print('representative decisions', len(best), round(time.time()-t0,1), 's')
ids = [v[0] for v in best.values()]
probs = collections.defaultdict(list)
for i in range(0, len(ids), 400):
    chunk = ids[i:i+400]
    for hid, b, o, lo, hi, p in conn.execute(f"SELECT history_id, bucket, ordinal, lower_c, upper_c, probability FROM basket_probability_bridge_bucket_probability WHERE history_id IN ({','.join('?'*len(chunk))})", chunk):
        probs[hid].append((int(o), str(b), lo, hi, float(p)))
conn.close()
print('bucket rows fetched', sum(len(v) for v in probs.values()), round(time.time()-t0,1), 's')
n = 0; miss = 0
with gzip.open(OUT, 'wt') as f:
    for (st, ed, stage, band), (hid, dec, h, tz) in sorted(best.items()):
        rows = sorted(probs.get(hid, []))
        if not rows: miss += 1; continue
        keys = [r[1] for r in rows]; lab = labels[(st, ed)]
        if str(lab['bucket']) not in keys: miss += 1; continue
        f.write(json.dumps({
            'station': st, 'event_date': ed, 'tz': tz, 'stage': stage, 'band': band, 'hours': round(h, 2), 'decision_at': dec, 'history_id': hid,
            'keys': keys, 'p': [r[4] for r in rows], 'intervals': {r[1]: [r[2], r[3]] for r in rows},
            'truth': str(lab['bucket']), 'known_at': str(lab['known_at']), 'unit': 'F' if '°F' in keys[0] else 'C',
        }, ensure_ascii=False) + '\n'); n += 1
print('rows written', n, 'skipped', miss, round(time.time()-t0,1), 's')
