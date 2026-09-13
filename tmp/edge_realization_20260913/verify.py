"""Bounded, read-only evidence for the 2026-09-13 edge design review.

Writes only this directory's evidence.json. Synthetic probes deliberately
document current behavior; they are not a strategy backtest or a fix.
"""
from __future__ import annotations

import json
import math
import resource
import shutil
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MAX_RSS = 384 * 1024**2
MAX_BYTES = 8 * 1024**2
START = time.monotonic()


def guard(*_):
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss *= 1 if sys.platform == "darwin" else 1024
    if rss <= 0 or rss > MAX_RSS:
        raise RuntimeError("RSS probe invalid or budget exceeded")
    if time.monotonic() - START > 45:
        raise RuntimeError("wall time budget exceeded")
    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        raise RuntimeError("minimum free disk violated")
    return 0


signal.signal(signal.SIGALRM, guard)
signal.setitimer(signal.ITIMER_REAL, 0.2, 0.2)
resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))
guard()

from bayes_basket_target import beta_edge_store as B
from bayes_basket_target import effective_edge_store as L
from bayes_basket_target.basket_kernel.delta_v2 import (
    BETA_EDGE_MODE, SEAT_SHAPE_RULE_VERSION, beta_belief_v1,
    effective_edge_lambda, deflate_window_partition_v1,
)

probes = {}

# Fit with an intercept, then serve without it: even the 5% gate can flip.
p, c, beta, alpha = 0.8, 0.6, 0.3, -0.08
served = beta_belief_v1(p, c, beta) - c
fitted = alpha + beta * (p - c)
assert served >= 0.05 * c and fitted < 0
probes["intercept_omission"] = {"served_edge": served, "fitted_conditional_edge": fitted}

# The fee/depth cost changes while all information stays fixed.
before, after = beta_belief_v1(.8, .6, .3), beta_belief_v1(.8, .7, .3)
assert after > before
probes["cost_changes_belief"] = {"belief_before": before, "belief_after": after}

# A lower coefficient bound is the UPPER edge when disagreement is negative.
d, lo, hi = -.2, -.8, .4
assert lo * d > 0 and min(lo*d, hi*d) < 0
probes["negative_disagreement_bound"] = {"current_edge": lo*d, "interval_worst_edge": min(lo*d, hi*d)}

# Sharing '-price' can produce beta=1 without a correct model probability.
xs, ys = [], []
for cost in (.35, .45, .55, .65):
    for hit in (0., 1.):
        xs.append(.7-cost)
        ys.append(hit-cost)
intercept, slope = B._ols(xs, ys)
assert math.isclose(slope, 1) and math.isclose(intercept, -.2)
probes["shared_price_regressor"] = {"beta": slope, "intercept": intercept, "true_probability": .5, "model_probability": .7}

# Two nested windows, same p and prices, different lambda bands.
small_mass = .4 * effective_edge_lambda(.31/.4)
large_mass = .7 * effective_edge_lambda(.34/.7)
assert small_mass > large_mass
distribution = {"a": .2, "b": .2, "c": .3, "d": .3}
small = deflate_window_partition_v1(distribution, ["a", "b"], claim_factor=effective_edge_lambda(.31/.4))
large = deflate_window_partition_v1(distribution, ["a", "b", "c"], claim_factor=effective_edge_lambda(.34/.7))
assert math.isclose(sum(small.values()), 1) and math.isclose(sum(large.values()), 1)
probes["window_dependent_probability"] = {"subset_probability": small_mass, "superset_probability": large_mass, "same_bucket_a_in_two_partitions": [small["a"], large["a"]]}

# Boundary sweeps: corrected affine edge must take the worse interval end.
checked = 0
for d in (-.5, -.2, -1e-6, 0., 1e-6, .2, .5):
    for lo, hi in ((-.8, .4), (0., 1.), (.2, .6), (1., 1.)):
        for alpha in (-.08, 0., .04):
            worst = min(alpha+lo*d, alpha+hi*d)
            assert all(worst <= alpha+(lo+(hi-lo)*i/20)*d+1e-12 for i in range(21))
            checked += 1
probes["interval_sign_boundary_sweep"] = {"cases": checked, "passed": True}

# Date-only beta population ignores when a settlement became knowable and
# converts a missing hit to a loss instead of rejecting it.
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE paper_trade_candidate_window(station_id TEXT,event_date TEXT,stage TEXT,decision_at_utc TEXT,raw_win_probability REAL,all_in_price REAL,hit INTEGER,settled_bucket TEXT,settled_at_utc TEXT)")
mem.execute("INSERT INTO paper_trade_candidate_window VALUES('TEST','2026-09-10','P0','2026-09-09T00:00:00Z',.7,.5,1,'b','2026-09-12T12:00:00Z')")
mem.execute("INSERT INTO paper_trade_candidate_window VALUES('MISS','2026-09-10','P0','2026-09-09T00:00:00Z',.7,.5,NULL,'b','2026-09-12T12:00:00Z')")
rows, _ = B.population(mem, stage_scope="P0", as_of="2026-09-10", anchored=False)
assert len(rows) == 2 and rows[1]["hit"] == 0
probes["beta_asof_and_null_label"] = {"included_future_known_labels": len(rows), "null_hit_coerced_to": rows[1]["hit"]}
mem.close()

