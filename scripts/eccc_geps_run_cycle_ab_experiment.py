#!/usr/bin/env python3
"""Measure ECCC GEPS d2 skill at 00Z against 12Z on the same event dates.

ECCC is the only native family without an 18Z run, so unlike ECMWF/GEFS/ICON
its d2 cycle is a choice rather than a given.  The archive was built on 00Z,
which fires 18 hours before the 18Z the other families use; 12Z is also
published and narrows that information deficit to 6 hours.  Nothing in the
codebase records why 00Z was picked, and ECCC is simultaneously the weakest
candidate in every station scan -- so the deficit may be self-inflicted rather
than a property of the model.

This runs the comparison offline: it captures 12Z for dates whose 00Z members
are already canonical, scores both against settled truth, and writes a report.
It never writes to the SSOT, so a losing experiment costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.eccc_geps_archive import (  # noqa: E402
    SUPPORTED_RUN_CYCLE_HOURS,
    capture_geps_station_days,
)
from scripts.backfill_eccc_geps_multistation_archive import (  # noqa: E402
    _station_scopes,
)

SYSTEM_ID = "ECCC_GEM_GLOBAL_ENS"


def _settled_truth(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> dict[str, float]:
    """Latest accepted revision of official daily high per station."""

    rows = conn.execute(
        "SELECT station_id,official_tmax_c FROM ("
        "  SELECT station_id,official_tmax_c,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY station_id ORDER BY revision_no DESC"
        "         ) rn"
        "  FROM calibration_station_daily_high_truth"
        "  WHERE local_date=? AND qc_status='ACCEPTED'"
        ") WHERE rn=1",
        (event_date,),
    ).fetchall()
    return {str(row[0]): float(row[1]) for row in rows}


def _canonical_00z_members(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> dict[str, list[float]]:
    """Members already ingested for the incumbent 00Z cycle."""

    rows = conn.execute(
        "SELECT station_id,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND target_local_date=? "
        "  AND archive_lead_bucket='d2' AND run_cycle='00Z' "
        "  AND offline_oos_eligible=1",
        (SYSTEM_ID, event_date),
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for station, value in rows:
        grouped.setdefault(str(station), []).append(float(value))
    return grouped


def _captured_members(archive: dict[str, object]) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = {}
    for capture in archive["captures"]:  # type: ignore[index]
        station = str(capture["station_id"])
        grouped[station] = [
            float(member["raw_tmax_c"]) for member in capture["members"]
        ]
    return grouped


def _score(
    members_by_station: dict[str, list[float]],
    truth_by_station: dict[str, float],
) -> list[dict[str, float | str]]:
    """Per-station error of the ensemble mean against settled truth."""

    scored: list[dict[str, float | str]] = []
    for station, members in sorted(members_by_station.items()):
        truth = truth_by_station.get(station)
        if truth is None or not members:
            continue
        mean_c = statistics.fmean(members)
        scored.append({
            "station_id": station,
            "ensemble_mean_c": mean_c,
            "truth_c": truth,
            "error_c": mean_c - truth,
        })
    return scored


def _aggregate(scored: list[dict[str, float | str]]) -> dict[str, float | int]:
    errors = [float(row["error_c"]) for row in scored]
    if not errors:
        return {"n": 0}
    return {
        "n": len(errors),
        "bias_c": round(statistics.fmean(errors), 4),
        "mae_c": round(statistics.fmean(abs(e) for e in errors), 4),
        "rmse_c": round(
            (statistics.fmean(e * e for e in errors)) ** 0.5, 4
        ),
    }


def _event_dates(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise SystemExit("eccc_ab_experiment_date_range_inverted")
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _run_one_date(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    challenger_hour: int,
    timeout_seconds: float,
) -> dict[str, object]:
    truth = _settled_truth(conn, event_date=event_date)
    if not truth:
        return {"event_date": event_date, "skipped": "no_settled_truth"}
    incumbent = _canonical_00z_members(conn, event_date=event_date)
    if not incumbent:
        return {"event_date": event_date, "skipped": "no_canonical_00z"}
    scopes = _station_scopes(conn, event_date=event_date)
    queried_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    archive = capture_geps_station_days(
        event_date=event_date,
        station_scopes=scopes,
        archive_query_at_utc=queried_at,
        timeout_seconds=timeout_seconds,
        run_cycle_hour=challenger_hour,
    )
    challenger = _captured_members(archive)
    # Score both cycles on the identical station set so the comparison cannot
    # be moved by one cycle happening to cover a station the other missed.
    common = sorted(set(incumbent) & set(challenger) & set(truth))
    incumbent_scored = _score(
        {station: incumbent[station] for station in common}, truth
    )
    challenger_scored = _score(
        {station: challenger[station] for station in common}, truth
    )
    return {
        "event_date": event_date,
        "station_count": len(common),
        "run_cycle_incumbent": "00Z",
        "run_cycle_challenger": archive["run_cycle"],
        "run_init_utc_challenger": archive["run_init_utc"],
        "incumbent": _aggregate(incumbent_scored),
        "challenger": _aggregate(challenger_scored),
        "per_station": {
            row["station_id"]: {
                "incumbent_error_c": round(float(row["error_c"]), 4),
                "challenger_error_c": round(
                    float(challenger_scored[index]["error_c"]), 4
                ),
            }
            for index, row in enumerate(incumbent_scored)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--challenger-run-cycle-hour",
        type=int,
        default=12,
        choices=list(SUPPORTED_RUN_CYCLE_HOURS),
    )
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    results: list[dict[str, object]] = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        for event_date in _event_dates(args.start_date, args.end_date):
            try:
                outcome = _run_one_date(
                    conn,
                    event_date=event_date,
                    challenger_hour=args.challenger_run_cycle_hour,
                    timeout_seconds=args.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - report, never abort run
                outcome = {
                    "event_date": event_date,
                    "failed": f"{type(exc).__name__}:{exc}",
                }
            results.append(outcome)
            print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))

    paired = [row for row in results if "incumbent" in row]
    summary: dict[str, object] = {
        "dates_attempted": len(results),
        "dates_paired": len(paired),
    }
    if paired:
        weights = [int(row["station_count"]) for row in paired]  # type: ignore[index]
        total = sum(weights)
        for side in ("incumbent", "challenger"):
            summary[side] = {
                metric: round(
                    sum(
                        float(row[side][metric]) * weight  # type: ignore[index]
                        for row, weight in zip(paired, weights)
                    )
                    / total,
                    4,
                )
                for metric in ("bias_c", "mae_c", "rmse_c")
            }
        summary["challenger_wins_mae"] = (
            summary["challenger"]["mae_c"] < summary["incumbent"]["mae_c"]  # type: ignore[index]
        )

    report = {
        "schema": "eccc_geps_run_cycle_ab_experiment_v1",
        "system_id": SYSTEM_ID,
        "summary": summary,
        "results": results,
        "writes_ssot": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(
        {"output": str(output), "summary": summary},
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
