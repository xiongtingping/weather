#!/usr/bin/env python3
"""Causally join NOAA PSL synoptic context to NCEI ZUUU hourly rows."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "ncei_zuuu_noaa_psl_synoptic_join_v1"


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _float(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _derive(synoptic: dict[str, Any]) -> dict[str, float | None]:
    u850 = _float(synoptic, "basin_center_uwnd_850_mps")
    v850 = _float(synoptic, "basin_center_vwnd_850_mps")
    u700 = _float(synoptic, "basin_center_uwnd_700_mps")
    v700 = _float(synoptic, "basin_center_vwnd_700_mps")
    rh850 = _float(synoptic, "basin_center_rhum_850_pct")
    rh700 = _float(synoptic, "basin_center_rhum_700_pct")
    omega700 = _float(synoptic, "basin_center_omega_700_pa_per_s")
    west_t = _float(synoptic, "west_rim_air_850_c")
    east_t = _float(synoptic, "east_basin_air_850_c")
    north_t = _float(synoptic, "north_basin_air_850_c")
    south_t = _float(synoptic, "south_basin_air_850_c")
    west_hgt = _float(synoptic, "west_rim_hgt_500_m")
    east_hgt = _float(synoptic, "east_basin_hgt_500_m")
    north_hgt = _float(synoptic, "north_basin_hgt_500_m")
    south_hgt = _float(synoptic, "south_basin_hgt_500_m")
    # Approximate grid distances around 30N.  The advection sign is positive
    # when the resolved 850-hPa flow carries warmer air into the basin.
    dx_km = 7.5 * 96.3
    dy_km = 5.0 * 111.0
    dtdx = (east_t - west_t) / dx_km if east_t is not None and west_t is not None else None
    dtdy = (north_t - south_t) / dy_km if north_t is not None and south_t is not None else None
    warm_advection = (
        -3600.0 * ((u850 / 1000.0) * dtdx + (v850 / 1000.0) * dtdy)
        if None not in (u850, v850, dtdx, dtdy)
        else None
    )
    return {
        "synoptic_850_wind_speed_mps": math.hypot(u850, v850) if u850 is not None and v850 is not None else None,
        "synoptic_700_wind_speed_mps": math.hypot(u700, v700) if u700 is not None and v700 is not None else None,
        "synoptic_850_700_shear_mps": math.hypot(u700 - u850, v700 - v850) if None not in (u850, v850, u700, v700) else None,
        "synoptic_air850_west_minus_east_c": west_t - east_t if west_t is not None and east_t is not None else None,
        "synoptic_air850_north_minus_south_c": north_t - south_t if north_t is not None and south_t is not None else None,
        "synoptic_hgt500_west_minus_east_m": west_hgt - east_hgt if west_hgt is not None and east_hgt is not None else None,
        "synoptic_hgt500_north_minus_south_m": north_hgt - south_hgt if north_hgt is not None and south_hgt is not None else None,
        "synoptic_warm_advection_850_c_per_h": warm_advection,
        "synoptic_westerly_dry_foehn_proxy": max(u850, 0.0) * max(0.0, 100.0 - rh850) / 100.0 if u850 is not None and rh850 is not None else None,
        "synoptic_southerly_moisture_proxy": max(v850, 0.0) * rh850 / 100.0 if v850 is not None and rh850 is not None else None,
        "synoptic_ascent_moisture_proxy": max(-omega700, 0.0) * rh700 / 100.0 if omega700 is not None and rh700 is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--synoptic", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    synoptic_rows: dict[datetime, dict[str, Any]] = {}
    with gzip.open(args.synoptic, "rt", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            valid = _parse(row["valid_time_utc"])
            synoptic_rows[valid] = dict(row)
    synoptic_times = sorted(synoptic_rows)
    if not synoptic_times:
        raise SystemExit("no synoptic rows")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / "hourly_features_with_synoptic.jsonl.gz"
    total = joined = missing = future_violation = 0
    lag_values: list[float] = []
    synoptic_index = 0
    current_time: datetime | None = None
    current_row: dict[str, Any] | None = None
    with gzip.open(args.features, "rt", encoding="utf-8") as source, gzip.open(output_path, "wt", encoding="utf-8") as target:
        for line in source:
            row = json.loads(line)
            total += 1
            observed_at = _parse(row["valid_time_utc"])
            while synoptic_index < len(synoptic_times) and synoptic_times[synoptic_index] <= observed_at:
                current_time = synoptic_times[synoptic_index]
                current_row = synoptic_rows[current_time]
                synoptic_index += 1
            if current_time is None or current_row is None or current_time.year < 2021 or current_time.year > 2025:
                missing += 1
                row["synoptic_join_available"] = False
                target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                continue
            lag_hours = (observed_at - current_time).total_seconds() / 3600.0
            if lag_hours < 0:
                future_violation += 1
            if lag_hours > 6.0:
                missing += 1
                row["synoptic_join_available"] = False
                target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                continue
            row["synoptic_join_available"] = True
            row["synoptic_valid_time_utc"] = current_time.isoformat().replace("+00:00", "Z")
            row["synoptic_lag_hours"] = round(lag_hours, 6)
            for key, value in current_row.items():
                if key != "valid_time_utc":
                    row[key] = float(value)
            row.update({key: round(value, 8) if value is not None else None for key, value in _derive(current_row).items()})
            joined += 1
            lag_values.append(lag_hours)
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_feature_rows": total,
        "synoptic_rows": len(synoptic_rows),
        "joined_rows": joined,
        "missing_rows": missing,
        "join_rate_pct": round(100.0 * joined / total, 4),
        "future_join_violation_count": future_violation,
        "lag_hours_min": min(lag_values) if lag_values else None,
        "lag_hours_max": max(lag_values) if lag_values else None,
        "causal_join_rule": "latest NOAA PSL 6-hourly valid_time not later than station observation valid_time",
        "output_path": output_path.name,
        "guards": {
            "historical_backfill": False,
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }
    (args.output_dir / "join_quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": future_violation == 0, **report}, ensure_ascii=False, sort_keys=True))
    return 0 if future_violation == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
