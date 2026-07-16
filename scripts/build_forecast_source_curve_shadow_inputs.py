#!/usr/bin/env python3
"""Build leakage-safe AMSC curve checkpoints from external observations."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "forecast_source_curve_shadow_inputs_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _time(value: Any, *, assume_local: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE if assume_local else timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_curve_shadow_inputs(
    *,
    market_events: Sequence[Mapping[str, Any]],
    forecast_snapshots: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    cutoffs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    events_by_date = {
        str(row.get("event_date_local") or ""): dict(row)
        for row in market_events
        if row.get("event_date_local") and row.get("event_id")
    }
    forecasts_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in forecast_snapshots:
        if snapshot.get("state") != "available":
            continue
        source_id = str(snapshot.get("source_id") or "")
        available_at = str(snapshot.get("as_of_eligible_at") or "")
        snapshot_id = str(snapshot.get("forecast_snapshot_id") or "")
        for point in snapshot.get("hourly_curve") or []:
            if not isinstance(point, Mapping):
                continue
            valid = _time(point.get("valid_time_local"), assume_local=True)
            temperature = _float(point.get("temperature_c"))
            if not source_id or valid is None or temperature is None:
                continue
            forecasts_by_date[str(snapshot.get("target_event_date_local") or "")].append(
                {
                    "source_id": source_id,
                    "valid_time": valid.isoformat().replace("+00:00", "Z"),
                    "as_of_eligible_at": available_at,
                    "temperature_c": temperature,
                    "forecast_snapshot_id": snapshot_id,
                }
            )
    observations_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cutoffs_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cutoff in cutoffs:
        cutoff_at = _time(cutoff.get("cutoff_at"))
        if cutoff_at is None or not cutoff.get("event_id") or not cutoff.get("cutoff_id"):
            continue
        cutoffs_by_event[str(cutoff["event_id"])].append({**dict(cutoff), "_parsed_cutoff_at": cutoff_at})
    for values in cutoffs_by_event.values():
        values.sort(key=lambda row: row["_parsed_cutoff_at"])
    excluded_counts: dict[str, int] = defaultdict(int)
    for row in observations:
        valid = _time(row.get("valid_time") or row.get("valid_time_utc"))
        available = _time(row.get("as_of_eligible_at") or row.get("first_seen_at"))
        temperature = _float(row.get("temperature_c"))
        arrival_quality = str(row.get("arrival_time_quality") or "")
        if valid is None or available is None or temperature is None:
            excluded_counts["observation_contract_incomplete"] += 1
            continue
        if arrival_quality != "observed_first_seen":
            excluded_counts["observation_arrival_lineage_not_strict"] += 1
            continue
        local_date = valid.astimezone(LOCAL_ZONE).date().isoformat()
        if local_date not in events_by_date:
            excluded_counts["observation_without_market_event"] += 1
            continue
        event = events_by_date[local_date]
        settlement_at = _time(event.get("resolved_at") or event.get("settlement_at"))
        if settlement_at is None:
            excluded_counts["market_settlement_time_missing"] += 1
            continue
        if available >= settlement_at:
            excluded_counts["observation_available_after_market_settlement"] += 1
            continue
        observations_by_date[local_date].append(
            {
                "valid_time": valid.isoformat().replace("+00:00", "Z"),
                "as_of_eligible_at": available.isoformat().replace("+00:00", "Z"),
                "arrival_time_quality": arrival_quality,
                "temperature_c": temperature,
            }
        )
    curve_inputs: list[dict[str, Any]] = []
    for event_date, event_observations in sorted(observations_by_date.items()):
        forecasts = forecasts_by_date.get(event_date, [])
        if not forecasts:
            excluded_counts["event_without_forecast_curves"] += 1
            continue
        ordered = sorted(event_observations, key=lambda row: row["as_of_eligible_at"])
        for index, observation in enumerate(ordered):
            event_id = str(events_by_date[event_date].get("event_id") or "")
            checkpoint_at = _time(observation["as_of_eligible_at"])
            eligible_cutoffs = [
                row for row in cutoffs_by_event.get(event_id, [])
                if checkpoint_at is not None and row["_parsed_cutoff_at"] <= checkpoint_at
            ]
            cutoff_id = str(eligible_cutoffs[-1].get("cutoff_id") or "event_day_start_local") if eligible_cutoffs else "event_day_start_local"
            curve_inputs.append(
                {
                    "event_id": event_id,
                    "cutoff_id": cutoff_id,
                    "curve_cutoff_at": observation["as_of_eligible_at"],
                    "observations": ordered[: index + 1],
                    "forecasts": forecasts,
                    "checkpoint_policy": "every_observed_first_seen_external_observation_no_hour_threshold",
                }
            )
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "curve_input_count": len(curve_inputs),
        "curve_event_day_count": len({row["event_id"] for row in curve_inputs}),
        "curve_inputs": curve_inputs,
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "guards": {
            "reads_existing_local_history": False,
            "uses_observations_after_curve_cutoff": False,
            "requires_observed_first_seen_arrival_lineage": True,
            "hour_specific_thresholds": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _rows(path: Path, keys: Sequence[str]) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in keys:
            if isinstance(value.get(key), list):
                return [dict(row) for row in value[key] if isinstance(row, Mapping)]
    raise ValueError(f"row_list_missing:{path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-events", type=Path, required=True)
    parser.add_argument("--forecast-snapshots", type=Path, action="append", required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--cutoffs", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = build_curve_shadow_inputs(
        market_events=_rows(args.market_events, ("events",)),
        forecast_snapshots=[
            row
            for path in args.forecast_snapshots
            for row in _rows(path, ("forecast_snapshots",))
        ],
        observations=_rows(args.observations, ("observations", "source_observations")),
        cutoffs=_rows(args.cutoffs, ("cutoffs",)) if args.cutoffs else (),
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        root = DEFAULT_RESEARCH_ROOT.resolve()
        if output != root and root not in output.parents:
            raise ValueError("curve_shadow_input_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
