"""PROTOCOL.md: spread-adjusted linear pool vs the served P0 (pool + bridge + radiation + width layer) and served + 271.

Two passes: build (fetch every P0's members and certificates once, rebuild the SLP vector for each c on the grid,
reconstruct the served table layer from the receipt) -> rows_d{K}.json.gz; score (walk-forward c, charter 240, per station).
usage: python3 run.py build K | python3 run.py score
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
sys.path.insert(0, str(ROOT / 'docs/research/p0_shoulder_buckets_20261016'))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_mode_shoulder, mode_shoulder_share_for  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402
from radiation_by_lead import layer_from_receipt  # noqa: E402
from pipeline_audit import auc  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
GRID = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
TABLE_271 = {"bins": {"h48_plus": {"share": 0.15}}}
MIN_TRAIN_DAYS = 10


def floored(vec, ordered):
    f, _ = floor_probabilities({b: float(vec[b]) for b in ordered})
    return [f[b] for b in ordered]


def build(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    art_cache = {}; stats = defaultdict(int)
    out = gzip.open(HERE / f'rows_d{K}.json.gz', 'wt')
    for (st, d), decs in sorted(by.items()):
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if not ok: continue
        dec = ok[-1]
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        sysp = json.loads(lin.get('native_system_probabilities_json') or '{}'); wts = json.loads(lin.get('system_weights_json') or '{}')
        cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
        runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}'); arts = json.loads(lin.get('system_artifact_shas_json') or '{}')
        cw = json.loads(lin.get('center_width_json') or 'null')
        rad = json.loads(lin.get('synoptic_cooling_json') or '{}') if lin.get('synoptic_cooling_json') else {}
        if not iv or not sysp:
            stats['incomplete'] += 1; continue
        ordered = NP._ordered_buckets(iv)
        ivs = {b: iv[b] for b in ordered}
        systems = [s for s in sysp if float(wts.get(s, 0)) > 0 and s in runs and s in arts and s in cen]
        comps = {}
        for s in systems:
            vals = [float(v) for (v,) in c.execute(
                "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
                " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 2: continue
            if arts[s] not in art_cache:
                a = c.execute('select bias_parameters_json, spread_parameters_json, reliability_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?', (arts[s],)).fetchone()
                art_cache[arts[s]] = None if a is None else tuple(json.loads(x) for x in a)
            if art_cache[arts[s]] is None: continue
            comps[s] = (vals, *art_cache[arts[s]])
        if set(comps) != set(systems) or not systems:
            stats['members_missing'] += 1; continue
        # check: rebuilding at c=1 reproduces the stored system vectors
        bad = False
        for s, (vals, bp, sp, rp) in comps.items():
            v = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bp, spread_parameters=sp, reliability_parameters=rp)
            if 0.5 * sum(abs(v[b] - float(sysp[s][b])) for b in ordered) > 1e-6: bad = True
        if bad:
            stats['rebuild_mismatch'] += 1; continue
        tw = sum(float(wts[s]) for s in systems)
        w = {s: float(wts[s]) / tw for s in systems}
        centers_key = NP._bucket_centers(iv, ordered)
        shift_sigma = rad.get('shift_sigma') if rad.get('state') == 'RESOLVED' else None
        slp = {}
        for cfac in GRID:
            vecs = {}
            for s, (vals, bp, sp, rp) in comps.items():
                sp2 = dict(sp); sp2['spread_factor'] = min(5.0, max(0.25, float(sp['spread_factor']) * cfac))
                vecs[s] = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bp, spread_parameters=sp2, reliability_parameters=rp)
            pooled, _ = bridged_cross_probability(system_probabilities=vecs, system_weights=w, system_centers_c={s: float(cen[s]) for s in systems},
                                                  ordered_keys=ordered, key_centers_c=centers_key)
            if shift_sigma is not None:
                spread_c = NP._distribution_sigma_c(pooled, centers_key)
                shift = float(shift_sigma) * spread_c
                if abs(shift) > 1e-9:
                    pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in centers_key], intervals=iv)
            slp[str(cfac)] = {'pool': floored(pooled, ordered)}
            if cw:
                centers, open_edges = served_bucket_axis(iv, ordered)
                try:
                    lay, _ = apply_served_table_layers(pooled, ordered_keys=ordered, key_centers_c=centers, center_width=layer_from_receipt(cw), open_edge_bounds_c=open_edges)
                    slp[str(cfac)]['with_layer'] = floored(lay, ordered)
                except ValueError:
                    pass
        hours = hours_to_settlement_if_known(d, as_of_utc=dec, settlement_timezone=tzs[st])
        sv = {b: float(served[b]) for b in ordered}
        res = mode_shoulder_share_for(TABLE_271, hours_to_settlement=hours)
        sv271 = apply_mode_shoulder(sv, share=res[1], ordered_keys=ordered)[0] if res else sv
        T = truth[(st, d)]
        ti = next(i for i, b in enumerate(ordered) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        order_c, cc, wd = A.centres(iv)
        out.write(json.dumps({'st': st, 'day': d, 'ti': ti, 'widths': list(np.diff(np.array(served_bucket_axis(iv, ordered)[0]))), 'hours': hours,
                              'unit': 'F' if '°F' in ordered[0] else 'C', 'served': floored(sv, ordered), 'served271': floored(sv271, ordered),
                              'slp': slp, 'n_sys': len(systems)}) + '\n')
        stats['rows'] += 1
    out.close()
    print(f'D-{K}', dict(stats))


def scores(p, r):
    p = np.asarray(p, float); ti = r['ti']
    cdf = np.cumsum(p)[:-1]
    return np.array([-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(len(p)) == ti)) ** 2).sum()),
                     float((((cdf - (ti <= np.arange(len(p) - 1))) ** 2) * np.asarray(r['widths'])).sum())])


def score():
    result = {}
    for K in (0, 1, 2):
        R = [json.loads(l) for l in gzip.open(HERE / f'rows_d{K}.json.gz', 'rt')]
        days = sorted({r['day'] for r in R}); di = {d: i for i, d in enumerate(days)}
        dd = np.array([r['day'] for r in R]); st = np.array([r['st'] for r in R])
        base = np.array([scores(r['served'], r) for r in R])
        s271 = np.array([scores(r['served271'], r) for r in R])
        slp = {cf: np.array([scores(r['slp'][str(cf)]['pool'], r) for r in R]) for cf in GRID}
        slpL = {cf: np.array([scores(r['slp'][str(cf)].get('with_layer', r['slp'][str(cf)]['pool']), r) for r in R]) for cf in GRID}
        # walk-forward c
        wf = np.full((len(R), 3), np.nan); chosen = {}
        wfL = np.full((len(R), 3), np.nan)
        for d in days:
            tr = dd < d
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            cf = min(GRID, key=lambda g: slp[g][tr, 0].mean()); chosen[d] = cf; wf[dd == d] = slp[cf][dd == d]
            cfL = min(GRID, key=lambda g: slpL[g][tr, 0].mean()); wfL[dd == d] = slpL[cfL][dd == d]
        tested = ~np.isnan(wf[:, 0])
        draws = np.random.default_rng(7).multinomial(len(days), np.full(len(days), 1 / len(days)), size=2000)

        def boot(delta, mask):
            ds = np.zeros(len(days)); dc = np.zeros(len(days))
            for i in np.where(mask & ~np.isnan(delta))[0]:
                ds[di[dd[i]]] += delta[i]; dc[di[dd[i]]] += 1
            if dc.sum() == 0: return None
            bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9); bs = bs[(draws @ dc) > 0]
            return [float(ds.sum() / dc.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)),
                    float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / len(bs)))]
        res = {'rows': len(R), 'tested_rows': int(tested.sum()), 'c_by_day': chosen, 'arms': {}}
        arms = {'SLP_wf - served': wf - base, 'SLP+layer_wf - served': wfL - base, 'served+271 - served': s271 - base,
                'SLP_wf - served+271': wf - s271}
        for cf in GRID: arms[f'SLP_c{cf} - served'] = slp[cf] - base
        for name, delta in arms.items():
            res['arms'][name] = {m: boot(delta[:, k], tested) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
        # per station, main arm
        per = {}
        for s in sorted(set(st)):
            b = boot((wf - base)[:, 0], tested & (st == s))
            if b and len(set(dd[tested & (st == s)])) >= 10: per[s] = b
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['stations'] = {s: {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        res['better_bh'] = sorted(s for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(s for s in passed if per[s][0] > 0)
        # mode / shoulder calibration on tested rows
        def calib(key):
            pm = []; ps = []; ym = []; ys = []
            for r, t in zip(R, tested):
                if not t: continue
                p = np.asarray(r['served'] if key == 'served' else r['slp'][str(chosen[r['day']])]['pool']); m = int(np.argmax(p))
                pm.append(p[m]); ps.append((p[m - 1] if m > 0 else 0) + (p[m + 1] if m + 1 < len(p) else 0)); ym.append(r['ti'] == m); ys.append(abs(r['ti'] - m) == 1)
            return [float(np.mean(pm)), float(np.mean(ym)), float(np.mean(ps)), float(np.mean(ys))]
        res['calibration'] = {'served': calib('served'), 'SLP_wf': calib('slp')}
        result[f'D{K}'] = res
        print(f"\n== D{K}: rows {len(R)} tested {int(tested.sum())}  c chosen {sorted(set(chosen.values()))} (last {chosen[max(chosen)] if chosen else None})")
        for name in ['SLP_wf - served', 'SLP+layer_wf - served', 'served+271 - served', 'SLP_wf - served+271'] + [f'SLP_c{cf} - served' for cf in GRID]:
            a = res['arms'][name]
            print(f"  {name:24s} dLog {a['dlog'][0]:+.4f} [{a['dlog'][1]:+.4f},{a['dlog'][2]:+.4f}]  dCRPS {a['dcrps'][0]:+.4f} [{a['dcrps'][1]:+.4f},{a['dcrps'][2]:+.4f}]  dBrier {a['dbrier'][0]:+.4f} [{a['dbrier'][1]:+.4f},{a['dbrier'][2]:+.4f}]")
        print(f"  per station (SLP_wf): BH better {res['better_bh']}  BH worse {res['worse_bh']}")
        cs, cl = res['calibration']['served'], res['calibration']['SLP_wf']
        print(f"  mode p/real: served {cs[0]:.3f}/{cs[1]:.3f}  SLP {cl[0]:.3f}/{cl[1]:.3f} | shoulders p/real: served {cs[2]:.3f}/{cs[3]:.3f}  SLP {cl[2]:.3f}/{cl[3]:.3f}")
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'build':
        build(int(sys.argv[2]))
    else:
        score()
