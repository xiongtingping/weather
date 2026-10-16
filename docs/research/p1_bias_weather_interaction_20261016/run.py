"""PROTOCOL.md: obs-minus-curve x rain/overcast/morning interaction arms, strict walk-forward.

Reuses p1_slope_refit_arms_20261016 (its design.pkl, its BASE walk, its Arm class).
usage: python3 run.py walk [--max-days N]   -> walk.jsonl (resumable)
       python3 run.py score                 -> readings.json
"""
from __future__ import annotations

import gzip, json, math, pickle, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREV = ROOT / 'docs/research/p1_slope_refit_arms_20261016'
sys.path.insert(0, str(PREV))
sys.argv = sys.argv[:1] + sys.argv[1:]
import run as R  # noqa: E402

COLS = list(R.DU.STATE_COLUMNS)


def interactions(design):
    raw = design['raw']
    rb = raw[:, COLS.index('root_bias_recent_c')]; pr = raw[:, COLS.index('precipitation_present')]
    cl = raw[:, COLS.index('cloud_cover_fraction')]
    early = (design['hours'] >= R.EARLY_FROM_HOURS).astype(float)
    return rb, pr, cl, early


class InterArm(R.Arm):
    def __init__(self, name, design, ext, var, shift):
        super().__init__('SHIFT' if shift else 'INTER', design, ext, var)
        self.label = name
        self.rb, self.pr, self.cl, self.early = interactions(design)

    def _inter(self, idx, scaling):
        f = scaling['fill']
        rb = np.where(np.isfinite(self.rb[idx]), self.rb[idx], f['rb'])
        pr = np.where(np.isfinite(self.pr[idx]), self.pr[idx], f['pr'])
        cl = np.where(np.isfinite(self.cl[idx]), self.cl[idx], f['cl'])
        z = np.stack([rb * pr, rb * cl, rb * self.early[idx]], 1)
        return (z - scaling['int_mean']) / scaling['int_scale']

    def x(self, idx, scaling):
        return np.hstack([R.FP.direct_x(self.d, idx, scaling), self._inter(idx, scaling)])

    def scaling(self, idx):
        s = R.FP.direct_scaling(self.d, idx)
        fill = {k: float(np.nanmean(v[idx])) if np.isfinite(v[idx]).any() else 0.0
                for k, v in (('rb', self.rb), ('pr', self.pr), ('cl', self.cl))}
        s['fill'] = fill; s['int_mean'] = 0.0; s['int_scale'] = 1.0
        z = self._inter(idx, s)
        s['int_mean'] = z.mean(0); s['int_scale'] = z.std(0) + 1e-9
        return s


def walk(max_days):
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design = P['design']
    ext, var = R.extras(design)
    arms = {'D1_INTER': InterArm('D1_INTER', design, ext, var, shift=False),
            'D2_INTER_SHIFT': InterArm('D2_INTER_SHIFT', design, ext, var, shift=True)}
    prev = [json.loads(l) for l in open(PREV / 'walk.jsonl')]
    out_path = HERE / 'walk.jsonl'
    done = {json.loads(l)['day'] for l in open(out_path)} if out_path.exists() else set()
    usable = np.where(design['usable'])[0]; starts = {}; n = 0
    for rec_prev in prev:
        day = rec_prev['day']
        if day in done: continue
        if n >= max_days: break
        t0 = time.time()
        rows_d = np.array(rec_prev['rows'])
        start = float(design['as_of'][rows_d].min())
        train = usable[design['known_at'][usable] <= start]
        rec = {'day': day, 'rows': rows_d.tolist(), 'arms': {}, 'params': {}}
        for name, arm in arms.items():
            lam, sc, theta, _ = arm.choose(train, start=starts.get(name))
            starts[name] = theta
            rec['arms'][name] = arm.served(rows_d, sc, theta).tolist(); rec['params'][name] = {'lambda': lam}
        with open(out_path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)} λ {[rec['params'][a]['lambda'] for a in arms]} ({time.time() - t0:.0f}s)", flush=True)


