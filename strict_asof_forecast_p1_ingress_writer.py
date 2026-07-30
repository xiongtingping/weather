"""Controlled P1 ingress for strict Tier-A forecast source observations.

This is the production-schema counterpart of the isolated staging proof.  It
only appends a previously validated Tier-A forecast SourceObservation to the
existing ``source_observations`` evidence table.  It neither canonicalizes a
METAR event nor writes a posterior, fact, decision, intent, or order.

The caller owns the outer transaction and the operational authorization.  The
writer refuses a database until the additive P1 schema is already present, so
a forecast evidence append cannot masquerade as a partial P1 deployment.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from sequential_bayes_schema_contract import TABLE_DDL, verify_additive_schema_compatibility
from source_observation_runtime import build_source_observation


SCHEMA_VERSION = "strict_asof_forecast_p1_ingress_writer_v1"
TIER_A = "A_live_observed_transport"
REQUIRED_SOURCE_COLUMNS = frozenset(
    {
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
        "forecast_peak_c",
        "raw_ref",
        "raw_sha256",
        "parser_version",
        "hard_eligible",
        "demotion_reason",
        "quality_flags_json",
        "observation_json",
        "created_at",
    }
)
INSERT_COLUMNS = (
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
    "response_duration_ms",
    "temperature_c",
    "dewpoint_c",
    "observed_today_max_c",
    "forecast_peak_c",
    "raw_ref",
    "raw_sha256",
    "parser_version",
    "hard_eligible",
    "demotion_reason",
    "quality_flags_json",
    "observation_json",
    "created_at",
)


class StrictForecastP1IngressError(RuntimeError):
    """Raised when a proposed strict forecast append is unsafe or inconsistent."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0] or "") for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _source_observation_identity_constraints(conn: sqlite3.Connection) -> tuple[bool, bool]:
    table_info = conn.execute("PRAGMA table_info(source_observations)").fetchall()
    source_event_id_primary_key = any(_text(row[1]) == "source_event_id" and int(row[5] or 0) > 0 for row in table_info)
    source_observation_id_unique = False
    for index in conn.execute("PRAGMA index_list(source_observations)").fetchall():
        index_name = _text(index[1])
        is_unique = int(index[2] or 0) == 1
        if not index_name or not is_unique:
            continue
        columns = [_text(row[2]) for row in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()]
        if columns == ["source_observation_id"]:
            source_observation_id_unique = True
            break
    return source_event_id_primary_key, source_observation_id_unique


