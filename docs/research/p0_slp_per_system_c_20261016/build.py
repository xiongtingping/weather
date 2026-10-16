"""PROTOCOL.md rows: each P0's systems rebuilt from members, every system's vector on the c grid.

Row selection and the per-system rebuild are p0_spread_adjusted_pool_20261016/run.py's (same decisions, same c=1 check).
usage: python3 build.py K   (K = 0, 1, 2)
"""
from __future__ import annotations

import gzip, json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_spread_adjusted_pool_20261016'))
import run as RN  # noqa: E402  (sets the import path for bayes_basket_target)
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402
import numpy as np  # noqa: E402

GRID = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]


def components(c, st, d, dec, lin, T, tz_name, art_cache, stats):
    iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
    sysp = json.loads(lin.get('native_system_probabilities_json') or '{}'); wts = json.loads(lin.get('system_weights_json') or '{}')
    cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
    runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}'); arts = json.loads(lin.get('system_artifact_shas_json') or '{}')
    rad = json.loads(lin.get('synoptic_cooling_json') or '{}') if lin.get('synoptic_cooling_json') else {}
    if not iv or not sysp:
        stats['incomplete'] += 1; return None
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
        stats['members_missing'] += 1; return None
    for s, (vals, bp, sp, rp) in comps.items():
        v = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bp, spread_parameters=sp, reliability_parameters=rp)
        if 0.5 * sum(abs(v[b] - float(sysp[s][b])) for b in ordered) > 1e-6:
            stats['rebuild_mismatch'] += 1; return None
    tw = sum(float(wts[s]) for s in systems)
    vecs = {}
    for s, (vals, bp, sp, rp) in comps.items():
        vecs[s] = {}
        for cf in GRID:
            sp2 = dict(sp); sp2['spread_factor'] = min(5.0, max(0.25, float(sp['spread_factor']) * cf))
            v = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bp, spread_parameters=sp2, reliability_parameters=rp)
            vecs[s][str(cf)] = [round(float(v[b]), 9) for b in ordered]
    ti = next(i for i, b in enumerate(ordered) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
    return {'st': st, 'day': d, 'dec': dec, 'ti': ti, 'ordered': ordered, 'iv': iv,
            'centers_key': NP._bucket_centers(iv, ordered),
            'widths': list(np.diff(np.array(served_bucket_axis(iv, ordered)[0]))),
            'hours': hours_to_settlement_if_known(d, as_of_utc=dec, settlement_timezone=tz_name),
            'w': {s: float(wts[s]) / tw for s in systems}, 'cen': {s: float(cen[s]) for s in systems},
            'shift_sigma': rad.get('shift_sigma') if rad.get('state') == 'RESOLVED' else None,
            'sf': {s: float(comps[s][2]['spread_factor']) for s in systems}, 'vecs': vecs}


def build(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
    c = sqlite3.connect(f'file:{RN.SSOT}?mode=ro', uri=True, timeout=5)
    art_cache = {}; stats = defaultdict(int)
    tmp = HERE / f'rows_d{K}.json.gz.tmp'
    out = gzip.open(tmp, 'wt')
    for (st, d), decs in sorted(by.items()):
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if not ok: continue
        dec = ok[-1]
        row = c.execute("select lineage_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        rec = components(c, st, d, dec, json.loads(row[0]), truth[(st, d)], tzs[st], art_cache, stats)
        if rec is None: continue
        out.write(json.dumps(rec) + '\n'); stats['rows'] += 1
    out.close(); tmp.rename(HERE / f'rows_d{K}.json.gz')
    print(f'D-{K}', dict(stats), flush=True)


if __name__ == '__main__':
    build(int(sys.argv[1]))
