#!/usr/bin/env python3
"""Export strict forward AMSC METAR/TAF evidence from the immutable raw archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_ARCHIVE_ROOT = ROOT / "data" / "amsc_raw_history"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
SCHEMA_VERSION = "amsc_source_skill_forward_export_v1"

from amsc_raw_history_archive import RAW_OBJECT_SCHEMA, raw_text_sha256
from external_forecast_snapshot_runtime import default_provider_family
from scripts.fetch_zuuu_metar_taf import source_payloads_from_amsc_payload


def _canonical_sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    return dict(value) if isinstance(value, Mapping) else {}


def export_amsc_source_skill_forward(archive_root: Path) -> dict[str, Any]:
    root = archive_root.expanduser().resolve()
    observations: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for path in sorted((root / "objects").glob("**/*.json.gz")):
        stored = _read_object(path)
        raw_text = str(stored.get("raw_text") or "").strip()
        raw_sha = str(stored.get("raw_sha256") or "")
        first_seen = str(stored.get("first_seen_at") or "")
        if stored.get("schema") != RAW_OBJECT_SCHEMA or not raw_text or raw_text_sha256(raw_text) != raw_sha:
            exclude("raw_object_integrity_invalid")
            continue
        parsed = source_payloads_from_amsc_payload(
            {"data": [{"content": raw_text}]},
            fetched_at=first_seen,
        )
        if len(parsed) != 1:
            exclude("raw_report_parse_failed")
            continue
        source = parsed[0]
        verified_valid = str(stored.get("verified_valid_time_utc") or "")
        parsed_valid = str(source.get("valid_time_utc") or "")
        if verified_valid and parsed_valid != verified_valid:
            exclude("parsed_valid_time_mismatch")
            continue
        object_ref = str(path.relative_to(root))
        common_lineage = {
            "raw_sha256": raw_sha,
            "raw_object_ref": object_ref,
            "first_seen_at": first_seen,
            "as_of_eligible_at": first_seen,
            "arrival_time_quality": str(stored.get("arrival_time_quality") or ""),
            "lineage_quality": "immutable_external_raw_object",
        }
        if str(stored.get("report_type") or "").upper() == "METAR":
            temperature = source.get("temperature_c")
            if temperature is None:
                exclude("metar_temperature_missing")
                continue
            observations.append(
                {
                    "schema": "external_curve_observation_v1",
                    "source_id": "zuuu_metar_raw_amsc",
                    "station_id": str(stored.get("station_id") or "ZUUU"),
                    "valid_time": parsed_valid,
                    "temperature_c": float(temperature),
                    **common_lineage,
                }
            )
            continue
        if str(stored.get("report_type") or "").upper() != "TAF":
            exclude("report_type_not_source_skill_relevant")
            continue
        peak = source.get("forecast_peak_c")
        event_date = str(source.get("event_date") or "")
        issued_at = str(source.get("issue_time_utc") or "")
        if peak is None or not event_date or not issued_at:
            exclude("taf_peak_contract_incomplete")
            continue
        source_id = "zuuu_taf_forecast_amsc"
        forecasts.append(
            {
                "schema": "external_forecast_snapshot_row_v1",
                "state": "available",
                "source_id": source_id,
                "provider_family": default_provider_family(source_id),
                "source_class": "forecast_weak_source",
                "station_id": str(stored.get("station_id") or "ZUUU"),
                "target_event_date_local": event_date,
                "issued_at": issued_at,
                "provider_available_at": first_seen,
                "as_of_eligible_at": first_seen,
                "forecast_peak_c": float(peak),
                "temperature_unit": "C",
                "forecast_snapshot_id": raw_sha,
                "response_sha256": raw_sha,
                "source_event_id": f"amsc_taf_{event_date}_{raw_sha[:16]}",
                "market_sources_excluded": True,
                **common_lineage,
            }
        )
    observations.sort(key=lambda row: (row["valid_time"], row["first_seen_at"], row["raw_sha256"]))
    forecasts.sort(key=lambda row: (row["target_event_date_local"], row["as_of_eligible_at"], row["raw_sha256"]))
    payload = {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "archive_root": str(root),
        "observation_count": len(observations),
        "forecast_snapshot_count": len(forecasts),
        "observations": observations,
        "forecast_snapshots": forecasts,
        "excluded_counts": dict(sorted(excluded.items())),
        "guards": {
            "reads_archive_db": False,
            "reads_facts_sqlite": False,
            "reads_external_raw_archive": True,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }
    payload["export_sha256"] = _canonical_sha(payload)
    return payload


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("amsc_source_skill_export_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = export_amsc_source_skill_forward(args.archive_root)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