def score():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    rb, pr, cl, _early = interactions(design)
    mk = {int(k): v for k, v in json.load(gzip.open(PREV / 'market.json.gz', 'rt')).items()}
    base = {json.loads(l)['day']: json.loads(l) for l in open(PREV / 'walk.jsonl')}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    ARMS = ('D1_INTER', 'D2_INTER_SHIFT')
    rows = []
    for rec in recs:
        b = base[rec['day']]
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']
            item = {'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]), 'morning': R.is_morning(m['local_hour']),
                    'rose': t > run, 'wet': (pr[i] == 1) or (cl[i] >= 0.875)}
            vecs = {'BASE': b['arms']['BASE'][j], **{a: rec['arms'][a][j] for a in ARMS}}
            if i in mk: vecs['MKT'] = mk[i]
            for a, v in vecs.items():
                p = np.asarray(v)
                if a != 'MKT': item[f'n_{a}'] = -math.log(max(p[t], 1e-300))
                if t > run:
                    up = p[run + 1:]; s = up.sum()
                    if s > 0.02:
                        u = up / s; item[f'c_{a}'] = -math.log(max(u[t - run - 1], R.FLOOR))
                        mo = int(np.argmax(u)); g = float((u * np.arange(len(u))).sum()) - mo; tt = t - run - 1
                        item[f'call_{a}'] = None if abs(g) < 1e-9 or tt == mo else float((g > 0) == (tt > mo))
            rows.append(item)
    rng = np.random.default_rng(7)

    def ci(sel, a, b):
        S = [r for r in rows if sel(r) and r.get(a) is not None and r.get(b) is not None]
        if not S: return None
        by = defaultdict(lambda: [0.0, 0.0])
        for r in S: by[r['day']][0] += r['w'] * (r[a] - r[b]); by[r['day']][1] += r['w']
        ud = sorted(by); tot = np.array([by[d][0] for d in ud]); wt = np.array([by[d][1] for d in ud])
        picks = rng.integers(0, len(ud), size=(2000, len(ud))); bs = tot[picks].sum(1) / wt[picks].sum(1)
        return [float(tot.sum() / wt.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), len(S), len(ud),
                float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / 2000))]

    sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
    import analyze as A  # noqa: PLC0415
    res = {}
    print(f"评估 {len(recs)} 个事件日、{len(rows)} 行")
    fmt = lambda v: '—' if v is None else f"{v[0]:+.4f} [{v[1]:+.4f}, {v[2]:+.4f}]（{v[3]}）"
    for a in ARMS:
        out = {
            'full': ci(lambda r: True, f'n_{a}', 'n_BASE'),
            'morning': ci(lambda r: r['morning'], f'n_{a}', 'n_BASE'),
            'morning_cond': ci(lambda r: r['morning'] and r['rose'], f'c_{a}', 'c_BASE'),
            'morning_cond_wet': ci(lambda r: r['morning'] and r['rose'] and r['wet'], f'c_{a}', 'c_BASE'),
            'morning_full_wet': ci(lambda r: r['morning'] and r['wet'], f'n_{a}', 'n_BASE'),
            'morning_cond_vs_market': ci(lambda r: r['morning'] and r['rose'], f'c_{a}', 'c_MKT'),
        }
        print(f"== {a}")
        for k, v in out.items(): print(f"   {k:24s} {fmt(v)}")
        per = {}
        for st in sorted({r['st'] for r in rows}):
            v = ci(lambda r, st=st: r['st'] == st, f'n_{a}', 'n_BASE')
            if v and v[4] >= 10: per[st] = v
        passed = A.bh({s: v[5] for s, v in per.items()})
        out['better'] = sorted(s for s in passed if per[s][0] < 0); out['worse'] = sorted(s for s in passed if per[s][0] > 0)
        out['stations'] = per
        print(f"   按站（{len(per)} 站）BH 显著好 {out['better']}  显著差 {out['worse']}")
        res[a] = out
    res['BASE_vs_market'] = ci(lambda r: r['morning'] and r['rose'], 'c_BASE', 'c_MKT')
    print('== BASE 上午条件 对市场', fmt(res['BASE_vs_market']))
    for a in ('BASE',) + ARMS + ('MKT',):
        S = [r[f'call_{a}'] for r in rows if r['morning'] and r.get(f'call_{a}') is not None and r.get('call_MKT') is not None]
        if S: print(f"   判左右（上午条件，有市场的行）{a}: {np.mean(S):.1%}（{len(S)}）")
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'walk':
        walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    else:
        score()
