"""PROTOCOL.md: the P0-M / P1 side-call gap to the market, step by step from each envelope's own lineage.

usage: python3 run.py
"""
from __future__ import annotations

import json, math, sqlite3, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as BR  # noqa: E402
from bayes_basket_target.station_day_board import ladder_axis  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
SEED, DRAWS = 7, 2000


def drop_renorm(vec, excluded):
    ex = {b.strip() for b in str(excluded or '').split(',') if b.strip()}
    v = {b: (0.0 if b in ex else float(p)) for b, p in vec.items()}
    t = sum(v.values())
    return {b: p / t for b, p in v.items()} if t > 0 else None


def root_vector(c, checkpoint, sha):
    for probs, stage, content in c.execute(
            "select probabilities_json, stage, content_sha256 from basket_probability_envelope where checkpoint_id=? and state='COMMITTED'",
            (checkpoint,)):
        if content == sha:
            return json.loads(probs), stage
    return None, None


def mi_bits(points):
    off = [p for p in points if p['truth_side'] != 0]
    n = len(off)
    if not n: return None
    mi = 0.0
    for s in (-1, 0, 1):
        ps = sum(p['sign'] == s for p in off) / n
        for t in (-1, 1):
            pt = sum(p['truth_side'] == t for p in off) / n
            j = sum(p['sign'] == s and p['truth_side'] == t for p in off) / n
            if j > 0: mi += j * math.log2(j / (ps * pt))
    return mi


def summary(points):
    off = [p for p in points if p['truth_side'] != 0]
    called = [p for p in off if p['sign'] != 0]
    return {'off': len(off), 'called_share': len(called) / len(off) if off else None,
            'hit': (sum(p['sign'] == p['truth_side'] for p in called) / len(called)) if called else None,
            'mi': mi_bits(points)}


def paired_diff(rows, a, b):
    """Hit-rate difference a - b on rows where both call a side and the truth left both modes; event-day bootstrap."""
    keep = [r for r in rows if r[a] and r[b] and r[a]['sign'] != 0 and r[b]['sign'] != 0
            and r[a]['truth_side'] != 0 and r[b]['truth_side'] != 0]
    if len(keep) < 20: return None
    by = defaultdict(lambda: [0.0, 0])
    for r in keep:
        d = float(r[a]['sign'] == r[a]['truth_side']) - float(r[b]['sign'] == r[b]['truth_side'])
        by[r['event_date']][0] += d; by[r['event_date']][1] += 1
    blocks = list(by.values()); rng = np.random.default_rng(SEED)
    tot = np.array([x[0] for x in blocks]); cnt = np.array([x[1] for x in blocks])
    picks = rng.integers(0, len(blocks), size=(DRAWS, len(blocks)))
    bs = tot[picks].sum(1) / cnt[picks].sum(1)
    return {'n': len(keep), 'days': len(blocks), 'diff': float(tot.sum() / cnt.sum()),
            'ci': [float(np.quantile(bs, .025)), float(np.quantile(bs, .975))]}


