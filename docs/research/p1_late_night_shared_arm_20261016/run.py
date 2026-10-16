"""PROTOCOL.md: the charter 274 form + hours-past-peak squared + remaining <= 3 h gate, strict walk-forward.

Reuses p1_slope_refit_arms_20261016 (design.pkl) and p1_evening_arm_20261016 (its EveningArm and walk as CURRENT).
usage: python3 run.py walk [--max-days N]   -> walk.jsonl (resumable)
       python3 run.py score                 -> readings.json
"""
from __future__ import annotations

import importlib.util, json, math, pickle, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREV = ROOT / 'docs/research/p1_slope_refit_arms_20261016'
EVE = ROOT / 'docs/research/p1_evening_arm_20261016'
sys.path.insert(0, str(PREV))
import run as R  # noqa: E402
spec = importlib.util.spec_from_file_location('evening_run', EVE / 'run.py')
E = importlib.util.module_from_spec(spec); spec.loader.exec_module(E)


def non_us(meta):
    return np.array([0.0 if str(m['st']).startswith('K') else 1.0 for m in meta])


class SharedNightArm(E.EveningArm):
    """The charter 274 form + hours-past-peak squared + the remaining <= 3 h gate."""

    def _night(self, idx, s):
        z = np.stack([self.hpp[idx] ** 2, (self.clock[idx] <= 3.0).astype(float)], 1)
        return (z - s['nt_mean']) / s['nt_scale']

    def x(self, idx, scaling):
        return np.hstack([super().x(idx, scaling), self._night(idx, scaling)])

    def scaling(self, idx):
        s = super().scaling(idx)
        s['nt_mean'] = 0.0; s['nt_scale'] = 1.0
        z = self._night(idx, s); s['nt_mean'] = z.mean(0); s['nt_scale'] = z.std(0) + 1e-9
        return s


def walk(max_days):
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    ext, var = R.extras(design)
    arm = SharedNightArm(design, ext, var)
    prev = [json.loads(l) for l in open(PREV / 'walk.jsonl')]
    out_path = HERE / 'walk.jsonl'
    done = {json.loads(l)['day'] for l in open(out_path)} if out_path.exists() else set()
    usable = np.where(design['usable'])[0]; th = None; n = 0
    for rec_prev in prev:
        day = rec_prev['day']
        if day in done: continue
        if n >= max_days: break
        t0 = time.time()
        rows_d = np.array(rec_prev['rows'])
        start = float(design['as_of'][rows_d].min())
        train = usable[design['known_at'][usable] <= start]
        lam, sc, th, _ = arm.choose(train, start=th)
        with open(out_path, 'a') as f:
            f.write(json.dumps({'day': day, 'rows': rows_d.tolist(), 'arms': {'SHARED': arm.served(rows_d, sc, th).tolist()},
                                'params': {'SHARED': {'lambda': lam}}}) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)} λ {lam} ({time.time() - t0:.0f}s)", flush=True)


def score():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    cur = {json.loads(l)['day']: json.loads(l) for l in open(EVE / 'walk.jsonl')}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    rows = []
    for rec in recs:
        c = cur[rec['day']]
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']
            item = {'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]), 'night': float(design['hours'][i]) <= 3.0,
                    'us': str(m['st']).startswith('K'), 'rose': float(t > run)}
            for a, v in (('CURRENT', c['arms']['EVENING'][j]), ('SHARED', rec['arms']['SHARED'][j])):
                p = np.asarray(v); item[f'n_{a}'] = -math.log(max(p[t], 1e-300)); item[f'up_{a}'] = float(p[run + 1:].sum())
            rows.append(item)
    rng = np.random.default_rng(7)

    def ci(sel):
        S = [r for r in rows if sel(r)]
        if not S: return None
        by = defaultdict(lambda: [0.0, 0.0])
        for r in S: by[r['day']][0] += r['w'] * (r['n_SHARED'] - r['n_CURRENT']); by[r['day']][1] += r['w']
        ud = sorted(by); tot = np.array([by[d][0] for d in ud]); wt = np.array([by[d][1] for d in ud])
        picks = rng.integers(0, len(ud), size=(2000, len(ud))); bs = tot[picks].sum(1) / wt[picks].sum(1)
        return [float(tot.sum() / wt.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), len(S), len(ud),
                float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / 2000))]

    sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
    import analyze as A  # noqa: PLC0415
    fmt = lambda v: '—' if v is None else f"{v[0]:+.4f} [{v[1]:+.4f}, {v[2]:+.4f}]（{v[3]} 行）"
    res = {k: ci(f) for k, f in (('full', lambda r: True), ('night', lambda r: r['night']),
                                  ('night_us', lambda r: r['night'] and r['us']), ('night_non_us', lambda r: r['night'] and not r['us']))}
    print(f"评估 {len(recs)} 个事件日、{len(rows)} 行")
    for k, v in res.items(): print(f"  {k:13s} {fmt(v)}")
    res['calibration'] = {}
    print('  深夜「还会升」校准（平均 / 实际 / Brier）')
    for name, us in (('美国', True), ('非美国', False)):
        S = [r for r in rows if r['night'] and r['us'] == us]
        out = {'n': len(S), 'actual': float(np.mean([r['rose'] for r in S]))}
        for a in ('CURRENT', 'SHARED'):
            out[a] = [float(np.mean([r[f'up_{a}'] for r in S])), float(np.mean([(r[f'up_{a}'] - r['rose']) ** 2 for r in S]))]
        res['calibration'][name] = out
        print(f"    {name:4s} 行 {out['n']:5d}  实际 {out['actual']:.4f}  现行 {out['CURRENT'][0]:.4f}（Brier {out['CURRENT'][1]:.5f}）  庚 {out['SHARED'][0]:.4f}（Brier {out['SHARED'][1]:.5f}）")
    for scope, sel in (('整梯子', lambda r, st: r['st'] == st), ('深夜', lambda r, st: r['st'] == st and r['night'])):
        per = {}
        for st in sorted({r['st'] for r in rows}):
            v = ci(lambda r, st=st: sel(r, st))
            if v and v[4] >= 10: per[st] = v
        passed = A.bh({s: v[5] for s, v in per.items()})
        better = sorted(s for s in passed if per[s][0] < 0); worse = sorted(s for s in passed if per[s][0] > 0)
        res[f'stations_{scope}'] = {'per': per, 'better': better, 'worse': worse}
        print(f"  按站·{scope}（{len(per)} 站）BH 显著好 {better}  显著差 {worse}")
        if scope == '深夜':
            print('   ' + '  '.join(f"{s} {v[0]:+.3f}" for s, v in sorted(per.items(), key=lambda kv: kv[1][0])[:8]), ' … ',
                  '  '.join(f"{s} {v[0]:+.3f}" for s, v in sorted(per.items(), key=lambda kv: kv[1][0])[-5:]))
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'walk':
        walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    else:
        score()
