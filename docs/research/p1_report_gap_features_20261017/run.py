"""PROTOCOL.md: P1 + report-gap features (gap_h, missing_n) on the charter 274 form, strict walk-forward.

usage: python3 run.py features              -> features.npz (gap_h, missing_n per design row)
       python3 run.py walk [--max-days N]   -> walk.jsonl (resumable)
       python3 run.py score                 -> readings.json
"""
from __future__ import annotations

import bisect, importlib.util, json, math, pickle, sqlite3, statistics, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREV = ROOT / 'docs/research/p1_slope_refit_arms_20261016'
EVE = ROOT / 'docs/research/p1_evening_arm_20261016'
sys.path.insert(0, str(PREV))
import run as R  # noqa: E402
spec = importlib.util.spec_from_file_location('evening_run', EVE / 'run.py')
E = importlib.util.module_from_spec(spec); spec.loader.exec_module(E)
ARCHIVE = ROOT / 'data/archive.db'
SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
GAP_CAP_H = 24.0
RATE_DAYS, RATE_MIN_DAYS = 14, 3


def ts(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00'))


def features():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); meta = P['meta']
    ssot = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    tzs = dict(ssot.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    ssot.close()
    stations = sorted({m['st'] for m in meta})
    days = sorted({m['day'] for m in meta})
    lo = (date.fromisoformat(days[0]) - timedelta(days=RATE_DAYS + 2)).isoformat()
    hi = (date.fromisoformat(days[-1]) + timedelta(days=2)).isoformat()
    arc = sqlite3.connect(f'file:{ARCHIVE}?mode=ro', uri=True, timeout=30)
    reports = {}
    t0 = time.time()
    for st in stations:
        rows = arc.execute("SELECT valid_at, canonical_committed_at FROM reported_trajectory_observations"
                           " WHERE station_id=? AND valid_at>=? AND valid_at<? AND source_lane='awc'", (st, lo, hi)).fetchall()
        reports[st] = sorted((ts(v), ts(c) if c else ts(v)) for v, c in rows)
    arc.close()
    print('loaded reports', sum(len(v) for v in reports.values()), f'{time.time() - t0:.0f}s', flush=True)
    # daily counts per station (by local date), for the expected rate
    daily = {}
    for st, rows in reports.items():
        z = ZoneInfo(tzs.get(st, 'UTC')); cnt = defaultdict(int)
        for v, _c in rows: cnt[v.astimezone(z).date().isoformat()] += 1
        daily[st] = cnt
    fleet_rate = statistics.median([c for st in daily for c in daily[st].values()] or [24]) / 24.0
    gap = np.full(len(meta), np.nan); missing = np.full(len(meta), np.nan)
    for i, m in enumerate(meta):
        st = m['st']; z = ZoneInfo(tzs.get(st, 'UTC')); at = ts(m['as_of'] if m['as_of'].endswith('Z') else m['as_of'] + 'Z')
        rows = reports.get(st, [])
        arrived = [v for v, c in rows if c <= at and v <= at]
        gap[i] = min(GAP_CAP_H, (at - max(arrived)).total_seconds() / 3600) if arrived else GAP_CAP_H
        day = m['day']; d0 = date.fromisoformat(day)
        prior = [daily[st].get((d0 - timedelta(days=k)).isoformat()) for k in range(1, RATE_DAYS + 1)]
        prior = [x for x in prior if x]
        rate = statistics.median(prior) / 24.0 if len(prior) >= RATE_MIN_DAYS else fleet_rate
        midnight = datetime.combine(d0, datetime.min.time(), z)
        elapsed = max(0.0, min(24.0, (at - midnight).total_seconds() / 3600))
        got = sum(1 for v in arrived if v.astimezone(z).date().isoformat() == day)
        missing[i] = max(0.0, rate * elapsed - got)
    np.savez(HERE / 'features.npz', gap=gap, missing=missing)
    print('gap_h median', float(np.nanmedian(gap)), 'p95', float(np.nanpercentile(gap, 95)),
          'missing_n median', float(np.nanmedian(missing)), 'p95', float(np.nanpercentile(missing, 95)))
    # the three outage days, for the record
    for st, day in (('KLAX', '2026-09-09'), ('KMIA', '2026-09-21'), ('RJTT', '2026-09-21')):
        idx = [i for i, m in enumerate(meta) if m['st'] == st and m['day'] == day]
        if idx: print(f'  {st} {day}: last sample gap_h {gap[idx[-1]]:.1f} missing_n {missing[idx[-1]]:.1f}')


class GapArm(E.EveningArm):
    def __init__(self, design, ext, var, gap, missing):
        super().__init__(design, ext, var)
        self.gap, self.missing = gap, missing

    def _gap(self, idx, s):
        g = np.where(np.isfinite(self.gap[idx]), self.gap[idx], s['gap_fill'])
        m = np.where(np.isfinite(self.missing[idx]), self.missing[idx], s['miss_fill'])
        return (np.stack([g, m], 1) - s['gp_mean']) / s['gp_scale']

    def x(self, idx, scaling):
        return np.hstack([super().x(idx, scaling), self._gap(idx, scaling)])

    def scaling(self, idx):
        s = super().scaling(idx)
        s['gap_fill'] = float(np.nanmean(self.gap[idx])); s['miss_fill'] = float(np.nanmean(self.missing[idx]))
        s['gp_mean'] = 0.0; s['gp_scale'] = 1.0
        z = self._gap(idx, s); s['gp_mean'] = z.mean(0); s['gp_scale'] = z.std(0) + 1e-9
        return s


def walk(max_days):
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design = P['design']
    F = np.load(HERE / 'features.npz')
    ext, var = R.extras(design)
    arm = GapArm(design, ext, var, F['gap'], F['missing'])
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
            f.write(json.dumps({'day': day, 'rows': rows_d.tolist(), 'arms': {'GAP': arm.served(rows_d, sc, th).tolist()},
                                'params': {'GAP': {'lambda': lam}}}) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)} λ {lam} ({time.time() - t0:.0f}s)", flush=True)


