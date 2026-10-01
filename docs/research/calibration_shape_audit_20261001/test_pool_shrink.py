"""Test of the two pre-pool shrinks (user: 「测」, 2026-10-01).

beta: recentre each system toward the weighted consensus centre by beta*(c_bar - c_k)   (between-system term)
gamma: scale each system about its own centre by gamma                                (within-system width kept after averaging)
Everything else in the chain is the production chain replayed from the receipts (cooling shift, width delta/lambda, floor).
Baseline = reconstructed actual served vector (fidelity gate 1e-7 against the stored vector).
Selection: leave-one-date-out on LogScore; verdict per charter 211/228: bucket Brier AND LogScore 95% upper bound < 0.
Only production primitives are called: shift_probability, apply_center_width_calibration, bridged_cross_probability, floor_probabilities.
"""
import collections, itertools, json, math, pathlib, resource, sys, time
import numpy as np
from bayes_basket_target.cross_consensus import bridged_cross_probability, distribution_sigma_c, shift_probability
from bayes_basket_target.native_ensemble_p0 import _ordered_buckets, _served_axis, _shift_probability_c
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, bucket_rung_width_c
from bayes_basket_target.materialization import floor_probabilities
OUT = pathlib.Path(__file__).parent; START = time.monotonic()
sys.path.insert(0, str(OUT.parent / 'p0_three_layer_ablation_20260928'))
import historical_width_pre198 as historical_width
resource.setrlimit(resource.RLIMIT_CPU, (900, 900)); rng = np.random.default_rng(20261001)
BETAS = (0.0, 0.25, 0.5, 0.75, 1.0); GAMMAS = (0.6, 0.7, 0.8, 0.9, 1.0)

def guard():
    if time.monotonic() - START > 1500 or not 0 < resource.getrusage(resource.RUSAGE_SELF).ru_maxrss < 1024 * 1024**2: raise RuntimeError('resources')
def norm(p): t = sum(p.values()); return {k: v / t for k, v in p.items()}
def delta(a, b): return max(abs(a[k] - b[k]) for k in a)

def chain(x, beta=None, gamma=1.0, width_lambda=None, production_bridge=True):
    """beta=None -> production bridge decides (baseline); beta given -> explicit recentre, bridge off."""
    geometry = x['bucket_intervals_c_json']; keys = _ordered_buckets(geometry); axis, edges = _served_axis(geometry, keys)
    systems = x['native_system_probabilities_json']; centers = x['native_calibrated_system_centers_c_json']; weights = x['system_weights_json']
    if not set(weights) <= set(systems) or not set(weights) <= set(centers): raise ValueError('weight_set')
    cw = x['center_width_json']
    if not cw: raise ValueError('no_width_receipt')
    if cw.get('sigma_floor_rungs', 0) != 0 or cw.get('width_tier') is not None: raise ValueError('unsupported_width_parameters')
    wf = apply_center_width_calibration
    if abs(cw['bucket_rung_c'] - bucket_rung_width_c(axis)) > 1e-8:
        if abs(cw['bucket_rung_c'] - historical_width.bucket_rung_width_c(axis)) > 1e-8: raise ValueError('rung_mismatch')
        wf = historical_width.apply_center_width_calibration
    comps = {}
    for s in weights:
        p = {k: float(systems[s].get(k, 0.0)) for k in keys}
        if gamma != 1.0:
            p, _ = wf(p, delta_c=0.0, width_lambda=float(gamma), ordered_keys=keys, key_centers_c=axis, open_edge_bounds_c=edges)
        comps[s] = norm(p)
    if beta is None and production_bridge and x.get('consensus_bridge_json'):
        p, receipt = bridged_cross_probability(system_probabilities=comps, system_weights=weights, system_centers_c=centers, ordered_keys=keys, key_centers_c=axis)
    else:
        cbar = sum(weights[s] * centers[s] for s in weights) / sum(weights.values())
        p = {k: 0.0 for k in keys}
        for s, w in weights.items():
            q = comps[s]
            if beta: q = shift_probability(q, delta_c=float(beta) * (cbar - centers[s]), ordered_keys=keys, key_centers_c=axis)
            for k in keys: p[k] += w * q[k]
        p = norm(p)
    cooling = x.get('synoptic_cooling_json')
    if cooling and cooling.get('shift_sigma') is not None:
        shift = float(cooling['shift_sigma']) * distribution_sigma_c([p[k] for k in keys], axis)
        if abs(shift) > 1e-12: p = _shift_probability_c(p, ordered=keys, bucket_centers_c=[a + shift for a in axis], intervals=geometry)
    elif cooling and cooling.get('shift_c', 0): raise ValueError('cooling_sigma_missing')
    nb = x.get('neighbour_direction_json')
    if nb and (nb.get('state') != 'RETIRED_CHARTER_147' or nb.get('applied_factors')): raise ValueError('non_identity_neighbour')
    lam = cw['width_lambda'] if width_lambda is None else width_lambda
    p, _ = wf(p, delta_c=cw['delta_c'], width_lambda=float(lam), ordered_keys=keys, key_centers_c=axis, open_edge_bounds_c=edges)
    band = x.get('band_reliability_json')
    if band and any(abs(float(v) - 1) > 1e-12 for v in (band.get('applied_factors') or {}).values()): raise ValueError('non_identity_band')
    fl = x.get('probability_floor_json')
    if fl and fl.get('epsilon') != 0.001: raise ValueError('different_floor')
    p, _ = floor_probabilities(norm(p))
    return p, keys

