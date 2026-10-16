"""Reading (not a gate): how well each signal calls the side -- truth above vs below the P0 mode -- by lead and station.

Rows: p0_slp_per_system_c_20261016 (D0/D1/D2, 09-01..10-14) joined to the served old-chain vector (p0_spread_adjusted_pool_20261016)
and the seated members (p0_slp_member_side_tilt_20261016). Only rows whose truth is off the mode bucket are scored.
"""
from __future__ import annotations

import gzip, json, math, sys
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_slp_member_side_tilt_20261016'))
import fit as F  # noqa: E402
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1  # noqa: E402
from bayes_basket_target.delta_v2_runtime import SKEW_GAP_THRESHOLD_SIGMA  # noqa: E402
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402


def auc(score, y):
    score = np.asarray(score, float); y = np.asarray(y, bool)
    pos, neg = score[y], score[~y]
    if not len(pos) or not len(neg): return float('nan')
    allv = np.concatenate([pos, neg]); ranks = allv.argsort().argsort() + 1.0
    # average ties
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, ranks); ranks = (sums / cnt)[inv]
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def skew_sign(p):
    keys = [str(i) for i in range(len(p))]
    r = mean_mode_skew_reading_v1(dict(zip(keys, p)), {k: i for i, k in enumerate(keys)}, threshold_sigma=SKEW_GAP_THRESHOLD_SIGMA)
    return int(r['sign'])