def verify_strict_forecast_p1_ingress_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Verify read/write prerequisites without changing the connection."""

    tables = _table_names(conn)
    p1_schema = verify_additive_schema_compatibility(conn)
    missing_p1_tables = sorted(set(TABLE_DDL) - tables)
    source_columns = _table_columns(conn, "source_observations") if "source_observations" in tables else set()
    missing_source_columns = sorted(REQUIRED_SOURCE_COLUMNS - source_columns)
    source_event_id_primary_key = False
    source_observation_id_unique = False
    if not missing_source_columns:
        source_event_id_primary_key, source_observation_id_unique = _source_observation_identity_constraints(conn)
    return {
        "schema": SCHEMA_VERSION,
        "ok": (
            p1_schema.get("ok") is True
            and not missing_p1_tables
            and not missing_source_columns
            and source_event_id_primary_key
            and source_observation_id_unique
        ),
        "missing_p1_tables": missing_p1_tables,
        "incompatible_p1_tables": sorted((p1_schema.get("incompatible_table_columns") or {}).keys()),
        "incompatible_p1_table_columns": dict(p1_schema.get("incompatible_table_columns") or {}),
        "accepted_p1_extension_columns": dict(p1_schema.get("accepted_extension_columns") or {}),
        "incompatible_p1_indexes": list(p1_schema.get("incompatible_indexes") or []),
        "missing_source_columns": missing_source_columns,
        "source_event_id_primary_key": source_event_id_primary_key,
        "source_observation_id_unique": source_observation_id_unique,
        "writes_archive_db": False,
        "creates_posterior": False,
        "places_orders": False,
    }


def _row_from_tier_a_proposal(proposal: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _mapping(proposal)
    observation = _mapping(value.get("source_observation"))
    payload = _mapping(observation.get("payload"))
    if value.get("ok") is not True or _text(value.get("state")) != "proposal_ready":
        raise StrictForecastP1IngressError("strict_forecast_p1_proposal_not_ready")
    boundary = _mapping(value.get("boundary"))
    if boundary.get("tier_a_only") is not True:
        raise StrictForecastP1IngressError("strict_forecast_p1_tier_a_boundary_missing")
    required = (
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
        "forecast_peak_c",
        "raw_ref",
        "raw_sha256",
        "parser_version",
    )
    missing = [field for field in required if observation.get(field) in (None, "")]
    if missing:
        raise StrictForecastP1IngressError(f"strict_forecast_p1_observation_fields_missing:{','.join(missing)}")
    if _text(observation.get("source_class")) != "forecast_weak_source":
        raise StrictForecastP1IngressError("strict_forecast_p1_source_class_invalid")
    if _text(payload.get("evidence_tier")) != TIER_A:
        raise StrictForecastP1IngressError("strict_forecast_p1_capture_not_tier_a")
    if payload.get("response_headers_observed") is not True:
        raise StrictForecastP1IngressError("strict_forecast_p1_response_headers_not_observed")
    if _text(payload.get("capture_time_provenance")) != "observed_transport":
        raise StrictForecastP1IngressError("strict_forecast_p1_capture_provenance_invalid")
    if payload.get("future_training_proposal_only") is not True:
        raise StrictForecastP1IngressError("strict_forecast_p1_future_training_boundary_missing")
    if _text(payload.get("first_system_seen_at")) != _text(observation.get("fetched_at")):
        raise StrictForecastP1IngressError("strict_forecast_p1_first_seen_binding_mismatch")
    if _text(payload.get("available_at")) != _text(observation.get("fetched_at")):
        raise StrictForecastP1IngressError("strict_forecast_p1_available_at_binding_mismatch")
    if _text(payload.get("raw_response_sha256")) != _text(observation.get("raw_sha256")):
        raise StrictForecastP1IngressError("strict_forecast_p1_raw_hash_binding_mismatch")
    if _text(payload.get("forecast_snapshot_id")) != _text(payload.get("raw_response_sha256")):
        raise StrictForecastP1IngressError("strict_forecast_p1_snapshot_raw_hash_binding_mismatch")
    value_kind = _text(payload.get("forecast_value_kind")) or "peak_point"
    if value_kind not in {"peak_point", "daily_max_event_window"}:
        raise StrictForecastP1IngressError("strict_forecast_p1_forecast_value_kind_invalid")
    if value_kind == "peak_point" and _text(payload.get("forecast_peak_valid_time")) != _text(observation.get("valid_time")):
        raise StrictForecastP1IngressError("strict_forecast_p1_peak_valid_time_binding_mismatch")
    if value_kind == "daily_max_event_window":
        if _text(payload.get("forecast_record_valid_time")) != _text(observation.get("valid_time")):
            raise StrictForecastP1IngressError("strict_forecast_p1_daily_window_valid_time_binding_mismatch")
        if payload.get("trajectory_eligible") is not False:
            raise StrictForecastP1IngressError("strict_forecast_p1_daily_window_trajectory_not_disabled")
    flags = observation.get("quality_flags")
    if not isinstance(flags, (list, tuple)):
        raise StrictForecastP1IngressError("strict_forecast_p1_quality_flags_invalid")
    required_flags = {"strict_asof_tier_a", "observed_transport", "response_headers_observed", "raw_provider_response_bound"}
    if not required_flags.issubset({_text(flag) for flag in flags}):
        raise StrictForecastP1IngressError("strict_forecast_p1_quality_flags_incomplete")
    if value_kind == "daily_max_event_window" and "daily_max_event_window" not in {_text(flag) for flag in flags}:
        raise StrictForecastP1IngressError("strict_forecast_p1_daily_window_quality_flag_missing")
    row = {
        "source_event_id": _text(observation.get("source_event_id")),
        "source_observation_id": _text(observation.get("source_observation_id")),
        "source_id": _text(observation.get("source_id")),
        "source_class": _text(observation.get("source_class")),
        "role": _text(observation.get("role")),
        "station_id": _text(observation.get("station_id")),
        "event_date": _text(observation.get("event_date")),
        "valid_time": _text(observation.get("valid_time")),
        "valid_time_local": _text(observation.get("valid_time_local")),
        "fetched_at": _text(observation.get("fetched_at")),
        "parsed_at": _text(observation.get("parsed_at")),
        "response_completed_at": _text(observation.get("response_completed_at")),
        "response_duration_ms": observation.get("response_duration_ms"),
        "temperature_c": observation.get("temperature_c"),
        "dewpoint_c": observation.get("dewpoint_c"),
        "observed_today_max_c": observation.get("observed_today_max_c"),
        "forecast_peak_c": observation.get("forecast_peak_c"),
        "raw_ref": _text(observation.get("raw_ref")),
        "raw_sha256": _text(observation.get("raw_sha256")),
        "parser_version": _text(observation.get("parser_version")),
        "hard_eligible": 0,
        "demotion_reason": "forecast_source_not_hard_observation",
        "quality_flags_json": _canonical_json(list(flags)),
        "observation_json": _canonical_json(dict(observation)),
        "created_at": _text(observation.get("fetched_at")),
    }
    expected_identity = build_source_observation(
        source_id=row["source_id"],
        station_id=row["station_id"],
        event_date=row["event_date"],
        valid_time=row["valid_time"],
        valid_time_local=row["valid_time_local"],
        fetched_at=row["fetched_at"],
        parsed_at=row["parsed_at"],
        response_completed_at=row["response_completed_at"],
        response_duration_ms=row["response_duration_ms"],
        temperature_c=row["temperature_c"],
        dewpoint_c=row["dewpoint_c"],
        observed_today_max_c=row["observed_today_max_c"],
        forecast_peak_c=row["forecast_peak_c"],
        raw_ref=row["raw_ref"],
        raw_sha256_override=row["raw_sha256"],
        parser_version=row["parser_version"],
        quality_flags=tuple(_text(flag) for flag in flags),
        payload=payload,
    )
    if (
        expected_identity.source_event_id != row["source_event_id"]
        or expected_identity.source_observation_id != row["source_observation_id"]
    ):
        raise StrictForecastP1IngressError("strict_forecast_p1_source_identity_binding_mismatch")
    return row, dict(observation)


def _existing_rows(conn: sqlite3.Connection, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    columns = [column for column in INSERT_COLUMNS if column in _table_columns(conn, "source_observations")]
    selected = ",".join(f'"{column}"' for column in columns)
    results = conn.execute(
        f"SELECT {selected} FROM source_observations WHERE source_event_id=? OR source_observation_id=?",
        (_text(row.get("source_event_id")), _text(row.get("source_observation_id"))),
    ).fetchall()
    return [dict(zip(columns, result)) for result in results]


def _json_equivalent(left: Any, right: Any) -> bool:
    try:
        left_value = json.loads(_text(left)) if isinstance(left, str) else left
        right_value = json.loads(_text(right)) if isinstance(right, str) else right
    except (TypeError, ValueError):
        return _text(left) == _text(right)
    return _canonical_json(left_value) == _canonical_json(right_value)


def _equivalent(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    for column in INSERT_COLUMNS:
        if column == "created_at":
            continue
        current = existing.get(column)
        expected = incoming.get(column)
        if column in {"quality_flags_json", "observation_json"}:
            if not _json_equivalent(current, expected):
                return False
        elif column == "hard_eligible":
            try:
                if int(current) != int(expected):
                    return False
            except (TypeError, ValueError):
                return False
        elif current != expected:
            return False
    return True


def persist_tier_a_forecast_p1_proposal(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one verified Tier-A forecast observation or fail closed.

    This function never starts, commits, or rolls back a transaction.  It is
    intended for an explicitly authorized outer migration/ingress transaction.
    """

    schema = verify_strict_forecast_p1_ingress_schema(conn)
    if schema["ok"] is not True:
        raise StrictForecastP1IngressError("strict_forecast_p1_schema_not_ready")
    row, _observation = _row_from_tier_a_proposal(proposal)
    existing_rows = _existing_rows(conn, row)
    if existing_rows:
        if len(existing_rows) == 1 and _equivalent(existing_rows[0], row):
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "idempotent",
                "source_event_id": row["source_event_id"],
                "source_observation_id": row["source_observation_id"],
                "writes_posterior": False,
                "places_orders": False,
            }
        raise StrictForecastP1IngressError("strict_forecast_p1_immutable_replay_drift")
    placeholders = ",".join("?" for _ in INSERT_COLUMNS)
    quoted_columns = ",".join(f'"{column}"' for column in INSERT_COLUMNS)
    try:
        conn.execute(
            f"INSERT INTO source_observations({quoted_columns}) VALUES({placeholders})",
            tuple(row[column] for column in INSERT_COLUMNS),
        )
    except sqlite3.IntegrityError as exc:
        existing_rows = _existing_rows(conn, row)
        if len(existing_rows) == 1 and _equivalent(existing_rows[0], row):
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "idempotent_after_concurrent_append",
                "source_event_id": row["source_event_id"],
                "source_observation_id": row["source_observation_id"],
                "writes_posterior": False,
                "places_orders": False,
            }
        raise StrictForecastP1IngressError("strict_forecast_p1_append_integrity_error") from exc
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "appended",
        "source_event_id": row["source_event_id"],
        "source_observation_id": row["source_observation_id"],
        "writes_posterior": False,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "StrictForecastP1IngressError",
    "persist_tier_a_forecast_p1_proposal",
    "verify_strict_forecast_p1_ingress_schema",
]