def score(p, hit): return (sum((v - (k == hit))**2 for k, v in p.items()), -math.log(p[hit]))

def main():
    cap = json.load(open(OUT / 'capture.json')); panels = {'last': [], 'first_gt36h': []}; checks = collections.Counter(); maxerr = 0.0
    for r in cap['rows']:
        for tag in ('last', 'first'):
            e = r.get(tag)
            if not e or 'rejected' in e: continue
            x = e['inputs']
            if not x.get('native_system_probabilities_json') or not x.get('center_width_json'): checks[f'{tag}_no_components'] += 1; continue
            if tag == 'first' and e['hours_to_midnight'] <= 36: continue
            try: base, keys = chain(x)
            except ValueError as err: checks[f'{tag}_{err}'] += 1; continue
            err = delta(base, e['probability']); maxerr = max(maxerr, err)
            if err > 1e-7: checks[f'{tag}_fidelity_fail'] += 1; continue
            iv = x['bucket_intervals_c_json']; hit = [k for k, (lo, hi) in iv.items() if (lo is None or r['truth_c'] >= lo) and (hi is None or r['truth_c'] < hi)]
            assert len(hit) == 1; hit = hit[0]
            arms = {'base': score(base, hit)}
            for b in BETAS: arms[f'beta{b}'] = score(chain(x, beta=b)[0], hit)
            for g in GAMMAS: arms[f'gamma{g}'] = score(chain(x, gamma=g)[0], hit)
            for b, g in itertools.product(BETAS, GAMMAS): arms[f'joint_b{b}_g{g}_w1'] = score(chain(x, beta=b, gamma=g, width_lambda=1.0)[0], hit)
            for b, g in itertools.product(BETAS, GAMMAS): arms[f'joint_b{b}_g{g}_w075'] = score(chain(x, beta=b, gamma=g)[0], hit)
            n = sum(1 for w in x['system_weights_json'].values() if w > 0)
            panels['last' if tag == 'last' else 'first_gt36h'].append(dict(station=r['station'], date=r['date'], n=n, version=e['posterior_version'], arms=arms,
                                                                            gap=(x.get('consensus_bridge_json') or {}).get('center_gap_c')))
            checks[f'{tag}_ok'] += 1
        guard()
    print('fidelity: max |reconstructed - stored| = %.2e' % maxerr, dict(checks), flush=True)

    def evaluate(rows, family, label):
        """LOO over dates: choose the family's member on LogScore from the other dates; evaluate on held-out."""
        dates = sorted({r['date'] for r in rows}); members = [a for a in rows[0]['arms'] if a.startswith(family)]
        if family == 'base': return None
        chosen = []; dB = []; dL = []; keyd = []; keysd = []
        for d in dates:
            tr = [r for r in rows if r['date'] != d]; te = [r for r in rows if r['date'] == d]
            best = min(members, key=lambda a: np.mean([r['arms'][a][1] for r in tr])); chosen.append(best)
            for r in te: dB.append(r['arms'][best][0] - r['arms']['base'][0]); dL.append(r['arms'][best][1] - r['arms']['base'][1]); keyd.append(d); keysd.append((r['station'], d))
        dB = np.array(dB); dL = np.array(dL)
        def boot(keys):
            u = sorted(set(keys)); idx = collections.defaultdict(list)
            for i, k in enumerate(keys): idx[k].append(i)
            outB = []; outL = []
            for _ in range(2000):
                pick = rng.choice(len(u), len(u), replace=True); sel = np.concatenate([idx[u[j]] for j in pick])
                outB.append(dB[sel].mean()); outL.append(dL[sel].mean())
            return [float(dB.mean()), float(np.percentile(outB, 2.5)), float(np.percentile(outB, 97.5))], [float(dL.mean()), float(np.percentile(outL, 2.5)), float(np.percentile(outL, 97.5))]
        bD, lD = boot(keyd); bS, lS = boot(keysd)
        insample = min(members, key=lambda a: np.mean([r['arms'][a][1] for r in rows]))
        return dict(n=len(rows), n_dates=len(dates), insample_best=insample, loo_chosen=dict(collections.Counter(chosen)),
                    delta_brier_dateblock=bD, delta_logscore_dateblock=lD, delta_brier_stationday=bS, delta_logscore_stationday=lS,
                    verdict_211_dateblock='PASS' if bD[2] < 0 and lD[2] < 0 else 'FAIL', verdict_211_stationday='PASS' if bS[2] < 0 and lS[2] < 0 else 'FAIL',
                    base_mean_brier=float(np.mean([r['arms']['base'][0] for r in rows])), base_mean_logscore=float(np.mean([r['arms']['base'][1] for r in rows])))
    report = {'fidelity_max_error': maxerr, 'checks': dict(checks)}
    fam = lambda r: int(r['version'].split('_v')[-1])
    groups = {'last_all': panels['last'], 'last_six_seated': [r for r in panels['last'] if r['n'] >= 6], 'last_le4_seated': [r for r in panels['last'] if r['n'] <= 4],
              'last_v19plus': [r for r in panels['last'] if fam(r) >= 19], 'first_gt36h_all': panels['first_gt36h']}
    for g, rows in groups.items():
        if len(rows) < 30: report[g] = {'n': len(rows)}; continue
        report[g] = {f: evaluate(rows, f, g) for f in ('beta', 'gamma', 'joint_b')}
        # in-sample grid (diagnostic only): mean dLogScore / dBrier per arm
        report[g]['grid_insample'] = {a: [float(np.mean([r['arms'][a][0] - r['arms']['base'][0] for r in rows])), float(np.mean([r['arms'][a][1] - r['arms']['base'][1] for r in rows]))] for a in rows[0]['arms']}
    (OUT / 'results_test.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for g in groups:
        print('##', g, report[g].get('n', ''))
        if 'beta' not in report[g]: continue
        for f in ('beta', 'gamma', 'joint_b'):
            e = report[g][f]
            print('   %-8s n=%d dates=%d  insample=%s  loo=%s' % (f, e['n'], e['n_dates'], e['insample_best'], e['loo_chosen']))
            print('            dBrier date %+.4f [%+.4f,%+.4f]  dLog date %+.4f [%+.4f,%+.4f]  -> %s ; station-day: Brier[%+.4f,%+.4f] Log[%+.4f,%+.4f] -> %s' % (
                *e['delta_brier_dateblock'], *e['delta_logscore_dateblock'], e['verdict_211_dateblock'], e['delta_brier_stationday'][1], e['delta_brier_stationday'][2], e['delta_logscore_stationday'][1], e['delta_logscore_stationday'][2], e['verdict_211_stationday']))
        gi = report[g]['grid_insample']
        top = sorted(gi.items(), key=lambda kv: kv[1][1])[:6]
        print('   in-sample best 6 by LogScore:', [(a, round(v[0], 4), round(v[1], 4)) for a, v in top])
if __name__ == '__main__': main()
