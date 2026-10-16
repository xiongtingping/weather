"""Forward read of the frozen SLP form (FROZEN.json): served weights, c walked forward, D1/D2 only.

熊先生 10-16「冻结「只拟 c、服务权重」的 SLP 形式，D1/D2 前推到 ≥40 个评估日（约 11 月上旬）再按本协议判一次」.

  python3 forward.py build K --to YYYY-MM-DD   rebuild rows for event dates 09-01..--to straight from the SSOT (the training
                                               rows of 10-16 came from a 10-15 extract that cannot see later days)
  python3 forward.py score                     run.py's own scoring on the rebuilt rows; refuses before the registered reread
                                               date and before 40 evaluated days, unless --allow-early

Every number in the procedure comes from run.py at the frozen commit (FROZEN.json pins its sha256); this file only selects rows.
"""
from __future__ import annotations

import gzip, hashlib, json, sqlite3, sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FROZEN = json.loads((HERE / 'FROZEN.json').read_text())
if hashlib.sha256((HERE / 'run.py').read_bytes()).hexdigest() != FROZEN['run_py_sha256']:
    raise SystemExit('run.py changed since the freeze; the forward read must use the frozen procedure')
sys.path.insert(0, str(HERE))
import run as R  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
OUT = HERE / 'forward'


def build(K, to_date):
    """Row selection for the forward read: run.build's own body, with the event-date window as a parameter and the
    station/time-zone, P0 and truth reads taken straight from the SSOT.  The arithmetic (system rebuild, bridge,
    radiation, table layer, floor) is run.py's functions."""
    import numpy as np
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    tzs = dict(c.execute('select station_id, settlement_timezone from calibration_station_market_timezone_binding'))
    truth = {}
    for st, d, t, rev in c.execute("select station_id, local_date, official_tmax_c, revision_no from calibration_station_daily_high_truth"
                                   " where source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' and local_date between ? and ? order by revision_no",
                                   (FROZEN['event_dates_from'], to_date)):
        truth[(st, d)] = float(t)
    OUT.mkdir(exist_ok=True)
    out = gzip.open(OUT / f'rows_d{K}.json.gz', 'wt')
    art_cache = {}; stats = defaultdict(int)
    for (st, d) in sorted(truth):
        if st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        row = c.execute("select decision_at_utc, lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=?"
                        " and state='COMMITTED' and decision_at_utc<=? and stage='P0' order by decision_at_utc desc limit 1", (st, d, cs)).fetchone()
        if row is None: continue
        dec, lin, served = row[0], json.loads(row[1]), json.loads(row[2])
        rec = R.build_row(c, st, d, dec, lin, served, truth[(st, d)], tzs[st], art_cache, stats)
        if rec is not None:
            out.write(json.dumps(rec) + '\n'); stats['rows'] += 1
    out.close(); c.close()
    print(f'D-{K}', dict(stats))


def score(allow_early):
    if date.today() < date.fromisoformat(FROZEN['reread_on_or_after']) and not allow_early:
        raise SystemExit(f"registered reread is on or after {FROZEN['reread_on_or_after']}")
    import numpy as np
    for K in (1, 2):
        rows = [json.loads(l) for l in gzip.open(OUT / f'rows_d{K}.json.gz', 'rt')]
        days = sorted({r['day'] for r in rows})
        evaluated = max(0, len(days) - R.MIN_TRAIN_DAYS)
        print(f'D{K}: event days {len(days)}, evaluated days {evaluated}')
        if evaluated < FROZEN['min_evaluated_days'] and not allow_early:
            raise SystemExit(f'D{K} has {evaluated} evaluated days, fewer than {FROZEN["min_evaluated_days"]}')
    R.HERE = OUT
    R.score()


if __name__ == '__main__':
    if sys.argv[1] == 'build':
        build(int(sys.argv[2]), sys.argv[sys.argv.index('--to') + 1])
    else:
        score('--allow-early' in sys.argv)
