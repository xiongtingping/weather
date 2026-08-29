"""Walk-forward replay of the per-scope bias estimator, three arms.

Question: how much of the August cold bias does charter 62's H15
half-life recover over the equal-weight window it replaced, and does a
continuous ramp extrapolation buy anything more?

Streams are rebuilt exactly the way the production fit consumes them:
per (system, station, lead_bucket, run_cycle), daily error =
official_tmax_c - ensemble member mean, ACCEPTED rows only.  For each
decision day D the training set is the rolling 60-day window ending
D-2 (two days of truth lag), and each arm predicts the bias it would
have served:

  EQ60  equal-weight mean            (the pre-charter-62 arithmetic)
  H15   half-life-15 weighted mean   (charter 62, shipped 08-25)
  RAMP  H15-weighted linear trend of the errors, extrapolated to D
        (the continuous ramp covariate candidate)

Score: corrected residual r = e_D - bias_hat.  mean(r) is the
systematic bias that survives; MAE(r) is the day-to-day cost.  Sliced
over all of August, over the 08-17..08-26 hot streak, and over the 51
(+1) missed station-days from the basket ledger.
"""

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bayes_basket_target_ssot.db"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)

# --- truth: newest accepted revision per station-day -------------------
truth_rows = conn.execute(
    "SELECT station_id, local_date, official_tmax_c, revision_no"
    " FROM calibration_station_daily_high_truth"
    " ORDER BY revision_no",
).fetchall()
truth = {}
for sid, d, tmax, _rev in truth_rows:
    truth[(str(sid), str(d))] = float(tmax)

# --- forecast means per scope-day --------------------------------------
fc_rows = conn.execute(
    "SELECT ensemble_system_id, station_id, target_local_date,"
    " archive_lead_bucket, run_cycle, AVG(raw_tmax_c)"
    " FROM calibration_native_ensemble_historical_member_run"
    " WHERE qc_status='ACCEPTED'"
    " GROUP BY 1,2,3,4,5",
).fetchall()

streams = defaultdict(dict)  # scope -> {date: error}
for system, sid, d, lead, cycle, fmean in fc_rows:
    t = truth.get((str(sid), str(d)))
    if t is None or fmean is None:
        continue
    scope = (str(system), str(sid), str(lead), str(cycle))
    streams[scope][str(d)] = t - float(fmean)

# --- the 51 (+1) station-days from the ledger --------------------------
import sys
sys.path.insert(0, str(ROOT / "scripts"))
import selection_market_dashboard as dash

led = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket,"
    " p.intended_legs_json, p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL",
).fetchall()
chosen_by_chain = defaultdict(set)
landed_by_chain = {}
for sid, d, landed, ij, pj, wj in led:
    key = (str(sid), str(d))
    landed_by_chain[key] = str(landed)
    got = []
    try:
        got = [str(b) for b in (json.loads(ij) if ij else {}).keys()]
    except (TypeError, ValueError, AttributeError):
        got = []
    if not got:
        for fb in (pj, wj):
            try:
                got = [str(b) for b in json.loads(fb or "[]")]
            except (TypeError, ValueError):
                got = []
            if got:
                break
    chosen_by_chain[key].update(got)
plus1_days = set()
for key, chos in chosen_by_chain.items():
    landed = landed_by_chain[key]
    if landed in chos:
        continue
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in chos)
        if o is not None
    })
    lo = dash._bucket_ordinal(landed)
    if lo is None or not ords:
        continue
    step = dash._bucket_step(landed)
    if lo > ords[-1] and max(1, round((lo - ords[-1]) / step)) == 1:
        plus1_days.add(key)
print(f"streams={len(streams)} truth_days={len(truth)}"
      f" plus1_station_days={len(plus1_days)}")

# --- walk-forward ------------------------------------------------------
WINDOW = 60
HALF_LIFE = 15.0
LAG = 2          # truth visibility lag assumed for the as-of cut
MIN_TRAIN = 20

DECISION_DAYS = [
    (date(2026, 8, 1) + timedelta(days=k)).isoformat() for k in range(26)
]


def arms(history):
    """history: list of (day_index, error), ascending.  Returns biases."""
    anchor = history[-1][0]
    w = [0.5 ** ((anchor - di) / HALF_LIFE) for di, _ in history]
    tot = sum(w)
    h15 = sum(wi * e for wi, (_, e) in zip(w, history)) / tot
    eq = mean(e for _, e in history)
    # H15-weighted linear trend, extrapolated to the anchor day + LAG
    xbar = sum(wi * di for wi, (di, _) in zip(w, history)) / tot
    sxx = sum(wi * (di - xbar) ** 2 for wi, (di, _) in zip(w, history))
    if sxx > 1e-9:
        slope = sum(
            wi * (di - xbar) * (e - h15)
            for wi, (di, e) in zip(w, history)
        ) / sxx
        ramp = h15 + slope * (anchor + LAG - xbar)
    else:
        ramp = h15
    return eq, h15, ramp


buckets = {
    "aug_all": lambda sid, d: True,
    "hot_streak_0817_0826": lambda sid, d: "2026-08-17" <= d <= "2026-08-26",
    "plus1_station_days": lambda sid, d: (sid, d) in plus1_days,
}
acc = {k: {"EQ60": [], "H15": [], "RAMP": [], "RAW": []}
       for k in buckets}

epoch = date(2026, 1, 1)

for scope, series in streams.items():
    _system, sid, _lead, _cycle = scope
    days = sorted(series)
    idx = {d: (date.fromisoformat(d) - epoch).days for d in days}
    for D in DECISION_DAYS:
        if D not in series:
            continue
        Dd = date.fromisoformat(D)
        lo = (Dd - timedelta(days=WINDOW + LAG) - epoch).days
        hi = (Dd - timedelta(days=LAG) - epoch).days
        hist = [(idx[d], series[d]) for d in days
                if lo <= idx[d] <= hi]
        if len(hist) < MIN_TRAIN:
            continue
        eq, h15, ramp = arms(hist)
        e = series[D]
        for name, ok in buckets.items():
            if not ok(sid, D):
                continue
            acc[name]["RAW"].append(e)
            acc[name]["EQ60"].append(e - eq)
            acc[name]["H15"].append(e - h15)
            acc[name]["RAMP"].append(e - ramp)

for name in buckets:
    print(f"\n== {name} ==")
    n = len(acc[name]["RAW"])
    print(f"  scope-days scored: {n}")
    if not n:
        continue
    for arm in ("RAW", "EQ60", "H15", "RAMP"):
        r = acc[name][arm]
        b = mean(r)
        mae = mean(abs(x) for x in r)
        print(f"  {arm:5s} remaining_bias={b:+.3f}C  MAE={mae:.3f}C")
