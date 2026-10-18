"""PROTOCOL.md (committed 6d810b1d before scoring): P1 root faded after the
peak, P1 ∝ root^w × mask × exp f, w = max(0, 1 − hpp/τ), against the charter
274 form, strict walk-forward on the same 30 days.

usage: python3 run.py walk [--max-days N]  -> walk.jsonl (resumable)
       python3 run.py score                -> readings.json
"""
from __future__ import annotations

import json, math, pickle, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREV = ROOT / 'docs/research/p1_slope_refit_arms_20261016'
EVE = ROOT / 'docs/research/p1_evening_arm_20261016'
import importlib.util  # noqa: E402

sys.path.insert(0, str(PREV))
# The evening arm imports the slope-refit module as ``run``; register it under
# that name first so this file's own name does not shadow it.
_spec = importlib.util.spec_from_file_location('run', PREV / 'run.py'); R = importlib.util.module_from_spec(_spec)
sys.modules['run'] = R; _spec.loader.exec_module(R)
_spec = importlib.util.spec_from_file_location('eve', EVE / 'run.py'); E = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(E)

TAUS = (math.inf, 6.0, 3.0, 1.5)


class FadeArm(E.EveningArm):
    """The charter 274 arm with the root's log weight w(hpp) per row."""

    def __init__(self, design, ext, var):
        super().__init__(design, ext, var)
        self.name = 'FADE'
        self.tau = math.inf

    def w(self, idx):
        if math.isinf(self.tau):
            return np.ones(len(idx))
        return np.maximum(0.0, 1.0 - self.hpp[idx] / self.tau)

    def post(self, idx, x, theta):
        live = self.d['live'][idx]
        logp = np.log(np.where(live, np.maximum(self.d['prior'][idx], 1e-300), 1.0))
        z = np.where(live, self.w(idx)[:, None] * logp + self.f(idx, x, theta), -np.inf)
        z = z - z.max(1, keepdims=True); p = np.where(live, np.exp(z), 0.0)
        return p / p.sum(1, keepdims=True)

    def choose(self, idx, start=None):
        days = sorted(set(self.d['day'][idx].tolist())); tail = set(days[-R.INNER_DAYS:])
        in_tail = np.isin(self.d['day'][idx], list(tail)); val = idx[in_tail]; inner = idx[~in_tail]
        inner = inner[self.d['known_at'][inner] <= self.d['as_of'][val].min()]
        best, best_score, theta = (R.LAMBDAS[-1], math.inf), -np.inf, start
        sc = self.scaling(inner)
        for tau in TAUS:
            self.tau = tau
            for lam in R.LAMBDAS:
                theta = self.fit(inner, sc, lam, start=theta)
                score = R.weighted_logscore(self.served(val, sc, theta), self.d, val)
                if score > best_score:
                    best, best_score = (lam, tau), score
        self.tau = best[1]
        scaling = self.scaling(idx)
        return best, scaling, self.fit(idx, scaling, best[0], start=start)


def walk(max_days):
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design = P['design']
    ext, var = R.extras(design)
    arm = FadeArm(design, ext, var)
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
        (lam, tau), sc, theta = arm.choose(train, start=start_theta); start_theta = theta
        rec = {'day': day, 'rows': rows_d.tolist(), 'arms': {'FADE': arm.served(rows_d, sc, theta).tolist()},
               'params': {'FADE': {'lambda': lam, 'tau': None if math.isinf(tau) else tau}}}
        with open(out_path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)} λ {lam} τ {tau} ({time.time() - t0:.0f}s)", flush=True)


def score():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    rmh_all, _c, _h = E.evening_raw(design)
    base = {json.loads(l)['day']: json.loads(l) for l in open(EVE / 'walk.jsonl')}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    rows = []
    for rec in recs:
        b = base[rec['day']]
        assert b['rows'] == rec['rows']
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']; lh = m['local_hour']
            item = {'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]),
                    'evening': lh is not None and float(lh) >= 17.0,
                    'rmh': float(rmh_all[i]) if np.isfinite(rmh_all[i]) else None, 'rose': float(t > run)}
            for a, v in (('E274', b['arms']['EVENING'][j]), ('FADE', rec['arms']['FADE'][j])):
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
    fmt = lambda v: '—' if v is None else f"{v[0]:+.4f} [{v[1]:+.4f}, {v[2]:+.4f}]（{v[3]} 行，{v[4]} 日）"
    res = {'full': ci(lambda r: True, 'n_FADE', 'n_E274'),
           'evening': ci(lambda r: r['evening'], 'n_FADE', 'n_E274'),
           'evening_rmh_ge1': ci(lambda r: r['evening'] and r['rmh'] is not None and r['rmh'] >= 1.0, 'n_FADE', 'n_E274'),
           'not_evening': ci(lambda r: not r['evening'], 'n_FADE', 'n_E274')}
    print(f"评估 {len(recs)} 个事件日、{len(rows)} 行（己 − 宪章 274）")
    for k, v in res.items(): print(f"  {k:18s} {fmt(v)}")
    taus = [rec['params']['FADE']['tau'] for rec in recs]
    res['tau_by_day'] = taus
    print('  每日选中 τ：', {str(t): taus.count(t) for t in sorted(set(taus), key=lambda x: (x is None, x))})
    print('  傍晚「还会升」校准（P 高于已测最高档 的平均 / 实际 / Brier）')
    res['calibration'] = {}
    for name, lo, hi in (('<0.5', -99, 0.5), ('0.5–1', 0.5, 1), ('1–2', 1, 2), ('≥2', 2, 99)):
        S = [r for r in rows if r['evening'] and r['rmh'] is not None and lo <= r['rmh'] < hi]
        if not S: continue
        out = {'n': len(S), 'actual': float(np.mean([r['rose'] for r in S]))}
        for a in ('E274', 'FADE'):
            out[a] = [float(np.mean([r[f'up_{a}'] for r in S])), float(np.mean([(r[f'up_{a}'] - r['rose']) ** 2 for r in S]))]
        res['calibration'][name] = out
        print(f"    根高出 {name:5s} 行 {out['n']:5d}  实际 {out['actual']:.3f}  274 {out['E274'][0]:.3f}（Brier {out['E274'][1]:.4f}）  己 {out['FADE'][0]:.3f}（Brier {out['FADE'][1]:.4f}）")
    per = {}
    for st in sorted({r['st'] for r in rows}):
        v = ci(lambda r, st=st: r['st'] == st, 'n_FADE', 'n_E274')
        if v and v[4] >= 10: per[st] = v
    passed = A.bh({s: v[5] for s, v in per.items()})
    res['better'] = sorted(s for s in passed if per[s][0] < 0); res['worse'] = sorted(s for s in passed if per[s][0] > 0)
    res['stations'] = per
    zuuu = per.get('ZUUU')
    print(f"  按站（{len(per)} 站）BH 显著好 {res['better']}  显著差 {res['worse']}；ZUUU {fmt(zuuu)}")
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'walk':
        walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    else:
        score()
