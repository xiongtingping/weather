"""PROTOCOL.md: three P1 slope refit arms against the current form, strict walk-forward.

usage: python3 run.py prepare            design -> design.pkl
       python3 run.py walk [--max-days N] walk forward, resumable -> walk.jsonl
       python3 run.py market             same-instant market for morning rows -> market.json.gz
       python3 run.py score              -> readings.json
"""
from __future__ import annotations

import gzip, json, math, pickle, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'scripts'))
import fit_p1_observation_likelihood as FP  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from bayes_basket_target import p1_direct_update as DU  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
LAMBDAS = FP.DIRECT_LAMBDAS
INNER_DAYS = FP.DIRECT_INNER_DAYS
FIRST_EVAL_INDEX = 14
KS = (1.0, 1.5, 2.0, 3.0)
EARLY_FROM_HOURS = 10.0
V_FLOOR = 0.25
FLOOR = 1e-3
def is_morning(hour):
    return hour is not None and 7.0 <= float(hour) < 13.0


# --------------------------------------------------------------------------- prepare

def prepare():
    target = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True)
    roots = FP.RootCache(target)
    raw = FP.load_samples(OL.SAMPLES_PATH, since=None, through='2026-10-15')
    samples, dropped = FP.label_samples(raw, roots, FP.load_truth_known_at(target))
    rows = FP.scorable_rows(samples, roots)
    clock = json.loads(OL.ARTIFACT_PATH.read_text())
    design = FP.direct_design(rows, clock)
    meta = []
    for r in rows:
        v = r['view']; order = sorted(v['ordinals'], key=lambda b: (v['ordinals'][b], b))
        s = r['sample']
        meta.append({'st': s['station_id'], 'day': s['event_date'], 'as_of': s['as_of_utc'], 'local_hour': s['local_hour'],
                     'order': order, 'running': order.index(r['running']), 'truth': order.index(r['truth'])})
    pickle.dump({'design': design, 'meta': meta}, open(HERE / 'design.pkl', 'wb'))
    print('rows', len(rows), 'dropped', dropped, 'usable', int(design['usable'].sum()), 'days', len(set(design['day'].tolist())))


# --------------------------------------------------------------------------- model

def extras(design):
    """root_mode_rung, root_mean_minus_mode, root rung variance (per row), from the masked root."""
    live = design['live']; prior = np.where(live, design['prior'], 0.0)
    mass = prior.sum(1, keepdims=True); p = np.divide(prior, mass, out=np.zeros_like(prior), where=mass > 0)
    r = design['rise'] + design['slope']                       # rung of each column (0 at the running high)
    mode = np.argmax(np.where(live, p, -1.0), axis=1)
    rows = np.arange(len(p))
    mode_r = r[rows, mode]
    ordv = np.tile(np.arange(p.shape[1]), (len(p), 1))
    mean_ord = (p * ordv).sum(1)
    mean_r = (p * r).sum(1)
    var_r = (p * (r - mean_r[:, None]) ** 2).sum(1)
    return np.stack([mode_r, mean_ord - mode], 1), np.maximum(var_r, V_FLOOR)


