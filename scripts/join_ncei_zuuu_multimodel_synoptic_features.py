#!/usr/bin/env python3
"""Causally join multiple six-hour global-model states to NCEI ZUUU rows."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import join_ncei_zuuu_synoptic_features as single


SCHEMA = "ncei_zuuu_multimodel_synoptic_join_v1"
MAX_LAG_HOURS = 6.0
CONSENSUS_FIELDS = (
    "basin_center_air_850_c",
    "basin_center_rhum_850_pct",
    "basin_center_rhum_700_pct",
    "basin_center_hgt_500_m",
    "basin_center_hgt_850_m",
    "synoptic_850_wind_speed_mps",
    "synoptic_700_wind_speed_mps",
    "synoptic_850_700_shear_mps",
    "synoptic_air850_west_minus_east_c",
    "synoptic_air850_north_minus_south_c",
    "synoptic_hgt500_west_minus_east_m",
    "synoptic_hgt500_north_minus_south_m",
    "synoptic_warm_advection_850_c_per_h",
    "synoptic_westerly_dry_foehn_proxy",
    "synoptic_southerly_moisture_proxy",
)


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _source_arg(value: str) -> tuple[str, Path]:
    name, separator, path = str(value).partition("=")
    if not separator or not name.strip() or not path.strip():
        raise argparse.ArgumentTypeError("model source must be NAME=/path/to/features.csv.gz")
    return name.strip(), Path(path).expanduser().resolve()


def _load(path: Path) -> tuple[list[datetime], dict[datetime, dict[str, Any]]]:
    rows: dict[datetime, dict[str, Any]] = {}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[_parse(row["valid_time_utc"])] = dict(row)
    return sorted(rows), rows


def _consensus(row: dict[str, Any], model_names: list[str]) -> dict[str, float | None]:
    output: dict[str, float | None] = {}
    for field in CONSENSUS_FIELDS:
        values = [_number(row.get(f"{model}__{field}")) for model in model_names]
        clean = [value for value in values if value is not None]
        output[f"mm_mean__{field}"] = sum(clean) / len(clean) if clean else None
        if len(clean) >= 2:
            mean = sum(clean) / len(clean)
            output[f"mm_spread__{field}"] = math.sqrt(sum((value - mean) ** 2 for value in clean) / len(clean))
        else:
            output[f"mm_spread__{field}"] = None
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--model-synoptic", action="append", type=_source_arg, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    sources = dict(args.model_synoptic)
    if len(sources) < 2:
        raise SystemExit("multimodel join requires at least two distinct models")
    loaded = {name: _load(path) for name, path in sources.items()}
    if any(not times for times, _ in loaded.values()):
        raise SystemExit("one or more model sources contain no rows")

    indexes = {name: 0 for name in sources}
    current_times: dict[str, datetime | None] = {name: None for name in sources}
    current_rows: dict[str, dict[str, Any] | None] = {name: None for name in sources}
    joined_by_model = {name: 0 for name in sources}
    missing_by_model = {name: 0 for name in sources}
    future_violations = {name: 0 for name in sources}
    lag_values = {name: [] for name in sources}
    total = all_joined = 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / "hourly_features_with_multimodel_synoptic.jsonl.gz"
    with gzip.open(args.features, "rt", encoding="utf-8") as source, gzip.open(output_path, "wt", encoding="utf-8") as target:
        for line in source:
            row = json.loads(line)
            total += 1
            observed_at = _parse(row["valid_time_utc"])
            available_names: list[str] = []
            for name, (times, table) in loaded.items():
                index = indexes[name]
                while index < len(times) and times[index] <= observed_at:
                    current_times[name] = times[index]
                    current_rows[name] = table[times[index]]
                    index += 1
                indexes[name] = index
                valid = current_times[name]
                values = current_rows[name]
                if valid is None or values is None:
                    row[f"{name}__join_available"] = False
                    missing_by_model[name] += 1
                    continue
                lag = (observed_at - valid).total_seconds() / 3600.0
                if lag < 0:
                    future_violations[name] += 1
                if lag < 0 or lag > MAX_LAG_HOURS:
                    row[f"{name}__join_available"] = False
                    missing_by_model[name] += 1
                    continue
                row[f"{name}__join_available"] = True
                row[f"{name}__valid_time_utc"] = valid.isoformat().replace("+00:00", "Z")
                row[f"{name}__synoptic_lag_hours"] = round(lag, 6)
                derived = single._derive(values)
                for key, value in values.items():
                    if key != "valid_time_utc":
                        row[f"{name}__{key}"] = _number(value)
                for key, value in derived.items():
                    row[f"{name}__{key}"] = round(value, 8) if value is not None else None
                available_names.append(name)
                joined_by_model[name] += 1
                lag_values[name].append(lag)
            row["multimodel_join_available"] = len(available_names) == len(sources)
            row["multimodel_available_model_count"] = len(available_names)
            row.update(_consensus(row, sorted(sources)))
            all_joined += int(row["multimodel_join_available"])
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_feature_rows": total,
        "models": sorted(sources),
        "source_paths": {name: str(path) for name, path in sources.items()},
        "all_models_joined_rows": all_joined,
        "all_models_join_rate_pct": round(100.0 * all_joined / total, 4),
        "model_quality": {
            name: {
                "joined_rows": joined_by_model[name],
                "missing_rows": missing_by_model[name],
                "join_rate_pct": round(100.0 * joined_by_model[name] / total, 4),
                "future_join_violation_count": future_violations[name],
                "lag_hours_min": min(lag_values[name]) if lag_values[name] else None,
                "lag_hours_max": max(lag_values[name]) if lag_values[name] else None,
            }
            for name in sorted(sources)
        },
        "future_join_violation_count": sum(future_violations.values()),
        "causal_join_rule": "per model: latest six-hour valid time not later than station observation, maximum lag six hours",
        "consensus_fields": list(CONSENSUS_FIELDS),
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
    (args.output_dir / "join_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["future_join_violation_count"] == 0, **report}, ensure_ascii=False, sort_keys=True))
    return 0 if report["future_join_violation_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
