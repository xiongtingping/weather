"""Execution-conditioned economic replay of the frozen candidates; frozen files only, no SQL."""
import sys, json, math, hashlib, collections
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'pareto_metrics'))
import evaluate as E
import numpy as np
C = E.C
C.study.OUT = HERE
ARMS = ['current', 'blend25', 'blend50', 'blend75', 'center_stack21', 'crps_hier21']
HURDLE = 0.05          # delta_v2.ECONOMIC_HURDLE_V1 (charter 130)
BUDGET = 20.0          # USDC per station-day, fixed before computing
FEES = [0.0, 0.02]     # runtime.py fee_rate=0.0 is the production contract; 0.02 sensitivity
KELLY = [1.0, 0.25]


def hurdle_taker(q, books, hit, fee):
    """Per-bucket charter-130 inequality, walking real ask levels, budget by edge ratio."""
    offers = []
    for b, levels in enumerate(books):
        for price, shares in levels:
            unit = price * (1 + fee)
            if 0 < price < 1 and q[b] >= (1 + HURDLE) * unit:
                offers.append((q[b] / unit, b, unit, shares))
    offers.sort(key=lambda o: -o[0])
    left = BUDGET; bought = collections.defaultdict(float); spent = collections.defaultdict(float)
    for _, b, unit, shares in offers:
        if left <= 1e-9:
            break
        n = min(shares, left / unit)
        bought[b] += n; spent[b] += n * unit; left -= n * unit
    return bought, spent


def kelly_multiplier(q, a, frac):
    """Horse-race Kelly with cash; returns wealth multiplier function of the settled bucket."""
    idx = [b for b in range(len(q)) if 0 < a[b] < 1]
    idx.sort(key=lambda b: -q[b] / a[b])
    S = []; R = 1.0; sq = sa = 0.0
    for b in idx:
        if q[b] / a[b] <= R or sa + a[b] >= 1:
            break
        S.append(b); sq += q[b]; sa += a[b]; R = (1 - sq) / (1 - sa)
    def m(y):
        full = q[y] / a[y] if y in S else R
        return 1 - frac + frac * full
    return m, S, R


def block_draws(n, rng, k=4000):
    return [[min(s + j, n - 1) for s in rng.integers(0, max(1, n - 2), math.ceil(n / 3)) for j in range(3)][:n] for _ in range(k)]


