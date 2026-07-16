"""Read-only H-data samples for offline physical daily-high research.

The source is a complete schedule-bound AMSC historical query, so its final
physical X is well-defined under the v2 target contract.  It is deliberately
*not* strict-as-of training evidence: H reports do not prove when our system
received them.  The output may support feature design and auxiliary physical
trajectory research, but it cannot be used to approve a strong-LR artifact,
materialize a live posterior, or evaluate the resident 3-second path.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from amsc_historical_backfill_archive import HISTORICAL_DATA_CLASS, load_amsc_historical_complete_primary_metar_day


SCHEMA_VERSION = "amsc_historical_physical_x_sample_batch_v1"
SAMPLE_SCHEMA_VERSION = "amsc_historical_physical_x_sample_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _feature_observation(report: Mapping[str, Any]) -> dict[str, Any]:
    parsed = _mapping(report.get("parsed_observation"))
    return {
        "historical_report_id": _text(report.get("historical_report_id")),
        "raw_sha256": _text(report.get("raw_sha256")),
        "valid_time_utc": _text(report.get("valid_time_utc")),
        "temperature_c": _finite(parsed.get("temperature_c")),
        "dewpoint_c": _finite(parsed.get("dewpoint_c")),
        "visibility_m": _finite(parsed.get("visibility_m")),
        "cloud_cover_pct": _finite(parsed.get("cloud_cover_pct")),
        "low_cloud_cover_pct": _finite(parsed.get("low_cloud_cover_pct")),
        "cloud_text": _text(parsed.get("cloud_text")),
        "weather_text": _text(parsed.get("weather_text")),
        "wind_dir_deg": _finite(parsed.get("wind_dir_deg")),
        "wind_speed_mps": _finite(parsed.get("wind_speed_mps")),
        "wind_gust_mps": _finite(parsed.get("wind_gust_mps")),
        "pressure_hpa": _finite(parsed.get("pressure_hpa")),
    }


def build_amsc_historical_physical_x_samples(
    archive_dir: str | Path,
    *,
    event_date: str,
) -> dict[str, Any]:
    """Build event-time physical X samples from a complete H sequence only."""

    sequence = load_amsc_historical_complete_primary_metar_day(archive_dir, event_date=event_date)
    if sequence.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": _text(event_date),
            "block_reasons": list(sequence.get("block_reasons") or ["historical_primary_metar_sequence_unavailable"]),
            "source_sequence": sequence,
            "read_only": True,
        }
    observations = [_feature_observation(_mapping(item)) for item in sequence.get("reports") or []]
    if any(item.get("temperature_c") is None for item in observations):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": _text(event_date),
            "block_reasons": ["historical_primary_metar_temperature_missing"],
            "source_sequence": sequence,
            "read_only": True,
        }
    final_physical_x_c = max(float(item["temperature_c"]) for item in observations)
    samples: list[dict[str, Any]] = []
    observed_max = float("-inf")
    for index, current in enumerate(observations):
        temperature = float(current["temperature_c"])
        observed_max = max(observed_max, temperature)
        previous = observations[index - 1] if index else None
        previous_temperature = _finite(previous.get("temperature_c")) if previous else None
        current_raw_sha = _text(current.get("raw_sha256"))
        history_raw_sha = [_text(item.get("raw_sha256")) for item in observations[:index]]
        sample_core = {
            "schema": SAMPLE_SCHEMA_VERSION,
            "station_id": _text(sequence.get("station_id")),
            "event_date": _text(event_date),
            "reporting_schedule_id": _text(sequence.get("reporting_schedule_id")),
            "current_raw_sha256": current_raw_sha,
            "pre_event_raw_sha256_list": history_raw_sha,
            "physical_x_raw_sha256_list": [_text(item.get("raw_sha256")) for item in observations],
        }
        sample_id = f"amsc_h_x_{_sha256(sample_core)[:32]}"
        features = {
            "current_observation": current,
            "pre_event_observation_count": index,
            "observed_max_c": observed_max,
            "current_minus_previous_c": (
                temperature - previous_temperature if previous_temperature is not None else None
            ),
            "temperature_path": [
                {"valid_time_utc": item["valid_time_utc"], "temperature_c": item["temperature_c"]}
                for item in observations[: index + 1]
            ],
        }
        label = {
            "physical_x_c": final_physical_x_c,
            "remaining_new_high_c": final_physical_x_c - observed_max,
            "current_to_final_high_c": final_physical_x_c - temperature,
            "daily_high_already_observed": observed_max >= final_physical_x_c,
        }
        samples.append(
            {
                **sample_core,
                "historical_physical_x_sample_id": sample_id,
                "cutoff_valid_time_utc": _text(current.get("valid_time_utc")),
                "features": features,
                "label": label,
                "eligibility": {
                    "historical_data_class": HISTORICAL_DATA_CLASS,
                    "offline_auxiliary_physical_research_eligible": True,
                    "strict_asof_training_eligible": False,
                    "strict_asof_training_na_reason": "historical_query_has_no_forward_first_actionable_seen_at",
                    "approved_strong_lr_artifact_training_eligible": False,
                    "p10_latency_evaluation_eligible": False,
                    "live_runtime_input_eligible": False,
                },
            }
        )
    manifest_core = {
        "schema": SCHEMA_VERSION,
        "station_id": _text(sequence.get("station_id")),
        "event_date": _text(event_date),
        "reporting_schedule_id": _text(sequence.get("reporting_schedule_id")),
        "sample_ids": [item["historical_physical_x_sample_id"] for item in samples],
        "physical_x_raw_sha256_list": [_text(item.get("raw_sha256")) for item in observations],
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available_auxiliary_only",
        "event_date": _text(event_date),
        "station_id": _text(sequence.get("station_id")),
        "sample_count": len(samples),
        "samples": samples,
        "manifest": {**manifest_core, "manifest_sha256": _sha256(manifest_core)},
        "source_sequence": {
            "schema": sequence.get("schema"),
            "reporting_schedule_id": sequence.get("reporting_schedule_id"),
            "coverage": sequence.get("coverage"),
        },
        "boundary": {
            "historical_data_class": HISTORICAL_DATA_CLASS,
            "strict_asof_oos_are_offline_promotion_only": True,
            "approved_strong_lr_artifact_training_eligible": False,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "SAMPLE_SCHEMA_VERSION", "build_amsc_historical_physical_x_samples"]
