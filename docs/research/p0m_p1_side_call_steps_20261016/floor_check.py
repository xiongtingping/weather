import sys, json, sqlite3
from collections import defaultdict
sys.path.insert(0, '/Users/xiong/weather')
from bayes_basket_target import board_readings as BR
from bayes_basket_target.station_day_board import ladder_axis
c = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=10)
rows = [r for r in BR.skew_truth_rows(c, since='2026-09-01') if r['market'] and r['stage'] in ('P0-M', 'P1')]
for stage in ('P0-M', 'P1'):
    cats = defaultdict(lambda: [0, 0])
    for r in [x for x in rows if x['stage'] == stage]:
        m, k = r['model'], r['market']
        mc, kc = m['sign'] != 0 and m['truth_side'] != 0, k['sign'] != 0 and k['truth_side'] != 0
        if not kc: continue
        env = c.execute("select bucket_ordinals_json, lineage_json from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc=? and stage=? limit 1",
                        (r['station_id'], r['event_date'], r['decided_at_utc'], r['stage'])).fetchone()
        lin = json.loads(env[1]); ords = json.loads(env[0])
        floor = lin.get('observed_hard_excluded_buckets') if stage == 'P0-M' else lin.get('hard_excluded_buckets')
        ex = {b.strip() for b in str(floor or '').split(',') if b.strip()}
        # the market's mode sits on the lowest still-possible rung -> 'below the mode' is impossible
        lowest_alive = min((o for b, o in ords.items() if b not in ex), default=None)
        at_floor = ords.get(k['mode']) == lowest_alive and bool(ex)
        key = ('market only' if not mc else 'both') + (' | 市场众数在已测下界' if at_floor else ' | 众数在下界之上')
        cats[key][0] += 1; cats[key][1] += k['sign'] == k['truth_side']
    print('==', stage)
    for key, (n, right) in sorted(cats.items()):
        print(f'   {key:24s} n={n:4d} 市场判对 {right / n:.1%}')
