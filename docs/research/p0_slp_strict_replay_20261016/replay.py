"""PROTOCOL.md: strict as-of replay of the current chain and SLP, D1/D2, event dates 07-20..10-14.

Per station: load the native cases once (oos._load_cases), know each run's arrival, then for each event date and K in {1,2}
take the decision instant t = local 09:00 of D-K, the newest arrived case per system, a walk-forward per-system fit on
the cases that had arrived and settled before t (oos._fit_parameters, memoised on the training fingerprint), and score:
  current (as-of tables)  -> pool -> bridge -> shape stack (radiation) -> width layer resolved at t -> floor
  current (today's tables, sensitivity)
  SLP(c) for c on the grid  -> same pool with spread_factor x c -> bridge -> shape stack -> floor (no width layer)
One gzip JSONL per station, written atomically; a station already written is skipped (resume).
usage: python3 replay.py [--budget-seconds 540] [--stations ...]
"""
from __future__ import annotations

import argparse, dataclasses, gzip, json, math, os, sqlite3, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability, radiation_shift_sigmas_for_lead  # noqa: E402
from bayes_basket_target.served_shape import (  # noqa: E402
    apply_served_table_layers, with_width_slice, with_f_far_tail, with_mode_shoulder, with_peak_ceiling,
)
from bayes_basket_target.center_width_calibration import far_peak_ceiling_serves  # noqa: E402
from bayes_basket_target.stable_pool import STABLE_POOL_KAPPA, stable_pool_weights  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402
from scripts.strict_replay_clock import case_truth_known, training_fingerprint  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
OUT = HERE / 'stations'
FIRST, LAST = '2026-07-20', '2026-10-14'
CUTOFF = '2026-10-16T00:00:00Z'
GRID_C = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
MIN_TRAIN = oos.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS


def utc(s):
    p = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    return p if p.tzinfo else p.replace(tzinfo=timezone.utc)


def zstamp(t):
    return t.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def run_init(case):
    inits = sorted({str(v or '') for v in case.get('member_native_run_init_utcs') or []})
    return inits[-1] if inits else ''


def layer_at(tables, *, seated, hours):
    layer = tables.get('center_width_layer')
    if layer is None:
        return None
    layer = with_width_slice(layer, tables.get('center_width_slices'), seated_system_count=seated, hours_to_settlement=hours)
    layer = with_f_far_tail(layer, hours_to_settlement=hours, f_ladder=False, enabled=False)  # integer grid, not an F ladder
    layer = with_mode_shoulder(layer, None, hours_to_settlement=hours)
    return with_peak_ceiling(layer, hours_to_settlement=hours, enabled=far_peak_ceiling_serves())


def floored(vec):
    from bayes_basket_target.materialization import floor_probabilities
    f, _ = floor_probabilities({k: float(v) for k, v in vec.items()})
    return {k: round(v, 7) for k, v in f.items()}