class Arm:
    def __init__(self, name, design, ext, var):
        self.name, self.d, self.ext, self.var = name, design, ext, var

    def x(self, idx, scaling):
        base = FP.direct_x(self.d, idx, scaling)
        if self.name == 'BASE':
            return base
        k0 = 1 + len(DU.KNOTS_HOURS); S = len(DU.STATE_COLUMNS)
        standard = base[:, k0:k0 + S]
        e = (self.ext[idx] - scaling['ext_mean']) / scaling['ext_scale']
        early = (self.d['hours'][idx] >= EARLY_FROM_HOURS).astype(float)[:, None]
        return np.hstack([base, e, early * np.hstack([standard, e])])

    def nb(self):
        return 3 if self.name == 'SHIFT' else 2

    def f(self, idx, x, theta):
        d = x.shape[1]; tr, ts = theta[:d], theta[d:2 * d]
        out = self.d['rise'][idx] * (x @ tr)[:, None] + self.d['slope'][idx] * (x @ ts)[:, None]
        off = 2 * d
        if self.name == 'SHIFT':
            r = self.d['rise'][idx] + self.d['slope'][idx]
            out = out + (r / self.var[idx][:, None]) * (x @ theta[off:off + d])[:, None]; off += d
        sl = self.d['slope'][idx]
        return out + sl * sl * theta[off] + self.d['top'][idx] * theta[off + 1]

    def post(self, idx, x, theta):
        live = self.d['live'][idx]
        z = np.where(live, np.log(np.where(live, np.maximum(self.d['prior'][idx], 1e-300), 1.0)) + self.f(idx, x, theta), -np.inf)
        z = z - z.max(1, keepdims=True); p = np.where(live, np.exp(z), 0.0)
        return p / p.sum(1, keepdims=True)

    def loss(self, theta, idx, x, lam):
        p = self.post(idx, x, theta); rows = np.arange(len(idx)); t = self.d['truth'][idx]; w = self.d['weight'][idx]
        loss = -(w * np.log(np.maximum(p[rows, t], 1e-300))).sum() + 0.5 * lam * (theta ** 2).sum()
        g = p.copy(); g[rows, t] -= 1.0; g *= w[:, None]
        d = x.shape[1]; sl = self.d['slope'][idx]
        parts = [x.T @ (g * self.d['rise'][idx]).sum(1), x.T @ (g * sl).sum(1)]
        if self.name == 'SHIFT':
            r = self.d['rise'][idx] + sl
            parts.append(x.T @ (g * r / self.var[idx][:, None]).sum(1))
        parts.append(np.array([(g * sl * sl).sum(), (g * self.d['top'][idx]).sum()]))
        return loss, np.concatenate(parts) + lam * theta

    def scaling(self, idx):
        s = FP.direct_scaling(self.d, idx)
        s['ext_mean'] = self.ext[idx].mean(0); s['ext_scale'] = self.ext[idx].std(0) + 1e-9
        return s

    def fit(self, idx, scaling, lam, start=None):
        x = self.x(idx, scaling)
        th0 = np.zeros(self.nb() * x.shape[1] + 2) if start is None or len(start) != self.nb() * x.shape[1] + 2 else start
        return minimize(self.loss, th0, args=(idx, x, lam), jac=True, method='L-BFGS-B', options={'maxiter': FP.DIRECT_MAX_ITER}).x

    def served(self, idx, scaling, theta):
        p = self.post(idx, self.x(idx, scaling), theta)
        return FP._served_vectors(p, self.d['live'][idx])

    def choose(self, idx, start=None):
        days = sorted(set(self.d['day'][idx].tolist())); tail = set(days[-INNER_DAYS:])
        in_tail = np.isin(self.d['day'][idx], list(tail)); val = idx[in_tail]; inner = idx[~in_tail]
        inner = inner[self.d['known_at'][inner] <= self.d['as_of'][val].min()]
        best, best_score, theta = LAMBDAS[-1], -np.inf, start
        sc = self.scaling(inner)
        for lam in LAMBDAS:
            theta = self.fit(inner, sc, lam, start=theta)
            score = weighted_logscore(self.served(val, sc, theta), self.d, val)
            if score > best_score: best, best_score = lam, score
        scaling = self.scaling(idx)
        return best, scaling, self.fit(idx, scaling, best, start=start), (inner, val)


def weighted_logscore(p, design, idx):
    w = design['weight'][idx]
    return float((w * np.log(np.maximum(p[np.arange(len(idx)), design['truth'][idx]], 1e-300))).sum() / w.sum())


def scale_slope(theta, d, k):
    th = theta.copy(); th[d:2 * d] *= k; th[-2] *= k
    return th


# --------------------------------------------------------------------------- walk