def main():
    pairs = {x['sample_sha']: x for x, _ in E.load()}
    weights = {r['sample_sha']: r for r in json.loads((HERE.parent / 'joint_objective/scores.json').read_text())}
    quotes = json.loads((HERE.parent / 'pareto_metrics/execution_window_quotes.json').read_text())
    rows = []; skipped = collections.Counter()
    for qt in quotes:
        x = pairs[qt['sample_sha']]; keys = sorted(x['weights'])
        _, _, p, hit, z, buckets = C.prob_components(x, keys)
        by_name = {b['bucket']: b for b in qt['buckets']}
        if set(by_name) != set(buckets):
            skipped['bucket_mismatch'] += 1; continue
        books = [[(float(l['price']), float(l['shares'])) for l in sorted(by_name[b]['ask_levels'], key=lambda l: float(l['price']))]
                 if by_name[b].get('ask_available') else [] for b in buckets]
        ask = np.array([lv[0][0] if lv else 0.0 for lv in books])
        bid = np.array([float(by_name[b].get('bid_price') or 0.0) if by_name[b].get('bid_available') else 0.0 for b in buckets])
        minimum = [float(by_name[b].get('min_order_size') or 0.0) for b in buckets]
        row = {'station': x['station'], 'day': x['event_date'], 'sample_sha': x['sample_sha'], 'hit': int(hit),
               'ask_sum': float(ask.sum()), 'arms': {}}
        # Market reference forecasts on the same quote.
        if (ask > 0).all():
            row['market_ask_logscore'] = math.log(ask[hit] / ask.sum())
        mid = np.where((ask > 0) & (bid > 0), (ask + bid) / 2, ask)
        if (mid > 0).all():
            row['market_mid_logscore'] = math.log(mid[hit] / mid.sum())
        for arm in ARMS:
            w = weights[x['sample_sha']]['arms'][arm]['weights']
            q = np.array([w[s] for s in keys]) @ p
            out = {'logscore': math.log(q[hit])}
            for fee in FEES:
                bought, spent = hurdle_taker(q, books, hit, fee)
                for b in list(bought):
                    if bought[b] < minimum[b]:
                        bought.pop(b); spent.pop(b)
                cost = sum(spent.values())
                out[f'fee{fee}'] = {'cost': cost, 'payout': bought.get(hit, 0.0),
                                    'pnl': bought.get(hit, 0.0) - cost,
                                    'claimed_ev': sum(q[b] * n for b, n in bought.items()) - cost,
                                    'legs': len(bought)}
            if (ask > 0).any():
                for frac in KELLY:
                    m, S, R = kelly_multiplier(q, ask, frac)
                    out[f'kelly{frac}'] = {'multiplier': m(hit), 'legs': len(S), 'reserve': R}
                out['proportional_log'] = math.log(q[hit] / ask[hit]) if ask[hit] > 0 else None
            row['arms'][arm] = out
        rows.append(row)

    days = sorted({r['day'] for r in rows}); n = len(days)
    rng = np.random.default_rng(230923); draws = block_draws(n, rng)
    by_day = {d: [r for r in rows if r['day'] == d] for d in days}

    def day_stats(arm, fee, frac):
        # columns: cost, pnl, claimed, logscore sum, count, log mean kelly multiplier
        out = []
        for d in days:
            rs = by_day[d]; a = [r['arms'][arm] for r in rs]
            out.append([sum(v[f'fee{fee}']['cost'] for v in a), sum(v[f'fee{fee}']['pnl'] for v in a),
                        sum(v[f'fee{fee}']['claimed_ev'] for v in a), sum(v['logscore'] for v in a), len(a),
                        math.log(np.mean([v[f'kelly{frac}']['multiplier'] for v in a]))])
        return np.array(out)

    result = {'schema': 'economic_replay_v1', 'status': 'execution_conditioned_replay_pool_stage',
              'sample_n': len(rows), 'of_probability_cohort': 515, 'dates': days, 'skipped': dict(skipped),
              'hurdle': HURDLE, 'budget_usdc': BUDGET, 'fees': FEES, 'kelly_fractions': KELLY, 'arms': {}}
    mk = [r['market_ask_logscore'] for r in rows if 'market_ask_logscore' in r]
    md = [r['market_mid_logscore'] for r in rows if 'market_mid_logscore' in r]
    result['market'] = {'ask_normalised_logscore': float(np.mean(mk)), 'ask_n': len(mk),
                        'mid_normalised_logscore': float(np.mean(md)), 'mid_n': len(md),
                        'mean_ask_sum': float(np.mean([r['ask_sum'] for r in rows]))}
    base = {(fee, frac): day_stats('current', fee, frac) for fee in FEES for frac in KELLY}
    for arm in ARMS:
        a = [r['arms'][arm] for r in rows]; entry = {'logscore': float(np.mean([v['logscore'] for v in a]))}
        entry['logscore_minus_market_ask'] = float(np.mean([r['arms'][arm]['logscore'] - r['market_ask_logscore'] for r in rows if 'market_ask_logscore' in r]))
        pl = [v['proportional_log'] for v in a if v.get('proportional_log') is not None]
        entry['proportional_log_growth_mean'] = float(np.mean(pl))
        for fee in FEES:
            for frac in KELLY:
                s = day_stats(arm, fee, frac); b = base[(fee, frac)]
                key = f'fee{fee}_kelly{frac}'
                cost, pnl, claimed = s[:, 0].sum(), s[:, 1].sum(), s[:, 2].sum()
                d_pnl = []; d_log = []; d_ls = []
                for ix in draws:
                    d_pnl.append((s[ix, 1].sum() - b[ix, 1].sum()) / len(rows))
                    d_log.append(s[ix, 5].sum() - b[ix, 5].sum())
                    d_ls.append((s[ix, 3].sum() - b[ix, 3].sum()) / s[ix, 4].sum())
                traded = sum(v[f'fee{fee}']['legs'] > 0 for v in a)
                entry[key] = {
                    'hurdle_taker': {'traded_station_days': traded, 'cost_usdc': cost, 'pnl_usdc': pnl,
                                     'roi': pnl / cost if cost else None, 'claimed_ev_usdc': claimed,
                                     'claimed_roi': claimed / cost if cost else None,
                                     'pnl_per_station_day_delta_ci95': list(np.quantile(d_pnl, [.025, .975])) if arm != 'current' else None,
                                     'pnl_negative_share': float(np.mean(np.array([s[ix, 1].sum() for ix in draws]) < 0))},
                    'kelly_portfolio': {'log_growth': float(s[:, 5].sum()), 'per_day': float(s[:, 5].mean()),
                                        'delta_ci95': list(np.quantile(d_log, [.025, .975])) if arm != 'current' else None},
                    'logscore_delta_ci95': list(np.quantile(d_ls, [.025, .975])) if arm != 'current' else None}
        result['arms'][arm] = entry
    result['inputs_sha256'] = {str(p.relative_to(C.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        HERE / 'PROTOCOL.md', Path(__file__), HERE.parent / 'joint_objective/scores.json',
        HERE.parent / 'pareto_metrics/execution_window_quotes.json', HERE.parent / 'reconstructed.json']}
    C.study.save('rows.json', rows); C.study.save('results.json', result)
    print(json.dumps({'market': result['market'], 'n': len(rows), 'skipped': dict(skipped)}, indent=1))
    for arm, e in result['arms'].items():
        k = e['fee0.0_kelly0.25']; k1 = e['fee0.0_kelly1.0']
        print(f"{arm:15s} LS {e['logscore']:+.4f} vsMkt {e['logscore_minus_market_ask']:+.4f} prop {e['proportional_log_growth_mean']:+.4f} | "
              f"S1 traded {k['hurdle_taker']['traded_station_days']} cost {k['hurdle_taker']['cost_usdc']:.1f} pnl {k['hurdle_taker']['pnl_usdc']:+.1f} "
              f"roi {k['hurdle_taker']['roi']:+.3f} claimed {k['hurdle_taker']['claimed_roi']:+.3f} dCI {k['hurdle_taker']['pnl_per_station_day_delta_ci95']} | "
              f"Kelly1 {k1['kelly_portfolio']['log_growth']:+.3f} Kelly.25 {k['kelly_portfolio']['log_growth']:+.3f} dCI {k['kelly_portfolio']['delta_ci95']}")


if __name__ == '__main__':
    main()