def main():
    rows = F.load_rows()
    mem = json.load(gzip.open(ROOT / 'docs/research/p0_slp_member_side_tilt_20261016/members.json.gz', 'rt'))
    served = {}
    for K in (0, 1, 2):
        for l in gzip.open(ROOT / f'docs/research/p0_spread_adjusted_pool_20261016/rows_d{K}.json.gz', 'rt'):
            r = json.loads(l); served[(K, r['st'], r['day'])] = r['served']
    out = []
    for r in rows:
        p = F.slp_vector(r); p = p / p.sum()
        m, z = F.side_z(p, r, mem[f"{r['K']}|{r['st']}|{r['day']}"])
        if z is None or r['ti'] == m: continue
        old = np.array(served[(r['K'], r['st'], r['day'])], float)
        up_mass = p[m + 1:].sum(); dn_mass = p[:m].sum()
        f_p = up_mass + 0.5 * p[m]
        f_mem = 1 / (1 + math.exp(-(z + math.log(min(.98, max(.02, f_p)) / (1 - min(.98, max(.02, f_p)))))))
        out.append(dict(K=r['K'], st=r['st'], day=r['day'], up=int(r['ti'] > m),
                        s_slp=math.log(up_mass / dn_mass), s_mem=math.log(f_mem / (1 - f_mem)), z=z,
                        sk_slp=skew_sign(p), sk_old=skew_sign(old), old_mode_same=int(int(np.argmax(old)) == m)))
    res = {}
    for K in (0, 1, 2):
        R = [o for o in out if o['K'] == K]
        y = np.array([o['up'] for o in R]); dd = np.array([o['day'] for o in R]); days = sorted(set(dd))
        rng = np.random.default_rng(7)
        def ci(fn):
            vals = []
            for _ in range(500):
                pick = rng.choice(days, len(days)); idx = np.concatenate([np.where(dd == d)[0] for d in pick])
                vals.append(fn(idx))
            return np.nanquantile(vals, [.025, .975])
        print(f'== D{K}: rows with truth off the mode {len(R)}, truth above {y.mean():.3f}')
        line = {}
        for name in ('s_slp', 's_mem', 'z'):
            s = np.array([o[name] for o in R]); a = auc(s, y); lo, hi = ci(lambda idx: auc(s[idx], y[idx]))
            line[name] = [a, lo, hi]
            print(f'  AUC {name:6s} {a:.3f} [{lo:.3f}, {hi:.3f}]')
        # walk-forward logistic on (s_slp, s_mem)
        comb = np.full(len(R), np.nan)
        X = np.column_stack([np.ones(len(R)), [o['s_slp'] for o in R], [o['s_mem'] for o in R]])
        for d in days:
            tr = dd < d
            if len(set(dd[tr])) < 10: continue
            w = np.zeros(3)
            for _ in range(200):
                pr = 1 / (1 + np.exp(-X[tr] @ w)); g = X[tr].T @ (pr - y[tr]); H = (X[tr].T * (pr * (1 - pr))) @ X[tr] + 1e-6 * np.eye(3)
                w -= np.linalg.solve(H, g)
            comb[dd == d] = X[dd == d] @ w
        ok = ~np.isnan(comb)
        a_c = auc(comb[ok], y[ok]); a_s = auc(X[ok, 1], y[ok])
        print(f'  walk-forward SLP+members: AUC {a_c:.3f} vs SLP alone on the same rows {a_s:.3f} (n={int(ok.sum())})')
        line['combined_wf'] = [a_c, a_s, int(ok.sum())]
        for lab in ('sk_slp', 'sk_old'):
            for sgn, nm in ((1, '右偏'), (-1, '左偏'), (0, '对称')):
                sel = np.array([o[lab] == sgn for o in R])
                if sel.sum():
                    print(f'  {lab} {nm}: {int(sel.sum())} 天（占 {sel.mean():.0%}），真值在众数上方 {y[sel].mean():.3f}')
        # confident calls by the combined score / by members
        for name, s in (('s_slp', X[:, 1]), ('s_mem', X[:, 2])):
            q = np.quantile(s, [1 / 3, 2 / 3])
            print(f'  {name} 三分位：判下 {y[s <= q[0]].mean():.3f}  中 {y[(s > q[0]) & (s <= q[1])].mean():.3f}  判上 {y[s > q[1]].mean():.3f}（真值在上方的比例）')
        st = np.array([o['st'] for o in R]); per = {}
        for s_ in sorted(set(st)):
            sel = st == s_
            if sel.sum() >= 12 and 0 < y[sel].mean() < 1:
                per[s_] = (auc(X[sel, 1], y[sel]), auc(X[sel, 2], y[sel]), int(sel.sum()))
        better = sorted(s_ for s_, v in per.items() if v[1] - v[0] > 0.1)
        print(f'  按站（≥12 行）{len(per)} 站：SLP AUC 中位 {np.median([v[0] for v in per.values()]):.3f}，成员 AUC 中位 {np.median([v[1] for v in per.values()]):.3f}；'
              f'成员比 SLP 高 0.1 以上 {len(better)} 站 {better}')
        res[f'D{K}'] = {'auc': line, 'stations': {k: list(v) for k, v in per.items()}}
    json.dump(res, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == "__main__" and len(sys.argv) == 1:
    main()


def tradeoff():
    """Calls vs accuracy as the SLP side threshold moves; and what the members say on the days SLP calls symmetric."""
    rows = F.load_rows()
    mem = json.load(gzip.open(ROOT / 'docs/research/p0_slp_member_side_tilt_20261016/members.json.gz', 'rt'))
    for K in (0, 1, 2):
        G = []
        for r in rows:
            if r['K'] != K: continue
            p = F.slp_vector(r); p = p / p.sum()
            m, z = F.side_z(p, r, mem[f"{r['K']}|{r['st']}|{r['day']}"])
            if z is None or r['ti'] == m: continue
            idx = np.arange(len(p)); mu = (p * idx).sum(); sd = math.sqrt((p * (idx - mu) ** 2).sum())
            G.append(((mu - m) / sd, int(r['ti'] > m), z))
        G = np.array(G)
        print(f'== D{K}: gap/sigma threshold -> share of days called, accuracy of the call')
        for t in (0.0, 0.1, 0.2, 0.35, 0.5):
            sel = np.abs(G[:, 0]) > t
            acc = ((G[sel, 0] > 0) == (G[sel, 1] == 1)).mean()
            print(f'   |gap|>{t:.2f}σ: 判了 {sel.mean():.0%} 天，判对 {acc:.1%}')
        sym = np.abs(G[:, 0]) <= 0.35
        print(f'   0.35σ 下判「对称」的 {int(sym.sum())} 天：按成员 z 判上/下的准确率 {((G[sym, 2] > 0) == (G[sym, 1] == 1)).mean():.1%}，AUC {auc(G[sym, 2], G[sym, 1]):.3f}')


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'tradeoff':
    tradeoff()