def walk(max_days):
    P = pickle.load(open(HERE / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    ext, var = extras(design)
    arms = {n: Arm(n, design, ext, var) for n in ('BASE', 'A_MODE_MORNING', 'SHIFT')}
    usable = np.where(design['usable'])[0]
    days = sorted(set(design['day'][usable].tolist()))
    out_path = HERE / 'walk.jsonl'
    done = {json.loads(l)['day'] for l in open(out_path)} if out_path.exists() else set()
    starts = {}
    n = 0
    for day in days[FIRST_EVAL_INDEX:]:
        if day in done: continue
        if n >= max_days: break
        t0 = time.time()
        rows_d = usable[design['day'][usable] == day]
        start = float(design['as_of'][rows_d].min())
        train = usable[design['known_at'][usable] <= start]
        rec = {'day': day, 'rows': rows_d.tolist(), 'arms': {}, 'params': {}}
        for name, arm in arms.items():
            lam, sc, theta, (inner, val) = arm.choose(train, start=starts.get(name))
            starts[name] = theta
            p = arm.served(rows_d, sc, theta)
            rec['arms'][name] = p.tolist(); rec['params'][name] = {'lambda': lam}
            if name == 'BASE':
                # 丙: k chosen on the last training days with BASE refitted without them, then applied to the full BASE fit.
                sc_in = arm.scaling(inner); th_in = arm.fit(inner, sc_in, lam, start=theta)
                d = arm.x(inner[:1], sc_in).shape[1]
                k_best = max(KS, key=lambda k: weighted_logscore(arm.served(val, sc_in, scale_slope(th_in, d, k)), design, val))
                rec['arms']['SCALED'] = arm.served(rows_d, sc, scale_slope(theta, d, k_best)).tolist()
                rec['params']['SCALED'] = {'k': k_best}
                if name == 'BASE':
                    # the morning slope the BASE fit serves, for the record
                    x = arm.x(rows_d, sc); ts = theta[d:2 * d]
                    rec['params']['BASE']['slope_u_morning_median'] = float(np.median((x @ ts)[[is_morning(meta[i]['local_hour']) for i in rows_d]])) if any(is_morning(meta[i]['local_hour']) for i in rows_d) else None
        with open(out_path, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        n += 1
        print(f"{day}: rows {len(rows_d)} train {len(train)}  k {rec['params']['SCALED']['k']}  λ {[rec['params'][a]['lambda'] for a in arms]}  ({time.time() - t0:.0f}s)", flush=True)


# --------------------------------------------------------------------------- market

def market():
    from bayes_basket_target import board_readings as BR  # noqa: PLC0415
    P = pickle.load(open(HERE / 'design.pkl', 'rb')); meta = P['meta']
    rows = set()
    for l in open(HERE / 'walk.jsonl'):
        rows.update(i for i in json.loads(l)['rows'] if is_morning(meta[i]['local_hour']))
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10); cache = {}; out = {}
    for i in sorted(rows):
        m = meta[i]; at = m['as_of'] if m['as_of'].endswith('Z') else m['as_of'] + 'Z'
        q = BR._ladder_market_reference(c, m['st'], m['day'], at, cache)
        if q and set(q) == set(m['order']):
            out[i] = [q[b] for b in m['order']]
    json.dump(out, gzip.open(HERE / 'market.json.gz', 'wt'))
    print('morning rows', len(rows), 'with market', len(out))


# --------------------------------------------------------------------------- score

def score():
    from bayes_basket_target.station_day_board import _float  # noqa: F401,PLC0415
    sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
    import analyze as A  # noqa: PLC0415
    P = pickle.load(open(HERE / 'design.pkl', 'rb')); design, meta = P['design'], P['meta']
    mk = {int(k): v for k, v in json.load(gzip.open(HERE / 'market.json.gz', 'rt')).items()} if (HERE / 'market.json.gz').exists() else {}
    recs = [json.loads(l) for l in open(HERE / 'walk.jsonl')]
    ARMS = ('A_MODE_MORNING', 'SHIFT', 'SCALED')
    rows = []
    for rec in recs:
        for j, i in enumerate(rec['rows']):
            m = meta[i]; t = m['truth']; run = m['running']
            item = {'i': i, 'st': m['st'], 'day': m['day'], 'w': float(design['weight'][i]), 'morning': is_morning(m['local_hour']), 'rose': t > run}
            for a in ('BASE',) + ARMS:
                p = np.asarray(rec['arms'][a][j]); item[f'ls_{a}'] = math.log(max(p[t], 1e-300))
                if t > run:
                    up = p[run + 1:]; s = up.sum()
                    item[f'cls_{a}'] = math.log(max(up[t - run - 1] / s, FLOOR)) if s > 0.02 else None
                    if s > 0.02:
                        u = up / s; mo = int(np.argmax(u)); g = float((u * np.arange(len(u))).sum()) - mo; tt = t - run - 1
                        item[f'call_{a}'] = None if abs(g) < 1e-9 or tt == mo else float((g > 0) == (tt > mo))
            if i in mk and t > run:
                q = np.asarray(mk[i]); up = q[run + 1:]; s = up.sum()
                if s > 0.02:
                    u = up / s; item['cls_MKT'] = math.log(max(u[t - run - 1], FLOOR)); mo = int(np.argmax(u)); g = float((u * np.arange(len(u))).sum()) - mo; tt = t - run - 1
                    item['call_MKT'] = None if abs(g) < 1e-9 or tt == mo else float((g > 0) == (tt > mo))
            rows.append(item)
    days = sorted({r['day'] for r in rows}); rng = np.random.default_rng(7)

    def wmean_ci(sel, key_a, key_b=None):
        S = [r for r in rows if sel(r) and r.get(key_a) is not None and (key_b is None or r.get(key_b) is not None)]
        if not S: return None
        by = defaultdict(lambda: [0.0, 0.0])
        for r in S:
            v = r[key_a] - (r[key_b] if key_b else 0.0); by[r['day']][0] += r['w'] * v; by[r['day']][1] += r['w']
        ud = sorted(by); tot = np.array([by[d][0] for d in ud]); wt = np.array([by[d][1] for d in ud])
        picks = rng.integers(0, len(ud), size=(2000, len(ud))); bs = tot[picks].sum(1) / wt[picks].sum(1)
        return [float(tot.sum() / wt.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), len(S), len(ud),
                float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / 2000))]

    res = {'eval_days': len(days), 'rows': len(rows), 'params': {r['day']: r['params'] for r in recs}}
    print(f"评估 {len(days)} 个事件日、{len(rows)} 行；丙 k 选择 {dict(sorted(defaultdict(int, {k: sum(1 for r in recs if r['params']['SCALED']['k'] == k) for k in KS}).items()))}")
    print('BASE 上午斜线中位（x·θ_slope）:', [round(r['params']['BASE'].get('slope_u_morning_median') or 0, 3) for r in recs][:40])
    # LogScore is log p (higher better); report delta of -log p (negative = arm better)
    for r in rows:
        for a in ('BASE',) + ARMS:
            r[f'nls_{a}'] = -r[f'ls_{a}']
            if r.get(f'cls_{a}') is not None: r[f'ncls_{a}'] = -r[f'cls_{a}']
        if r.get('cls_MKT') is not None: r['ncls_MKT'] = -r['cls_MKT']
    for a in ARMS:
        full = wmean_ci(lambda r: True, f'nls_{a}', 'nls_BASE')
        morn = wmean_ci(lambda r: r['morning'], f'nls_{a}', 'nls_BASE')
        cond = wmean_ci(lambda r: r['morning'] and r['rose'], f'ncls_{a}', 'ncls_BASE')
        vmk = wmean_ci(lambda r: r['morning'] and r['rose'], f'ncls_{a}', 'ncls_MKT')
        res[a] = {'full': full, 'morning': morn, 'morning_cond': cond, 'morning_cond_vs_market': vmk}
        print(f"== {a}")
        print(f"   整梯子 Δ（臂−BASE，−logp）{full[0]:+.4f} [{full[1]:+.4f}, {full[2]:+.4f}]  行 {full[3]}")
        print(f"   上午整梯子 Δ {morn[0]:+.4f} [{morn[1]:+.4f}, {morn[2]:+.4f}]  上午条件 Δ {cond[0]:+.4f} [{cond[1]:+.4f}, {cond[2]:+.4f}]  行 {cond[3]}")
        if vmk: print(f"   上午条件 对市场 Δ {vmk[0]:+.4f} [{vmk[1]:+.4f}, {vmk[2]:+.4f}]  行 {vmk[3]}")
    base_mk = wmean_ci(lambda r: r['morning'] and r['rose'], 'ncls_BASE', 'ncls_MKT')
    res['BASE_vs_market'] = base_mk
    if base_mk: print(f"== BASE 上午条件 对市场 Δ {base_mk[0]:+.4f} [{base_mk[1]:+.4f}, {base_mk[2]:+.4f}]  行 {base_mk[3]}")
    for a in ('BASE',) + ARMS + ('MKT',):
        S = [r[f'call_{a}'] for r in rows if r['morning'] and r.get(f'call_{a}') is not None and r.get('call_MKT') is not None]
        if S: print(f"   判左右（上午条件，有市场的行）{a}: {np.mean(S):.1%}（{len(S)}）")
    for a in ARMS:
        per = {}
        for st in sorted({r['st'] for r in rows}):
            v = wmean_ci(lambda r, st=st: r['st'] == st, f'nls_{a}', 'nls_BASE')
            if v and v[4] >= 10: per[st] = v
        passed = A.bh({s: v[5] for s, v in per.items()})
        better = sorted(s for s in passed if per[s][0] < 0); worse = sorted(s for s in passed if per[s][0] > 0)
        res[a]['stations'] = {s: v for s, v in per.items()}; res[a]['better'] = better; res[a]['worse'] = worse
        print(f"   {a} 按站（{len(per)} 站）BH 显著好 {better}  显著差 {worse}")
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'prepare': prepare()
    elif cmd == 'walk': walk(int(sys.argv[sys.argv.index('--max-days') + 1]) if '--max-days' in sys.argv else 999)
    elif cmd == 'market': market()
    else: score()
