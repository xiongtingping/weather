#!/usr/bin/env python3
"""Health check for the two tables P0 serves from, and their pointers.

Serving is a lookup: the correction table holds per-system bias/spread/
reliability, the weight table holds the blend, and the activation table
decides which generation of each is live.  Nothing in those rows says
whether the stored number still deserves trust, so the failures found on
2026-08-15 were all invisible until someone asked by hand:

* a spread factor pinned at a bound that had since been raised, on 46 live
  scopes, because only systems that won a seat were ever refitted;
* a weight row six days older than the correction rows beside it, because
  the cohort selector never offers the cycle the packet actually carries;
* a bias whose stability nobody had checked -- it turned out steady to
  0.06C across halves, but that was luck, not evidence.

Each check reports what is served against what the evidence supports.
Read-only: opens the database in immutable mode and writes nothing.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
LIVE_LEAD_BUCKETS = ("h00_12", "h12_24", "h24_48")
SPREAD_BOUND = 5.0
# A split-half bias that moves by more than this is not one number any more.
# Scaled to the station's own residual spread rather than a fixed degree so a
# volatile station is not flagged for volatility it always had.
BIAS_DRIFT_SIGMA = 1.0
MIN_SPLIT_SAMPLES = 16
# Two tables, two refresh paths.  A gap this wide means one of them stopped.
STALE_TABLE_GAP_DAYS = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _live_activations(conn: sqlite3.Connection, now: str) -> dict:
    """Newest generation per (station, system, season, lead, cycle)."""

    top: dict[tuple, tuple] = {}
    for row in conn.execute(
        "SELECT v.station_id,v.ensemble_system_id_or_star,v.season_bin,"
        "v.lead_bucket,v.run_cycle,v.generation,a.spread_parameters_json,"
        "a.bias_parameters_json,a.train_end_date "
        "FROM calibration_native_ensemble_activation v "
        "JOIN calibration_native_ensemble_system_artifact a "
        "ON a.system_artifact_id=v.artifact_id "
        "WHERE v.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND v.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND v.valid_from_utc<=? AND v.valid_until_utc>?",
        (now, now),
    ):
        key = tuple(str(value) for value in row[:5])
        if key not in top or int(row[5]) > int(top[key][5]):
            top[key] = row
    return top


def check_spread_bound(live: dict) -> list[dict]:
    """A spread factor sitting exactly on a bound the evidence exceeds."""

    findings = []
    for key, row in live.items():
        station, system, _season, lead, cycle = key
        if lead not in LIVE_LEAD_BUCKETS:
            continue
        spread = json.loads(str(row[6]))
        served = float(spread.get("spread_factor") or 0.0)
        # The scale the fit actually sizes width by: the residual sigma about
        # the corrected centre, not the RMSE about zero.  Reading the RMSE
        # here after the fit moved to sigma made this check demand the width
        # the bias correction already removed -- 127 of 510 live scopes
        # reported as too narrow on the day the fit changed, every one of
        # them a false alarm.  Artifacts fitted before the change carry no
        # sigma and were genuinely sized by RMSE, so they fall back to it and
        # are still judged by the rule they were built under.
        scale = float(
            spread.get("train_residual_sigma_c")
            or spread.get("train_rmse_c")
            or 0.0
        )
        mean_spread = float(spread.get("train_mean_spread_c") or 0.0)
        if mean_spread <= 0.0:
            continue
        wanted = min(SPREAD_BOUND, max(0.5, scale / mean_spread))
        if wanted - served > 0.05:
            findings.append({
                "check": "spread_below_evidence",
                "station_id": station,
                "ensemble_system_id": system,
                "lead_bucket": lead,
                "run_cycle": cycle,
                "served_spread_factor": round(served, 3),
                "evidence_spread_factor": round(wanted, 3),
                "narrower_by": round(wanted / served, 2) if served else None,
                "train_end_date": str(row[8]),
            })
    return findings


def check_table_freshness(conn: sqlite3.Connection, live: dict) -> list[dict]:
    """The weight row aging while the correction rows beside it advance."""

    correction_end: dict[tuple, str] = {}
    for key, row in live.items():
        station, _system, season, lead, cycle = key
        if lead not in LIVE_LEAD_BUCKETS:
            continue
        scope = (station, season, lead, cycle)
        end = str(row[8] or "")
        if end > correction_end.get(scope, ""):
            correction_end[scope] = end
    weight_end: dict[tuple, str] = {}
    for station, season, lead, cycle, end in conn.execute(
        "SELECT station_id,season_bin,lead_bucket,run_cycle,"
        "MAX(train_end_date) FROM "
        "calibration_cross_native_ensemble_selection_evidence "
        "GROUP BY station_id,season_bin,lead_bucket,run_cycle"
    ):
        weight_end[(str(station), str(season), str(lead), str(cycle))] = str(end or "")
    findings = []
    for scope, corrected in sorted(correction_end.items()):
        weighted = weight_end.get(scope)
        if not weighted or not corrected:
            continue
        gap = (
            datetime.fromisoformat(corrected)
            - datetime.fromisoformat(weighted)
        ).days
        if gap >= STALE_TABLE_GAP_DAYS:
            findings.append({
                "check": "weight_table_behind_correction_table",
                "station_id": scope[0],
                "lead_bucket": scope[2],
                "run_cycle": scope[3],
                "correction_train_end": corrected,
                "weight_train_end": weighted,
                "gap_days": gap,
            })
    return findings


def check_bias_stability(conn: sqlite3.Connection, live: dict) -> list[dict]:
    """Whether a served bias is one number or two glued together.

    Recomputed from the cases rather than read from the row, because the
    row stores only the fit, never its stability.  Split at the median
    date; a bias that moves by more than its own residual sigma between
    halves is not describing a standing offset.
    """

    truth: dict[tuple[str, str], float] = {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT station_id,local_date,official_tmax_c "
            "FROM calibration_station_daily_high_truth"
        )
    }
    served_systems = {
        (key[0], key[1]) for key in live if key[3] in LIVE_LEAD_BUCKETS
    }
    errors: dict[tuple[str, str], list[tuple[str, float]]] = (
        collections.defaultdict(list)
    )
    for station, system, day, mean in conn.execute(
        "SELECT station_id,ensemble_system_id,target_local_date,"
        "AVG(raw_tmax_c) FROM calibration_native_ensemble_member_run "
        "GROUP BY station_id,ensemble_system_id,target_local_date,run_cycle"
    ):
        pair = (str(station), str(system))
        if pair not in served_systems:
            continue
        observed = truth.get((str(station), str(day)))
        if observed is None:
            continue
        errors[pair].append((str(day), observed - float(mean)))
    findings = []
    for pair, rows in sorted(errors.items()):
        if len(rows) < MIN_SPLIT_SAMPLES:
            continue
        rows.sort()
        half = len(rows) // 2
        early = [value for _day, value in rows[:half]]
        late = [value for _day, value in rows[half:]]
        sigma = statistics.pstdev([value for _day, value in rows])
        drift = statistics.fmean(late) - statistics.fmean(early)
        if sigma > 0.0 and abs(drift) > BIAS_DRIFT_SIGMA * sigma:
            findings.append({
                "check": "bias_drifted_between_halves",
                "station_id": pair[0],
                "ensemble_system_id": pair[1],
                "samples": len(rows),
                "early_bias_c": round(statistics.fmean(early), 2),
                "late_bias_c": round(statistics.fmean(late), 2),
                "drift_c": round(drift, 2),
                "residual_sigma_c": round(sigma, 2),
                "split_at": rows[half][0],
            })
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # ``mode=ro`` and not ``immutable=1``: the resident writes continuously,
    # and immutable tells SQLite the file cannot change, so it skips the WAL
    # and reads a torn view -- observed here as "database disk image is
    # malformed" on the second run, which is the loud failure; the quiet one
    # is a stale answer that looks fine.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = _utc_now()
    live = _live_activations(conn, now)
    findings = (
        check_spread_bound(live)
        + check_table_freshness(conn, live)
        + check_bias_stability(conn, live)
    )
    conn.close()

    by_check = collections.Counter(row["check"] for row in findings)
    payload = {
        "schema": "native_calibration_health_v1",
        "checked_at_utc": now,
        "live_scope_count": sum(
            1 for key in live if key[3] in LIVE_LEAD_BUCKETS
        ),
        "finding_count": len(findings),
        "by_check": dict(by_check),
        "findings": findings,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 1 if findings else 0
    print(f"活 scope {payload['live_scope_count']}  发现 {len(findings)} 项")
    for check, count in by_check.most_common():
        print(f"  {check}: {count}")
    for row in findings[:20]:
        print("   ", json.dumps(row, ensure_ascii=False))
    if len(findings) > 20:
        print(f"    … 另有 {len(findings)-20} 项，用 --json 看全")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
