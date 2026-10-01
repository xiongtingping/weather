"""Re-test of the model-vs-market gap decomposition on ACTUAL served P0 (user: 「重新测试，这个是过期数据」, 2026-10-01).

Same protocol as model_market_gap_decomposition_20260926 (functions imported from its decompose.py):
every committed P0 update for event days 2026-09-20..09-30 paired with the last committed WS book within
30 minutes before the decision; LogScore/Brier gap split into centre / width / shape (two orders averaged);
station-day equal weight, event-day bootstrap.  Difference to 09-26: the model vector is the vector that was
actually served (real arrivals, real seating), not a replay.
"""
import collections, datetime as dt, json, math, pathlib, resource, sqlite3, sys, time
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/model_market_gap_decomposition_20260926'))
import decompose as D
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
from bayes_basket_target.settlement_time import hours_to_settlement_if_known
from bayes_basket_target.probability_bridge import history_envelope_payload
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
resource.setrlimit(resource.RLIMIT_CPU, (1700, 1700)); START = time.monotonic()
DATE0, DATE1 = '2026-09-20', '2026-09-30'
def guard():
    if time.monotonic() - START > 1750 or resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 1536 * 1024**2: raise RuntimeError('resources')

def main():
    c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2); c.execute('pragma query_only=ON')
    now = dt.datetime.now(dt.timezone.utc); now_s = now.isoformat().replace('+00:00', 'Z')
    zones = dict(c.execute('select station_id,settlement_timezone from calibration_station_market_timezone_binding'))
    stations = sorted(r['station'] for r in json.loads((ROOT / 'tmp/model_weight_verify_20260928/evidence.json').read_text())['latest_station_rows'])
    truth = {}
    for r in c.execute(f"select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth where local_date between ? and ? and source_lane in {FORMAL_TRUTH_LANES_SQL} and qc_status='ACCEPTED' order by revision_no", (DATE0, DATE1)):
        truth[(r[0], r[1])] = float(r[2])
    labels = D.load_labels(c)
    items = []; skipped = collections.Counter(); versions = collections.Counter()
    for st in stations:
        for off in range(11):
            guard(); day = (dt.date(2026, 9, 20) + dt.timedelta(days=off)).isoformat(); key = (st, day)
            if key not in truth: skipped['no_truth'] += 1; continue
            midnight = dt.datetime.fromisoformat(day).replace(tzinfo=__import__('zoneinfo').ZoneInfo(zones[st])).astimezone(dt.timezone.utc)
            if midnight + dt.timedelta(days=1) > now: skipped['not_closed'] += 1; continue
            ladder = oos._station_day_ladder(c, station_id=st, event_date=day, as_of_utc=now_s)
            if ladder is None: skipped['no_ladder'] += 1; continue
            geo = D.ladder_geometry(ladder); keys = geo['keys']
            # hit bucket from formal truth on the ladder intervals; cross-check with settlement label when present
            iv = ladder.get('intervals_c') or {}
            hit = None
            if key in labels and labels[key][0] in keys and abs(labels[key][1] - truth[key]) < 0.05: hit = keys.index(labels[key][0])
            if hit is None:
                for i, k in enumerate(keys):
                    lo, hi = (iv.get(k) or (None, None))
                    if (lo is None or truth[key] >= lo) and (hi is None or truth[key] < hi): hit = i; break
            if hit is None: skipped['no_hit'] += 1; continue
            cutoff = (midnight + dt.timedelta(days=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            rows = c.execute("""select checkpoint_id,envelope_content_sha256,envelope_json,decision_at_utc from basket_probability_bridge_history
                where station_id=? and event_date=? and stage='P0' and state='COMMITTED' and decision_at_utc<? order by decision_at_utc""", (st, day, cutoff)).fetchall()
            first_done = False
            for h in rows:
                book = c.execute("""select observed_at,buckets_json from basket_official_ws_snapshot indexed by basket_official_ws_snapshot_asof_idx
                    where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1""", (st, day, h[3])).fetchone()
                if book is None or D._instant(h[3]) - D._instant(book[0]) > D.MAX_BOOK_AGE: skipped['no_fresh_book'] += 1; continue
                market = market_reference_v1(json.loads(book[1]))
                if market['state'] != 'complete' or set(market['vector']) != set(keys): skipped['book_incomplete_or_mismatch'] += 1; continue
                env = history_envelope_payload(c, checkpoint_id=h[0], envelope_content_sha256=h[1], stored=h[2])
                if set(env['probabilities']) != set(keys): skipped['ladder_mismatch'] += 1; continue
                hours = hours_to_settlement_if_known(day, as_of_utc=h[3], settlement_timezone=zones[st])
                b = next((i for i, (lo, hi) in enumerate(D.BINS) if hours is not None and lo <= hours < hi), None)
                if b is None: skipped['no_bin'] += 1; continue
                p = D.as_vec(env['probabilities'], keys); m = D.as_vec(market['vector'], keys)
                d = {'stage': 'P0', 'station': st, 'date': day, 'at': h[3], 'bin': b, 'hours': hours, **geo, 'p': p, 'm': m, 'hit': hit, 'truth_c': truth[key],
                     'version': env['posterior_version'], 'unit': 'F' if any('°F' in k for k in keys) else 'C', 'first_gt36': (not first_done) and hours > 36}
                first_done = first_done or hours > 36
                d.update(D.analyse_decision(d)); items.append(d); versions[d['version']] += 1
        print(st, len(items), round(time.monotonic() - START), flush=True)
    rng = np.random.default_rng(D.SEED)
    def summ(sub):
        if len(sub) < 30: return {'decisions': len(sub)}
        cnt = collections.Counter((d['station'], d['date']) for d in sub)
        out = {'decisions': len(sub), 'station_days': len(cnt), 'days': len({d['date'] for d in sub})}
        for metric in ('ls', 'br'):
            for part in ('gap', 'centre', 'width', 'shape'): out[f'{metric}_{part}'] = D.weighted_ci(sub, lambda d, k=f'{metric}_{part}': d[k], rng)
        out['rmse_p'] = math.sqrt(D.weighted_ci(sub, lambda d: (d['mean_p'] - d['truth_c'])**2, rng)[0]); out['rmse_m'] = math.sqrt(D.weighted_ci(sub, lambda d: (d['mean_m'] - d['truth_c'])**2, rng)[0])
        out['bias_p'] = D.weighted_ci(sub, lambda d: d['mean_p'] - d['truth_c'], rng); out['bias_m'] = D.weighted_ci(sub, lambda d: d['mean_m'] - d['truth_c'], rng)
        out['sd_p'] = D.weighted_ci(sub, lambda d: d['sd_p'], rng)[0]; out['sd_m'] = D.weighted_ci(sub, lambda d: d['sd_m'], rng)[0]
        out['market_closer_frac'] = D.weighted_ci(sub, lambda d: float(abs(d['mean_m'] - d['truth_c']) < abs(d['mean_p'] - d['truth_c'])), rng)[0]
        return out
    strict = [d for d in items if 24 <= d['hours'] < 60]
    periods = {'09-20..09-25 (v8-v15, seating in progress)': lambda d: d['date'] <= '2026-09-25', '09-26..09-30 (v19/v20, six seated)': lambda d: d['date'] >= '2026-09-26'}
    report = {'skipped': dict(skipped), 'versions': dict(versions), 'n_items': len(items), 'all_bins_all_dates': {D.LABELS[i]: summ([d for d in items if d['bin'] == i]) for i in range(6)},
              'strict_24_60h': {k: summ([d for d in strict if f(d)]) for k, f in periods.items()}, 'strict_24_60h_all': summ(strict),
              'by_bin_recent': {D.LABELS[i]: summ([d for d in items if d['bin'] == i and d['date'] >= '2026-09-26']) for i in range(6)},
              'first_gt36h_recent': summ([d for d in items if d['first_gt36'] and d['date'] >= '2026-09-26']),
              'strict_recent_by_unit': {u: summ([d for d in strict if d['date'] >= '2026-09-26' and d['unit'] == u]) for u in ('C', 'F')},
              'strict_recent_by_date': {dd: summ([d for d in strict if d['date'] == dd]) for dd in sorted({d['date'] for d in strict if d['date'] >= '2026-09-26'})}}
    # gap by |mean_p - mean_m| bucket, recent strict
    rec = [d for d in strict if d['date'] >= '2026-09-26']; cnt = collections.Counter((d['station'], d['date']) for d in rec)
    tot = sum(d['ls_gap'] / cnt[(d['station'], d['date'])] for d in rec); totc = sum(d['ls_centre'] / cnt[(d['station'], d['date'])] for d in rec); W = sum(1 / cnt[(d['station'], d['date'])] for d in rec)
    gb = {}
    for lo, hi, lab in ((0, .5, '<0.5C'), (.5, 1, '0.5-1C'), (1, 1.5, '1-1.5C'), (1.5, 99, '>=1.5C')):
        s = [d for d in rec if lo <= abs(d['mean_p'] - d['mean_m']) < hi]
        if not s: continue
        w = [1 / cnt[(d['station'], d['date'])] for d in s]
        gb[lab] = dict(share_decisions=sum(w) / W, share_gap=sum(d['ls_gap'] * x for d, x in zip(s, w)) / tot if tot else None, share_centre=sum(d['ls_centre'] * x for d, x in zip(s, w)) / totc if totc else None,
                       market_closer=float(np.average([abs(d['mean_m'] - d['truth_c']) < abs(d['mean_p'] - d['truth_c']) for d in s], weights=w)))
    report['recent_strict_by_mean_gap'] = gb
    # per station centre loss, recent strict
    ps = collections.defaultdict(list)
    for d in rec: ps[d['station']].append(d['ls_centre'] / cnt[(d['station'], d['date'])])
    report['recent_strict_station_centre_loss_top'] = sorted(((s, round(sum(v) / len({dd for dd in [x for x in [1]]}) if False else sum(v) / len({(d['date']) for d in rec if d['station'] == s}), 3)) for s, v in ps.items()), key=lambda kv: -kv[1])[:10]
    (HERE / 'results_gap_retest.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=float))
    def fmt(v): return '%+.3f [%+.3f,%+.3f]' % tuple(v) if isinstance(v, list) else v
    print('skipped', dict(skipped)); print('versions', dict(versions))
    for sec in ('strict_24_60h', 'by_bin_recent', 'strict_recent_by_unit', 'strict_recent_by_date', 'all_bins_all_dates'):
        print('##', sec)
        for k, v in report[sec].items():
            if 'ls_gap' not in v: print('  ', k, v); continue
            print('   %-42s n=%5d sd=%4d days=%2d | LS gap %s centre %s width %s shape %s | Brier gap %s centre %s | rmse p %.2f m %.2f sd p %.2f m %.2f market closer %.2f' % (
                k, v['decisions'], v['station_days'], v['days'], fmt(v['ls_gap']), fmt(v['ls_centre']), fmt(v['ls_width']), fmt(v['ls_shape']), fmt(v['br_gap']), fmt(v['br_centre']), v['rmse_p'], v['rmse_m'], v['sd_p'], v['sd_m'], v['market_closer_frac']))
    v = report['first_gt36h_recent']; print('## first_gt36h_recent', {k: (fmt(x) if isinstance(x, list) else x) for k, x in v.items() if k in ('decisions', 'station_days', 'ls_gap', 'ls_centre', 'ls_width', 'ls_shape', 'br_gap', 'rmse_p', 'rmse_m')})
    print('## recent strict by |mean gap|', json.dumps(gb, indent=0, default=float)); print('## station centre loss top', report['recent_strict_station_centre_loss_top'])
if __name__ == '__main__': main()