def score():
    P = pickle.load(open(PREV / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    F = np.load(HERE / 'features.npz')
    cur = {json.loads(l)['day']: json.loads(l) for l in open(EVE / 'walk.jsonl')}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    rows = []
    for rec in recs:
        c = cur[rec['day']]
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']
            item = {'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]), 'night': float(design['hours'][i]) <= 3.0,
                    'us': str(m['st']).startswith('K'), 'rose': float(t > run),
                    'outage': bool((F['missing'][i] >= 2) or (F['gap'][i] >= 2))}
            for a, v in (('CURRENT', c['arms']['EVENING'][j]), ('GAP', rec['arms']['GAP'][j])):
                p = np.asarray(v); item[f'n_{a}'] = -math.log(max(p[t], 1e-300)); item[f'up_{a}'] = float(p[run + 1:].sum())
            rows.append(item)
    rng = np.random.default_rng(7)

    def ci(sel):
        S = [r for r in rows if sel(r)]
        if not S: return None
        by = defaultdict(lambda: [0.0, 0.0])
        for r in S: by[r['day']][0] += r['w'] * (r['n_GAP'] - r['n_CURRENT']); by[r['day']][1] += r['w']
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
    for name, sel in (('美国', lambda r: r['us']), ('非美国', lambda r: not r['us']), ('断档行', lambda r: r['outage']), ('正常行', lambda r: not r['outage'])):
        S = [r for r in rows if r['night'] and sel(r)]
        if not S: continue
        out = {'n': len(S), 'actual': float(np.mean([r['rose'] for r in S]))}
        for a in ('CURRENT', 'GAP'):
            out[a] = [float(np.mean([r[f'up_{a}'] for r in S])), float(np.mean([(r[f'up_{a}'] - r['rose']) ** 2 for r in S]))]
        res['calibration'][name] = out
        print(f"    {name:4s} 行 {out['n']:5d}  实际 {out['actual']:.4f}  现行 {out['CURRENT'][0]:.4f}（Brier {out['CURRENT'][1]:.5f}）  辛 {out['GAP'][0]:.4f}（Brier {out['GAP'][1]:.5f}）")
    for scope, sel in (('整梯子', lambda r, st: r['st'] == st), ('深夜', lambda r, st: r['st'] == st and r['night'])):
        per = {}
        for st in sorted({r['st'] for r in rows}):
            v = ci(lambda r, st=st: sel(r, st))
            if v and v[4] >= 10: per[st] = v
        passed = A.bh({s: v[5] for s, v in per.items()})
        better = sorted(s for s in passed if per[s][0] < 0); worse = sorted(s for s in passed if per[s][0] > 0)
        res[f'stations_{scope}'] = {'per': per, 'better': better, 'worse': worse}
        print(f"  按站·{scope}（{len(per)} 站）BH 显著好 {better}  显著差 {worse}")
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'features': features()
    elif cmd == 'walk': walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    else: score()
