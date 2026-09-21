"""Charter 192 (read-only): what a fresher ICON/ECCC run is worth, on settled truth.

Usage: .venv/bin/python docs/research/follow_run_value_20260921.py OUT.json [MIN_TRAIN]
(results: follow_run_value_20260921_min5.json / _min10.json beside this file).

Calibration and scoring reuse production's own functions
(``native_ensemble_oos._fit_parameters`` / ``_apply_parameters`` /
``_metrics``); each (system, station, run slot) is walked forward on its own
history with a truth-knowability gap, min 5 training days, 60-day window.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.truth_exclusions import is_excluded  # noqa: E402

DB = ROOT / "data/bayes_basket_target_ssot.db"
START, END = "2026-07-10", "2026-09-20"
SYSTEMS = {
    "ECMWF_IFS_ENS_OPEN_PF50": ("ECMWF_OPEN_DATA_S3", 50),
    "NOAA_GEFS_05": ("NOAA_GEFS_PDS_S3", 31),
    "DWD_ICON_GLOBAL_EPS": ("DWD_OPENDATA_ICON_EPS", 40),
    "ECCC_GEM_GLOBAL_ENS": ("ECCC_DD_WEATHER", 21),
}
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("follow_run_value.json")
MIN_TRAIN = int(sys.argv[2]) if len(sys.argv) > 2 else oos.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.execute("PRAGMA query_only=1")

truth: dict[tuple[str, str], float] = {}
for station, day, value in con.execute(
    "SELECT t.station_id,t.local_date,t.official_tmax_c "
    "FROM calibration_station_daily_high_truth t "
    "WHERE t.source_lane='AWC' AND t.qc_status='ACCEPTED' "
    "AND t.authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
    "AND t.local_date BETWEEN ? AND ? "
    "AND t.revision_no=(SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth t2 "
    " WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date "
    " AND t2.settlement_definition=t.settlement_definition AND t2.source_lane='AWC' "
    " AND t2.qc_status='ACCEPTED')",
    (START, END),
):
    if not is_excluded(str(station), str(day)):
        truth[(str(station), str(day))] = float(value)
print("truth station-days", len(truth), file=sys.stderr)

# (system, station, day, run_init) -> {member: value}
runs: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(dict)
bad: set[tuple[str, str, str, str]] = set()
for system, (transport, _n) in SYSTEMS.items():
    for row in con.execute(
        "SELECT station_id,target_local_date,native_run_init_utc,member_id,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "INDEXED BY calibration_native_member_skill_idx "
        "WHERE ensemble_system_id=? AND transport_provider_id=? AND qc_status='ACCEPTED' "
        "AND target_local_date BETWEEN ? AND ? "
        "AND authority_scope='CALIBRATION_TRAIN_ONLY' AND holdout_eligible=0",
        (system, transport, START, END),
    ):
        station, day, run_init, member, value = row
        if not run_init:
            continue
        key = (system, str(station), str(day), str(run_init))
        prev = runs[key].get(str(member))
        if prev is not None and abs(prev - float(value)) > 1e-9:
            bad.add(key)
        runs[key][str(member)] = float(value)
    print(system, "runs", sum(1 for k in runs if k[0] == system), file=sys.stderr)

# slot = (lead_days, cycle hour)
cases: dict[tuple[str, str, int, int], list[dict]] = defaultdict(list)
for (system, station, day, run_init), members in runs.items():
    if (system, station, day, run_init) in bad:
        continue
    if len(members) != SYSTEMS[system][1] or (station, day) not in truth:
        continue
    ri = datetime.fromisoformat(run_init.replace("Z", "+00:00")).astimezone(timezone.utc)
    lead = (date.fromisoformat(day) - ri.date()).days
    if lead not in (0, 1, 2):
        continue
    cases[(system, station, lead, ri.hour)].append({
        "event_date": day,
        "values": [members[m] for m in sorted(members)],
        "actual_tmax_c": truth[(station, day)],
    })

# walk-forward calibrated prediction per slot
pred: dict[tuple[str, str, int, int, str], dict] = {}
for (system, station, lead, cyc), rows in cases.items():
    rows.sort(key=lambda r: r["event_date"])
    for row in rows:
        t = date.fromisoformat(row["event_date"])
        lo = (t - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
        # a run issued on D-lead serves decisions that day; the newest closed
        # truth then is D-lead-1
        hi = (t - timedelta(days=lead + 1)).isoformat()
        train = [r for r in rows if lo <= r["event_date"] <= hi]
        if len(train) < MIN_TRAIN:
            continue
        params = oos._fit_parameters(train)
        prob = oos._apply_parameters(row["values"], params)
        m = oos._metrics(prob, row["actual_tmax_c"])
        vals = row["values"]
        mean = sum(vals) / len(vals)
        pred[(system, station, lead, cyc, row["event_date"])] = {
            "prob": prob, "crps": m["crps"], "nll": m["nll"], "brier": m["brier"],
            "abs_err_bc": abs(mean + params["bias_c"] - row["actual_tmax_c"]),
            "actual": row["actual_tmax_c"],
        }
print("predictions", len(pred), file=sys.stderr)


def paired(system_new, slot_new, system_old, slot_old, systems=None):
    """Per station-day paired deltas (new - old)."""
    out = []
    for (system, station, lead, cyc, day), p in pred.items():
        if system != system_new or (lead, cyc) != slot_new:
            continue
        q = pred.get((system_old, station, slot_old[0], slot_old[1], day))
        if q is None:
            continue
        out.append((day, station, p["crps"] - q["crps"], p["nll"] - q["nll"], q["crps"], q["nll"], p["abs_err_bc"] - q["abs_err_bc"]))
    return out


def summarize(rows, reps=2000, seed=7):
    if not rows:
        return {"n_station_days": 0}
    days = sorted({r[0] for r in rows})
    by_day = defaultdict(list)
    for r in rows:
        by_day[r[0]].append(r)
    def stat(sample_days):
        sel = [r for d in sample_days for r in by_day[d]]
        dc = sum(r[2] for r in sel) / len(sel)
        dn = sum(r[3] for r in sel) / len(sel)
        base = sum(r[4] for r in sel) / len(sel)
        return dc, dn, dc / base if base else 0.0
    point = stat(days)
    rng = random.Random(seed)
    boots = [stat([rng.choice(days) for _ in days]) for _ in range(reps)]
    def ci(i):
        vals = sorted(b[i] for b in boots)
        return [vals[int(0.025 * reps)], vals[int(0.975 * reps) - 1]]
    return {
        "n_station_days": len(rows), "n_event_days": len(days),
        "stations": len({r[1] for r in rows}),
        "first_day": days[0], "last_day": days[-1],
        "d_rps": point[0], "d_rps_ci95": ci(0),
        "d_rps_rel": point[2], "d_rps_rel_ci95": ci(2),
        "d_nll": point[1], "d_nll_ci95": ci(1),
        "base_rps": sum(r[4] for r in rows) / len(rows),
        "d_mae_bias_corrected_c": (sum(r[6] for r in rows) / len(rows)) if len(rows[0]) > 6 else None,
    }


result = {"window": [START, END], "per_system": {}, "pool": {}}
comparisons = {
    # the proposed d1/d0 follows, measured on ECMWF/GEFS as proxies
    "d1_06Z_vs_d1_00Z": ((1, 6), (1, 0)),
    "d1_12Z_vs_d1_00Z": ((1, 12), (1, 0)),
    "d1_18Z_vs_d1_00Z": ((1, 18), (1, 0)),
    "d1_18Z_vs_d1_12Z": ((1, 18), (1, 12)),
    "d0_06Z_vs_d0_00Z": ((0, 6), (0, 0)),
    "d0_12Z_vs_d0_00Z": ((0, 12), (0, 0)),
    "d0_18Z_vs_d0_00Z": ((0, 18), (0, 0)),
    # the same 12h step one lead out, on all four systems (direct ICON/ECCC)
    "d1_00Z_vs_d2_12Z": ((1, 0), (2, 12)),
    # ECCC's own 12Z vs 00Z at d2 (overlap 07-15..08-12)
    "d2_12Z_vs_d2_00Z": ((2, 12), (2, 0)),
}
for system in SYSTEMS:
    for name, (new, old) in comparisons.items():
        result["per_system"][f"{system}:{name}"] = summarize(paired(system, new, system, old))

# P0-level analogue: ECMWF/GEFS on the fresher run in both arms; ICON/ECCC
# 12h behind (status quo shape) versus caught up (the proposal's shape).
def pool_delta(ens_slot, lag_slot, fresh_slot):
    rows = []
    for (system, station, lead, cyc, day), p in pred.items():
        if system != "ECMWF_IFS_ENS_OPEN_PF50" or (lead, cyc) != ens_slot:
            continue
        g = pred.get(("NOAA_GEFS_05", station, ens_slot[0], ens_slot[1], day))
        il = pred.get(("DWD_ICON_GLOBAL_EPS", station, lag_slot[0], lag_slot[1], day))
        cl = pred.get(("ECCC_GEM_GLOBAL_ENS", station, lag_slot[0], lag_slot[1], day))
        inew = pred.get(("DWD_ICON_GLOBAL_EPS", station, fresh_slot[0], fresh_slot[1], day))
        cnew = pred.get(("ECCC_GEM_GLOBAL_ENS", station, fresh_slot[0], fresh_slot[1], day))
        if None in (g, il, cl, inew, cnew):
            continue
        def pool(members):
            keys = set().union(*(m["prob"] for m in members))
            return {k: sum(m["prob"].get(k, 0.0) for m in members) / len(members) for k in keys}
        a = oos._metrics(pool([p, g, il, cl]), p["actual"])
        b = oos._metrics(pool([p, g, inew, cnew]), p["actual"])
        rows.append((day, station, b["crps"] - a["crps"], b["nll"] - a["nll"], a["crps"], a["nll"]))
    return rows

result["pool"]["ecmwf_gefs_d1_00Z__icon_eccc_d2_12Z_vs_d1_00Z"] = summarize(
    pool_delta((1, 0), (2, 12), (1, 0))
)
OUT.write_text(json.dumps(result, indent=1, sort_keys=True))
for k, v in result["per_system"].items():
    if v.get("n_station_days"):
        print(f"{k:55s} n={v['n_station_days']:5d} days={v['n_event_days']:3d} "
              f"dRPS={v['d_rps']:+.4f} [{v['d_rps_ci95'][0]:+.4f},{v['d_rps_ci95'][1]:+.4f}] "
              f"rel={v['d_rps_rel']*100:+.1f}% [{v['d_rps_rel_ci95'][0]*100:+.1f},{v['d_rps_rel_ci95'][1]*100:+.1f}] "
              f"dNLL={v['d_nll']:+.4f} [{v['d_nll_ci95'][0]:+.4f},{v['d_nll_ci95'][1]:+.4f}] base={v['base_rps']:.3f} dMAE={v['d_mae_bias_corrected_c'] if v['d_mae_bias_corrected_c'] is None else round(v['d_mae_bias_corrected_c'],3)}")
for k, v in result["pool"].items():
    print(k, json.dumps(v))
