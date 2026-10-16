"""PROTOCOL.md: morning P1 shape above the observed high vs the market.

usage: python3 run.py build   -> rows.json.gz
       python3 run.py         score
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as BR  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
HOURS = (7, 8, 9, 10, 11, 12)
SINCE, FLOOR, MIN_UP = '2026-09-01', 1e-3, 0.02
SEED, DRAWS = 7, 2000


def z(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def build():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    tzs = dict(c.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    labels = {k: v for k, v in knowable_labels(c).items() if str(k[1]) >= SINCE}
    cb, cm = {}, {}
    out = gzip.open(HERE / 'rows.json.gz.tmp', 'wt'); n = 0; miss = defaultdict(int)
    for (st, day), lab in sorted(labels.items()):
        if st not in tzs: continue
        tz = ZoneInfo(tzs[st]); settled = str(lab['bucket'])
        for hour in HOURS:
            at = z(datetime.fromisoformat(day).replace(hour=hour, tzinfo=tz))
            stage, p, order, _rung = BR._served_belief_vector(c, st, day, at, cb)
            if stage != 'P1' or p is None: miss['not P1'] += 1; continue
            q = BR._ladder_market_reference(c, st, day, at, cm)
            if q is None: miss['no market'] += 1; continue
            if set(q) != set(p) or settled not in p: miss['ladder'] += 1; continue
            env = c.execute("select lineage_json from basket_probability_envelope INDEXED BY basket_probability_envelope_station_stage_recency_idx"
                            " where station_id=? and stage='P1' and state='COMMITTED' and decision_at_utc<=? and event_date=?"
                            " order by decision_at_utc desc limit 1", (st, at, day)).fetchone()
            if env is None: miss['no envelope'] += 1; continue
            lin = json.loads(env[0])
            try:
                high = float(lin.get('reported_high_c'))
            except (TypeError, ValueError):
                miss['no high'] += 1; continue
            iv = json.loads(lin['bucket_intervals_c_json'])
            hb = next((b for b, (lo, hi) in iv.items() if (lo is None or lo <= high) and (hi is None or high < hi)), None)
            if hb is None or hb not in order: miss['high off ladder'] += 1; continue
            feats = (json.loads(lin.get('p1_observation_likelihood_json') or '{}').get('features')) or {}
            out.write(json.dumps({'st': st, 'day': day, 'hour': hour, 'order': order, 'p': p, 'q': q, 'settled': settled,
                                  'h': hb, 'feats': {k: feats.get(k) for k in ('precipitation_present', 'cloud_cover_fraction', 'drawdown_from_high_c')}}) + '\n')
            n += 1
    out.close(); (HERE / 'rows.json.gz.tmp').rename(HERE / 'rows.json.gz')
    print('rows', n, dict(miss))


def cond(vec, keys):
    m = sum(vec[k] for k in keys)
    return None if m < MIN_UP else {k: vec[k] / m for k in keys}, m


def side_call(v, keys_sorted, order):
    m = max(keys_sorted, key=lambda k: (v[k], -order[k]))
    mean = sum(v[k] * order[k] for k in keys_sorted)
    gap = mean - order[m]
    return m, (1 if gap > 1e-9 else -1 if gap < -1e-9 else 0)


def score():
    R = [json.loads(l) for l in gzip.open(HERE / 'rows.json.gz', 'rt')]
    recs = []
    for r in R:
        order = r['order']; up = sorted([k for k in order if order[k] > order[r['h']]], key=lambda k: order[k])
        if not up: continue
        rose = order[r['settled']] > order[r['h']]
        pc, pm = cond(r['p'], up); qc, qm = cond(r['q'], up)
        rec = {'st': r['st'], 'day': r['day'], 'hour': r['hour'], 'rose': rose, 'p_up': pm, 'q_up': qm, 'feats': r['feats']}
        if rose and pc and qc:
            t = r['settled']
            rec['ls_p'] = -math.log(max(pc[t], FLOOR)); rec['ls_q'] = -math.log(max(qc[t], FLOOR))
            for name, v in (('p', pc), ('q', qc)):
                m, s = side_call(v, up, order)
                rec[f'mode_{name}'] = float(m == t)
                ts = (order[t] > order[m]) - (order[t] < order[m])
                rec[f'call_{name}'] = None if s == 0 or ts == 0 else float(s == ts)
            rec['dist_up'] = order[t] - order[r['h']]
        recs.append(rec)
    rose_rows = [x for x in recs if 'ls_p' in x]
    print(f'行 {len(recs)}（站日 {len({(x["st"], x["day"]) for x in recs})}）；真值高于已测最高 {np.mean([x["rose"] for x in recs]):.1%}；'
          f'进入条件评估 {len(rose_rows)} 行 / {len({(x["st"], x["day"]) for x in rose_rows})} 站日')

    def station_days(rows):
        g = defaultdict(list)
        for x in rows: g[(x['st'], x['day'])].append(x)
        out = []
        for (st, day), xs in g.items():
            item = {'st': st, 'day': day}
            for k in ('ls_p', 'ls_q', 'mode_p', 'mode_q'):
                item[k] = float(np.mean([x[k] for x in xs]))
            for k in ('call_p', 'call_q'):
                v = [x[k] for x in xs if x[k] is not None]; item[k] = float(np.mean(v)) if v else None
            out.append(item)
        return out

    def boot(vals, days):
        ud = sorted(set(days)); idx = {d: np.where(np.asarray(days) == d)[0] for d in ud}
        rng = np.random.default_rng(SEED); v = np.asarray(vals, float); res = []
        for _ in range(DRAWS):
            pick = np.concatenate([idx[d] for d in rng.choice(ud, len(ud))]); res.append(v[pick].mean())
        return [float(np.quantile(res, .025)), float(np.quantile(res, .975)),
                float(max(2 * min((np.asarray(res) >= 0).mean(), (np.asarray(res) <= 0).mean()), 1 / DRAWS))]

    def report(label, rows):
        S = station_days(rows)
        if len(S) < 10: print(f'  {label}: {len(S)} 站日（不足）'); return None
        d = [x['ls_p'] - x['ls_q'] for x in S]; days = [x['day'] for x in S]; ci = boot(d, days)
        cp = [x['call_p'] for x in S if x['call_p'] is not None]; cq = [x['call_q'] for x in S if x['call_q'] is not None]
        print(f"  {label:22s} 站日 {len(S):4d}  条件LogScore 我们 {np.mean([x['ls_p'] for x in S]):.3f} 市场 {np.mean([x['ls_q'] for x in S]):.3f}  Δ {np.mean(d):+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}]"
              f"  众数命中 {np.mean([x['mode_p'] for x in S]):.1%}/{np.mean([x['mode_q'] for x in S]):.1%}  判左右 {np.mean(cp) if cp else float('nan'):.1%}/{np.mean(cq) if cq else float('nan'):.1%}")
        return {'n': len(S), 'dls': float(np.mean(d)), 'ci': ci[:2], 'p': ci[2], 'ls_p': float(np.mean([x['ls_p'] for x in S])),
                'ls_q': float(np.mean([x['ls_q'] for x in S])), 'mode': [float(np.mean([x['mode_p'] for x in S])), float(np.mean([x['mode_q'] for x in S]))],
                'call': [float(np.mean(cp)) if cp else None, float(np.mean(cq)) if cq else None]}

    res = {'all': report('全体', rose_rows)}
    # where the truth landed above the high
    dist = defaultdict(int)
    for x in rose_rows: dist[min(x['dist_up'], 4)] += 1
    print('  真值离已测最高档（档）分布:', dict(sorted(dist.items())))
    groups = {
        '有雨': lambda f: f.get('precipitation_present') == 1, '无雨': lambda f: f.get('precipitation_present') == 0,
        '全阴(≥0.875)': lambda f: (f.get('cloud_cover_fraction') or -1) >= 0.875, '非全阴': lambda f: f.get('cloud_cover_fraction') is not None and f['cloud_cover_fraction'] < 0.875,
        '低于最高 0': lambda f: f.get('drawdown_from_high_c') == 0, '低于最高 1–2': lambda f: f.get('drawdown_from_high_c') in (1, 2, 1.0, 2.0),
        '低于最高 ≥3': lambda f: (f.get('drawdown_from_high_c') or -1) >= 3,
        '有雨+全阴+低≥2': lambda f: f.get('precipitation_present') == 1 and (f.get('cloud_cover_fraction') or 0) >= 0.875 and (f.get('drawdown_from_high_c') or 0) >= 2,
    }
    for g, fn in groups.items():
        res[g] = report(g, [x for x in rose_rows if fn(x['feats'])])
    # rise or not (reported, not a criterion)
    rb = [((x['p_up'] - x['rose']) ** 2, (x['q_up'] - x['rose']) ** 2) for x in recs]
    print(f"  「升不升」Brier 我们 {np.mean([a for a, _ in rb]):.3f} 市场 {np.mean([b for _, b in rb]):.3f}（{len(rb)} 行）")
    per = {}
    for st in sorted({x['st'] for x in rose_rows}):
        S = station_days([x for x in rose_rows if x['st'] == st])
        if len(S) < 10: continue
        d = [x['ls_p'] - x['ls_q'] for x in S]; ci = boot(d, [x['day'] for x in S])
        per[st] = [float(np.mean(d)), ci[0], ci[1], ci[2], len(S)]
    passed = A.bh({s: v[3] for s, v in per.items()})
    better = sorted(s for s in passed if per[s][0] < 0); worse = sorted(s for s in passed if per[s][0] > 0)
    print(f"  按站（≥10 站日）{len(per)} 站：BH 显著好 {better}  显著差 {worse}")
    print('   ' + '  '.join(f"{s} {v[0]:+.2f}" for s, v in sorted(per.items(), key=lambda kv: kv[1][0])))
    res['stations'] = per; res['better'] = better; res['worse'] = worse
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    build() if len(sys.argv) > 1 and sys.argv[1] == 'build' else score()
