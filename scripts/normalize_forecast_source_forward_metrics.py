#!/usr/bin/env python3
"""Normalize immutable WU/TWC forward-metric envelopes for source skill."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
SCHEMA_VERSION = "forecast_source_forward_metrics_normalization_v1"

from external_forecast_snapshot_runtime import validate_external_forecast_raw_envelope
from scripts.forecast_source_forward_snapshot import FORWARD_METRICS_SCHEMA


def normalize_forward_metrics_envelopes(envelopes: list[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for raw in envelopes:
        envelope = dict(raw)
        contract = validate_external_forecast_raw_envelope(envelope)
        response_sha = str(envelope.get("response_sha256") or "")
        if not contract["ok"]:
            trace.append({"response_sha256": response_sha, "state": "unavailable", "state_reason": "envelope_contract_failed"})
            continue
        try:
            payload = json.loads(str(envelope.get("response_body_utf8") or ""))
        except json.JSONDecodeError:
            trace.append({"response_sha256": response_sha, "state": "unavailable", "state_reason": "forward_metrics_body_invalid"})
            continue
        raw_provider_response_present = False
        if isinstance(payload, Mapping) and payload.get("schema") == "forecast_source_forward_capture_payload_v2":
            raw_provider_response_present = bool(str(payload.get("raw_provider_response_utf8") or ""))
            payload = payload.get("derived_forecast")
        if not isinstance(payload, Mapping) or payload.get("schema") != FORWARD_METRICS_SCHEMA:
            continue
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
        value_kind = str(payload.get("forecast_value_kind") or metrics.get("forecast_value_kind") or "")
        record_valid_time = str(payload.get("forecast_record_valid_time") or metrics.get("forecast_record_valid_time") or "")
        trajectory_eligible = payload.get("trajectory_eligible")
        if not value_kind and str(metrics.get("source") or "") == "weather_com_forecast_daily_peak":
            value_kind = "daily_max_event_window"
            record_valid_time = str(metrics.get("forecast_valid_local") or "")
            trajectory_eligible = False
        if not value_kind:
            value_kind = "peak_point"
        if trajectory_eligible is None:
            trajectory_eligible = value_kind == "peak_point"
        peak = payload.get("forecast_peak_c")
        if peak is None:
            trace.append({"response_sha256": response_sha, "state": "unavailable", "state_reason": "forecast_peak_missing"})
            continue
        curve = []
        for point in (payload.get("hourly_curve") or []) if trajectory_eligible is True else []:
            if not isinstance(point, Mapping) or point.get("temperature_c") is None or not point.get("valid_time_local"):
                continue
            curve.append(
                {
                    "valid_time_local": str(point["valid_time_local"]),
                    "temperature_c": float(point["temperature_c"]),
                }
            )
        row = {
            "schema": "external_forecast_snapshot_row_v1",
            "state": "available",
            "source_id": str(payload.get("source_id") or envelope.get("source_id") or ""),
            "provider_family": str(payload.get("provider_family") or envelope.get("provider_family") or ""),
            "source_class": "forecast_weak_source",
            "station_id": str(payload.get("station_id") or ""),
            "target_event_date_local": str(payload.get("target_event_date_local") or "")[:10],
            "issued_at": str(envelope.get("response_completed_at") or ""),
            "provider_available_at": str(envelope.get("provider_available_at") or ""),
            "as_of_eligible_at": str(envelope.get("as_of_eligible_at") or ""),
            "forecast_peak_c": float(peak),
            "temperature_unit": "C",
            "hourly_curve": curve,
            "forecast_value_kind": value_kind,
            "forecast_record_valid_time": record_valid_time,
            "trajectory_eligible": trajectory_eligible is True,
            "forecast_snapshot_id": response_sha,
            "response_sha256": response_sha,
            "source_event_id": str(envelope.get("source_event_id") or ""),
            "lineage_quality": "immutable_envelope",
            "capture_time_provenance": str(envelope.get("capture_time_provenance") or "unknown"),
            "raw_provider_response_present": raw_provider_response_present,
            "market_sources_excluded": True,
        }
        rows.append(row)
        trace.append({"response_sha256": response_sha, "state": "available", "source_id": row["source_id"]})
    rows.sort(key=lambda row: (row["target_event_date_local"], row["as_of_eligible_at"], row["source_id"], row["response_sha256"]))
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "input_envelope_count": len(envelopes),
        "forecast_snapshot_count": len(rows),
        "forecast_snapshots": rows,
        "normalization_trace": trace,
        "guards": {
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _load_envelopes(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.expanduser().glob("**/*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, Mapping):
            rows.append(dict(value))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelopes", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = normalize_forward_metrics_envelopes(_load_envelopes(args.envelopes))
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        root = DEFAULT_RESEARCH_ROOT.resolve()
        if output != root and root not in output.parents:
            raise ValueError("forward_metrics_normalization_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