# Lambda's as_of supplies a floor but no ceiling to its population.
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE paper_trade_candidate_window(candidate_id TEXT,station_id TEXT,event_date TEXT,stage TEXT,decision_at_utc TEXT,buckets_json TEXT,win_probability REAL,all_in_price REAL,hit INTEGER)")
mem.execute("CREATE TABLE paper_trade_settlement(position_id TEXT,stage TEXT,event_date TEXT,hit INTEGER)")
for i, day in enumerate(("2026-09-01", "2026-09-05", "2026-09-12")):
    mem.execute("INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?)", (str(i),str(i),day,"P0",day+"T00:00:00Z",'["b"]',.6,.4,1))
    mem.execute("INSERT INTO paper_trade_settlement VALUES(?,?,?,?)", (str(i),"P0",day,1))
fit = L.fit_lambda_table(mem, as_of="2026-09-05", stage_scope="P0")
count = sum(row["seats"] for row in fit["evidence"])
assert count == 3
probes["lambda_asof_ceiling"] = {"as_of": "2026-09-05", "included_rows": count, "expected_rows_with_date_ceiling": 2}
mem.close()

# Toy execution: alpha survives small depth, disappears at larger depth;
# a maker fill can select exactly the less favorable outcomes.
p_model, market, slope = .8, .5, .3
p_star = market + slope*(p_model-market)
probes["execution_example"] = {"p_model": p_model, "market": market, "beta": slope, "belief": p_star, "small_taker_cost": .54, "small_edge": p_star-.54, "large_taker_cost": .58, "large_edge": p_star-.58, "maker_fill_probability": .4, "maker_price": .51, "p_given_fill": .48, "maker_attempt_ev": .4*(.48-.51)}
assert p_star-.54 >= .05*.54 and p_star-.58 < .05*.58
assert probes["execution_example"]["maker_attempt_ev"] < 0

db = ROOT / "data/bayes_basket_target_ssot.db"
conn = sqlite3.connect(db.as_uri()+"?mode=ro", uri=True, timeout=2)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only=ON")
conn.execute("PRAGMA cache_size=-2048")
conn.execute("PRAGMA temp_store=MEMORY")
conn.set_progress_handler(guard, 20000)
conn.execute("BEGIN")
snapshot = {"read_at_utc": datetime.now(timezone.utc).isoformat(), "db": str(db)}
snapshot["domains"] = {}
for table, cols in {
    "calibration_beta_edge_table": ("stage_scope","price_basis","activated","accumulating"),
    "calibration_effective_edge_lambda": ("stage_scope","claim_scope","activated","band_axis"),
    "paper_trade_candidate_window": ("stage","seat_shape_rule","band_axis"),
}.items():
    snapshot["domains"][table] = {col:[row[0] for row in conn.execute(f"SELECT DISTINCT {col} FROM {table} LIMIT 30")] for col in cols}
snapshot["beta_activated"] = []
for row in conn.execute("SELECT stage_scope,beta,beta_lcb,beta_ucb,intercept,row_count,day_count,accumulating,fitted_at_utc,as_of_event_date,evidence_json FROM calibration_beta_edge_table WHERE activated=1 ORDER BY stage_scope"):
    item = dict(row)
    evidence = json.loads(item.pop("evidence_json"))
    item.update({key:evidence.get(key) for key in ("anchor_utc","excluded_before_anchor")})
    snapshot["beta_activated"].append(item)
snapshot["lambda_columns"] = [row[1] for row in conn.execute("PRAGMA table_info(calibration_effective_edge_lambda)")]
snapshot["lambda_activated"] = []
for row in conn.execute("SELECT stage_scope,claim_scope,fitted_at_utc,seat_count,band_axis,bands_json FROM calibration_effective_edge_lambda WHERE activated=1"):
    item = dict(row)
    payload = json.loads(item.pop("bands_json"))
    item["payload_seat_shape_rule"] = payload.get("seat_shape_rule")
    snapshot["lambda_activated"].append(item)
snapshot["candidate_groups"] = [dict(row) for row in conn.execute("SELECT stage,seat_shape_rule,COUNT(*) AS rows,COUNT(DISTINCT event_date) AS event_days,COUNT(hit) AS labeled,MIN(event_date) AS first_day,MAX(event_date) AS last_day FROM paper_trade_candidate_window GROUP BY stage,seat_shape_rule")]
count = mismatch = missing = input_bytes = 0
for row in conn.execute("SELECT buckets_json,settled_bucket,hit FROM paper_trade_candidate_window WHERE settled_bucket IS NOT NULL LIMIT 20001"):
    count += 1
    payload = str(row["buckets_json"])
    input_bytes += len(payload.encode())
    assert count <= 20000 and len(payload) <= 65536 and input_bytes <= MAX_BYTES
    if row["hit"] is None:
        missing += 1
    else:
        mismatch += int(int(row["hit"]) != int(row["settled_bucket"] in json.loads(payload)))
snapshot["label_check"] = {"rows": count, "mismatch": mismatch, "missing": missing}
conn.rollback()
conn.close()
out = {"scope": "source audit, synthetic counterexamples, and read-only SSOT snapshot; NOT OOS or a live profitability claim", "source_default_beta_mode": BETA_EDGE_MODE, "source_seat_shape_rule": SEAT_SHAPE_RULE_VERSION, "probes": probes, "snapshot": snapshot}
encoded = json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)
assert len(encoded.encode()) < 2*1024**2
guard()
Path(__file__).with_name("evidence.json").write_text(encoded+"\n")
print(json.dumps({"probes": len(probes), "boundary_cases": checked, "label_check": snapshot["label_check"], "evidence": str(Path(__file__).with_name("evidence.json"))}))
