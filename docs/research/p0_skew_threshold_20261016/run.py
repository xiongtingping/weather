"""PROTOCOL.md: skew threshold t (sigma units) re-selected on the live SLP vectors by mutual information.

usage: python3 run.py
"""
from __future__ import annotations

import json, math, sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_slp_member_side_tilt_20261016'))
import fit as F  # noqa: E402
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1  # noqa: E402

GRID = [round(0.05 * k, 2) for k in range(13)]
LIVE = 0.35
MIN_TRAIN_DAYS = 10


def mutual_info(label, y):
    """label in {-1,0,1}, y in {0,1}; bits."""
    n = len(y)
    if n == 0: return 0.0
    mi = 0.0
    for a in (-1, 0, 1):
        pa = np.mean(label == a)
        if pa == 0: continue
        for b in (0, 1):
            pab = np.mean((label == a) & (y == b)); pb = np.mean(y == b)
            if pab > 0: mi += pab * math.log2(pab / (pa * pb))
    return mi


def labels(g, t):
    return np.where(g > t, 1, np.where(g < -t, -1, 0))


def curve(g, y):
    out = []
    for t in GRID:
        lab = labels(g, t); called = lab != 0
        right = ((lab == 1) & (y == 1)) | ((lab == -1) & (y == 0))
        out.append({'t': t, 'called': float(called.mean()), 'acc': float(right[called].mean()) if called.any() else float('nan'),
                    'net': float((right.sum() - (called & ~right).sum()) / len(y)), 'mi': mutual_info(lab, y)})
    return out


def main():
    rows = F.load_rows()
    recs = []
    for r in rows:
        p = F.slp_vector(r); p = p / p.sum()
        keys = [str(i) for i in range(len(p))]
        rd = mean_mode_skew_reading_v1(dict(zip(keys, p)), {k: i for i, k in enumerate(keys)}, threshold_sigma=0.0)
        if not rd['readable'] or not rd['sigma_buckets']: continue
        m = int(rd['mode_ordinal']); ti = r['ti']
        if ti == m: continue
        recs.append((r['K'], r['st'], r['day'], float(rd['gap_buckets']) / float(rd['sigma_buckets']), int(ti > m)))
    result = {}
    allg = np.array([x[3] for x in recs]); ally = np.array([x[4] for x in recs])
    print('三档合并：t  判了  判对  净判对  互信息(bit)')
    for c in curve(allg, ally):
        print(f"  {c['t']:.2f}  {c['called']:.0%}  {c['acc']:.1%}  {c['net']:+.3f}  {c['mi']:.4f}")
    result['pooled_curve'] = curve(allg, ally)
    for K in (0, 1, 2):
        R = [x for x in recs if x[0] == K]
        g = np.array([x[3] for x in R]); y = np.array([x[4] for x in R]); dd = np.array([x[2] for x in R]); st = np.array([x[1] for x in R])
        days = sorted(set(dd)); di = {d: k for k, d in enumerate(days)}
        cv = curve(g, y)
        best_all = max(cv, key=lambda c: (round(c['mi'], 9), c['t']))
        print(f"== D{K}: {len(R)} 行；全期互信息最大 t={best_all['t']:.2f}（判了 {best_all['called']:.0%}、判对 {best_all['acc']:.1%}、{best_all['mi']:.4f} bit）；"
              f"0.35：判了 {cv[7]['called']:.0%}、判对 {cv[7]['acc']:.1%}、{cv[7]['mi']:.4f} bit")
        for c in cv:
            print(f"    t={c['t']:.2f} 判了 {c['called']:.0%} 判对 {c['acc']:.1%} 净 {c['net']:+.3f} MI {c['mi']:.4f}")
        wf_t = np.full(len(R), np.nan); chosen = {}
        for d in days:
            tr = dd < d
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            best = max(GRID, key=lambda t: (round(mutual_info(labels(g[tr], t), y[tr]), 9), t))
            chosen[d] = best; wf_t[dd == d] = best
        ok = ~np.isnan(wf_t)
        lab_wf = np.where(g > wf_t, 1, np.where(g < -wf_t, -1, 0)); lab_live = labels(g, LIVE)
        mi_wf = mutual_info(lab_wf[ok], y[ok]); mi_live = mutual_info(lab_live[ok], y[ok])
        tdays = sorted(set(dd[ok])); rng = np.random.default_rng(7); diffs = []
        idx_by = {d: np.where((dd == d) & ok)[0] for d in tdays}
        for _ in range(2000):
            pick = rng.choice(tdays, len(tdays)); idx = np.concatenate([idx_by[d] for d in pick])
            diffs.append(mutual_info(lab_wf[idx], y[idx]) - mutual_info(lab_live[idx], y[idx]))
        lo, hi = np.quantile(diffs, [.025, .975])
        def acc(lab):
            called = lab[ok] != 0; right = ((lab[ok] == 1) & (y[ok] == 1)) | ((lab[ok] == -1) & (y[ok] == 0))
            return called.mean(), right[called].mean()
        cw, aw = acc(lab_wf); cl, al = acc(lab_live)
        print(f"  前推 t（{dict(sorted({v: list(chosen.values()).count(v) for v in set(chosen.values())}.items()))}）对 0.35，评估 {int(ok.sum())} 行 {len(tdays)} 日："
              f"互信息 {mi_wf:.4f} 对 {mi_live:.4f}，差 {mi_wf - mi_live:+.4f} [{lo:+.4f}, {hi:+.4f}]；判了/判对 前推 {cw:.0%}/{aw:.1%}、0.35 {cl:.0%}/{al:.1%}")
        per = {}
        for s in sorted(set(st)):
            sel = st == s
            if sel.sum() < 12: continue
            def a2(t):
                lab = labels(g[sel], t); called = lab != 0
                right = ((lab == 1) & (y[sel] == 1)) | ((lab == -1) & (y[sel] == 0))
                return [float(called.mean()), float(right[called].mean()) if called.any() else None]
            per[s] = {'best_all': a2(best_all['t']), 'live': a2(LIVE), 'n': int(sel.sum())}
        worse = [s for s, v in per.items() if v['best_all'][1] is not None and v['live'][1] is not None and v['best_all'][1] < 0.5]
        print(f"  按站（≥12 行）{len(per)} 站；在 t={best_all['t']:.2f} 下判对率低于一半的站：{worse}")
        result[f'D{K}'] = {'curve': cv, 'best_all': best_all, 'chosen': chosen, 'wf_vs_live': [mi_wf, mi_live, float(lo), float(hi)],
                           'calls': {'wf': [float(cw), float(aw)], 'live': [float(cl), float(al)]}, 'stations': per}
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