def main():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    rows = BR.skew_truth_rows(c, since='2026-09-01')
    out = {'P0-M': [], 'P1': []}; miss = defaultdict(int)
    for r in rows:
        if r['stage'] not in out or not r['market']: continue
        env = c.execute("select probabilities_json, bucket_ordinals_json, lineage_json from basket_probability_envelope where station_id=? and event_date=?"
                        " and state='COMMITTED' and decision_at_utc=? and stage=? limit 1",
                        (r['station_id'], r['event_date'], r['decided_at_utc'], r['stage'])).fetchone()
        lin = json.loads(env[2]); axis = ladder_axis(json.loads(env[1]), json.loads(lin['bucket_intervals_c_json']))
        point = lambda v: None if v is None else BR._skew_point(v, axis, r['truth_bucket'])
        rec = {'station': r['station_id'], 'event_date': r['event_date'], 'market': r['market'], 'served': r['model']}
        if r['stage'] == 'P0-M':
            root, _ = root_vector(c, lin.get('p0_root_checkpoint_id'), lin.get('p0_root_content_sha256'))
            if root is None: miss['P0-M root'] += 1; continue
            s1 = drop_renorm(root, lin.get('observed_hard_excluded_buckets'))
            cens = json.loads(lin['observed_floor_censoring_json']) if lin.get('observed_floor_censoring_json') else None
            s2 = cens['proportional_probabilities'] if cens else s1
            taf = json.loads(lin.get('p0m_taf_likelihood_json') or '{}').get('state') or lin.get('taf_likelihood_mode') or 'NONE'
            rec.update(S0=point(root), S1=point(s1), S2=point(s2), S3=r['model'], taf=taf)
        else:
            root, rstage = root_vector(c, lin.get('prediction_root_checkpoint_id'), lin.get('prediction_root_content_sha256'))
            if root is None: miss['P1 root'] += 1; continue
            t1 = drop_renorm(root, lin.get('hard_excluded_buckets'))
            lik = json.loads(lin['p1_observation_likelihood_json']) if lin.get('p1_observation_likelihood_json') else {}
            t2 = t1
            if t1 is not None and lik.get('state') == 'APPLIED':
                upd = lik.get('log_update_by_bucket') or {}
                v = {b: p * math.exp(float(upd.get(b, 0.0))) for b, p in t1.items()}
                t = sum(v.values()); t2 = {b: p / t for b, p in v.items()}
            rec.update(T0=point(root), T1=point(t1), T2=point(t2), T3=r['model'], root_stage=rstage,
                       lik_state=lik.get('state') or 'NONE')
        out[r['stage']].append(rec)
    print('missing roots:', dict(miss))
    result = {}
    for stage, steps in (('P0-M', ['S0', 'S1', 'S2', 'S3']), ('P1', ['T0', 'T1', 'T2', 'T3'])):
        R = out[stage]
        print(f'\n== {stage}: {len(R)} 站日（有市场）, {len({r["event_date"] for r in R})} 个事件日')
        res = {'n': len(R), 'steps': {}, 'adjacent': {}, 'vs_market': {}}
        for k in steps + ['market']:
            s = summary([r[k] for r in R if r[k]]); res['steps'][k] = s
            print(f"  {k:6s} 判出 {s['called_share']:.1%}  判对 {s['hit']:.1%}  {s['mi']:.3f} bit  (离开众数 {s['off']})")
        for a, b in zip(steps[1:], steps[:-1]):
            d = paired_diff(R, a, b); res['adjacent'][f'{a}-{b}'] = d
            if d: print(f"  {a}−{b}: {d['diff']:+.3f} [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}]  n={d['n']}")
        for a in steps:
            d = paired_diff(R, a, 'market'); res['vs_market'][a] = d
            if d: print(f"  {a}−市场: {d['diff']:+.3f} [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}]  n={d['n']}")
        key = 'taf' if stage == 'P0-M' else 'root_stage'
        for g in sorted({r[key] for r in R}):
            sub = [r for r in R if r[key] == g]
            if len(sub) < 30: continue
            line = '  '.join(f"{k} {summary([r[k] for r in sub if r[k]])['hit']:.1%}" for k in steps + ['market'])
            dd = paired_diff(sub, steps[2] if stage == 'P0-M' else steps[2], steps[1])
            print(f"   [{key}={g}] n={len(sub)}  {line}" + (f"  | {steps[2]}−{steps[1]} {dd['diff']:+.3f} [{dd['ci'][0]:+.3f},{dd['ci'][1]:+.3f}]" if dd else ''))
            res.setdefault('groups', {})[g] = {'n': len(sub), 'hits': {k: summary([r[k] for r in sub if r[k]])['hit'] for k in steps + ['market']}, 'step': dd}
        result[stage] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == "__main__" and len(sys.argv) == 1:
    main()


def overlap():
    """Diagnostic (not in the protocol's decision rule): where the market's extra right calls come from --
    days both call, only the market calls, only we call (served vector vs market, truth off the respective mode)."""
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    rows = [r for r in BR.skew_truth_rows(c, since='2026-09-01') if r['market'] and r['stage'] in ('P0', 'P0-M', 'P1')]
    for stage in ('P0', 'P0-M', 'P1'):
        R = [r for r in rows if r['stage'] == stage]
        cats = defaultdict(lambda: [0, 0, 0, 0])  # n, model right, market right, both-off
        for r in R:
            m, k = r['model'], r['market']
            mc, kc = m['sign'] != 0 and m['truth_side'] != 0, k['sign'] != 0 and k['truth_side'] != 0
            key = 'both' if mc and kc else 'market only' if kc else 'model only' if mc else None
            if key is None: continue
            if key == 'both': key += ' agree' if m['sign'] == k['sign'] else ' disagree'
            x = cats[key]; x[0] += 1
            x[1] += mc and m['sign'] == m['truth_side']; x[2] += kc and k['sign'] == k['truth_side']
        print(f'== {stage}')
        for key in ('both agree', 'both disagree', 'market only', 'model only'):
            n, a, b, _ = cats[key]
            if n: print(f'   {key:14s} n={n:4d}  模型判对 {a / n if key != "market only" else float("nan"):.1%}  市场判对 {b / n if key != "model only" else float("nan"):.1%}')


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'overlap':
    overlap()
