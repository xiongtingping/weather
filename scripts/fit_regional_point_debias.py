#!/usr/bin/env python3
"""Fit charter 95's single-system correction: how cold each regional lane runs.

At a decision instant the fusion is handed the curve's remaining-segment peak.
This fits the offset that statement carries, per system and per station, from
settled days: for every regional run, at every formal observation instant after
it, the residual is the day's settled maximum minus the curve's remaining peak
-- kept only where that peak is still the curve's own maximum, because past it
the remaining segment describes cooling and the gap is mechanical rather than a
bias (see ``regional_point_debias`` for the two readings that settled this).

The station offset is shrunk toward its own lane's pooled offset by
``n / (n + SHRINK_K)``, so a station with three days of history is corrected
mostly by what its lane does everywhere and a station with three weeks is
corrected by itself.  Lead-hour cells -- charter 95's planned axis -- are
fitted and written beside the station offsets; the measurement put the signal
in the station, so serving reads the station unless
``PM_REGIONAL_POINT_DEBIAS_AXIS=station_lead`` says otherwise.

``--replay-table`` emits one artifact per event date fitted only on strictly
earlier dates, the discipline the ceiling and the path prior replay use.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bayes_basket_target.regional_path_prior import (  # noqa: E402
    MAX_RUN_AGE_HOURS,
)
from bayes_basket_target.regional_point_debias import (  # noqa: E402
    LEAD_EDGES, MIN_POOL_SAMPLES, RULE_VERSION, SCHEMA, SHRINK_K,
    lead_bin, shrunk,
)

FORMAL = ("METAR", "SPECI")
#: A lead cell needs this many before it is written as servable.
MIN_LEAD_CELL_SAMPLES = 30


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def collect(target_db: Path, archive_db: Path) -> list[dict]:
    target = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=120.0)
    archive = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=120.0)
    # The calibration lane's settled high, for the reason charter 99 records:
    # the trajectory lane's station-day bounds row is wrong on 87 station-days
    # and a fit trained on it learns that days do not warm.
    truth = {
        (str(r[0]), str(r[1])): float(r[2])
        for r in target.execute(
            "SELECT t.station_id,t.local_date,t.official_tmax_c "
            "FROM calibration_station_daily_high_truth AS t "
            "WHERE t.source_lane='AWC' AND t.qc_status='ACCEPTED' "
            "AND t.authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
            "AND t.revision_no=("
            "  SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth AS t2 "
            "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date "
            "  AND t2.source_lane='AWC' AND t2.qc_status='ACCEPTED')"
        )
    }
    formal: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in archive.execute(
        "SELECT station_id,valid_at,reported_temperature_c"
        " FROM reported_trajectory_observations"
        " WHERE reported_temperature_c IS NOT NULL AND report_type IN (?,?)",
        FORMAL,
    ):
        try:
            formal[str(row[0])].append((_parse(row[1]), float(row[2])))
        except (TypeError, ValueError):
            continue
    for points in formal.values():
        points.sort()
    samples: list[dict] = []
    for row in target.execute(
        "SELECT ensemble_system_id,station_id,target_local_date,"
        "settlement_timezone,run_init_utc,hourly_c_json"
        " FROM calibration_regional_point_run ORDER BY target_local_date"
    ):
        system_id, station, day, zone_name, run_init_utc, curve_json = (
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5])
        )
        if (station, day) not in truth:
            continue
        try:
            zone = ZoneInfo(zone_name)
            curve = {_parse(k): float(v) for k, v in json.loads(curve_json).items()}
            run_init = _parse(run_init_utc)
        except (ValueError, KeyError, TypeError):
            continue
        if not curve:
            continue
        curve_peak = max(curve.values())
        day_start = datetime.combine(
            datetime.fromisoformat(day).date(), datetime.min.time(), tzinfo=zone
        ).astimezone(timezone.utc)
        close = day_start + timedelta(days=1)
        actual = truth[(station, day)]
        for when, _value in [
            p for p in formal.get(station, []) if day_start <= p[0] < close
        ]:
            if when <= run_init:
                continue
            age = (when - run_init).total_seconds() / 3600.0
            if age > MAX_RUN_AGE_HOURS:
                continue
            remaining = [v for stamp, v in curve.items() if stamp > when]
            if not remaining:
                continue
            peak = max(remaining)
            if peak < curve_peak - 1e-9:
                # The curve's own maximum is behind this instant: what is left
                # is a cooling statement, and the gap to the day's settled high
                # is arithmetic, not error.  Pooled, this half reads +3.9C.
                continue
            samples.append({
                "system_id": system_id, "station_id": station, "event_date": day,
                "run_age_hours": age, "error_c": actual - peak,
            })
    return samples


def _scope(samples: list[dict]) -> dict:
    """One system's pooled offset, its stations' shrunk offsets, its cells."""

    errors = [s["error_c"] for s in samples]
    pooled = statistics.mean(errors)
    per_station: dict[str, list[dict]] = defaultdict(list)
    per_lead: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        per_station[sample["station_id"]].append(sample)
        per_lead[lead_bin(sample["run_age_hours"])].append(sample["error_c"])
    stations = {}
    for station, rows in sorted(per_station.items()):
        values = [r["error_c"] for r in rows]
        cells: dict[str, dict] = {}
        by_lead: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            by_lead[lead_bin(row["run_age_hours"])].append(row["error_c"])
        for index, cell_values in sorted(by_lead.items()):
            if len(cell_values) < MIN_LEAD_CELL_SAMPLES:
                continue
            cells[str(index)] = {
                "hours_from": LEAD_EDGES[index],
                "hours_to": LEAD_EDGES[index + 1],
                "bias_c": shrunk(cell_values, pooled),
                "own_bias_c": statistics.mean(cell_values),
                "sample_n": len(cell_values),
            }
        stations[station] = {
            "bias_c": shrunk(values, pooled),
            "own_bias_c": statistics.mean(values),
            "sample_n": len(values),
            "lead_cells": cells,
        }
    served = [
        s["error_c"] - stations[s["station_id"]]["bias_c"] for s in samples
    ]
    return {
        "sample_n": len(samples),
        "station_n": len(per_station),
        "station_day_n": len({(s["station_id"], s["event_date"]) for s in samples}),
        "trained_from": min(s["event_date"] for s in samples),
        "trained_through": max(s["event_date"] for s in samples),
        "pooled_bias_c": pooled,
        "pooled_sigma_c": statistics.pstdev(errors) if len(errors) > 1 else 0.0,
        # In-fit, so not a gate -- the gate is the as-of holdout in the
        # module docstring.  Written because a refit that stops helping
        # here has stopped helping anywhere.
        "raw_mae_c": sum(map(abs, errors)) / len(errors),
        "debiased_mae_c": sum(map(abs, served)) / len(served),
        "stations": stations,
        "lead_cells": {
            str(index): {
                "hours_from": LEAD_EDGES[index],
                "hours_to": LEAD_EDGES[index + 1],
                "bias_c": statistics.mean(values),
                "sample_n": len(values),
            }
            for index, values in sorted(per_lead.items())
        },
    }


def fit(samples: list[dict]) -> dict:
    by_system: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        by_system[str(sample["system_id"])].append(sample)
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "lead_edges": list(LEAD_EDGES),
        "shrink_k": SHRINK_K,
        "minimum_pool_samples": MIN_POOL_SAMPLES,
        "systems": {
            system: _scope(rows)
            for system, rows in sorted(by_system.items())
            if len(rows) >= MIN_POOL_SAMPLES
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--through", type=str, default=None)
    parser.add_argument("--replay-table", action="store_true")
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
        / "regional_point_debias_artifact.json",
    )
    args = parser.parse_args()
    samples = collect(args.db, args.archive)
    if args.through:
        samples = [s for s in samples if s["event_date"] <= args.through]
    if not samples:
        raise SystemExit("no samples")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.replay_table:
        table = {}
        for date in sorted({s["event_date"] for s in samples}):
            earlier = [s for s in samples if s["event_date"] < date]
            if len(earlier) < MIN_POOL_SAMPLES:
                continue
            fitted = fit(earlier)
            if fitted["systems"]:
                table[date] = fitted
        payload = {
            "schema": SCHEMA + "_replay", "dates": table,
            "covered_from": min(table) if table else None,
            "covered_through": max(table) if table else None,
        }
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"{args.out}\n  {len(table)} 个事件日各一条只用更早日期拟合的表")
        return 0
    artifact = fit(samples)
    artifact["trained_through"] = max(s["event_date"] for s in samples)
    args.out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(f"{args.out}")
    print("  逐站去偏（正＝该系统偏冷，服务时加回去）：")
    for system, scope in sorted(artifact["systems"].items()):
        print(
            f"\n    {system}  样本 {scope['sample_n']} · {scope['station_day_n']} 站日 · "
            f"{scope['station_n']} 站 · {scope['trained_from']}..{scope['trained_through']}"
        )
        print(
            f"      池化偏冷 {scope['pooled_bias_c']:+.2f}°C · σ {scope['pooled_sigma_c']:.2f}"
            f" · 拟合内 MAE {scope['raw_mae_c']:.2f} → {scope['debiased_mae_c']:.2f}"
        )
        for station, cell in sorted(scope["stations"].items()):
            print(
                f"      {station}  n={cell['sample_n']:5d}  站自身 {cell['own_bias_c']:+5.2f}"
                f"  → 收缩后 {cell['bias_c']:+5.2f}°C"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
