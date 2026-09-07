#!/usr/bin/env python3
"""Fit charter 95's live-gap slope: how today's own error moves the anchor.

For every regional run, at every formal observation instant after it, the
residual is the day's settled maximum minus ``max(running high, curve's
remaining peak)`` -- the same quantity charter 99's cell bias models -- and the
regressor is the live gap, the newest report minus this run's value for the
hour then in progress.  The fit is ``residual ~ b * (gap - gap_bar)``: slope
only, centred, because the level is already charter 99's and correcting it
twice is what sank the historical debias (2026-09-07).

Slopes are shrunk toward zero by ``n/(n+K)`` -- a thin sample's claim about a
relationship should mostly be "no relationship" -- and per-station slopes are
fitted and written beside the system's, for audit, without being served.

``--replay-table`` emits one artifact per event date fitted only on strictly
earlier dates, the discipline every other layer in this lane replays under.
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
from bayes_basket_target.regional_live_gap import (  # noqa: E402
    MAX_GAP_C, MIN_POOL_SAMPLES, RULE_VERSION, SCHEMA, SHRINK_K,
    live_gap_c, shrunk_slope,
)

FORMAL = ("METAR", "SPECI")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def collect(target_db: Path, archive_db: Path) -> list[dict]:
    target = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=120.0)
    archive = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=120.0)
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
        day_start = datetime.combine(
            datetime.fromisoformat(day).date(), datetime.min.time(), tzinfo=zone
        ).astimezone(timezone.utc)
        close = day_start + timedelta(days=1)
        actual = truth[(station, day)]
        running = None
        for when, value in [
            p for p in formal.get(station, []) if day_start <= p[0] < close
        ]:
            running = value if running is None else max(running, value)
            if when <= run_init:
                continue
            if (when - run_init).total_seconds() / 3600.0 > MAX_RUN_AGE_HOURS:
                continue
            remaining = [v for stamp, v in curve.items() if stamp > when]
            if not remaining:
                continue
            # The run's value for the hour now in progress.  The curve is
            # keyed on the hour it is valid for, so the report at 14:23 is
            # compared against the 14:00 value, and the next hour is the
            # fallback for a curve that starts after this instant.
            hour = when.replace(minute=0, second=0, microsecond=0)
            now_c = curve.get(hour, curve.get(hour + timedelta(hours=1)))
            gap = live_gap_c(value, now_c)
            if gap is None:
                continue
            samples.append({
                "system_id": system_id, "station_id": station, "event_date": day,
                "gap_c": max(-MAX_GAP_C, min(MAX_GAP_C, gap)),
                "error_c": actual - max(float(running), max(remaining)),
                "hours_left": (close - when).total_seconds() / 3600.0,
            })
    return samples


def _scope(rows: list[dict]) -> dict:
    pairs = [(r["gap_c"], r["error_c"]) for r in rows]
    slope, gap_bar, count = shrunk_slope(pairs)
    by_station: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_station[row["station_id"]].append(row)
    # In-fit only, so not a gate: the gate is the serving measurement.  What
    # this says is whether the slope explains any of the residual at all.
    served = [r - slope * (g - gap_bar) for g, r in pairs]
    raw_var = statistics.pvariance([r for _, r in pairs]) if len(pairs) > 1 else 0.0
    left_var = statistics.pvariance(served) if len(served) > 1 else 0.0
    return {
        "sample_n": count,
        "station_n": len(by_station),
        "station_day_n": len({(r["station_id"], r["event_date"]) for r in rows}),
        "trained_from": min(r["event_date"] for r in rows),
        "trained_through": max(r["event_date"] for r in rows),
        "slope_c_per_c": slope,
        "gap_bar_c": gap_bar,
        "residual_mean_c": statistics.mean([r for _, r in pairs]),
        "variance_explained": (
            (raw_var - left_var) / raw_var if raw_var > 0 else 0.0
        ),
        "stations": {
            station: {
                "slope_c_per_c": shrunk_slope(
                    [(r["gap_c"], r["error_c"]) for r in station_rows])[0],
                "gap_bar_c": shrunk_slope(
                    [(r["gap_c"], r["error_c"]) for r in station_rows])[1],
                "sample_n": len(station_rows),
            }
            for station, station_rows in sorted(by_station.items())
        },
    }


def fit(samples: list[dict]) -> dict:
    by_system: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        by_system[str(sample["system_id"])].append(sample)
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "shrink_k": SHRINK_K,
        "max_gap_c": MAX_GAP_C,
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
        / "regional_live_gap_artifact.json",
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
    print("  活偏差斜率（残差 ≈ 斜率 × (活偏差 − 中心)，正=模型此刻越冷、真值越高于锚）：")
    for system, scope in sorted(artifact["systems"].items()):
        print(
            f"\n    {system}  样本 {scope['sample_n']} · {scope['station_day_n']} 站日 · "
            f"{scope['station_n']} 站 · {scope['trained_from']}..{scope['trained_through']}"
        )
        print(
            f"      斜率 {scope['slope_c_per_c']:+.4f} °C/°C · 活偏差中心 "
            f"{scope['gap_bar_c']:+.3f}°C · 残差均值 {scope['residual_mean_c']:+.3f}°C"
            f" · 解释方差 {100*scope['variance_explained']:.1f}%"
        )
        for station, cell in sorted(scope["stations"].items()):
            print(f"      {station}  n={cell['sample_n']:5d}  斜率 {cell['slope_c_per_c']:+.4f}"
                  f"  中心 {cell['gap_bar_c']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
