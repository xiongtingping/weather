#!/usr/bin/env python3
"""Fit the regional models' bias calibration, the way the global models' is fitted.

User ruling 2026-09-18 ("把所有区域模型也做偏差校准（和全球模型方式一样）").
Every regional run whose peak window is complete is one case: the station's
settled daily high against the run's forecast maximum.  Scopes, weights,
terciles and the residual sigma are the global layer's own
(``native_ensemble_oos._fit_parameters``); see
``bayes_basket_target.regional_point_debias`` for the form and the holdout
reading that chose it.

Two products, both written by one run so they can never describe different
fits:

* the production artifact -- every settled day, the calibration production
  serves today (a day being served has not settled, so it is never in it);
* the replay table -- for every event date, the calibration fitted only on
  strictly earlier days.  The sample builder the temperature path's
  regression is fitted on, the walk-forward that gates it, and the ledger's
  replay all read this, so no replayed decision sees an offset its morning
  could not have known.

Daily in ``calibration_refit_sweep.sh``.  Content is deterministic given the
lane, the truth and ``--before``, so a rerun on unchanged evidence the same day
rewrites the same bytes.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bayes_basket_target.regional_point_debias import (  # noqa: E402
    DEFAULT_ARTIFACT, DEFAULT_REPLAY_TABLE, MIN_TRAIN_DAYS, RULE_VERSION,
    SCHEMA, fit_calibration,
)
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402

TRUTH_SQL = (
    "SELECT t.station_id,t.local_date,t.official_tmax_c "
    "FROM calibration_station_daily_high_truth AS t "
    "WHERE t.source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND t.qc_status='ACCEPTED' "
    "AND t.authority_scope IN "
    "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
    "AND t.revision_no=("
    "  SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth AS t2 "
    "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date "
    "  AND t2.source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND t2.qc_status='ACCEPTED')"
)
RUNS_SQL = (
    "SELECT ensemble_system_id, station_id, target_local_date, run_init_utc,"
    " run_cycle_hour, lead_hours_to_local_day_start, forecast_tmax_c,"
    " remaining_peak_complete, extraction_version"
    " FROM calibration_regional_point_run"
)


def collect(db: Path) -> tuple[list[dict], dict[tuple[str, str], float]]:
    """The lane's runs and the calibration lane's settled highs.

    The calibration lane's truth, for the reason charter 99 records: the
    trajectory lane's station-day bounds row is wrong on 87 station-days and
    a fit trained on it learns that days do not warm.
    """

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120.0)
    try:
        truth = {
            (str(r[0]).upper(), str(r[1])): float(r[2])
            for r in conn.execute(TRUTH_SQL)
        }
        columns = (
            "ensemble_system_id", "station_id", "target_local_date",
            "run_init_utc", "run_cycle_hour", "lead_hours_to_local_day_start",
            "forecast_tmax_c", "remaining_peak_complete", "extraction_version",
        )
        # Charter 200: rows read at a water cell never train the offset of
        # the land cell that replaced them.
        from bayes_basket_target.regional_point_lane import retired_cell_row  # noqa: PLC0415

        runs = [
            run for run in (dict(zip(columns, row)) for row in conn.execute(RUNS_SQL))
            if not retired_cell_row(
                run["ensemble_system_id"], run["station_id"], run["extraction_version"]
            )
        ]
    finally:
        conn.close()
    return runs, truth


def _write(path: Path, payload: dict) -> None:
    """Atomic: production reads this file on every compile."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=1, sort_keys=True)
    handle, temporary = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        # mkstemp makes the file owner-only; the artifacts beside this one
        # are world-readable and the retention scan reads every JSON here.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def replay_table(
    runs: list[dict], truth: dict[tuple[str, str], float], *, through: str,
) -> dict:
    """One calibration per event date, each fitted on strictly earlier days."""

    days = sorted({str(run["target_local_date"]) for run in runs})
    if not days:
        return {"schema": SCHEMA + "_replay", "dates": {}}
    start = date.fromisoformat(days[0]) + timedelta(days=MIN_TRAIN_DAYS)
    end = date.fromisoformat(through)
    table: dict[str, dict] = {}
    cursor = start
    while cursor <= end:
        fitted = fit_calibration(runs, truth, before_date=cursor.isoformat())
        if fitted["systems"]:
            table[cursor.isoformat()] = {"systems": fitted["systems"]}
        cursor += timedelta(days=1)
    return {
        "schema": SCHEMA + "_replay",
        "rule_version": RULE_VERSION,
        "dates": table,
        "covered_from": min(table) if table else None,
        "covered_through": max(table) if table else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--replay-out", type=Path, default=DEFAULT_REPLAY_TABLE)
    parser.add_argument(
        "--before", type=str, default=None,
        help="fit as a decision on this date would (default: tomorrow UTC, "
             "i.e. every settled day)",
    )
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    runs, truth = collect(args.db)
    if not runs:
        raise SystemExit("no regional runs")
    today = datetime.now(timezone.utc).date()
    before = args.before or (today + timedelta(days=1)).isoformat()
    artifact = fit_calibration(runs, truth, before_date=before)
    _write(args.out, artifact)
    if not args.no_replay:
        table = replay_table(runs, truth, through=before)
        _write(args.replay_out, table)
    print(f"{args.out}  （训练截至 {artifact['trained_through']}，规则 {RULE_VERSION}）")
    if not args.no_replay:
        print(
            f"{args.replay_out}  {len(table['dates'])} 个事件日各一条只用更早日期拟合的表"
            f"（{table.get('covered_from')}..{table.get('covered_through')}）"
        )
    if args.quiet:
        return 0
    print("  逐站偏差（正＝该模型偏冷，服务时加回去；站级 = 全部 run 按日汇成一例）：")
    for system, scope in sorted(artifact["systems"].items()):
        stations = scope["stations"]
        print(f"\n    {system}  {len(stations)} 站")
        for station, fitted in sorted(stations.items()):
            base = fitted["station"]
            print(
                f"      {station}  {base['train_days']:3d} 天  偏差 {base['bias_c']:+5.2f}°C"
                f"  残差σ {base['train_residual_sigma_c']:.2f}"
                f"  拟合内原始 MAE {base['raw_mae_c']:.2f}"
                f"  起报×提前量格 {len(fitted['cycles'])} 个"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
