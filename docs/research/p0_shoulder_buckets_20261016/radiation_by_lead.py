"""Charter 222 radiation shift, re-evaluated per lead (熊先生 10-16 「D1 重新评估辐射平移」).

For P0s decided before local 09:00 of D-K (K = 0, 1, 2), 09-01..10-14: rebuild the post-pool chain with production functions
(pool -> consensus bridge -> radiation shift), then serve BOTH the radiation-shifted and the unshifted vector through the
P0's own table layer reconstructed from its receipt (served_shape.apply_served_table_layers + the 1e-3 floor).  The
reconstruction is checked against the stored served vector.  Scores: LogScore / CRPS / Brier (charter 240), station-day
equal weight, day-block bootstrap; which-shoulder AUC around the served mode.  usage: python3 radiation_by_lead.py K
"""
from __future__ import annotations

import json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.served_shape import CenterWidthLayer, apply_served_table_layers  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from pipeline_audit import auc  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'


def layer_from_receipt(r):
    sliced = r.get('width_slice_tail_weight')
    return CenterWidthLayer(
        delta_c=float(r.get('delta_c') or 0.0),
        width_lambda=float(r.get('stage_width_lambda') or r.get('width_lambda') or 1.0),
        station_width_scale=float(r.get('station_width_scale') or 1.0),
        sigma_floor_rungs=float(r.get('sigma_floor_rungs') or 0.0),
        width_slice=r.get('width_slice') if sliced else None,
        width_slice_tail_weight=float(sliced) if sliced else None,
        width_slice_tail_scale=float(r.get('width_slice_tail_scale') or 0.0) if sliced else None,
        peak_ceiling=float(r['peak_ceiling']) if r.get('peak_ceiling') is not None else None,
    )


def main(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    recs = []; stats = defaultdict(int); tvs = []
    for (st, d), decs in by.items():
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if not ok: continue
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, ok[-1])).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        sysp = json.loads(lin.get('native_system_probabilities_json') or '{}'); wts = json.loads(lin.get('system_weights_json') or '{}')
        cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
        cw = json.loads(lin.get('center_width_json') or 'null')
        rad = json.loads(lin.get('synoptic_cooling_json') or '{}') if lin.get('synoptic_cooling_json') else {}
        if not iv or not sysp or not cw: stats['incomplete'] += 1; continue
        ordered = NP._ordered_buckets(iv)
        systems = [s for s in sysp if float(wts.get(s, 0)) > 0 and set(sysp[s]) == set(ordered)]
        if not systems or not all(s in cen for s in systems): stats['systems'] += 1; continue
        tw = sum(float(wts[s]) for s in systems)
        br, _ = bridged_cross_probability(system_probabilities={s: sysp[s] for s in systems}, system_weights={s: float(wts[s]) / tw for s in systems},
                                          system_centers_c={s: float(cen[s]) for s in systems}, ordered_keys=ordered, key_centers_c=NP._bucket_centers(iv, ordered))
        shift = float(rad.get('shift_c') or 0.0) if rad.get('state') == 'RESOLVED' else 0.0
        withrad = NP._shift_probability_c(br, ordered=ordered, bucket_centers_c=[v + shift for v in NP._bucket_centers(iv, ordered)], intervals=iv) if abs(shift) > 1e-9 else br
        centers, open_edges = served_bucket_axis(iv, ordered)
        layer = layer_from_receipt(cw)

        def serve(v):
            out, _ = apply_served_table_layers(v, ordered_keys=ordered, key_centers_c=centers, center_width=layer, open_edge_bounds_c=open_edges)
            f, _ = floor_probabilities(out)
            return np.array([f[b] for b in ordered])
        try:
            s_with = serve(withrad); s_without = serve(br)
        except Exception as exc:  # noqa: BLE001
            stats['layer_error:' + type(exc).__name__] += 1; continue
        sv = np.array([float(served[b]) for b in ordered]); sv /= sv.sum()
        tv = 0.5 * np.abs(s_with - sv).sum(); tvs.append(tv)
        if tv > 0.02: stats['reconstruction_off'] += 1; continue
        order_c, cc, w = A.centres(iv)
        T = truth[(st, d)]
        ti = next(i for i, b in enumerate(ordered) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        m = int(np.argmax(sv))

        def sc(p):
            cdf = np.cumsum(p)[:-1]
            return (-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(len(p)) == ti)) ** 2).sum()),
                    float((((cdf - (ti <= np.arange(len(p) - 1))) ** 2) * np.diff(np.array(centers))).sum()))
        recs.append({'st': st, 'day': d, 'shift': shift, 'with': sc(s_with), 'without': sc(s_without), 'm': m, 'ti': ti,
                     'split_with': float(s_with[m + 1] / (s_with[m + 1] + s_with[m - 1])) if 0 < m < len(sv) - 1 else None,
                     'split_without': float(s_without[m + 1] / (s_without[m + 1] + s_without[m - 1])) if 0 < m < len(sv) - 1 else None})
    print(f'D-{K}: rows {len(recs)}  reconstruction TV median {np.median(tvs):.2e} p95 {np.quantile(tvs, .95):.2e}  {dict(stats)}')
    days = sorted({r['day'] for r in recs}); di = {d: i for i, d in enumerate(days)}
    draws = np.random.default_rng(7).multinomial(len(days), np.full(len(days), 1 / len(days)), size=2000)
    out = {'K': K, 'n': len(recs), 'mean_abs_shift_c': float(np.mean([abs(r['shift']) for r in recs]))}
    for k, name in enumerate(('dlog', 'dbrier', 'dcrps')):
        ds = np.zeros(len(days)); dc = np.zeros(len(days))
        for r in recs:
            ds[di[r['day']]] += r['with'][k] - r['without'][k]; dc[di[r['day']]] += 1
        bs = (draws @ ds) / (draws @ dc)
        out[name] = [float(ds.sum() / dc.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975))]
    sh = [r for r in recs if abs(r['ti'] - r['m']) == 1 and r['split_with'] is not None]
    y = [float(r['ti'] == r['m'] + 1) for r in sh]
    out['shoulder_auc_with'] = auc([r['split_with'] for r in sh], y); out['shoulder_auc_without'] = auc([r['split_without'] for r in sh], y)
    out['shoulder_n'] = len(sh)
    print(f"  radiation shift (with - without; negative = the shift helps): dLog {out['dlog'][0]:+.4f} [{out['dlog'][1]:+.4f},{out['dlog'][2]:+.4f}]"
          f"  dCRPS {out['dcrps'][0]:+.4f} [{out['dcrps'][1]:+.4f},{out['dcrps'][2]:+.4f}]  dBrier {out['dbrier'][0]:+.4f} [{out['dbrier'][1]:+.4f},{out['dbrier'][2]:+.4f}]"
          f"  | mean |shift| {out['mean_abs_shift_c']:.3f}C | shoulder AUC with {out['shoulder_auc_with']:.3f} without {out['shoulder_auc_without']:.3f} (n{len(sh)})")
    json.dump(out, open(HERE / f'readings_radiation_d{K}.json', 'w'), indent=1)


if __name__ == '__main__':
    main(int(sys.argv[1]))
