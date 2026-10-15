"""PROTOCOL.md: new member-level shoulder information over the six-ensemble equal-mean baseline."""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/raw_member_shoulder_skill_20261016'))
from analyze import auc, auc_ci, to_unit, bucket  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
ENS = ['DWD_ICON_GLOBAL_EPS', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50', 'NOAA_AIGEFS_025', 'NOAA_GEFS_05']
DET = {'ECMWF_IFS_HRES_NATIVE_POINT': 'HRES_3h', 'UKMO_GLOBAL_10KM_NATIVE_POINT': 'UKMO_pmax', 'NOAA_GFS_025_HOURLY_NATIVE_POINT': 'GFS_1h',
       'NOAA_GFS_025_NATIVE_POINT': 'GFS_3h', 'NOAA_GFS_SFLUX_13KM_NATIVE_POINT': 'GFS_SFLUX_pmax', 'ECMWF_AIFS_SINGLE_POINT': 'AIFS1_6h',
       'NOAA_AIGFS_025_NATIVE_POINT': 'AIGFS_6h'}
D_FROM, D_TO = '2026-08-10', '2026-10-14'


def utc(s): return datetime.fromisoformat(str(s).replace('Z', '+00:00')).astimezone(timezone.utc)


def lead_class(L):
    return 'D0' if L < 12 else ('D1' if L < 36 else ('D2' if L < 60 else None))


def main():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    tz = dict(c.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    truth = {}
    for st, d, t, rev in c.execute("select station_id, local_date, official_tmax_c, revision_no from calibration_station_daily_high_truth"
                                   " where source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' and local_date between ? and ? order by revision_no", (D_FROM, D_TO)):
        truth[(st, d)] = float(t)
    unit = {}
    for st, rj in c.execute("select station_id, registry_json from basket_target_market_registry where station_id!='' group by station_id"):
        unit[st] = 'F' if '°F' in rj else 'C'
    c.close()
    day_start = {}

    def L_of(st, d, run):
        k = (st, d)
        if k not in day_start:
            day_start[k] = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
        return (day_start[k] - utc(run)).total_seconds() / 3600
    # ensemble members (reuse the previous study's extract, filtered to the window)
    runs = defaultdict(list)  # (sys, st, day, cls) -> [(run, vals)]
    for line in gzip.open(ROOT / 'docs/research/raw_member_shoulder_skill_20261016/members.jsonl.gz', 'rt'):
        r = json.loads(line)
        if not (D_FROM <= r['day'] <= D_TO) or not r['run'] or r['st'] not in tz: continue
        cls = lead_class(L_of(r['st'], r['day'], r['run']))
        if cls: runs[(r['sys'], r['st'], r['day'], cls)].append((r['run'], r['vals']))
    for v in runs.values(): v.sort()
    det = defaultdict(list)
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    for sysid, st, d, run, tmax in c.execute(
            "select ensemble_system_id, station_id, target_local_date, run_init_utc, forecast_tmax_c from calibration_regional_point_run"
            " where remaining_peak_complete=1 and target_local_date between ? and ? and ensemble_system_id in (%s)" % ','.join('?' * len(DET)),
            (D_FROM, D_TO, *DET)):
        if st not in tz or tmax is None: continue
        cls = lead_class(L_of(st, d, run))
        if cls: det[(DET[sysid], st, d, cls)].append((run, float(tmax)))
    c.close()
    for v in det.values(): v.sort()
    print('ensemble cells', len(runs), 'deterministic cells', len(det))

    # walk-forward bias per (source, station, class)
    def wf_bias(series):  # series: {(src, st, cls): [(day, err)]}
        cache = {}
        def get(src, st, cls, day):
            k = (src, st, cls, day)
            if k in cache: return cache[k]
            own = [e for d, e in series.get((src, st, cls), []) if d < day]
            if len({d for d, _ in series.get((src, st, cls), []) if d < day}) >= 5:
                v = float(np.mean(own))
            else:
                fl = [e for (s2, _, c2), lst in series.items() if s2 == src and c2 == cls for d, e in lst if d < day]
                v = float(np.mean(fl)) if len(fl) >= 20 else None
            cache[k] = v
            return v
        return get
    ens_err = defaultdict(list)
    for (s, st, d, cls), lst in runs.items():
        ens_err[(s, st, cls)].append((d, truth.get((st, d), np.nan) - float(np.mean(lst[-1][1]))))
    for k in ens_err: ens_err[k] = [x for x in ens_err[k] if not math.isnan(x[1])]
    det_err = defaultdict(list)
    for (s, st, d, cls), lst in det.items():
        if (st, d) in truth: det_err[(s, st, cls)].append((d, truth[(st, d)] - lst[-1][1]))
    eb = wf_bias(ens_err); db = wf_bias(det_err)

    units = []
    keys = {(st, d, cls) for (_, st, d, cls) in runs}
    for st, d, cls in keys:
        if (st, d) not in truth: continue
        u = unit.get(st, 'C')
        cen_by = {}; prev_by = {}; frac_by = {}; med_by = {}
        for s in ENS:
            lst = runs.get((s, st, d, cls))
            if not lst: continue
            b = eb(s, st, cls, d)
            if b is None: continue
            vals = np.array(lst[-1][1]) + b
            cen_by[s] = to_unit(float(vals.mean()), u)
            med_by[s] = to_unit(float(np.median(vals)), u)
            frac_by[s] = vals  # converted later relative to the bucket centre
            if len(lst) >= 2:
                prev_by[s] = to_unit(float(np.mean(lst[-2][1])) + b, u)
        if len(cen_by) < 4: continue
        base = float(np.mean(list(cen_by.values())))
        km, cen, w = bucket(base, u); kt, _, _ = bucket(to_unit(truth[(st, d)], u), u)
        if abs(kt - km) != 1: continue
        rec = {'st': st, 'day': d, 'cls': cls, 'y': float(kt == km + 1), 'base': (base - cen) / w}
        if len(prev_by) >= 3:
            rec['lag_prev'] = float(np.mean([(v - cen) / w for v in prev_by.values()]))
            rec['lag_trend'] = float(np.mean([(cen_by[s] - prev_by[s]) / w for s in prev_by]))
            rec['lag_mean2'] = float(np.mean([((cen_by[s] + prev_by[s]) / 2 - cen) / w for s in prev_by]))
        rec['shape_frac'] = float(np.mean([np.mean([to_unit(x, u) > cen for x in v]) - 0.5 for v in frac_by.values()]))
        rec['shape_median'] = float(np.mean([(v - cen) / w for v in med_by.values()]))
        dets = {}
        for name in DET.values():
            lst = det.get((name, st, d, cls))
            if not lst: continue
            b = db(name, st, cls, d)
            if b is None: continue
            dets[name] = (to_unit(lst[-1][1] + b, u) - cen) / w
            rec['det_' + name] = dets[name]
        if len(dets) >= 4:
            rec['det_equal'] = float(np.mean(list(dets.values())))
        # paired GFS sampling: same run
        g1 = det.get(('GFS_1h', st, d, cls)); g3 = det.get(('GFS_3h', st, d, cls))
        if g1 and g3 and g1[-1][0] == g3[-1][0]:
            rec['pair_gfs'] = True
        units.append(rec)
    print('shoulder units', len(units), {k: sum(1 for x in units if x['cls'] == k) for k in ('D0', 'D1', 'D2')})
    json.dump(units, gzip.open(HERE / 'units.json.gz', 'wt'))

    cands = ['lag_prev', 'lag_trend', 'lag_mean2', 'shape_frac', 'shape_median'] + ['det_' + n for n in DET.values()] + ['det_equal']
    days = sorted({x['day'] for x in units})

    def logit_fit(X, y, ridge=1.0):
        b = np.zeros(X.shape[1])
        for _ in range(50):
            p = 1 / (1 + np.exp(-X @ b)); g = X.T @ (y - p) - ridge * np.r_[0, b[1:]]
            H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.diag(np.r_[0, np.ones(X.shape[1] - 1)]) + 1e-9 * np.eye(X.shape[1])
            b += np.linalg.solve(H, g)
        return b

    def increment(sel, cand):
        sel = [x for x in sel if cand in x]
        if len(sel) < 150: return None
        d_arr = np.array([x['day'] for x in sel]); y = np.array([x['y'] for x in sel])
        xb = np.array([x['base'] for x in sel]); xc = np.array([x[cand] for x in sel])
        ll0 = np.full(len(sel), np.nan); ll1 = np.full(len(sel), np.nan); p0s = np.full(len(sel), np.nan); p1s = np.full(len(sel), np.nan)
        ud = sorted(set(d_arr))
        for d in ud:
            tr = d_arr < d; te = d_arr == d
            if len(set(d_arr[tr])) < 15: continue
            X0 = np.c_[np.ones(tr.sum()), xb[tr]]; X1 = np.c_[X0, xc[tr]]
            b0 = logit_fit(X0, y[tr]); b1 = logit_fit(X1, y[tr])
            p0 = 1 / (1 + np.exp(-(np.c_[np.ones(te.sum()), xb[te]] @ b0))); p1 = 1 / (1 + np.exp(-(np.c_[np.ones(te.sum()), xb[te], xc[te]] @ b1)))
            p0s[te] = p0; p1s[te] = p1
            ll0[te] = -(y[te] * np.log(p0) + (1 - y[te]) * np.log(1 - p0)); ll1[te] = -(y[te] * np.log(p1) + (1 - y[te]) * np.log(1 - p1))
        ok = ~np.isnan(ll0)
        if ok.sum() < 100: return None
        dl = ll1[ok] - ll0[ok]; dd = d_arr[ok]; uu = np.unique(dd); rng = np.random.default_rng(7)
        idx_by = {d: np.where(dd == d)[0] for d in uu}
        bs = []; ba = []
        for _ in range(500):
            pick = np.concatenate([idx_by[d] for d in rng.choice(uu, len(uu))])
            bs.append(dl[pick].mean()); ba.append(auc(p1s[ok][pick], y[ok][pick]) - auc(p0s[ok][pick], y[ok][pick]))
        a_alone, lo, hi = auc_ci(xc, y, d_arr, reps=200)
        return {'n': int(ok.sum()), 'alone_auc': a_alone, 'alone_lo': lo, 'alone_hi': hi, 'base_auc': auc(p0s[ok], y[ok]), 'with_auc': auc(p1s[ok], y[ok]),
                'dlogloss': float(dl.mean()), 'dll_lo': float(np.quantile(bs, .025)), 'dll_hi': float(np.quantile(bs, .975)),
                'dauc_lo': float(np.quantile(ba, .025)), 'dauc_hi': float(np.quantile(ba, .975))}

    out = {'n_units': len(units), 'baseline_auc_all': auc([x['base'] for x in units], [x['y'] for x in units]), 'by_class': {}}
    print('baseline equal-mean AUC (all units)', round(out['baseline_auc_all'], 3))
    for cls in ('ALL', 'D0', 'D1', 'D2'):
        sel = units if cls == 'ALL' else [x for x in units if x['cls'] == cls]
        print(f'\n== {cls} (units {len(sel)}, baseline AUC {auc([x["base"] for x in sel], [x["y"] for x in sel]):.3f})')
        res = {}
        for cand in cands:
            r = increment(sel, cand)
            if r is None: continue
            res[cand] = r
            flag = 'GAIN' if r['dll_hi'] < 0 else ('worse' if r['dll_lo'] > 0 else '')
            print(f"  {cand:22s} n{r['n']:5d} alone AUC {r['alone_auc']:.3f} [{r['alone_lo']:.3f},{r['alone_hi']:.3f}]  base {r['base_auc']:.3f} -> with {r['with_auc']:.3f}"
                  f"  dLogLoss {r['dlogloss']:+.4f} [{r['dll_lo']:+.4f},{r['dll_hi']:+.4f}] {flag}")
        out['by_class'][cls] = res
    # D: GFS sampling paired
    pr = [x for x in units if x.get('pair_gfs') and 'det_GFS_1h' in x and 'det_GFS_3h' in x]
    if pr:
        y = [x['y'] for x in pr]; d_arr = [x['day'] for x in pr]
        a1 = auc_ci([x['det_GFS_1h'] for x in pr], y, d_arr); a3 = auc_ci([x['det_GFS_3h'] for x in pr], y, d_arr)
        print(f'\nGFS same run: hourly AUC {a1[0]:.3f} [{a1[1]:.3f},{a1[2]:.3f}]  3-hourly {a3[0]:.3f} [{a3[1]:.3f},{a3[2]:.3f}]  (units {len(pr)})')
        out['gfs_pair'] = {'hourly': a1, 'three_hourly': a3, 'n': len(pr)}
    json.dump(out, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
