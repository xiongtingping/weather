"""Reading (user 2026-10-16 「先统计这类日子占多少、剔除后判对率变多少」): how many skew calls sit on a
「下界日」 -- the vector's mode is the rung holding the running observed high, so nothing below the mode
can still happen and any lean reads right -- and the side-call hit rates with those rows left out.

Two instant sets: the board's (skew_truth_rows: P0-M last before P1, P1 first; running high from the
envelope's own lineage) and local 09/12/15 (this directory's rows.json.gz; running high from the AWC
reports in hand, event days <= 10-14).
usage: python3 floor_days.py
"""
from __future__ import annotations

import gzip, json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as BR  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
INPUTS = ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db'


def ts(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00'))


def holding(bounds, high):
    for b, (lo, hi) in bounds.items():
        if (lo is None or lo <= high) and (hi is None or high < hi):
            return b
    return None


def report(label, rows):
    print(f'== {label}: {len(rows)} 行')
    for src, word in (('model', '我们'), ('market', '市场')):
        off = [r for r in rows if r[src]['truth_side'] != 0]
        called = [r for r in off if r[src]['sign'] != 0]
        floor = [r for r in called if r[f'{src}_floor']]
        keep = [r for r in called if not r[f'{src}_floor']]
        hit = lambda xs: sum(r[src]['sign'] == r[src]['truth_side'] for r in xs) / len(xs) if xs else float('nan')
        all_floor = sum(r[f'{src}_floor'] for r in rows)
        print(f'  {word}: 众数在下界 {all_floor}/{len(rows)}（{all_floor / len(rows):.1%}）；判出且真值离开众数 {len(called)}，其中下界日 {len(floor)}（{len(floor) / max(1, len(called)):.1%}，判对 {hit(floor):.1%}）'
              f'；判对率 全部 {hit(called):.1%} → 剔除下界日 {hit(keep):.1%}（{len(keep)} 行），判出比例 {len(called) / max(1, len(off)):.1%} → {len(keep) / max(1, len(off) - len(floor)):.1%}')


def main():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=10)
    board = []
    for r in BR.skew_truth_rows(c, since='2026-09-01'):
        if r['stage'] == 'P0':
            r['model_floor'] = r['market_floor'] = False
        else:
            lin = json.loads(c.execute("select lineage_json from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED'"
                                       " and decision_at_utc=? and stage=? limit 1", (r['station_id'], r['event_date'], r['decided_at_utc'], r['stage'])).fetchone()[0])
            high = lin.get('observed_running_high_c') if r['stage'] == 'P0-M' else lin.get('reported_high_c')
            try:
                high = float(high)
            except (TypeError, ValueError):
                high = None
            bounds = {b: tuple(v) for b, v in json.loads(lin['bucket_intervals_c_json']).items()}
            floor = holding(bounds, high) if high is not None else None
            r['model_floor'] = floor is not None and r['model']['mode'] == floor
            r['market_floor'] = floor is not None and bool(r['market']) and r['market']['mode'] == floor
        if r['market']:
            board.append(r)
    for stage in ('P0', 'P0-M', 'P1'):
        report(f'看板时刻 {stage}', [r for r in board if r['stage'] == stage])
    inp = sqlite3.connect(INPUTS)
    tzs = dict(c.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    obs = defaultdict(list)
    for st, valid, temp, comm in inp.execute("select station_id, valid_at, temp_c, committed_at from obs where temp_c is not null and coalesce(correction_suspected,0)=0"):
        obs[st].append((ts(comm), ts(valid), float(temp)))
    bounds_cache = {}
    clock = []
    for line in gzip.open(HERE / 'rows.json.gz', 'rt'):
        r = json.loads(line)
        if r['day'] > '2026-10-14':
            continue
        key = (r['st'], r['day'])
        if key not in bounds_cache:
            row = c.execute("select json_extract(lineage_json,'$.bucket_intervals_c_json') from basket_probability_envelope"
                            " where station_id=? and event_date=? and state='COMMITTED' order by decision_at_utc desc limit 1", key).fetchone()
            bounds_cache[key] = {b: tuple(v) for b, v in json.loads(row[0]).items()} if row and row[0] else {}
        tz = ZoneInfo(tzs[r['st']]); t = datetime.fromisoformat(r['day']).replace(hour=r['hour'], tzinfo=tz)
        seen = [temp for comm, v, temp in obs.get(r['st'], []) if comm <= t and v.astimezone(tz).date().isoformat() == r['day']]
        floor = holding(bounds_cache[key], max(seen)) if seen and bounds_cache[key] else None
        r['model_floor'] = floor is not None and r['model']['mode'] == floor
        r['market_floor'] = floor is not None and r['market']['mode'] == floor
        clock.append(r)
    for h in (9, 12, 15):
        report(f'当地 {h:02d}:00（≤10-14）', [r for r in clock if r['hour'] == h])


if __name__ == '__main__':
    main()
