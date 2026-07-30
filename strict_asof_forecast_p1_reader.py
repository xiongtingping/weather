"""Read and validate strict Tier-A forecast observations from a P1 database.

``source_observations`` stores its full immutable observation in
``observation_json`` while retaining searchable columns.  This reader is the
only P1 bridge back to ForecastRevision: it rehydrates the original contract
and verifies column/payload bindings before returning any row to weak
evidence.  It is read-only and cannot create posterior or trading state.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Mapping

from sequential_bayes_schema_contract import verify_additive_schema_compatibility
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE


_WEAK_SOURCE_BY_PROVIDER_SOURCE = {
    "ecmwf_model_peak": "ecmwf",
    "gfs_model_peak": "gfs",
    "icon_model_peak": "icon",
    "zuuu_taf_forecast_amsc": "taf",
    "wu_forecast_high": "wu",
    "weather_com_forecast_peak": "twc",
    "twc_forecast_peak": "twc",
}


SCHEMA_VERSION = "strict_asof_forecast_p1_reader_v1"
STRICT_FORECAST_PARSER_VERSIONS = frozenset(
    {
        "strict_asof_forecast_source_observation_proposal_v1",
        "official_nwp_daily_peak_strict_asof_proposal_v1",
    }
)
TIER_A = "A_live_observed_transport"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-12
    except (TypeError, ValueError):
        return left in (None, "") and right in (None, "")


def _table_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1] or "") for row in conn.execute("PRAGMA table_info(source_observations)").fetchall()]


def _parse_observation_json(value: Any) -> Mapping[str, Any]:
    try:
        decoded = json.loads(_text(value))
    except (TypeError, ValueError):
        return {}
    return _mapping(decoded)


def _json_equivalent(left: Any, right: Any) -> bool:
    try:
        left_value = json.loads(_text(left)) if isinstance(left, str) else left
        right_value = json.loads(_text(right)) if isinstance(right, str) else right
    except (TypeError, ValueError):
        return _text(left) == _text(right)
    return json.dumps(left_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
        right_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _validate_binding(stored: Mapping[str, Any], observation: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field in (
        "source_event_id",
        "source_observation_id",
        "source_id",
        "source_class",
        "role",
        "station_id",
        "event_date",
        "valid_time",
        "valid_time_local",
        "fetched_at",
        "parsed_at",
        "response_completed_at",
        "raw_ref",
        "raw_sha256",
        "parser_version",
        "demotion_reason",
    ):
        if _text(stored.get(field)) != _text(observation.get(field)):
            reasons.append(f"stored_observation_{field}_binding_mismatch")
    if not _float_equal(stored.get("forecast_peak_c"), observation.get("forecast_peak_c")):
        reasons.append("stored_observation_forecast_peak_binding_mismatch")
    if not _float_equal(stored.get("hard_eligible"), 0):
        reasons.append("stored_observation_hard_eligibility_invalid")
    if not _json_equivalent(stored.get("quality_flags_json"), observation.get("quality_flags") or []):
        reasons.append("stored_observation_quality_flags_binding_mismatch")
    if _text(stored.get("source_class")) != "forecast_weak_source":
        reasons.append("stored_observation_source_class_invalid")
    if _text(observation.get("source_id")) not in SOURCE_UNIVERSE:
        reasons.append("stored_observation_weak_source_id_not_in_unique_universe")
    payload = _mapping(observation.get("payload"))
    provider_source_id = _text(payload.get("provider_source_id"))
    expected_weak_source_id = _WEAK_SOURCE_BY_PROVIDER_SOURCE.get(provider_source_id, provider_source_id)
    if not provider_source_id or expected_weak_source_id != _text(observation.get("source_id")):
        reasons.append("stored_observation_provider_to_weak_source_identity_invalid")
    if _text(payload.get("weak_source_id")) != _text(observation.get("source_id")):
        reasons.append("stored_observation_weak_source_payload_binding_mismatch")
    if _text(payload.get("evidence_tier")) != TIER_A:
        reasons.append("stored_observation_capture_not_tier_a")
    if payload.get("response_headers_observed") is not True:
        reasons.append("stored_observation_response_headers_not_observed")
    if _text(payload.get("capture_time_provenance")) != "observed_transport":
        reasons.append("stored_observation_capture_provenance_invalid")
    if payload.get("future_training_proposal_only") is not True:
        reasons.append("stored_observation_future_training_boundary_missing")
    if _text(payload.get("first_system_seen_at")) != _text(observation.get("fetched_at")):
        reasons.append("stored_observation_first_seen_binding_mismatch")
    if _text(payload.get("available_at")) != _text(observation.get("fetched_at")):
        reasons.append("stored_observation_available_at_binding_mismatch")
    if _text(payload.get("raw_response_sha256")) != _text(observation.get("raw_sha256")):
        reasons.append("stored_observation_raw_hash_binding_mismatch")
    if _text(payload.get("forecast_snapshot_id")) != _text(payload.get("raw_response_sha256")):
        reasons.append("stored_observation_snapshot_binding_mismatch")
    kind = _text(payload.get("forecast_value_kind")) or "peak_point"
    if kind == "peak_point":
        if _text(payload.get("forecast_peak_valid_time")) != _text(observation.get("valid_time")):
            reasons.append("stored_observation_peak_valid_time_binding_mismatch")
    elif kind == "daily_max_event_window":
        if _text(payload.get("forecast_record_valid_time")) != _text(observation.get("valid_time")):
            reasons.append("stored_observation_daily_window_valid_time_binding_mismatch")
        if payload.get("trajectory_eligible") is not False:
            reasons.append("stored_observation_daily_window_trajectory_not_disabled")
    else:
        reasons.append("stored_observation_forecast_value_kind_invalid")
    flags = {_text(item) for item in (observation.get("quality_flags") or [])}
    required_flags = {"strict_asof_tier_a", "observed_transport", "response_headers_observed", "raw_provider_response_bound"}
    if not required_flags.issubset(flags):
        reasons.append("stored_observation_quality_flags_incomplete")
    if kind == "daily_max_event_window" and "daily_max_event_window" not in flags:
        reasons.append("stored_observation_daily_window_quality_flag_missing")
    return sorted(set(reasons))


def load_strict_forecast_p1_source_observations(
    conn: sqlite3.Connection,
    *,
    event_date: str,
) -> dict[str, Any]:
    """Load only contract-bound Tier-A forecast rows for one target day."""

    # Runtime readers and writers must share the same P1 compatibility
    # boundary. Later production generations may add columns, but every base
    # P1 column, type, constraint, index and trigger remains mandatory.
    p1_schema = verify_additive_schema_compatibility(conn)
    columns = _table_columns(conn)
    required_columns = {"source_class", "event_date", "parser_version", "observation_json"}
    if p1_schema.get("ok") is not True or not required_columns.issubset(columns):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["p1_or_source_observation_schema_not_ready"],
            "p1_schema": p1_schema,
            "read_only": True,
        }
    day = _text(event_date)[:10]
    rows = conn.execute(
        """
        SELECT * FROM source_observations
        WHERE source_class='forecast_weak_source'
          AND event_date=?
          AND parser_version IN (?,?)
        ORDER BY fetched_at,source_event_id
        """,
        (day, *sorted(STRICT_FORECAST_PARSER_VERSIONS)),
    ).fetchall()
    observations: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        stored = dict(zip(columns, row))
        observation = dict(_parse_observation_json(stored.get("observation_json")))
        reasons = _validate_binding(stored, observation)
        if reasons:
            blocked.append(
                {
                    "source_event_id": _text(stored.get("source_event_id")),
                    "source_observation_id": _text(stored.get("source_observation_id")),
                    "block_reasons": reasons,
                }
            )
            continue
        observations.append(observation)
    # Source validity is independent.  A malformed/late source must be
    # quarantined by itself, not turn a valid ECMWF+ICON+GFS (or GFS+TAF)
    # source set into an empty day.  The unique joint weak model receives the
    # surviving exact source set and explicit missingness downstream.
    available = bool(observations)
    return {
        "schema": SCHEMA_VERSION,
        "ok": available,
        "state": "available" if available and not blocked else "available_with_quarantined_sources" if available else "blocked",
        "event_date": day,
        "source_observations": observations,
        "source_observation_count": len(observations),
        "blocked": blocked,
        "block_reasons": [] if available else ["strict_forecast_source_observations_missing_or_invalid"],
        "p1_schema": p1_schema,
        "boundary": {
            "read_only": True,
            "tier_a_only": True,
            "stored_json_column_bindings_revalidated": True,
            "invalid_source_quarantined_without_blocking_valid_source_set": True,
            "missing_source_is_explicit_model_input": True,
            "creates_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "load_strict_forecast_p1_source_observations"]