def replay_station(conn, station):
    natives = set(oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    systems = oos._load_systems(conn)
    family = {s: str(systems[s]['independence_family_id']) for s in natives if s in systems}
    all_cases = oos._load_cases(conn, station_id=station, as_of_utc=CUTOFF)
    by_scope = {scope: sorted(rows, key=lambda c: (c['event_date'], c['aggregator_snapshot_at_utc']))
                for scope, rows in all_cases.items()
                if scope[0] in family and all(c.get('transport_provider_id') != 'OPEN_METEO' for c in rows)}
    fetched = {}
    for system, day, lead, cycle, init, stamp in conn.execute(
            "SELECT ensemble_system_id, target_local_date, archive_lead_bucket, run_cycle, native_run_init_utc, MIN(archive_query_at_utc)"
            " FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND target_local_date>=? AND qc_status='ACCEPTED' GROUP BY 1,2,3,4,5",
            (station, (date.fromisoformat(FIRST) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS + 5)).isoformat())):
        if stamp: fetched[(str(system), str(day), str(lead), str(cycle), str(init or ''))] = utc(stamp)

    def arrival(scope, case):
        key = (scope[0], case['event_date'], str(case.get('archive_lead_bucket') or ''), scope[3], run_init(case))
        return fetched.get(key) or utc(case['aggregator_snapshot_at_utc'])
    tz_row = conn.execute("SELECT settlement_timezone FROM calibration_station_market_timezone_binding WHERE station_id=?", (station,)).fetchone()
    if tz_row is None:
        return []
    zone = ZoneInfo(tz_row[0])
    try:
        latitude = float(station_coordinates(station)['latitude'])
    except Exception:  # noqa: BLE001
        latitude = None
    memo = {}

    def fit(scope, case, knowable):
        start = (date.fromisoformat(case['event_date']) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
        train = [c for c in by_scope[scope] if start <= c['event_date'] < case['event_date'] and arrival(scope, c) < knowable and case_truth_known(c, knowable)]
        key = (scope, training_fingerprint(train))
        if key not in memo:
            memo[key] = oos._fit_parameters(train) if len(train) >= MIN_TRAIN else None
        return memo[key], train
    tables_now = oos.p0_gate_tables(conn, station_id=station)
    tables_memo = {}
    out = []
    days = sorted({c['event_date'] for rows in by_scope.values() for c in rows if FIRST <= c['event_date'] <= LAST})
    for event_date in days:
        for K in (1, 2):
            t = (datetime.fromisoformat(event_date).replace(hour=9, tzinfo=zone) - timedelta(days=K)).astimezone(timezone.utc)
            packet = {}
            for scope, rows in by_scope.items():
                for case in rows:
                    if case['event_date'] != event_date: continue
                    at = arrival(scope, case)
                    if at >= t: continue
                    cur = packet.get(scope[0])
                    if cur is None or (run_init(case), at) > (cur[1], cur[0]):
                        packet[scope[0]] = (at, run_init(case), scope, case)
            rows_by_sys = {}
            for s, (_at, _ri, scope, case) in packet.items():
                params, train = fit(scope, case, t)
                if params is None: continue
                values = [float(v) for v in case['values']]
                rows_by_sys[s] = {'params': params, 'values': values, 'scope': scope,
                                  'center': oos._corrected_center(sum(values) / len(values), params),
                                  'actual': float(case['actual_tmax_c']),
                                  'error_scale': float(params.get('train_residual_sigma_c') or params['train_rmse_c']),
                                  'train_n': len(train),
                                  'residuals': {c['event_date']: float(c['actual_tmax_c']) - sum(float(v) for v in c['values']) / len(c['values']) for c in train}}
            pool = sorted(rows_by_sys)
            if len({family[s] for s in pool}) < 2: continue
            skill, _rule = oos._seat_weights(
                residuals_by_system={s: rows_by_sys[s]['residuals'] for s in pool},
                train_error_scale_by_system={s: rows_by_sys[s]['error_scale'] for s in pool},
                family_by_system={s: family[s] for s in pool}, family_caps=oos.DEFAULT_FAMILY_WEIGHT_CAPS,
                train_n_by_system={s: rows_by_sys[s]['train_n'] for s in pool})
            skill = {s: float(skill.get(s, 0.0)) for s in pool}
            tot = sum(skill.values())
            if tot <= 0: continue
            weights = stable_pool_weights({s: v / tot for s, v in skill.items()}, {s: family[s] for s in pool}, kappa=STABLE_POOL_KAPPA)
            weights = {s: v for s, v in weights.items() if v > 0}
            seated = sorted(weights)
            newest_scope = max(packet.values(), key=lambda v: (v[1], v[0]))[2]
            sigma_per_unit = radiation_shift_sigmas_for_lead(newest_scope[2])
            hours = hours_to_settlement_if_known(event_date, as_of_utc=zstamp(t), settlement_timezone=tz_row[0])
            hk = t.strftime('%Y-%m-%dT%H')
            if hk not in tables_memo:
                tables_memo[hk] = oos.p0_gate_tables(conn, station_id=station, tables_as_of_utc=zstamp(t))

            def chain(spread_mult):
                probs = {}
                for s in seated:
                    p = dict(rows_by_sys[s]['params']); p['spread_factor'] = min(5.0, max(0.25, float(p['spread_factor']) * spread_mult))
                    probs[s] = oos._apply_parameters(rows_by_sys[s]['values'], p)
                grid = sorted(set().union(*(set(p) for p in probs.values())), key=lambda k: int(k))
                probs = {s: {k: float(probs[s].get(k, 0.0)) for k in grid} for s in seated}
                centers = {s: float(rows_by_sys[s]['center']) for s in seated}
                blended, _ = bridged_cross_probability(system_probabilities=probs, system_weights=weights, system_centers_c=centers,
                                                       ordered_keys=grid, key_centers_c=[float(int(k)) for k in grid])
                signals = oos._holdout_conditioning_signals(conn, station_id=station, holdout_days=[event_date],
                                                            predictions_by_system={s: {event_date: {'probability': probs[s], 'calibrated_center_c': centers[s], 'decision_at_utc': zstamp(t)}} for s in seated},
                                                            radiation_shift_sigmas=sigma_per_unit)
                cooling = (signals.get(event_date) or {}).get('synoptic_cooling_shift_sigma')
                shaped = oos._holdout_shape_stack(blended, grid=grid, cooling_shift_sigma=cooling, station_latitude=latitude,
                                                  neighbour_center_c=sum(weights[s] * centers[s] for s in seated), center_width_layer=None)
                return grid, shaped
            grid, base = chain(1.0)
            rec = {'station': station, 'event_date': event_date, 'K': K, 't': zstamp(t), 'hours': hours, 'actual': rows_by_sys[seated[0]]['actual'],
                   'seated': seated, 'arms': {}}
            for name, tables in (('current_asof', tables_memo[hk]), ('current_today', tables_now)):
                layer = layer_at(tables, seated=len(seated), hours=hours)
                v = base
                if layer is not None:
                    v, _ = apply_served_table_layers(base, ordered_keys=grid, key_centers_c=[float(int(k)) for k in grid], center_width=layer)
                rec['arms'][name] = floored(v)
            rec['cw_sha_asof'] = tables_memo[hk].get('center_width_sha256')
            for cf in GRID_C:
                g2, v = chain(cf)
                rec['arms'][f'slp_{cf}'] = floored(v)
            out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget-seconds', type=float, default=540.0)
    ap.add_argument('--stations', nargs='*')
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    stations = a.stations or [r[0] for r in conn.execute('SELECT station_id FROM calibration_station_market_timezone_binding ORDER BY station_id')]
    start = time.time()
    for st in stations:
        path = OUT / f'{st}.jsonl.gz'
        if path.exists(): continue
        if time.time() - start > a.budget_seconds:
            print('budget reached'); break
        t0 = time.time()
        try:
            rows = replay_station(conn, st)
        except Exception as exc:  # noqa: BLE001
            print(st, 'ERROR', type(exc).__name__, str(exc)[:200], flush=True); continue
        tmp = path.with_suffix('.tmp')
        with gzip.open(tmp, 'wt') as f:
            for r in rows: f.write(json.dumps(r) + '\n')
        os.replace(tmp, path)
        print(st, len(rows), f'{time.time() - t0:.0f}s', flush=True)
    done = len(list(OUT.glob('*.jsonl.gz')))
    print('stations done', done, 'of', len(stations))


if __name__ == '__main__':
    main()
