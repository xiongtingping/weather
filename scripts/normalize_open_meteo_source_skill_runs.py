#!/usr/bin/env python3
"""Normalize immutable Open-Meteo exact-run envelopes into strict-as-of rows."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from external_forecast_snapshot_runtime import validate_external_forecast_raw_envelope


SCHEMA_VERSION = "open_meteo_source_skill_normalization_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_exact_run_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    contract = validate_external_forecast_raw_envelope(envelope)
    if not contract["ok"]:
        return {"state": "unavailable", "state_reason": "immutable_envelope_contract_failed", "contract": contract}
    parameters = envelope.get("request_parameters") if isinstance(envelope.get("request_parameters"), Mapping) else {}
    if parameters.get("eligible_for_exact_cutoff_pairing") is not True:
        return {"state": "unavailable", "state_reason": "request_not_exact_cutoff_eligible", "contract": contract}
    target_date = str(parameters.get("target_event_date_local") or "")[:10]
    cutoff_at = str(parameters.get("cutoff_at") or "")
    if not target_date or not cutoff_at:
        return {"state": "unavailable", "state_reason": "target_date_or_cutoff_missing", "contract": contract}
    try:
        payload = json.loads(str(envelope.get("response_body_utf8") or ""))
    except json.JSONDecodeError:
        return {"state": "unavailable", "state_reason": "open_meteo_response_json_invalid", "contract": contract}
    hourly = payload.get("hourly") if isinstance(payload, Mapping) and isinstance(payload.get("hourly"), Mapping) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    temperatures = hourly.get("temperature_2m") if isinstance(hourly.get("temperature_2m"), list) else []
    if len(times) != len(temperatures):
        return {"state": "unavailable", "state_reason": "hourly_time_temperature_length_mismatch", "contract": contract}
    curve = []
    for valid_time, raw_temperature in zip(times, temperatures):
        temperature = _float(raw_temperature)
        if str(valid_time)[:10] != target_date or temperature is None:
            continue
        curve.append({"valid_time_local": str(valid_time), "temperature_c": temperature})
    if not curve:
        return {"state": "unavailable", "state_reason": "target_event_day_temperature_curve_missing", "contract": contract}
    peak = max(row["temperature_c"] for row in curve)
    peak_times = [row["valid_time_local"] for row in curve if row["temperature_c"] == peak]
    as_of_eligible_at = str(envelope.get("as_of_eligible_at") or "")
    if as_of_eligible_at and cutoff_at and as_of_eligible_at > cutoff_at:
        return {"state": "unavailable", "state_reason": "run_not_available_at_cutoff", "contract": contract}
    row = {
        "schema": "external_forecast_snapshot_row_v1",
        "state": "available",
        "source_class": "forecast_weak_source",
        "source_id": str(envelope.get("source_id") or ""),
        "provider_family": str(envelope.get("provider_family") or ""),
        "station_id": "ZUUU",
        "target_event_date_local": target_date,
        "cutoff_id": str(parameters.get("cutoff_id") or ""),
        "cutoff_at": cutoff_at,
        "issued_at": str(parameters.get("run_initialized_at") or parameters.get("run") or ""),
        "provider_available_at": str(envelope.get("provider_available_at") or ""),
        "as_of_eligible_at": as_of_eligible_at,
        "availability_quality": str(parameters.get("availability_quality") or ""),
        "forecast_snapshot_id": str(envelope.get("response_sha256") or ""),
        "response_sha256": str(envelope.get("response_sha256") or ""),
        "source_event_id": str(envelope.get("source_event_id") or ""),
        "forecast_peak_c": peak,
        "forecast_peak_times_local": peak_times,
        "temperature_unit": "C",
        "hourly_curve": curve,
        "lineage_quality": "immutable_envelope",
        "market_sources_excluded": True,
    }
    return {"state": "available", "state_reason": "", "row": row, "contract": contract}


def normalize_envelopes(envelopes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    trace = []
    for envelope in envelopes:
        result = normalize_exact_run_envelope(envelope)
        trace.append(
            {
                "response_sha256": str(envelope.get("response_sha256") or ""),
                "source_id": str(envelope.get("source_id") or ""),
                "state": result.get("state"),
                "state_reason": result.get("state_reason"),
            }
        )
        if result.get("state") == "available":
            rows.append(result["row"])
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "input_envelope_count": len(envelopes),
        "available_row_count": len(rows),
        "forecast_snapshots": rows,
        "normalization_trace": trace,
        "guards": {
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _load_envelopes(path: Path) -> list[dict[str, Any]]:
    paths = sorted(path.rglob("*.json.gz")) if path.is_dir() else [path]
    output = []
    for item in paths:
        if item.suffix == ".gz":
            with gzip.open(item, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(item.read_text(encoding="utf-8"))
        if isinstance(value, Mapping):
            output.append(dict(value))
    return output


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("normalization_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = normalize_envelopes(_load_envelopes(args.envelopes))
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
