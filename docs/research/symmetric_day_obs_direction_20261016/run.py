"""PROTOCOL.md: on the P0-M/P1 days our vector reads symmetric, do the observations in hand separate the side?

usage: python3 run.py
"""
from __future__ import annotations

import json, math, sqlite3, sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as BR  # noqa: E402
from bayes_basket_target.station_day_board import ladder_axis  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
INPUTS = ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db'
FEATS = ('f_high', 'f_last', 'f_rate', 'f_hour')
SEED, DRAWS, MIN_TRAIN_DAYS = 7, 2000, 10


def ts(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00'))


def auc(score, y):
    score = np.asarray(score, float); y = np.asarray(y, bool)
    if y.all() or (~y).all(): return float('nan')
    allv = np.concatenate([score[y], score[~y]]); ranks = allv.argsort().argsort() + 1.0
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    ranks = (np.bincount(inv, ranks) / cnt)[inv]; npos = y.sum()
    return float((ranks[:npos].sum() - npos * (npos + 1) / 2) / (npos * (~y).sum()))


def boot_auc(score, y, days):
    ud = sorted(set(days)); idx = {d: np.where(np.asarray(days) == d)[0] for d in ud}
    rng = np.random.default_rng(SEED); vals = []
    for _ in range(DRAWS):
        pick = np.concatenate([idx[d] for d in rng.choice(ud, len(ud))])
        vals.append(auc(np.asarray(score)[pick], np.asarray(y)[pick]))
    return [float(np.nanquantile(vals, .025)), float(np.nanquantile(vals, .975))]


def boot_mean(vals, days):
    ud = sorted(set(days)); idx = {d: np.where(np.asarray(days) == d)[0] for d in ud}
    rng = np.random.default_rng(SEED); out = []
    v = np.asarray(vals, float)
    for _ in range(DRAWS):
        pick = np.concatenate([idx[d] for d in rng.choice(ud, len(ud))]); out.append(v[pick].mean())
    return [float(np.quantile(out, .025)), float(np.quantile(out, .975))]


def logistic(X, y, l2=1e-3):
    w = np.zeros(X.shape[1])
    for _ in range(100):
        p = 1 / (1 + np.exp(-X @ w)); g = X.T @ (p - y) + l2 * w
        H = (X.T * (p * (1 - p))) @ X + l2 * np.eye(X.shape[1]); step = np.linalg.solve(H, g); w -= step
        if np.abs(step).max() < 1e-8: break
    return w


def main():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    inp = sqlite3.connect(INPUTS)
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    obs = defaultdict(list)  # station -> [(committed, valid, temp)]
    for st, valid, temp, comm in inp.execute(
            "select station_id, valid_at, temp_c, committed_at from obs where temp_c is not null and coalesce(correction_suspected,0)=0"):
        obs[st].append((ts(comm), ts(valid), float(temp)))
    rows = [r for r in BR.skew_truth_rows(c, since='2026-09-01') if r['stage'] in ('P0-M', 'P1')]
    recs = []; miss = defaultdict(int)
    for r in rows:
        m = r['model']
        if m['sign'] != 0 or m['truth_side'] == 0: continue
        env = c.execute("select bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json') from basket_probability_envelope"
                        " where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc=? and stage=? limit 1",
                        (r['station_id'], r['event_date'], r['decided_at_utc'], r['stage'])).fetchone()
        axis = ladder_axis(json.loads(env[0]), json.loads(env[1]))
        centre = next(a['centre_c'] for a in axis if a['bucket'] == m['mode'])
        widths = [a['hi_c'] - a['lo_c'] for a in axis if a['lo_c'] is not None and a['hi_c'] is not None]
        w = float(np.median(widths)) if widths else 1.0
        tz = ZoneInfo(tzs.get(r['station_id']) or 'UTC'); t = ts(r['decided_at_utc'])
        day = r['event_date']
        seen = sorted((v, temp) for comm, v, temp in obs.get(r['station_id'], [])
                      if comm <= t and v.astimezone(tz).date().isoformat() == day)
        rec = {'stage': r['stage'], 'station': r['station_id'], 'day': day, 'y': int(m['truth_side'] > 0),
               'market': r['market']['sign'] if r['market'] else None,
               'market_right': (r['market']['sign'] == r['market']['truth_side']) if r['market'] and r['market']['sign'] != 0 and r['market']['truth_side'] != 0 else None,
               'f_hour': t.astimezone(tz).hour + t.astimezone(tz).minute / 60}
        if seen:
            rec['f_high'] = (max(x[1] for x in seen) - centre) / w
            rec['f_last'] = (seen[-1][1] - centre) / w
            last_v = seen[-1][0]
            prior = [x for x in seen if timedelta(hours=1.5) <= last_v - x[0] <= timedelta(hours=2.5)]
            if prior:
                p = min(prior, key=lambda x: abs((last_v - x[0]).total_seconds() - 7200))
                rec['f_rate'] = (seen[-1][1] - p[1]) / ((last_v - p[0]).total_seconds() / 3600)
        else:
            miss['no obs yet'] += 1
        recs.append(rec)
    print('rows (our symmetric, truth off our mode):', len(recs), dict(miss))
    result = {}
    for stage in ('P0-M', 'P1'):
        for pop in ('market_leans', 'all_symmetric'):
            R = [x for x in recs if x['stage'] == stage and (pop == 'all_symmetric' or (x['market'] not in (None, 0)))]
            if not R: continue
            days = [x['day'] for x in R]
            res = {'n': len(R), 'event_days': len(set(days)), 'above_share': float(np.mean([x['y'] for x in R])), 'single': {}}
            print(f"\n== {stage} | {pop}: {len(R)} 行，{len(set(days))} 个事件日，真值在上方 {res['above_share']:.1%}")
            for f in FEATS:
                S = [x for x in R if f in x]
                if len(S) < 30 or len({x['y'] for x in S}) < 2:
                    res['single'][f] = {'n': len(S)}
                    print(f"  {f:7s} n={len(S):4d} （不足 30 行，不算）")
                    continue
                a = auc([x[f] for x in S], [x['y'] for x in S]); ci = boot_auc([x[f] for x in S], [x['y'] for x in S], [x['day'] for x in S])
                res['single'][f] = {'n': len(S), 'auc': a, 'ci': ci}
                print(f"  {f:7s} n={len(S):4d} AUC {a:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]")
            S = [x for x in R if all(f in x for f in FEATS)]
            S.sort(key=lambda x: x['day'])
            if len(S) < 30:
                print(f"  组合：四项都有的只有 {len(S)} 行，不算")
                result[f'{stage}|{pop}'] = res
                continue
            X = np.array([[1.0] + [x[f] for f in FEATS] for x in S]); y = np.array([x['y'] for x in S]); dd = np.array([x['day'] for x in S])
            mu = X[:, 1:].mean(0); sd = X[:, 1:].std(0) + 1e-9; X[:, 1:] = (X[:, 1:] - mu) / sd
            pred = np.full(len(S), np.nan)
            for d in sorted(set(dd)):
                tr = dd < d
                if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
                wts = logistic(X[tr], y[tr]); pred[dd == d] = 1 / (1 + np.exp(-X[dd == d] @ wts))
            ok = ~np.isnan(pred)
            if ok.sum() >= 30:
                a = auc(pred[ok], y[ok]); ci = boot_auc(pred[ok], y[ok], list(dd[ok]))
                right = ((pred[ok] > 0.5) == (y[ok] == 1)).astype(float); rci = boot_mean(right, list(dd[ok]))
                mk = [S[i]['market_right'] for i in np.where(ok)[0]]
                mk_called = [v for v in mk if v is not None]
                res['combo'] = {'n': int(ok.sum()), 'days': len(set(dd[ok])), 'auc': a, 'auc_ci': ci, 'hit': float(right.mean()), 'hit_ci': rci,
                                'market_hit_same_rows': float(np.mean(mk_called)) if mk_called else None, 'market_called': len(mk_called)}
                print(f"  组合前推 n={int(ok.sum())}（{len(set(dd[ok]))} 日）AUC {a:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]  判对 {right.mean():.1%} [{rci[0]:.1%}, {rci[1]:.1%}]"
                      f"  | 同批行市场判对 {np.mean(mk_called):.1%}（判出 {len(mk_called)}）" if mk_called else '')
                wfull = logistic(X, y); print('  全样本系数（标准化）:', dict(zip(FEATS, np.round(wfull[1:], 2))))
            result[f'{stage}|{pop}'] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
