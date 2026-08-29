"""Task A part 3: per-leg vs per-station-day denominators for the same bands,
plus how one-sided the posted window is around its own centre."""
import sys, json, sqlite3, collections, statistics
c = sqlite3.connect('file:data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=60)
c.row_factory = sqlite3.Row
settle = {(r['station_id'],r['event_date']):r for r in c.execute(
  "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows = c.execute(
  "SELECT station_id,event_date,stage,decision_at_utc,ladder_center_bucket,"
  " posted_buckets_json,leg_shares_json FROM paper_trade_position"
  " ORDER BY station_id,event_date,decision_at_utc").fetchall()
by_sd = collections.defaultdict(list)
for r in rows: by_sd[(r['station_id'],r['event_date'])].append(r)

# --- window shape around its own centre (posted window, entry row) ---
shape = collections.Counter(); sides = collections.Counter()
for k,v in by_sd.items():
    r = v[0]; w = json.loads(r['posted_buckets_json'] or '[]')
    if r['ladder_center_bucket'] not in w: continue
    i = w.index(r['ladder_center_bucket'])
    lo, hi = i, len(w)-1-i
    shape[(lo,hi)] += 1
    sides['both' if lo and hi else ('endpoint' if (lo==0)!=(hi==0) else 'single')] += 1
print('posted window shape (buckets below centre, above centre), entry row, %d station-days:' % sum(shape.values()))
for kk,n in sorted(shape.items(), key=lambda x:-x[1]):
    print('   below=%d above=%d : %3d  (%.1f%%)' % (kk[0],kk[1],n,100*n/sum(shape.values())))
print('   centre on an endpoint of its own window: %d / %d = %.1f%%' % (
    sides['endpoint'], sum(shape.values()), 100*sides['endpoint']/sum(shape.values())))

# --- per-station-day band outcomes on the legs actually BOUGHT ---
agg = collections.defaultdict(lambda: dict(sd=0, win=0, legs=0))
n_sd = 0
for k,v in sorted(by_sd.items()):
    s = settle.get(k)
    if s is None: continue
    n_sd += 1
    bands = collections.defaultdict(set)
    for r in v:
        w = json.loads(r['posted_buckets_json'] or '[]')
        ctr = r['ladder_center_bucket']
        if ctr not in w: continue
        for b,q in json.loads(r['leg_shares_json'] or '{}').items():
            if float(q) <= 0 or b not in w: continue
            d = abs(w.index(b) - w.index(ctr))
            bands['centre' if d==0 else ('shoulder' if d==1 else 'rest')].add(b)
    for name, bs in bands.items():
        agg[name]['sd'] += 1
        agg[name]['legs'] += len(bs)
        if s['settled_bucket'] in bs: agg[name]['win'] += 1

print('\n=== same bands, PER-STATION-DAY denominator (n=%d settled station-days) ===' % n_sd)
print('%-10s %8s %10s %10s %12s' % ('band','st-days','distinct b','won','P(settled in band | band held)'))
for name in ('centre','shoulder','rest'):
    a = agg[name]
    print('%-10s %8d %10d %10d %12.1f%%   (per-leg equivalent %.1f%%, buckets/day %.2f)' % (
        name, a['sd'], a['legs'], a['win'], 100*a['win']/a['sd'],
        100*a['win']/a['legs'], a['legs']/a['sd']))

# --- of the days truth landed exactly one ladder step out, was that side on the window? ---
def ords(r):
    return {x['bucket']:x['ordinal'] for x in c.execute(
      "SELECT b.bucket,b.ordinal FROM basket_probability_bridge_history h"
      " JOIN basket_probability_bridge_bucket_probability b ON b.history_id=h.history_id"
      " WHERE h.station_id=? AND h.event_date=? AND h.stage=? AND h.decision_at_utc=?",
      (r['station_id'],r['event_date'],r['stage'],r['decision_at_utc']))}
cnt = collections.Counter()
for k,v in sorted(by_sd.items()):
    s = settle.get(k)
    if s is None: continue
    r = v[0]; o = ords(r); w = json.loads(r['posted_buckets_json'] or '[]')
    if s['settled_bucket'] not in o or r['ladder_center_bucket'] not in o: continue
    d = o[s['settled_bucket']] - o[r['ladder_center_bucket']]
    if abs(d) != 1: continue
    cnt['total'] += 1
    cnt['on_posted_window' if s['settled_bucket'] in w else 'off_posted_window'] += 1
print('\ntruth one ladder step from the ladder centre: %d station-days;'
      ' that bucket was on the posted window %d, off it %d (%.0f%% off)' % (
      cnt['total'], cnt['on_posted_window'], cnt['off_posted_window'],
      100*cnt['off_posted_window']/cnt['total']))
