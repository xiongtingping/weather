"""PROTOCOL.md: P1 evening arm (clock x root-above-high, hours past peak), strict walk-forward.

Reuses p1_slope_refit_arms_20261016 (design.pkl, BASE walk, Arm class).
usage: python3 run.py walk [--max-days N]   -> walk.jsonl (resumable)
       python3 run.py score                 -> readings.json
"""
from __future__ import annotations

import json, math, pickle, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREV = ROOT / 'docs/research/p1_slope_refit_arms_20261016'
sys.path.insert(0, str(PREV))
import run as R  # noqa: E402

COLS = list(R.DU.STATE_COLUMNS)
FLEET_PEAK = float(json.loads(R.OL.ARTIFACT_PATH.read_text())['station_clock']['fleet_peak_local_h'])


def evening_raw(design):
    rmh = design['raw'][:, COLS.index('root_mean_minus_high_c')]
    clock = design['hours']
    hpp = np.maximum(0.0, 24.0 - FLEET_PEAK - clock)
    return rmh, clock, hpp


class EveningArm(R.Arm):
    def __init__(self, design, ext, var):
        super().__init__('EVENING', design, ext, var)
        self.rmh, self.clock, self.hpp = evening_raw(design)

    def _cols(self, idx, s):
        rmh = np.where(np.isfinite(self.rmh[idx]), self.rmh[idx], s['rmh_fill'])
        z = np.stack([self.clock[idx] * rmh, self.hpp[idx], self.hpp[idx] * rmh], 1)
        return (z - s['ev_mean']) / s['ev_scale']

    def x(self, idx, scaling):
        return np.hstack([R.FP.direct_x(self.d, idx, scaling), self._cols(idx, scaling)])

    def scaling(self, idx):
        s = R.FP.direct_scaling(self.d, idx)
        v = self.rmh[idx]; s['rmh_fill'] = float(np.nanmean(v)) if np.isfinite(v).any() else 0.0
        s['ev_mean'] = 0.0; s['ev_scale'] = 1.0
        z = self._cols(idx, s); s['ev_mean'] = z.mean(0); s['ev_scale'] = z.std(0) + 1e-9
        return s


def walk(max_days):
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design = P['design']
    ext, var = R.extras(design)
    arm = EveningArm(design, ext, var)
    prev = [json.loads(l) for l in open(PREV / 'walk.jsonl')]
    out_path = HERE / 'walk.jsonl'
    done = {json.loads(l)['day'] for l in open(out_path)} if out_path.exists() else set()
    usable = np.where(design['usable'])[0]; start_theta = None; n = 0
    for rec_prev in prev:
        day = rec_prev['day']
        if day in done: continue
        if n >= max_days: break
        t0 = time.time()
        rows_d = np.array(rec_prev['rows'])
        start = float(design['as_of'][rows_d].min())
        train = usable[design['known_at'][usable] <= start]
        lam, sc, theta, _ = arm.choose(train, start=start_theta); start_theta = theta
        rec = {'day': day, 'rows': rows_d.tolist(), 'arms': {'EVENING': arm.served(rows_d, sc, theta).tolist()}, 'params': {'EVENING': {'lambda': lam}}}
        with open(out_path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)} λ {lam} ({time.time() - t0:.0f}s)", flush=True)


def score():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    rmh_all, _clock, _hpp = evening_raw(design)
    base = {json.loads(l)['day']: json.loads(l) for l in open(PREV / 'walk.jsonl')}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    rows = []
    for rec in recs:
        b = base[rec['day']]
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']; lh = m['local_hour']
            item = {'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]),
                    'evening': lh is not None and float(lh) >= 17.0, 'rmh': float(rmh_all[i]) if np.isfinite(rmh_all[i]) else None,
                    'rose': float(t > run)}
            for a, v in (('BASE', b['arms']['BASE'][j]), ('EVENING', rec['arms']['EVENING'][j])):
                p = np.asarray(v); item[f'n_{a}'] = -math.log(max(p[t], 1e-300)); item[f'up_{a}'] = float(p[run + 1:].sum())
            rows.append(item)
    rng = np.random.default_rng(7)

    def ci(sel, a, b):
        S = [r for r in rows if sel(r)]
        if not S: return None
        by = defaultdict(lambda: [0.0, 0.0])
        for r in S: by[r['day']][0] += r['w'] * (r[a] - r[b]); by[r['day']][1] += r['w']
        ud = sorted(by); tot = np.array([by[d][0] for d in ud]); wt = np.array([by[d][1] for d in ud])
        picks = rng.integers(0, len(ud), size=(2000, len(ud))); bs = tot[picks].sum(1) / wt[picks].sum(1)
        return [float(tot.sum() / wt.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), len(S), len(ud),
                float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / 2000))]

    sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
    import analyze as A  # noqa: PLC0415
    fmt = lambda v: '—' if v is None else f"{v[0]:+.4f} [{v[1]:+.4f}, {v[2]:+.4f}]（{v[3]} 行）"
    res = {'full': ci(lambda r: True, 'n_EVENING', 'n_BASE'),
           'evening': ci(lambda r: r['evening'], 'n_EVENING', 'n_BASE'),
           'evening_rmh_ge1': ci(lambda r: r['evening'] and r['rmh'] is not None and r['rmh'] >= 1.0, 'n_EVENING', 'n_BASE')}
    print(f"评估 {len(recs)} 个事件日、{len(rows)} 行")
    for k, v in res.items(): print(f"  {k:18s} {fmt(v)}")
    print('  傍晚「还会升」校准（P 高于已测最高档 的平均 / 实际 / Brier）')
    res['calibration'] = {}
    for name, lo, hi in (('<0.5', -99, 0.5), ('0.5–1', 0.5, 1), ('1–2', 1, 2), ('≥2', 2, 99)):
        S = [r for r in rows if r['evening'] and r['rmh'] is not None and lo <= r['rmh'] < hi]
        if not S: continue
        out = {'n': len(S), 'actual': float(np.mean([r['rose'] for r in S]))}
        for a in ('BASE', 'EVENING'):
            out[a] = [float(np.mean([r[f'up_{a}'] for r in S])), float(np.mean([(r[f'up_{a}'] - r['rose']) ** 2 for r in S]))]
        res['calibration'][name] = out
        print(f"    根高出 {name:5s} 行 {out['n']:5d}  实际 {out['actual']:.3f}  BASE {out['BASE'][0]:.3f}（Brier {out['BASE'][1]:.4f}）  戊 {out['EVENING'][0]:.3f}（Brier {out['EVENING'][1]:.4f}）")
    per = {}
    for st in sorted({r['st'] for r in rows}):
        v = ci(lambda r, st=st: r['st'] == st, 'n_EVENING', 'n_BASE')
        if v and v[4] >= 10: per[st] = v
    passed = A.bh({s: v[5] for s, v in per.items()})
    res['better'] = sorted(s for s in passed if per[s][0] < 0); res['worse'] = sorted(s for s in passed if per[s][0] > 0)
    res['stations'] = per
    print(f"  按站（{len(per)} 站）BH 显著好 {res['better']}  显著差 {res['worse']}")
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'walk':
        walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    else:
        score()
