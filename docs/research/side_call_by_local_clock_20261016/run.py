"""PROTOCOL.md: side call at local 09/12/15 -- served belief vs market -- and observations on our symmetric days.

usage: python3 run.py build   rows -> rows.json.gz
       python3 run.py         score
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as BR  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
from bayes_basket_target.stage_authority import served_belief_row  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
INPUTS = ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db'
CLOCKS = (9, 12, 15)
SINCE, OBS_UNTIL = '2026-09-01', '2026-10-14'
SEED, DRAWS, MIN_TRAIN_DAYS = 7, 2000, 10


def ts(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00'))


def z(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def build():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    tzs = dict(c.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    inp = sqlite3.connect(INPUTS)
    obs = defaultdict(list)
    for st, valid, temp, comm in inp.execute(
            "select station_id, valid_at, temp_c, committed_at from obs where temp_c is not null and coalesce(correction_suspected,0)=0"):
        obs[st].append((ts(comm), ts(valid), float(temp)))
    labels = {k: v for k, v in knowable_labels(c).items() if str(k[1]) >= SINCE}
    cache_b, cache_m = {}, {}
    out = gzip.open(HERE / 'rows.json.gz.tmp', 'wt'); n = 0; miss = defaultdict(int)
    for (st, day), lab in sorted(labels.items()):
        if st not in tzs: miss['no tz'] += 1; continue
        tz = ZoneInfo(tzs[st]); settled = str(lab['bucket'])
        for hour in CLOCKS:
            t = datetime.fromisoformat(day).replace(hour=hour, tzinfo=tz); at = z(t)
            stage, p, order, rung = BR._served_belief_vector(c, st, day, at, cache_b)
            q = BR._ladder_market_reference(c, st, day, at, cache_m)
            if p is None: miss['no belief'] += 1; continue
            if q is None: miss['no market'] += 1; continue
            if set(p) != set(q) or settled not in p: miss['ladder mismatch'] += 1; continue
            axis = [{'bucket': b, 'ordinal': o} for b, o in sorted(order.items(), key=lambda x: x[1])]
            mp = BR._skew_point(p, axis, settled); mq = BR._skew_point(q, axis, settled)
            if mp is None or mq is None: miss['unreadable'] += 1; continue
            hid = served_belief_row(c, station_id=st, event_date=day, at_utc=at)['history_id']
            bounds = {str(b): (lo, hi) for b, lo, hi in c.execute(
                'select bucket, lower_c, upper_c from basket_probability_bridge_bucket_probability where history_id=?', (hid,))}
            lo, hi = bounds.get(mp['mode'], (None, None))
            centre = (lo + hi) / 2 if lo is not None and hi is not None else (hi - 0.5 * rung if lo is None and hi is not None and rung else lo + 0.5 * rung if lo is not None and rung else None)
            rec = {'st': st, 'day': day, 'hour': hour, 'stage': stage, 'model': mp, 'market': mq}
            if centre is not None and rung and day <= OBS_UNTIL:
                seen = sorted((v, temp) for comm, v, temp in obs.get(st, []) if comm <= t and v.astimezone(tz).date().isoformat() == day)
                if seen:
                    rec['f_high'] = (max(x[1] for x in seen) - centre) / rung
                    rec['f_last'] = (seen[-1][1] - centre) / rung
                    lv = seen[-1][0]
                    prior = [x for x in seen if timedelta(hours=1.5) <= lv - x[0] <= timedelta(hours=2.5)]
                    if prior:
                        pr = min(prior, key=lambda x: abs((lv - x[0]).total_seconds() - 7200))
                        rec['f_rate'] = (seen[-1][1] - pr[1]) / ((lv - pr[0]).total_seconds() / 3600)
            out.write(json.dumps(rec) + '\n'); n += 1
    out.close(); (HERE / 'rows.json.gz.tmp').rename(HERE / 'rows.json.gz')
    print('rows', n, dict(miss))


def mi(rows, src):
    off = [r[src] for r in rows if r[src]['truth_side'] != 0]; n = len(off)
    if not n: return None
    v = 0.0
    for s in (-1, 0, 1):
        ps = sum(p['sign'] == s for p in off) / n
        for t in (-1, 1):
            pt = sum(p['truth_side'] == t for p in off) / n
            j = sum(p['sign'] == s and p['truth_side'] == t for p in off) / n
            if j > 0: v += j * math.log2(j / (ps * pt))
    return v


def boot(vals, days):
    ud = sorted(set(days)); idx = {d: np.where(np.asarray(days) == d)[0] for d in ud}
    rng = np.random.default_rng(SEED); v = np.asarray(vals, float); out = []
    for _ in range(DRAWS):
        pick = np.concatenate([idx[d] for d in rng.choice(ud, len(ud))]); out.append(v[pick].mean())
    return [float(np.quantile(out, .025)), float(np.quantile(out, .975))]


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


def logistic(X, y, l2=1e-2):
    w = np.zeros(X.shape[1])
    for _ in range(100):
        p = 1 / (1 + np.exp(-X @ w)); g = X.T @ (p - y) + l2 * w
        H = (X.T * (p * (1 - p))) @ X + l2 * np.eye(X.shape[1]); s = np.linalg.solve(H, g); w -= s
        if np.abs(s).max() < 1e-8: break
    return w


def part_one(R, label, res):
    off = lambda src: [r for r in R if r[src]['truth_side'] != 0]
    line = {}
    for src in ('model', 'market'):
        o = off(src); called = [r for r in o if r[src]['sign'] != 0]
        line[src] = {'called_share': len(called) / len(o) if o else None,
                     'hit': (sum(r[src]['sign'] == r[src]['truth_side'] for r in called) / len(called)) if called else None,
                     'mi': mi(R, src), 'off': len(o)}
    both = [r for r in R if all(r[s]['sign'] != 0 and r[s]['truth_side'] != 0 for s in ('model', 'market'))]
    d = [float(r['model']['sign'] == r['model']['truth_side']) - float(r['market']['sign'] == r['market']['truth_side']) for r in both]
    diff = (float(np.mean(d)), boot(d, [r['day'] for r in both])) if len(both) >= 20 else (None, None)
    cats = defaultdict(lambda: [0, 0, 0])
    for r in R:
        m, k = r['model'], r['market']
        mc, kc = m['sign'] != 0 and m['truth_side'] != 0, k['sign'] != 0 and k['truth_side'] != 0
        key = ('同向' if m['sign'] == k['sign'] else '反向') if mc and kc else '只有市场' if kc else '只有我们' if mc else None
        if key: cats[key][0] += 1; cats[key][1] += mc and m['sign'] == m['truth_side']; cats[key][2] += kc and k['sign'] == k['truth_side']
    stages = defaultdict(int)
    for r in R: stages[r['stage']] += 1
    print(f"== {label}: {len(R)} 站日，{len({r['day'] for r in R})} 个事件日，信念阶段 {dict(stages)}")
    for src, w in (('model', '我们'), ('market', '市场')):
        x = line[src]; print(f"  {w}：判出 {x['called_share']:.1%}  判对 {x['hit']:.1%}  {x['mi']:.3f} bit")
    if diff[0] is not None:
        print(f"  两边都判出（{len(both)} 行）我们−市场 判对 {diff[0]:+.3f} [{diff[1][0]:+.3f}, {diff[1][1]:+.3f}]")
    for key in ('同向', '反向', '只有市场', '只有我们'):
        n, a, b = cats[key]
        if n: print(f"    {key:4s} {n:4d} 行  我们判对 {'—' if key == '只有市场' else f'{a / n:.1%}'}  市场判对 {'—' if key == '只有我们' else f'{b / n:.1%}'}")
    res[label] = {'n': len(R), 'stages': dict(stages), 'sides': line, 'paired_diff': diff, 'cats': {k: list(v) for k, v in cats.items()}}


def part_two(R, label, res):
    F = ('f_high', 'f_last', 'f_rate')
    for pop in ('market_leans', 'all_symmetric'):
        S = [r for r in R if r['model']['sign'] == 0 and r['model']['truth_side'] != 0 and 'f_high' in r
             and (pop == 'all_symmetric' or r['market']['sign'] != 0)]
        out = {'n': len(S)}
        print(f"  -- 第二部分 {label} · {pop}: {len(S)} 行（有实况），真值在上方 {np.mean([r['model']['truth_side'] > 0 for r in S]) if S else float('nan'):.1%}")
        if len(S) < 30: res[f'{label}|{pop}'] = out; continue
        y = np.array([r['model']['truth_side'] > 0 for r in S], float); dd = np.array([r['day'] for r in S])
        for f in F:
            T = [r for r in S if f in r]
            if len(T) < 30: print(f"     {f} n={len(T)} 不足"); continue
            a = auc([r[f] for r in T], [r['model']['truth_side'] > 0 for r in T]); ci = boot_auc([r[f] for r in T], [r['model']['truth_side'] > 0 for r in T], [r['day'] for r in T])
            out[f] = [a, ci, len(T)]; print(f"     {f:6s} n={len(T):4d} AUC {a:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]")
        T = sorted([r for r in S if all(f in r for f in F)], key=lambda r: r['day'])
        if len(T) < 30: print(f"     组合：三项都有 {len(T)} 行，不足"); res[f'{label}|{pop}'] = out; continue
        hours = sorted({r['hour'] for r in T})
        X = np.array([[1.0] + [r[f] for f in F] + [float(r['hour'] == h) for h in hours[1:]] for r in T])
        mu = X[:, 1:4].mean(0); sd = X[:, 1:4].std(0) + 1e-9; X[:, 1:4] = (X[:, 1:4] - mu) / sd
        yy = np.array([r['model']['truth_side'] > 0 for r in T], float); d2 = np.array([r['day'] for r in T])
        pred = np.full(len(T), np.nan)
        for d in sorted(set(d2)):
            tr = d2 < d
            if len(set(d2[tr])) < MIN_TRAIN_DAYS: continue
            w = logistic(X[tr], yy[tr]); pred[d2 == d] = 1 / (1 + np.exp(-X[d2 == d] @ w))
        ok = ~np.isnan(pred)
        if ok.sum() < 30: print(f"     组合前推只有 {int(ok.sum())} 行"); res[f'{label}|{pop}'] = out; continue
        a = auc(pred[ok], yy[ok]); ci = boot_auc(pred[ok], yy[ok], list(d2[ok]))
        right = ((pred[ok] > 0.5) == (yy[ok] == 1)).astype(float); rci = boot(right, list(d2[ok]))
        mk = [T[i]['market']['sign'] == T[i]['market']['truth_side'] for i in np.where(ok)[0]
              if T[i]['market']['sign'] != 0 and T[i]['market']['truth_side'] != 0]
        out['combo'] = {'n': int(ok.sum()), 'auc': a, 'ci': ci, 'hit': float(right.mean()), 'hit_ci': rci, 'market_hit': float(np.mean(mk)) if mk else None, 'market_n': len(mk)}
        print(f"     组合前推 n={int(ok.sum())} AUC {a:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]  判对 {right.mean():.1%} [{rci[0]:.1%}, {rci[1]:.1%}]"
              + (f"  | 同批市场判对 {np.mean(mk):.1%}（{len(mk)}）" if mk else ''))
        w = logistic(X, yy); print('     全样本系数（标准化）:', dict(zip(F, np.round(w[1:4], 2))))
        res[f'{label}|{pop}'] = out


def score():
    R = [json.loads(l) for l in gzip.open(HERE / 'rows.json.gz', 'rt')]
    res = {}
    for h in CLOCKS:
        part_one([r for r in R if r['hour'] == h], f'{h:02d}:00', res)
    print()
    for h in CLOCKS:
        part_two([r for r in R if r['hour'] == h], f'{h:02d}:00', res)
    part_two(R, '三个钟点合并', res)
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    build() if len(sys.argv) > 1 and sys.argv[1] == 'build' else score()
